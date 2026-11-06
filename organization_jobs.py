"""Immutable organization approvals, persistent steps and exclusive resource leases."""
import hashlib
import json
import os
import time
import uuid
from contextlib import nullcontext
from pathlib import Path
from media_locations import fingerprint
from storage_inventory import volume_identity, destination_key, root_file_id
from task_journal import TaskJournal
from resource_leases import ResourceLeases,ResourceBusy,path_resource
from organization_preflight import validate_manifest,no_links,valid_component,PreflightError

TERMINAL={'done','cancelled'}
TRANSITIONS={
 'pending':{'copying','staging','binding','cancelled','waiting'},
 'staging':{'staged','interrupted'},
 'staged':{'committing','interrupted'},
 'waiting':{'pending','cancelled'},
 'copying':{'verified','interrupted','cancelled'},
 'verified':{'committing','interrupted'},
 'committing':{'committed','interrupted'},
 'committed':{'binding','interrupted'},
 'binding':{'done','needs_binding','interrupted'},
 'needs_binding':{'binding','cancelled'},
 'interrupted':{'pending','verified','committed','needs_binding','cancelled'},
 'done':set(),'cancelled':set(),
}


def identify(path):
    parent=Path(path)
    while not parent.exists() and parent!=parent.parent:parent=parent.parent
    return volume_identity(parent)[0]


def digest(payload):
    return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()).hexdigest()


def root_proof(root):
    root=no_links(root)
    if not root.is_dir():return None
    volume,stable=volume_identity(root)
    if not stable:return None
    identity=root_file_id(root)
    return {'volume':volume,'directory':identity} if identity else None


def verify_root_proofs(proofs, replacements=None):
    replacements=replacements or {}
    verified={}
    for original,expected in proofs.items():
        candidate=no_links(replacements.get(original,original))
        if root_proof(candidate)!=expected:
            raise ValueError('整理根目录的卷或目录身份已变化，请核对原盘及原目录')
        verified[original]=str(candidate)
    return verified


