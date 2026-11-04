"""Read-only reconciliation of physical history before database rollback.

Reports are private application data. They never move, delete or repair media.
"""
import base64
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from organization_preflight import no_links
from media_locations import fingerprint
from storage_inventory import volume_identity

ACTIVE={'queued','running','waiting','partial','interrupted'}
META={'keep','unbind_copy'}
PUBLISHED={'committed','binding','needs_binding','done'}


def hashed(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()).hexdigest()
def manifest_digest(value):return hashed(value)
def rows(c,table):
    cursor=c.execute('SELECT * FROM "'+table.replace('"','""')+'"')
    names=[column[0] for column in cursor.description]
    return [dict(zip(names,row)) for row in cursor]


def journal(c):
    tables={row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    result={name:rows(c,name) if name in tables else [] for name in ('organization_jobs','organization_steps','organization_inverses')}
    jobs={}
    for raw in result['organization_jobs']:
        if not {'id','payload','approval_digest','state','created'}<=set(raw):raise ValueError('整理日志结构不完整，不能自动对账')
        job=dict(raw,payload=json.loads(raw['payload']))
        if job['state'] not in ACTIVE|{'done','cancelled','failed'}:raise ValueError('未知整理任务状态，不能自动对账')
        if manifest_digest(job['payload'])!=job['approval_digest']:raise ValueError('整理批准摘要不一致，保留现场核对')
        if not isinstance(job['payload'],dict) or not isinstance(job['payload'].get('entries'),list) or not job['payload']['entries'] or any(not isinstance(e,dict) or e.get('operation') not in META|{'copy','move','archive'} for e in job['payload']['entries']):raise ValueError('整理清单不完整或操作未知')
        jobs[job['id']]=job
    grouped={ident:[] for ident in jobs}
    for step in result['organization_steps']:
        if not {'job_id','ordinal','state','details'}<=set(step) or step['job_id'] not in jobs:raise ValueError('整理步骤缺少所属任务')
        step['details']=json.loads(step['details'])
        if not isinstance(step['details'],dict):raise ValueError('整理步骤详情不完整')
        grouped[step['job_id']].append(step)
    for job in jobs.values():
        job['steps']=sorted(grouped[job['id']],key=lambda s:s['ordinal'])
        if [s['ordinal'] for s in job['steps']]!=list(range(len(job['payload']['entries']))):raise ValueError('整理步骤不完整')
    return {'jobs':jobs,'inverses':result['organization_inverses']}


def metadata_only(c):
    try:model=journal(c)
    except (ValueError,KeyError,TypeError,sqlite3.DatabaseError):return False
    for job in model['jobs'].values():
        if job['state'] in ACTIVE:return False
        for entry,step in zip(job['payload']['entries'],job['steps']):
            if entry.get('operation') not in META:return False
            allowed={'pending','binding','needs_binding','done','cancelled','interrupted','waiting'} if entry['operation']=='keep' else {'pending','done'}
            if step['state'] not in allowed or any(step['details'].get(key) for key in ('temporary','previous_temporaries')):return False
    return True


def semantic_signature(c):
    """Ignore disposable caches/leases, retain authoritative and unknown tables."""
    digest=hashlib.sha256()
    omitted={'task_journal','task_events','resource_claims','resource_scopes','resource_waiters','organization_leases','organization_maintenance','media_relation_revision'}
    for kind,name,table,sql in c.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE type!='table' AND sql IS NOT NULL ORDER BY type,name"):
        if table in omitted or table.startswith(('sqlite_','browse_','inventory_query_')):continue
        digest.update(json.dumps([kind,name,table,sql],ensure_ascii=True).encode())
    for name,sql in c.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
        if name in omitted or name.startswith(('sqlite_','browse_','inventory_query_')):continue
        digest.update(json.dumps([name,sql],ensure_ascii=True).encode())
        quoted='"'+name.replace('"','""')+'"'
        try:cursor=c.execute('SELECT * FROM '+quoted+' ORDER BY rowid')
        except sqlite3.OperationalError:
            columns=[r[1] for r in c.execute('PRAGMA table_info('+quoted+')')]
            cursor=c.execute('SELECT * FROM '+quoted+' ORDER BY '+','.join('"'+v.replace('"','""')+'"' for v in columns))
        # Unknown WITHOUT ROWID tables need a stable all-column order instead.
        for row in cursor:
            values=[{'blob':base64.b64encode(v).decode()} if isinstance(v,bytes) else v for v in row]
            digest.update(json.dumps(values,ensure_ascii=True,separators=(',',':')).encode()+b'\n')
    return digest.hexdigest()


def object_key(device,identity):
    if not isinstance(device,str) or not isinstance(identity,list) or len(identity)!=2 or not all(isinstance(v,int) and v for v in identity):raise ValueError('缺少可靠文件身份，不能自动对账')
    return hashed([device,identity])


class RestoreAudit:
    def __init__(self,current,incoming):self.current,self.incoming=current,incoming

    def inspect(self,artifact=None,settings=None):
        current=journal(self.current);incoming=journal(self.incoming);union={};objects={};problems=[]
        def issue(code,job=None,path=None):problems.append({'code':code,'job':job,'path':path})
        for model in (current,incoming):
            for ident,job in model['jobs'].items():
                if ident in union and union[ident]['approval_digest']!=job['approval_digest']:raise ValueError('同任务出现不同批准清单')
                union[ident]=job
        ordered=sorted(union.values(),key=lambda j:(j['created'],j['id']))
        def register(entry,path,device,identity,created=None,required=False):
            key=object_key(device,identity);stamp=entry['fingerprint']
            if key not in objects:objects[key]={'key':key,'initial':path,'device':device,'identity':identity,'fingerprint':stamp,'created':created,'required':required,'conflict':False}
            else:
                obj=objects[key];obj['conflict']|=obj['fingerprint']!=stamp;obj['required']|=required
                if created and obj['created']!=created:raise ValueError('文件创建归属冲突')
            return key
        # Creation records take precedence over later source observations.
        for job in ordered:
            if {e.get('operation') for e in job['payload']['entries']}!={'copy'}:continue
            for ordinal,entry in enumerate(job['payload']['entries']):
                candidates=[m['jobs'][job['id']]['steps'][ordinal] for m in (current,incoming) if job['id'] in m['jobs']]
                published=[s for s in candidates if s['state'] in PUBLISHED]
                if not published:continue
                identities={tuple(s['details'].get('file_identity',[])) for s in published}
                if len(identities)!=1:raise ValueError('复制目标身份日志不一致')
                register(entry,entry['destination'],entry['target_device'],list(identities.pop()),created=job['id'],required=True)
        for job in ordered:
            for entry in job['payload']['entries']:
                op=entry.get('operation')
                if op in META:continue
                if op not in ('copy','move','archive'):raise ValueError('未知整理操作，不能自动对账')
                register(entry,entry['source'],entry['source_device'],entry['source_identity'],required=op in ('move','archive'))
        for obj in objects.values():
            if obj['required'] and obj['conflict']:issue('content_history_changed',path=obj['initial'])
        def replay(model,role):
            positions={key:obj['initial'] for key,obj in objects.items() if not obj['created']}
            reversed_jobs={r['original_job_id']:model['jobs'][r['inverse_job_id']] for r in model['inverses'] if r.get('inverse_job_id') in model['jobs'] and model['jobs'][r['inverse_job_id']]['state']=='done'}
            for job in sorted(model['jobs'].values(),key=lambda j:(j['created'],j['id'])):
                if job['state'] in ACTIVE and job['id'] not in reversed_jobs:issue(role+'_active_job',job['id'])
                safe_metadata=metadata_job_safe(job)
                for entry,step in zip(job['payload']['entries'],job['steps']):
                    op=entry['operation'];state=step['state']
                    if op in META:
                        if not safe_metadata:issue(role+'_metadata_state',job['id'])
                        continue
                    for record in ([{'path':step['details'].get('temporary')}]+step['details'].get('previous_temporaries',[])):
                        temp=record.get('path')
                        if temp and os.path.lexists(temp):issue(role+'_temporary_retained',job['id'],temp)
                    if op=='copy':
                        if state=='done':
                            key=object_key(entry['target_device'],step['details']['file_identity']);positions[key]=entry['destination']
                        elif state not in ('pending','cancelled','copying','verified','interrupted'):issue(role+'_copy_needs_reconciliation',job['id'])
                        continue
                    key=object_key(entry['source_device'],entry['source_identity'])
                    if state=='done':target=entry['destination']
                    elif job['id'] in reversed_jobs and state not in ('pending','cancelled'):
                        inverse=reversed_jobs[job['id']]
                        matching=[e for e in inverse['payload']['entries'] if e.get('source_identity')==entry['source_identity'] and e.get('source_device')==entry['source_device']]
                        if len(matching)!=1:issue(role+'_inverse_ambiguous',job['id']);continue
                        target=matching[0]['source']
                    elif state in ('pending','cancelled'):continue
                    else:issue(role+'_move_needs_reconciliation',job['id']);continue
                    if positions.get(key)!=entry['source']:issue(role+'_path_chain_changed',job['id'],entry['source'])
                    positions[key]=target
            return positions
        actual=replay(current,'current');desired=replay(incoming,'incoming');proofs=[];retained=[]
        for key,obj in sorted(objects.items()):
            if not obj['required']:continue
            expected=desired.get(key);path=actual.get(key) or expected
            if expected and path!=expected:issue('file_position_differs_from_backup',path=expected)
            if not path:continue
            try:
                p=Path(path)
                if no_links(p)!=p:raise ValueError('path changed')
                stat=p.stat()
                if [stat.st_dev,stat.st_ino]!=obj['identity'] or volume_identity(p)[0]!=obj['device'] or fingerprint(p)!=obj['fingerprint']:raise ValueError('proof changed')
                proof={'path':path,'device':obj['device'],'identity':obj['identity'],'fingerprint':obj['fingerprint']}
                proofs.append(proof)
                if key not in desired:retained.append(proof)
            except (OSError,ValueError):issue('file_unavailable_or_changed',path=path)
        body={'version':1,'artifact':artifact,'settings':settings,'current_signature':semantic_signature(self.current),'incoming_signature':semantic_signature(self.incoming),
              'ready':not problems,'problems':problems,'files':proofs,'retained_files':retained,'journals':{'current':current,'incoming':incoming}}
        body['digest']=hashed(body)
        return body


def metadata_job_safe(job):
    return all(e.get('operation') in META and s['state'] in ({'pending','binding','needs_binding','done','cancelled','interrupted','waiting'} if e['operation']=='keep' else {'pending','done'}) and not any(s['details'].get(k) for k in ('temporary','previous_temporaries')) for e,s in zip(job['payload']['entries'],job['steps']))


class RestorePermit:
    """Only returned after a fresh full audit; never constructed from an HTTP flag."""
    def __init__(self,report):self.report=report
    def validate(self,connection,role):
        if not self.report['ready'] or semantic_signature(connection)!=self.report[role+'_signature']:raise ValueError('恢复对账已过期，请重新预览')


def approve_report(current,incoming,previous,artifact=None,settings=None):
    fresh=RestoreAudit(current,incoming).inspect(artifact,settings)
    if not fresh['ready']:raise ValueError('文件日志仍有未解决项，不能恢复数据库')
    if not isinstance(previous,dict) or previous.get('digest')!=fresh['digest']:raise ValueError('文件或资料在对账后变化，请重新预览')
    return RestorePermit(fresh)


def settings_signature(data):
    data=Path(data)
    return hashed([(p.name,hashlib.sha256(p.read_bytes()).hexdigest()) for p in sorted(data.glob('*.json')) if not any(word in p.name for word in ('runtime-instance','restart')) and not p.is_symlink()])


def file_hash(path):
    result=hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda:source.read(4*1024*1024),b''):result.update(block)
    return result.hexdigest()


def save_private_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    if no_links(path.parent)!=path.parent or path.exists():raise ValueError('对账记录路径不可用')
    with path.open('x',encoding='utf-8') as output:
        json.dump(value,output,ensure_ascii=True);output.flush();os.fsync(output.fileno())


def store_plan(data,report,backup):
    import uuid
    ident=uuid.uuid4().hex
    report.update(id=ident,backup=backup)
    save_private_json(Path(data)/'restore-plans'/(ident+'.json'),report)
    return report


def load_plan(data,token,backup):
    import re
    if not isinstance(token,dict) or not isinstance(token.get('id'),str) or not re.fullmatch('[a-f0-9]{32}',token['id']):raise ValueError('恢复对账标识无效')
    path=Path(data)/'restore-plans'/(token['id']+'.json')
    if no_links(path)!=path or path.stat().st_size>512_000_000:raise ValueError('恢复对账路径无效')
    previous=json.loads(path.read_text('utf-8'))
    if previous.get('backup')!=backup or previous.get('digest')!=token.get('digest'):raise ValueError('恢复对账不属于此备份')
    return previous