class OrganizationJobs:
    def __init__(self,db,device_identity=identify):
        self.db,self.device_identity=db,device_identity
        self.journal=TaskJournal(db)
        self.leases=ResourceLeases(db)
        with db() as c:
            c.execute('CREATE TABLE IF NOT EXISTS organization_drafts(id TEXT PRIMARY KEY,digest TEXT UNIQUE,payload TEXT,created REAL)')
            c.execute('CREATE TABLE IF NOT EXISTS organization_jobs(id TEXT PRIMARY KEY,approval_digest TEXT UNIQUE,payload TEXT,state TEXT,created REAL,updated REAL)')
            c.execute("CREATE TABLE IF NOT EXISTS organization_steps(job_id TEXT,ordinal INTEGER,state TEXT,details TEXT DEFAULT '{}',updated REAL,PRIMARY KEY(job_id,ordinal))")
            c.execute('CREATE TABLE IF NOT EXISTS organization_step_events(sequence INTEGER PRIMARY KEY,job_id TEXT,ordinal INTEGER,state TEXT,created REAL)')
            c.execute('CREATE TABLE IF NOT EXISTS organization_control(job_id TEXT PRIMARY KEY,cancel_requested INTEGER DEFAULT 0)')
            c.execute('CREATE TABLE IF NOT EXISTS organization_leases(resource TEXT PRIMARY KEY,job_id TEXT)')
            c.execute('CREATE TABLE IF NOT EXISTS organization_recovery_maps(job_id TEXT PRIMARY KEY,approval_digest TEXT,checkpoint_digest TEXT,roots TEXT,confirmed REAL)')
            if 'activated' not in {r[1] for r in c.execute('PRAGMA table_info(organization_recovery_maps)')}:
                c.execute('ALTER TABLE organization_recovery_maps ADD COLUMN activated INTEGER NOT NULL DEFAULT 0')
            c.execute('CREATE TABLE IF NOT EXISTS organization_maintenance(job_id TEXT PRIMARY KEY)')
            c.execute('CREATE TABLE IF NOT EXISTS organization_superseded(job_id TEXT PRIMARY KEY,replacement_draft TEXT,original_job TEXT)')
            if 'pause_requested' not in {r[1] for r in c.execute('PRAGMA table_info(organization_control)')}:
                c.execute('ALTER TABLE organization_control ADD COLUMN pause_requested INTEGER DEFAULT 0')

    def prepare(self,entries,source_root,target_root,rules_version=1,register=None,retained=None,*,source_roots=None,target_roots=None):
        if type(rules_version) is not int or rules_version<1:raise ValueError('Invalid rule version')
        source_root=no_links(source_root);target_root=no_links(target_root)
        multiple=source_roots is not None or target_roots is not None
        source_roots=list(dict.fromkeys(no_links(root) for root in (source_roots or [source_root])))
        target_roots=list(dict.fromkeys(no_links(root) for root in (target_roots or [target_root])))
        if not isinstance(entries,list) or not entries:raise ValueError('Choose files before approval')
        operations={entry.get('operation') for entry in entries}
        if len(operations)!=1 or not operations<={'copy','move','keep','archive'}:raise ValueError('Choose a single supported operation')
        moving=operations in ({'move'},{'archive'})
        keeping=operations=={'keep'}
        if not moving and not keeping and any(source.is_relative_to(target) or target.is_relative_to(source) for source in source_roots for target in target_roots):raise ValueError('Copy target must be independent of source scope')
        sources={destination_key(no_links(entry['source'])) for entry in entries}
        if len(sources)!=len(entries):raise ValueError('Duplicate source location')
        proofs={}
        for root in dict.fromkeys(source_roots+target_roots):
            proof=root_proof(root)
            if proof:proofs[str(root)]=proof
        preflight=validate_manifest(entries,self.device_identity)
        checked=[];destinations=set()
        for entry in entries:
            source=no_links(entry['source']);raw_target=Path(entry['destination']);target=no_links(raw_target.parent)/raw_target.name
            entry_source=no_links(entry.get('source_root',source_root));entry_target=no_links(entry.get('target_root',target_root))
            if entry_source not in source_roots or entry_target not in target_roots or not source.is_relative_to(entry_source) or not target.is_relative_to(entry_target):raise ValueError('Entry escapes approved roots')
            key=destination_key(target)
            if keeping and source!=target:raise ValueError('原位保留不能修改路径')
            if not keeping and any(not valid_component(part,operations=={'archive'}) for part in target.parts[1:]):raise ValueError('Invalid target path component')
            if (target.exists() and not keeping and not (moving and key in sources)) or key in destinations:raise ValueError('Target collision')
            source_device=self.device_identity(source);target_device=self.device_identity(target.parent)
            if moving and source_device!=target_device:raise ValueError('Cross-device moves must use verified copy first')
            destinations.add(key)
            stamp=fingerprint(source)
            source_info=source.stat();source_identity=[source_info.st_dev,source_info.st_ino]
            if self.device_identity(source)!=source_device or entry.get('source_device',source_device)!=source_device:raise PreflightError(['device_mismatch'])
            if entry.get('fingerprint',stamp)!=stamp or entry.get('source_identity',source_identity)!=source_identity:raise PreflightError(['source_changed'])
            asset_id=register(entry,stamp,source_device) if register else entry.get('asset_id')
            checked.append({'source':str(source),'destination':str(target),'video_id':entry.get('video_id'),
                            'operation':entry['operation'],'fingerprint':stamp,'source_identity':source_identity,'source_device':source_device,
                            'target_device':target_device,'asset_id':asset_id,'role':entry.get('role','media'),'parent_asset_id':entry.get('parent_asset_id'),'metadata':entry.get('metadata',{})})
            if multiple:checked[-1].update(source_root=str(entry_source),target_root=str(entry_target))
            if operations=={'archive'}:
                if not isinstance(entry.get('archive_guard'),dict):raise ValueError('归档需要目标副本证明')
                checked[-1]['archive_guard']=entry['archive_guard']
        payload={'entries':checked,'source_root':str(source_root),'target_root':str(target_root),'rules_version':rules_version,'retained':retained or [],'preflight':preflight}
        if multiple:payload.update(source_roots=[str(root) for root in source_roots],target_roots=[str(root) for root in target_roots])
        verify_root_proofs(proofs)
        if proofs:payload['root_proofs']=proofs
        stamp=digest(payload);draft_id=uuid.uuid4().hex
        with self.db() as c:
            c.execute('INSERT OR IGNORE INTO organization_drafts VALUES(?,?,?,?)',(draft_id,stamp,json.dumps(payload),time.time()))
            draft_id=c.execute('SELECT id FROM organization_drafts WHERE digest=?',(stamp,)).fetchone()[0]
        return {'id':draft_id,'digest':stamp,'payload':payload,'files':len(checked),'bytes':sum(e['fingerprint']['size'] for e in checked)}

    def approve(self,draft_id,expected_digest):
        with self.db() as c:
            draft=c.execute('SELECT * FROM organization_drafts WHERE id=?',(draft_id,)).fetchone()
            if not draft or draft['digest']!=expected_digest:raise ValueError('Approval does not match the stored plan')
            existing=c.execute('SELECT id FROM organization_jobs WHERE approval_digest=?',(expected_digest,)).fetchone()
        if existing:return existing['id']
        payload=json.loads(draft['payload'])
        if digest(payload)!=expected_digest:raise ValueError('Stored approval content changed')
        verify_root_proofs(payload.get('root_proofs',{}))
        if any(entry['operation'] not in ('copy','move','keep','archive') for entry in payload['entries']):raise ValueError('此计划需要使用对应撤销批准入口')
        validate_manifest(payload['entries'],self.device_identity)
        sources={destination_key(e['source']) for e in payload['entries']}
        for entry in payload['entries']:
            source=no_links(entry['source']);raw_target=Path(entry['destination']);target=no_links(raw_target.parent)/raw_target.name
            if entry['operation']=='keep' and source!=target:raise ValueError('原位保留不能修改路径')
            allowed_existing=entry['operation']=='keep' or entry['operation'] in ('move','archive') and destination_key(target) in sources
            if str(source)!=entry['source'] or str(target)!=entry['destination'] or (target.exists() and not allowed_existing):raise ValueError('Path changed since preview')
            if self.device_identity(source)!=entry['source_device'] or self.device_identity(target.parent)!=entry['target_device']:raise ValueError('Device changed since preview')
            if fingerprint(source)!=entry['fingerprint'] or [source.stat().st_dev,source.stat().st_ino]!=entry['source_identity']:raise ValueError('Source changed since preview')
        job_id=uuid.uuid4().hex;now=time.time()
        with self.db() as c:
            c.execute('INSERT OR IGNORE INTO organization_jobs VALUES(?,?,?,?,?,?)',(job_id,expected_digest,json.dumps(payload),'queued',now,now))
            job_id=c.execute('SELECT id FROM organization_jobs WHERE approval_digest=?',(expected_digest,)).fetchone()[0]
            for ordinal in range(len(payload['entries'])):c.execute('INSERT OR IGNORE INTO organization_steps(job_id,ordinal,state,updated) VALUES(?,?,?,?)',(job_id,ordinal,'pending',now))
        self.journal.observe('organize',job_id,'queued',{'total':len(payload['entries']),'cancellable':True})
        return job_id

    def get(self,job_id,connection=None):
        with (self.db() if connection is None else nullcontext(connection)) as c:
            job=c.execute('SELECT * FROM organization_jobs WHERE id=?',(job_id,)).fetchone()
            if not job:raise ValueError('Unknown organization job')
            result=dict(job);result['steps']=[dict(r) for r in c.execute('SELECT * FROM organization_steps WHERE job_id=? ORDER BY ordinal',(job_id,))]
        result['payload']=json.loads(result['payload'])
        for step in result['steps']:step['details']=json.loads(step['details'])
        return result

    def verify_recovery_roots(self, job_id, replacements):
        return self._verified_recovery_roots(self.get(job_id),replacements)

    @staticmethod
    def _verified_recovery_roots(job,replacements):
        payload=job['payload']
        if digest(payload)!=job['approval_digest']:raise ValueError('Approved manifest changed')
        if not isinstance(replacements,dict) or not replacements:
            raise ValueError('请选择需要重连的整理根目录')
        if any(not isinstance(key,str) or not isinstance(value,str) or not Path(value).is_absolute() for key,value in replacements.items()):
            raise ValueError('请提供绝对目录路径')
        proofs=payload.get('root_proofs',{})
        if any(root not in proofs for root in replacements):
            raise ValueError('批准清单缺少原目录证明，无法安全重连')
        roots=verify_root_proofs({root:proofs[root] for root in replacements},replacements)
        if len({destination_key(root) for root in roots.values()})!=len(roots):
            raise ValueError('不同整理根不能映射到同一个目录')
        return roots

    def recovery_map_preview(self,job_id,replacements):
        with self.db() as c:
            return self._recovery_map_preview(c,job_id,replacements)

    def _recovery_map_preview(self,c,job_id,replacements,bindings_out=None):
        job=self.get(job_id,c)
        if job['state'] not in ('interrupted','waiting','failed','partial','cancelled','done'):
            raise ValueError('请先暂停或停止整理任务')
        owner='organize:'+job_id
        if (c.execute('SELECT 1 FROM organization_leases WHERE job_id=?',(job_id,)).fetchone()
                or c.execute('SELECT 1 FROM organization_maintenance WHERE job_id=?',(job_id,)).fetchone()
                or c.execute('SELECT 1 FROM resource_waiters WHERE owner=?',(owner,)).fetchone()
                or c.execute('SELECT 1 FROM resource_claims WHERE owner=?',(owner,)).fetchone()):
            raise ValueError('整理任务仍在运行、等待资源或维护中')
        roots=self._verified_recovery_roots(job,replacements)
        checkpoints=digest(job['steps'])
        active=c.execute('SELECT roots,activated FROM organization_recovery_maps WHERE job_id=?',(job_id,)).fetchone()
        previous=json.loads(active['roots']) if active and active['activated'] else {}
        completed=job['state']=='done'
        payload=job['payload']
        targets=set(payload.get('target_roots',[payload['target_root']]))
        if not completed and previous and previous!=roots:
            if targets.intersection(previous) or any(entry['operation']=='move' for entry in payload['entries']):
                raise ValueError('已生效的目标重连包含文件检查点，需要单独核对后才能再次更换')
        # Inspect effective paths without activating the map. Existing bindings
        # must pass the file and ownership checks below before confirmation.
        mapping={'approval_digest':job['approval_digest'],'checkpoint_digest':checkpoints,
                 'roots':json.dumps(roots),'activated':True}
        effective,_=self._effective_execution_job(c,job,mapping,previous)
        preview={'id':job_id,'approval_digest':job['approval_digest'],'checkpoint_digest':checkpoints,
                 'roots':roots,'state':job['state'],'updated':job['updated']}
        bindings=self._recovery_target_bindings(c,job,effective,roots,previous)
        if completed or bindings:
            preview.update(binding_count=len(bindings),binding_digest=digest(bindings))
            if bindings_out is not None:bindings_out.extend(bindings)
        return dict(preview,digest=digest(preview))

    def _recovery_target_bindings(self,c,job,effective,roots,previous):
        payload=job['payload']
        targets=set(payload.get('target_roots',[payload['target_root']]))
        changed={root for root in roots if roots[root]!=previous.get(root,root)}
        if job['state']=='done' and (set(previous)-set(roots) or not changed<=targets):
            raise ValueError('已完成任务只允许重连已绑定目标，保留其他映射')
        bindings=[]
        tables={row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for original,entry,step in zip(payload['entries'],effective['payload']['entries'],effective['steps']):
            root=original.get('target_root',payload['target_root'])
            if root not in changed:continue
            old=str(Path(previous.get(root,root))/Path(original['destination']).relative_to(root))
            new=entry['destination']
            companion=entry.get('role')=='companion'
            table='media_resources' if companion else 'media_locations'
            if table not in tables:
                if step['state']=='done':raise ValueError('目标没有已完成的绑定证明')
                continue
            binding_id=step['details'].get('binding_id')
            if binding_id:
                row=c.execute(f'SELECT * FROM {table} WHERE id=?',(binding_id,)).fetchone()
            else:
                row=c.execute(f'SELECT * FROM {table} WHERE device_token=? AND path=?',(entry['target_device'],old)).fetchone()
            if not row and step['state']!='done' and not binding_id:continue
            if c.execute('SELECT 1 FROM organization_inverses WHERE original_job_id=?',(job['id'],)).fetchone():
                raise ValueError('已有撤销计划，需先核对撤销记录')
            if step['state'] not in ('done','binding','needs_binding'):
                raise ValueError('目标已有绑定，但整理检查点尚未确认')
            if os.path.lexists(old):raise ValueError('原目标仍存在，请核对实际挂载位置')
            asset=entry.get('parent_asset_id') if companion else entry['asset_id']
            if not row or (row['asset_id'],row['device_token'],row['path'],row['origin_task'])!=(asset,entry['target_device'],old,job['id']):
                raise ValueError('原目标绑定已变化，不能自动重连')
            if c.execute(f'SELECT 1 FROM {table} WHERE path=?',(new,)).fetchone():
                raise ValueError('新目标路径已有绑定')
            video=None
            if not companion:
                video=c.execute('SELECT v.id,v.path FROM videos v JOIN media_assets a ON a.video_id=v.id WHERE a.id=?',(asset,)).fetchone()
                if not video or bool(row['preferred'])!=(video['path']==old) or c.execute('SELECT 1 FROM videos WHERE path=?',(new,)).fetchone():
                    raise ValueError('默认播放位置或新目标索引已变化')
            path=no_links(new)
            if str(path)!=new or self.device_identity(path)!=entry['target_device']:
                raise ValueError('新目标路径或卷身份已变化')
            expected=step['details'].get('file_identity') if entry['operation']=='copy' else entry['source_identity']
            before=path.stat()
            if not expected or before.st_ino!=expected[1]:raise ValueError('新目标文件身份与任务不符')
            stamp=fingerprint(path)
            after=path.stat()
            approved=entry['fingerprint']
            if (after.st_dev,after.st_ino,after.st_mtime_ns)!=(before.st_dev,before.st_ino,stamp['mtime_ns']) or any(stamp[key]!=approved[key] for key in ('size','sha256')):
                raise ValueError('新目标文件与批准内容不符')
            bindings.append({'table':table,'row':dict(row),'path':new,'stamp':stamp,'identity':[after.st_dev,after.st_ino],
                             'ordinal':step['ordinal'],'video':dict(video) if video else None,'operation':entry['operation'],'details':step['details']})
        return bindings

    def _apply_recovery_target_bindings(self,c,job,bindings):
        from media_locations import MediaLocations
        for binding in bindings:
            row=binding['row'];stamp=binding['stamp'];path=Path(binding['path'])
            info=path.stat()
            if no_links(path)!=path or [info.st_dev,info.st_ino]!=binding['identity'] or (info.st_size,info.st_mtime_ns)!=(stamp['size'],stamp['mtime_ns']):
                raise ValueError('确认期间新目标文件发生变化')
            table=binding['table']
            c.execute(f'UPDATE {table} SET path=?,size=?,mtime_ns=? WHERE id=?',
                      (str(path),stamp['size'],stamp['mtime_ns'],row['id']))
            if table=='media_locations':
                c.execute('UPDATE media_locations SET verified=? WHERE id=?',(time.time(),row['id']))
                if row['preferred']:MediaLocations.update_video_location(c,binding['video']['id'],path,stamp)
            if binding['operation']=='copy':
                ordinal=binding['ordinal']
                details=dict(binding['details'],file_identity=binding['identity'])
                c.execute('UPDATE organization_steps SET details=? WHERE job_id=? AND ordinal=?',
                          (json.dumps(details),job['id'],ordinal))

    def confirm_recovery_map(self,job_id,replacements,expected_digest):
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            bindings=[]
            preview=self._recovery_map_preview(c,job_id,replacements,bindings)
            if preview['digest']!=expected_digest:
                raise ValueError('任务检查点或重连范围已变化，请重新核对')
            current=c.execute('SELECT roots,activated FROM organization_recovery_maps WHERE job_id=?',(job_id,)).fetchone()
            if bindings:
                job=self.get(job_id,c)
                owner='reconnect-target:'+job_id
                resources={path_resource(path) for binding in bindings for path in (binding['row']['path'],binding['path'])}
                resources.update('asset:'+binding['row']['asset_id'] for binding in bindings)
                resources.update('device:'+binding['row']['device_token'] for binding in bindings)
                resources.update('video:'+str(binding['video']['id']) for binding in bindings if binding['video'])
                if not self.leases.acquire_in(c,'organize-reconnect','write',owner,resources,
                                              allowed_reconcile=('reconcile:'+job_id,)):raise ResourceBusy()
                self._apply_recovery_target_bindings(c,job,bindings)
                checkpoint=digest(self.get(job_id,c)['steps'])
                c.execute('INSERT OR REPLACE INTO organization_recovery_maps VALUES(?,?,?,?,?,1)',
                          (job_id,preview['approval_digest'],checkpoint,json.dumps(preview['roots']),time.time()))
                c.execute('DELETE FROM resource_claims WHERE owner=?',(owner,))
                self.refresh_reconciliation(c)
                return preview
            if preview['state']=='done':return preview
            if current and current['activated'] and json.loads(current['roots'])==preview['roots']:
                return preview
            c.execute('INSERT OR REPLACE INTO organization_recovery_maps(job_id,approval_digest,checkpoint_digest,roots,confirmed) VALUES(?,?,?,?,?)',
                      (job_id,preview['approval_digest'],preview['checkpoint_digest'],json.dumps(preview['roots']),time.time()))
        return preview

    def _effective_execution_job(self,c,job,mapping=None,previous_roots=None):
        if mapping is None:mapping=c.execute('SELECT * FROM organization_recovery_maps WHERE job_id=?',(job['id'],)).fetchone()
        if not mapping:return job,None
        if mapping['approval_digest']!=job['approval_digest']:
            raise ValueError('重连授权与原批准清单不一致')
        if not mapping['activated'] and mapping['checkpoint_digest']!=digest(job['steps']):
            raise ValueError('任务检查点已变化，请重新核对重连')
        roots=self._verified_recovery_roots(job,json.loads(mapping['roots']))
        payload=job['payload']
        entries=[];steps=[]
        used=set()
        for entry,step in zip(payload['entries'],job['steps']):
            operation=entry['operation']
            if operation not in ('copy','move'):
                raise ValueError('此任务的操作类型暂不支持挂载重连')
            source_root=entry.get('source_root',payload['source_root'])
            target_root=entry.get('target_root',payload['target_root'])
            if source_root in roots:
                source=self._recovery_path(entry['source'],source_root,roots[source_root])
                entry=dict(entry,source=source,source_root=roots[source_root],approved_source=entry['source'],
                           reconnected_source_proof=payload['root_proofs'][source_root])
                used.add(source_root)
            if target_root in roots:
                destination=self._recovery_path(entry['destination'],target_root,roots[target_root])
                if destination!=entry['destination'] and not mapping['activated']:
                    if step['state']=='done' or self._target_already_bound(c,entry):
                        raise ValueError('目标已有片库绑定，需要核对后才能更换挂载位置')
                entry=dict(entry,destination=destination,target_root=roots[target_root],
                           reconnected_target_proof=payload['root_proofs'][target_root])
                if operation=='copy':
                    step=dict(step,details=self._recovery_copy_details(step['details'],target_root,roots[target_root],(previous_roots or {}).get(target_root)))
                used.add(target_root)
            entries.append(entry);steps.append(step)
        if used!=set(roots):raise ValueError('重连范围包含未使用的根目录')
        return dict(job,payload=dict(payload,entries=entries),steps=steps),mapping

    @staticmethod
    def _target_already_bound(c,entry):
        table='media_resources' if entry.get('role')=='companion' else 'media_locations'
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name=?",(table,)).fetchone():return False
        query=f'SELECT 1 FROM {table} WHERE path=? AND device_token=?'
        parameters=(entry['destination'],entry['target_device'])
        if entry['operation']=='move':
            asset=entry.get('parent_asset_id') if entry.get('role')=='companion' else entry.get('asset_id')
            if not asset:return False
            query+=' AND asset_id=?'
            parameters+=(asset,)
        return bool(c.execute(query,parameters).fetchone())

    @staticmethod
    def _recovery_path(raw,old,new):
        candidate=Path(new)/Path(raw).relative_to(old)
        if no_links(candidate)!=candidate:
            raise ValueError('重连文件路径发生变化')
        return str(candidate)

    @classmethod
    def _recovery_copy_details(cls,details,old,new,previous=None):
        def mapped(raw):
            path=Path(raw)
            if path.is_relative_to(new):return raw
            source=previous if previous and path.is_relative_to(previous) else old
            return cls._recovery_path(raw,source,new)
        details=dict(details)
        if details.get('temporary'):details['temporary']=mapped(details['temporary'])
        if 'previous_temporaries' in details:
            details['previous_temporaries']=[
                dict(record,path=mapped(record['path'])) if isinstance(record,dict) and record.get('path')
                else mapped(record) if isinstance(record,str) else record for record in details['previous_temporaries']]
        return details

    def effective_job(self,job_id):
        with self.db() as c:
            job=self.get(job_id,c)
            mapping=c.execute('SELECT activated FROM organization_recovery_maps WHERE job_id=?',(job_id,)).fetchone()
            return self._effective_execution_job(c,job)[0] if mapping and mapping['activated'] else job

    @staticmethod
    def check_reconnected_source(entry,path):
        if (root_proof(entry['source_root'])!=entry['reconnected_source_proof']
                or path.stat().st_ino!=entry['source_identity'][1]):
            raise ValueError('重连后的目录或源文件身份与批准清单不一致')

    def resources(self,job,c=None):
        values=set()
        for entry in job['payload']['entries']:
            values.update(path_resource(entry[key]) for key in ('source','destination'))
            values.update('device:'+entry[key] for key in ('source_device','target_device'))
            if entry.get('video_id') is not None:values.add('video:'+str(entry['video_id']))
            for key in ('asset_id','parent_asset_id'):
                if entry.get(key):values.add('asset:'+entry[key])
            if entry.get('archive_guard'):values.add(path_resource(entry['archive_guard']['path']))
        return sorted(values)

    def claim(self,job_id):
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            job=self.get(job_id,c)
            resources=sorted({e[key] for e in job['payload']['entries'] for key in ('source_device','target_device')})
            if c.execute('SELECT 1 FROM organization_superseded WHERE job_id=?',(job_id,)).fetchone():raise ValueError('撤销计划已重新审阅，请使用新任务')
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='organization_inverses'").fetchone() and c.execute('SELECT 1 FROM organization_inverses WHERE original_job_id=? AND inverse_job_id IS NOT NULL',(job_id,)).fetchone():raise ValueError('Rollback approved; original execution cannot resume')
            if c.execute('SELECT 1 FROM organization_maintenance WHERE job_id=?',(job_id,)).fetchone():raise ValueError('Job maintenance in progress')
            state=c.execute('SELECT state FROM organization_jobs WHERE id=?',(job_id,)).fetchone()[0]
            if state=='running' or state in TERMINAL:raise ValueError('Job already claimed or finished')
            allowed={'reconcile:'+job_id}
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='organization_inverses'").fetchone():
                allowed.update('reconcile:'+row[0] for row in c.execute('SELECT original_job_id FROM organization_inverses WHERE inverse_job_id=?',(job_id,)))
            effective,mapping=self._effective_execution_job(c,job)
            paths=self.resources(job,c)
            if mapping:paths=sorted(set(paths)|set(self.resources(effective,c)))
            held=self.leases.acquire_in(c,'organize','write','organize:'+job_id,paths,queue=True,allowed_reconcile=allowed)
            if held:
                for resource in resources:
                    owner=c.execute('SELECT job_id FROM organization_leases WHERE resource=?',(resource,)).fetchone()
                    if owner and owner['job_id']!=job_id:raise ValueError('Device is leased by another job')
                    c.execute('INSERT OR REPLACE INTO organization_leases VALUES(?,?)',(resource,job_id))
                c.execute("UPDATE organization_jobs SET state='running',updated=? WHERE id=?",(time.time(),job_id))
                if mapping and not mapping['activated']:
                    c.execute('UPDATE organization_recovery_maps SET activated=1 WHERE job_id=?',(job_id,))
        if not held:raise ResourceBusy()
        self.journal.observe('organize',job_id,'running',{'total':len(job['steps']),'cancellable':True})
        return effective

    def transition(self,job_id,ordinal,expected,state,details=None):
        if state not in TRANSITIONS.get(expected,set()):raise ValueError('Invalid execution transition')
        with self.db() as c:
            job=c.execute('SELECT state FROM organization_jobs WHERE id=?',(job_id,)).fetchone()
            if not job or job['state']!='running':raise ValueError('Execution lease not held')
            changed=c.execute('UPDATE organization_steps SET state=?,details=?,updated=? WHERE job_id=? AND ordinal=? AND state=?',
                              (state,json.dumps(details or {}),time.time(),job_id,ordinal,expected)).rowcount
            if changed!=1:raise ValueError('Step changed; reconciliation required')
            c.execute('INSERT INTO organization_step_events(job_id,ordinal,state,created) VALUES(?,?,?,?)',(job_id,ordinal,state,time.time()))

    def finish(self,job_id,state):
        if state not in ('done','partial','interrupted','waiting','cancelled','failed'):raise ValueError('Invalid job outcome')
        with self.db() as c:
            rows=[r[0] for r in c.execute('SELECT state FROM organization_steps WHERE job_id=?',(job_id,))]
            if not rows:raise ValueError('Unknown job')
            if state=='done' and any(value!='done' for value in rows):raise ValueError('Unfinished files cannot be reported as complete')
            c.execute('UPDATE organization_jobs SET state=?,updated=? WHERE id=?',(state,time.time(),job_id))
            c.execute('DELETE FROM organization_leases WHERE job_id=?',(job_id,))
            c.execute('DELETE FROM resource_claims WHERE owner=?',('organize:'+job_id,))
            paused=c.execute('SELECT pause_requested FROM organization_control WHERE job_id=?',(job_id,)).fetchone()
            if state!='waiting' or paused and paused[0]:c.execute('DELETE FROM resource_waiters WHERE owner=?',('organize:'+job_id,))
            self.refresh_reconciliation(c)
        self.observe_progress(job_id,state)

    def refresh_reconciliation(self,c):
        """Keep uncertain file mutations quarantined while paused or awaiting repair."""
        reversed_jobs=set()
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='organization_inverses'").fetchone():
            reversed_jobs={row[0] for row in c.execute("SELECT i.original_job_id FROM organization_inverses i JOIN organization_jobs j ON j.id=i.inverse_job_id WHERE j.state='done'")}
        c.execute("DELETE FROM resource_claims WHERE kind='organize-reconcile'")
        for job in c.execute("SELECT id,payload FROM organization_jobs WHERE state!='done'").fetchall():
            if job['id'] in reversed_jobs:continue
            entries=json.loads(job['payload'])['entries']
            steps=c.execute('SELECT ordinal,state,details FROM organization_steps WHERE job_id=?',(job['id'],)).fetchall()
            uncertain=False
            for step in steps:
                operation=entries[step['ordinal']]['operation'];state=step['state']
                if state in ('committing','committed','binding','needs_binding') or (operation in ('move','archive') and state in ('staging','staged','interrupted')):
                    uncertain=True;break
            if uncertain:
                owner='reconcile:'+job['id'];c.execute('INSERT INTO resource_claims VALUES(?,?,?,?)',(owner,'organize-reconcile','write',time.time()))
                scopes=set(self.resources({'payload':{'entries':entries}},c))
                mapping=c.execute('SELECT roots FROM organization_recovery_maps WHERE job_id=? AND activated=1',(job['id'],)).fetchone()
                if mapping:
                    payload=json.loads(job['payload']);roots=json.loads(mapping['roots'])
                    for entry in entries:
                        for field,root_field in (('source','source_root'),('destination','target_root')):
                            root=entry.get(root_field,payload[root_field])
                            if root in roots:scopes.add(path_resource(Path(roots[root])/Path(entry[field]).relative_to(root)))
                c.executemany('INSERT INTO resource_scopes VALUES(?,?)',((owner,resource) for resource in sorted(scopes)))

    def request_cancel(self,job_id):
        self.get(job_id)
        with self.db() as c:
            c.execute('INSERT INTO organization_control(job_id,cancel_requested) VALUES(?,1) ON CONFLICT(job_id) DO UPDATE SET cancel_requested=1',(job_id,))
            c.execute('DELETE FROM resource_waiters WHERE owner=?',('organize:'+job_id,))

    def _skip_preview(self,c,job_id,ordinal):
        job=c.execute('SELECT * FROM organization_jobs WHERE id=?',(job_id,)).fetchone()
        if not job:raise ValueError('整理任务不存在')
        paused=c.execute('SELECT pause_requested FROM organization_control WHERE job_id=?',(job_id,)).fetchone()
        if job['state'] in ('queued','running','done') or job['state']=='waiting' and not (paused and paused[0]):raise ValueError('请先暂停任务并等待停止，再审阅跳过范围')
        if c.execute('SELECT 1 FROM organization_maintenance WHERE job_id=?',(job_id,)).fetchone():raise ValueError('任务维护中，请稍后重试')
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='organization_inverses'").fetchone() and c.execute('SELECT 1 FROM organization_inverses WHERE original_job_id=? OR inverse_job_id=?',(job_id,job_id)).fetchone():raise ValueError('已有撤销计划，请先处理该计划，不能跳过其中条目')
        payload=json.loads(job['payload']);entries=payload['entries']
        if digest(payload)!=job['approval_digest']:raise ValueError('批准清单已变化，保留现场核对')
        if type(ordinal) is not int or not 0<=ordinal<len(entries):raise ValueError('请选择当前任务中的文件')
        entry=entries[ordinal]
        if entry['operation'] not in ('copy','move','keep'):raise ValueError('归档或撤销任务须完整处理，不能跳过')
        owner=entry.get('parent_asset_id') if entry.get('role')=='companion' else entry.get('asset_id')
        indices=[i for i,e in enumerate(entries) if i==ordinal or owner and (e.get('parent_asset_id')==owner or e.get('asset_id')==owner)]
        steps=[dict(r) for r in c.execute('SELECT * FROM organization_steps WHERE job_id=? ORDER BY ordinal',(job_id,))]
        for step in steps:step['details']=json.loads(step['details'])
        if [s['ordinal'] for s in steps]!=list(range(len(entries))):raise ValueError('步骤记录不完整，请保留现场核对')
        if any(steps[i]['details'].get('skip') for i in indices):raise ValueError('此文件组已跳过；需要重新整理时请制定新计划')
        if any(steps[i]['state'] not in ({'pending','copying','verified'} if entries[i]['operation']=='copy' else {'pending'}) for i in indices):raise ValueError('此文件组已有提交、绑定或移动暂存，请继续核对或预览撤销，不能跳过')
        frozen={'job':job_id,'approval':job['approval_digest'],'state':job['state'],'updated':job['updated'],'members':[{'ordinal':i,'entry':entries[i],'step':steps[i]} for i in indices]}
        return {'id':job_id,'ordinal':ordinal,'digest':digest(frozen),'bytes':sum(entries[i]['fingerprint']['size'] for i in indices),'items':[{'ordinal':i,'source':entries[i]['source'],'destination':entries[i]['destination'],'role':entries[i].get('role','media'),'state':steps[i]['state']} for i in indices]},steps

    def skip_preview(self,job_id,ordinal):
        with self.db() as c:return self._skip_preview(c,job_id,ordinal)[0]

    def skip(self,job_id,ordinal,expected_digest):
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE');preview,steps=self._skip_preview(c,job_id,ordinal)
            if preview['digest']!=expected_digest:raise ValueError('跳过范围已变化，请重新审阅')
            now=time.time()
            for item in preview['items']:
                step=steps[item['ordinal']];step['details']['skip']={'confirmed_at':now,'group_ordinal':ordinal}
                c.execute('UPDATE organization_steps SET details=?,updated=? WHERE job_id=? AND ordinal=?',(json.dumps(step['details']),now,job_id,item['ordinal']))
                c.execute("INSERT INTO organization_step_events(job_id,ordinal,state,created) VALUES(?,?,'skipped',?)",(job_id,item['ordinal'],now))
            settled=all(s['state']=='done' or s['details'].get('skip') for s in steps)
            state='cancelled' if settled else 'partial'
            c.execute('UPDATE organization_jobs SET state=?,updated=? WHERE id=?',(state,now,job_id))
            c.execute('DELETE FROM resource_waiters WHERE owner=?',('organize:'+job_id,))
            self.refresh_reconciliation(c)
        self.observe_progress(job_id,state,reason='explicit_skip')
        return {'skipped':len(preview['items']),'state':state}

    def cancel_requested(self,job_id):
        with self.db() as c:row=c.execute('SELECT cancel_requested FROM organization_control WHERE job_id=?',(job_id,)).fetchone()
        return bool(row and row[0])

    def request_pause(self,job_id):
        job=self.get(job_id)
        if job['state'] in TERMINAL:raise ValueError('Finished jobs cannot be paused')
        with self.db() as c:
            c.execute('INSERT INTO organization_control(job_id,pause_requested) VALUES(?,1) ON CONFLICT(job_id) DO UPDATE SET pause_requested=1',(job_id,))
            c.execute('DELETE FROM resource_waiters WHERE owner=?',('organize:'+job_id,))

    def pause_requested(self,job_id):
        with self.db() as c:row=c.execute('SELECT pause_requested FROM organization_control WHERE job_id=?',(job_id,)).fetchone()
        return bool(row and row[0])

    def progress(self,job_id):
        job=self.get(job_id);counts={};copied=verified=committed=0
        for entry,step in zip(job['payload']['entries'],job['steps']):
            state=step['state'];size=entry['fingerprint']['size'];counts[state]=counts.get(state,0)+1
            full=state in ('verified','committing','committed','binding','needs_binding','done')
            if entry['operation']=='copy':
                copied+=size if full else min(size,max(0,step['details'].get('bytes',0))) if state=='copying' else 0
            if full:verified+=size
            if state in ('committed','binding','needs_binding','done'):committed+=size
        return {'total':len(job['steps']),'processed':counts.get('done',0),'skipped':sum(bool(s['details'].get('skip')) for s in job['steps']),'bytes_total':sum(e['fingerprint']['size'] for e in job['payload']['entries']),
                'bytes_processed':copied,'bytes_verified':verified,'bytes_committed':committed,'phases':counts}

    def observe_progress(self,job_id,state='running',phase=None,reason=None):
        summary=self.progress(job_id);summary.pop('phases')
        if phase:summary['phase']=phase
        if reason:summary['reason']=reason
        elif state=='waiting' and self.pause_requested(job_id):summary['reason']='user_paused'
        summary['cancellable']=state in ('queued','running','waiting')
        self.journal.observe('organize',job_id,state,summary)

    def resume(self,job_id):
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            steps=c.execute('SELECT state,details FROM organization_steps WHERE job_id=?',(job_id,)).fetchall()
            if steps and all(s['state']=='done' or json.loads(s['details']).get('skip') for s in steps):raise ValueError('全部条目已处理或跳过；跳过文件需制定新计划')
            if c.execute('SELECT 1 FROM organization_superseded WHERE job_id=?',(job_id,)).fetchone():raise ValueError('撤销计划已重新审阅，请使用新任务')
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='organization_inverses'").fetchone() and c.execute('SELECT 1 FROM organization_inverses WHERE original_job_id=? AND inverse_job_id IS NOT NULL',(job_id,)).fetchone():raise ValueError('Rollback approved; original execution cannot resume')
            if c.execute('SELECT 1 FROM organization_maintenance WHERE job_id=?',(job_id,)).fetchone():raise ValueError('Job maintenance in progress')
            self._effective_execution_job(c,self.get(job_id,c))
            changed=c.execute("UPDATE organization_jobs SET state='queued',updated=? WHERE id=? AND state IN ('cancelled','interrupted','partial','failed','waiting')",(time.time(),job_id)).rowcount
            if changed!=1:raise ValueError('Only stopped jobs can be resumed')
            c.execute('DELETE FROM organization_control WHERE job_id=?',(job_id,))
        self.journal.observe('organize',job_id,'queued',{'reason':'explicit_resume'})

    def recover(self):
        """Startup only, after the application has acquired exclusive DataGuard."""
        with self.db() as c:
            jobs=[r[0] for r in c.execute("SELECT id FROM organization_jobs WHERE state='running'")]
            c.execute("UPDATE organization_jobs SET state='interrupted' WHERE state='running'")
            c.execute('DELETE FROM organization_leases')
            c.execute('DELETE FROM organization_maintenance')
            c.execute("DELETE FROM resource_waiters WHERE owner LIKE 'organize:%'")
            c.execute("DELETE FROM resource_claims WHERE kind IN ('organize','cleanup')")
            self.refresh_reconciliation(c)
        for job_id in jobs:self.journal.observe('organize',job_id,'interrupted',{'reason':'restart_reconciliation_required'})
        return jobs
