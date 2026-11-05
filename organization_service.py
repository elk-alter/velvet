"""Organization application service: frozen inventory plans to persistent execution."""
from organization_preflight import LABELS
import json
import hashlib
import time
import threading
from pathlib import Path
from media_locations import MediaLocations
from media_companions import destination as companion_destination
from companion_bindings import CompanionBindings
from organization_jobs import OrganizationJobs,identify,no_links
from organization_executor import CopyExecutor
from organization_moves import MoveExecutor
from organization_cleanup import CopyCleanup
from organization_copy_undo import CopyUndo
from organization_keep import KeepExecutor
from organization_archive import SourceArchive
from archive_policy import is_archive_path
from inventory_query import InventoryQuery
from organization_planner import QueryPlanner
from resource_leases import ResourceBusy
from restore_safety import require_reconciled_files


class OrganizationService:
    def __init__(self,db,inventory,device_identity=identify):
        self.db,self.inventory=db,inventory
        self.jobs=OrganizationJobs(db,device_identity)
        self.locations=MediaLocations(db,device_identity)
        self.copy=CopyExecutor(self.jobs,self.bind_copy)
        self.resources=CompanionBindings(db,device_identity)
        self.move=MoveExecutor(self.jobs,self.bind_moves)
        self.cleanup=CopyCleanup(self.jobs)
        self.copy_undo=CopyUndo(self.jobs,self.locations)
        self.keep=KeepExecutor(self.jobs,self.bind_keep)
        self.archive=SourceArchive(self.jobs)
        self.move.validator=self.archive.validate_guard
        self.planner=QueryPlanner(inventory,InventoryQuery(inventory),self.jobs.leases)
        with db() as c:c.execute('CREATE TABLE IF NOT EXISTS organization_draft_origins(draft_id TEXT PRIMARY KEY,preview_id TEXT)')
        self.wake=threading.Event();self.stopping=threading.Event()
        self.dispatch=threading.Lock();self.thread=None

    def prepare(self,plan_id,expected_digest,selected,operation='copy'):
        if operation not in ('copy','move','keep'):raise ValueError('请选择原位保留、复制或同盘移动')
        with self.jobs.leases.hold('organization-preview'):
            with self.db() as c:row=c.execute('SELECT * FROM organization_plans WHERE id=?',(plan_id,)).fetchone()
            if not row:raise ValueError('整理预览不存在')
            payload=json.loads(row['payload'])
            if (operation=='keep')!=(payload.get('operation')=='keep'):raise ValueError('原位保留需要独立预览，请返回规则页选择操作')
            unsigned={key:value for key,value in payload.items() if key!='digest'}
            if payload['digest']!=expected_digest or hashlib.sha256(json.dumps(unsigned,sort_keys=True).encode()).hexdigest()!=expected_digest:raise ValueError('整理预览已变化，请重新载入')
            if not isinstance(selected,list) or not selected or any(type(i) is not int or not 0<=i<len(payload['entries']) for i in selected):raise ValueError('请选择当前预览中的文件')
            if len(selected)!=len(set(selected)):raise ValueError('选择包含重复条目')
            frozen=payload.get('source_devices') or {row['device_id']:{'root':payload['source_root'],'identity':payload['source_device_identity']}}
            contexts={}
            for index in selected:
                device_id=payload['entries'][index].get('device_id',row['device_id'])
                if device_id in contexts:continue
                device=self.inventory.device(device_id);source_root=no_links(device['root'])
                self.inventory.verify_root(device)
                if device_id not in frozen or str(source_root)!=frozen[device_id]['root'] or device['identity']!=frozen[device_id]['identity'] or self.jobs.device_identity(source_root)!=device['identity']:raise ValueError('源设备或根目录变化，请重新生成预览')
                contexts[device_id]=source_root
            self.locations.migrate()
            entries=[];originals={};resource_records={};retained=[];companions=[];assets={}
            for index in sorted(selected):
                original=payload['entries'][index]
                source_root=contexts[original.get('device_id',row['device_id'])]
                if original.get('conflicts'):raise ValueError('请先解决预览中的冲突')
                source=no_links(source_root/original['relative'])
                if is_archive_path(source) or is_archive_path(original['destination']):raise ValueError('归档目录仅通过受控归档/恢复任务操作')
                if str(source)!=original['source']:raise ValueError('预览源路径变化')
                with self.db() as c:video=c.execute('SELECT id FROM videos WHERE path=?',(str(source),)).fetchone()
                metadata=dict(original.get('source_metadata') or {})
                annotation=metadata.get('organization_annotation') or {}
                if annotation.get('title'):
                    metadata['title']=annotation['title'];metadata['locked_fields']=json.dumps(['title'])
                entries.append({'metadata':metadata,'source':str(source),'destination':original['destination'],'video_id':video['id'] if video else None,'operation':operation})
                if payload.get('source_devices'):entries[-1].update(source_root=str(source_root),target_root=str(source_root) if operation=='keep' else row['target'])
                originals[str(source)]=original
                for resource in original.get('companions',[]):
                    name=resource['name']
                    if Path(name).name!=name:raise ValueError('伴随资源路径无效')
                    resource_source=source.parent/name
                    if resource.get('shared'):
                        retained.append({'source':str(resource_source),'reason':resource.get('reason','shared_resource'),'operation':'retain'})
                        continue
                    if not resource.get('available') or 'mtime_ns' not in resource:raise ValueError('伴随资源未确认或快照过旧，请重新盘点')
                    target=companion_destination(source,original['destination'],name)
                    companions.append({'source':str(resource_source),'destination':str(target),'video_id':None,'operation':operation,'role':'companion','metadata':{'parent_source':str(source),'kind':resource_source.suffix.lower()}})
                    if payload.get('source_devices'):companions[-1].update(source_root=str(source_root),target_root=str(source_root) if operation=='keep' else row['target'])
                    resource_records[str(no_links(resource_source))]=resource
            entries.extend(companions)
            def register(entry,stamp,device_token):
                source=str(no_links(entry['source']))
                original=resource_records[source] if entry.get('role')=='companion' else originals[source]
                media_original=originals[entry['metadata']['parent_source']] if entry.get('role')=='companion' else original
                expected_device=media_original.get('source_device_identity',payload.get('source_device_identity'))
                if device_token!=expected_device:raise ValueError('源设备在完整校验期间变化，请重新盘点并确认')
                if (stamp['size'],stamp['mtime_ns'])!=(original['size'],original['mtime_ns']) or (original.get('digest') and original['digest']!=stamp['sha256']):raise ValueError('源文件已变化，请重新盘点')
                if entry.get('role')=='companion':
                    entry['parent_asset_id']=assets[entry['metadata']['parent_source']];return None
                asset=self.locations.register_verified(entry['source'],stamp,device_token,entry.get('video_id'),entry['metadata'])
                assets[entry['source']]=asset;return asset
            roots=sorted(set(contexts.values()),key=str);target_root=roots[0] if operation=='keep' else row['target']
            options={'source_roots':roots,'target_roots':roots if operation=='keep' else [target_root]} if payload.get('source_devices') else {}
            draft=self.jobs.prepare(entries,roots[0],target_root,register=register,retained=list({item['source']:item for item in retained}.values()),**options)
            with self.db() as c:c.execute('INSERT OR IGNORE INTO organization_draft_origins VALUES(?,?)',(draft['id'],plan_id))
            return draft

    def bind_copy(self,entry,job_id):
        if entry.get('role')=='companion':return self.resources.bind_copy(entry,job_id)
        video_id,location_id=self.locations.bind_committed_copy(entry['asset_id'],entry['destination'],entry['fingerprint'],entry['target_device'],job_id,entry.get('metadata'),entry.get('verified_identity'))
        self.locations.prefer(video_id,location_id,entry.get('verified_identity'))
        return location_id

    def bind_keep(self,entry,job_id):
        if entry.get('role')=='companion':return self.resources.bind_copy(entry,job_id)
        vid,location=self.locations.bind_committed_copy(entry['asset_id'],entry['source'],entry['fingerprint'],entry['source_device'],job_id,entry.get('metadata'),entry['source_identity'])
        with self.db() as c:current=c.execute('SELECT path FROM videos WHERE id=?',(vid,)).fetchone()
        if current and current['path']==entry['source']:self.locations.prefer(vid,location,entry['source_identity'])
        return location

    def bind_moves(self,entries,job_id):
        media=[entry for entry in entries if entry.get('role')!='companion']
        checked=self.resources.prepare_moves([entry for entry in entries if entry.get('role')=='companion'])
        resource_bindings={}
        def apply(c):resource_bindings.update(self.resources.apply_moves(c,checked,job_id))
        return self.locations.bind_moves(media,job_id,apply)|resource_bindings

    def approve_inverse(self,job_id,expected_digest):
        job=self.jobs.get(job_id)
        inverse=(self.copy_undo.approve if job['payload']['entries'][0]['operation']=='copy' else self.move.approve_undo)(job_id,expected_digest)
        self.wake.set();return inverse

    def prepare_inverse(self,job_id):
        job=self.jobs.get(job_id)
        return (self.copy_undo.prepare if job['payload']['entries'][0]['operation']=='copy' else self.move.prepare_undo)(job_id)

    def approve(self,draft_id,expected_digest):
        with self.db() as c:
            stored=c.execute('SELECT payload FROM organization_drafts WHERE id=?',(draft_id,)).fetchone()
            if c.execute('SELECT 1 FROM organization_inverses WHERE draft_id=?',(draft_id,)).fetchone():raise ValueError('反向计划需要使用撤销确认入口')
        if stored and any(e['operation']=='archive' for e in json.loads(stored[0])['entries']):raise ValueError('归档计划需要单独确认并使用归档批准入口')
        with self.db() as c:existing=c.execute('SELECT j.id FROM organization_jobs j JOIN organization_drafts d ON d.digest=j.approval_digest WHERE d.id=? AND d.digest=?',(draft_id,expected_digest)).fetchone()
        if existing:return existing['id']
        with self.jobs.leases.hold('organization-approval'):
            job_id=self.jobs.approve(draft_id,expected_digest)
        self.wake.set()
        return job_id

    def control(self,job_id,action):
        if action=='resume':self.jobs.resume(job_id)
        else:
            if action not in ('pause','cancel'):raise ValueError('Unknown organization control')
            with self.db() as c:
                c.execute('BEGIN IMMEDIATE')
                job=c.execute('SELECT state FROM organization_jobs WHERE id=?',(job_id,)).fetchone()
                if not job:raise ValueError('Unknown organization job')
                if job['state']=='done':return self.jobs.get(job_id)
                if action=='pause' and job['state'] not in ('queued','waiting','running'):raise ValueError('Only active jobs can be paused')
                column='pause_requested' if action=='pause' else 'cancel_requested'
                c.execute('DELETE FROM resource_waiters WHERE owner=?',('organize:'+job_id,))
                c.execute('INSERT INTO organization_control(job_id,'+column+') VALUES(?,1) ON CONFLICT(job_id) DO UPDATE SET '+column+'=1',(job_id,))
                if job['state']!='running':
                    state='waiting' if action=='pause' else 'cancelled'
                    c.execute('UPDATE organization_jobs SET state=?,updated=? WHERE id=?',(state,time.time(),job_id))
            current=self.jobs.get(job_id)
            if current['state']!='running':self.jobs.observe_progress(job_id,current['state'])
        self.wake.set()
        return self.jobs.get(job_id)

    def run_once(self,job_id):
        job=self.jobs.get(job_id)
        if job['state'] not in ('queued','waiting'):return job
        if self.jobs.cancel_requested(job_id):return self.control(job_id,'cancel')
        if self.jobs.pause_requested(job_id):return self.control(job_id,'pause')
        try:
            operations={entry['operation'] for entry in job['payload']['entries']}
            def draining():
                if self.stopping.is_set():self.jobs.request_pause(job_id)
                return False
            if operations=={'copy'}:return self.copy.run(job_id,draining)
            if operations=={'move'}:return self.move.run(job_id,draining)
            if operations=={'unbind_copy'}:return self.copy_undo.run(job_id,draining)
            if operations=={'keep'}:return self.keep.run(job_id,draining)
            if operations=={'archive'}:return self.move.run(job_id,draining)
            raise ValueError('Unsupported approved operation')
        except ResourceBusy:
            self.jobs.finish(job_id,'waiting')
            self.jobs.observe_progress(job_id,'waiting',reason='media_resource_busy')
            return self.jobs.get(job_id)

    def start(self):
        with self.dispatch:
            if self.thread and self.thread.is_alive():self.wake.set();return
            self.stopping.clear()
            self.thread=threading.Thread(target=self.work,daemon=True)
            self.thread.start()

    def stop(self):
        self.stopping.set()
        with self.db() as c:running=[r[0] for r in c.execute("SELECT id FROM organization_jobs WHERE state='running'")]
        for job_id in running:self.jobs.request_pause(job_id)
        self.wake.set()

    def work(self):
        while not self.stopping.is_set():
            with self.db() as c:
                rows=c.execute("SELECT j.id FROM organization_jobs j LEFT JOIN organization_control control ON control.job_id=j.id WHERE j.state IN ('queued','waiting') AND COALESCE(control.pause_requested,0)=0 ORDER BY j.created,j.id").fetchall()
            progressed=False
            for row in rows:
                if self.stopping.is_set():break
                try:result=self.run_once(row['id'])
                except Exception as error:
                    current=self.jobs.get(row['id'])
                    if current['state'] in ('done','cancelled') or self.jobs.pause_requested(row['id']):continue
                    self.jobs.finish(row['id'],'failed');self.jobs.observe_progress(row['id'],'failed',reason=type(error).__name__)
                    progressed=True;continue
                if result['state']!='waiting':progressed=True;break
            if not progressed:self.wake.wait(1);self.wake.clear()

    def before_restore(self):
        with self.db() as c:require_reconciled_files(c)

    def preview(self,plan_id,offset=0,limit=50):
        with self.db() as c:row=c.execute('SELECT * FROM organization_plans WHERE id=?',(plan_id,)).fetchone()
        if not row:raise ValueError('整理预览不存在')
        payload=json.loads(row['payload']);entries=payload['entries'];offset=max(0,int(offset));limit=min(100,max(1,int(limit)))
        return {'id':row['id'],'digest':payload['digest'],'device_id':row['device_id'],'target':row['target'],'source_root':payload['source_root'],
                'items':[dict(entry,index=index,conflict_messages=[LABELS.get(code,code) for code in entry.get('conflicts',[])]) for index,entry in enumerate(entries[offset:offset+limit],offset)],'total':len(entries),'offset':offset,'limit':limit,'bytes':payload['bytes'],'space':payload.get('space',{}),'operation':payload.get('requested_operation','copy')}

    def selected(self,plan_id,scope):
        if isinstance(scope,list):return scope
        if scope not in ('all','eligible'):raise ValueError('请选择明确的操作范围')
        with self.db() as c:row=c.execute('SELECT payload FROM organization_plans WHERE id=?',(plan_id,)).fetchone()
        if not row:raise ValueError('整理预览不存在')
        entries=json.loads(row[0])['entries']
        return [i for i,entry in enumerate(entries) if scope=='all' or not entry.get('conflicts')]

    def draft(self,draft_id,offset=0,limit=50,retained_offset=0):
        with self.db() as c:
            row=c.execute('SELECT * FROM organization_drafts WHERE id=?',(draft_id,)).fetchone()
            inverse=c.execute('SELECT original_job_id FROM organization_inverses WHERE draft_id=?',(draft_id,)).fetchone()
            origin=c.execute('SELECT preview_id FROM organization_draft_origins WHERE draft_id=?',(draft_id,)).fetchone()
            archive=c.execute('SELECT original_job_id FROM organization_archive_plans WHERE draft_id=?',(draft_id,)).fetchone()
        if not row:raise ValueError('Unknown organization draft')
        payload=json.loads(row['payload']);entries=payload['entries'];offset=max(0,int(offset));limit=min(100,max(1,int(limit)))
        return {'archive_of':archive[0] if archive else None,'preview_id':origin['preview_id'] if origin else None,'id':row['id'],'digest':row['digest'],'items':entries[offset:offset+limit],'total':len(entries),'bytes':sum(entry['fingerprint']['size'] for entry in entries),'offset':offset,'limit':limit,
                'retained':payload.get('retained',[])[max(0,int(retained_offset)):max(0,int(retained_offset))+limit],'retained_total':len(payload.get('retained',[])),'retained_offset':max(0,int(retained_offset)),'operation':entries[0]['operation'],'inverse_of':inverse['original_job_id'] if inverse else None,
                'preflight':payload.get('preflight'),'media_count':sum(entry.get('role')!='companion' for entry in entries),'resource_count':sum(entry.get('role')=='companion' for entry in entries)}

    def listing(self,offset=0,limit=25):
        offset=max(0,int(offset));limit=min(100,max(1,int(limit)))
        with self.db() as c:
            total=c.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0]
            rows=[dict(r) for r in c.execute("WITH page AS (SELECT id,state,created,updated FROM organization_jobs ORDER BY created DESC,id DESC LIMIT ? OFFSET ?) SELECT p.*,COUNT(s.ordinal) AS total,COALESCE(SUM(s.state='done'),0) AS processed,COALESCE(SUM(json_type(s.details,'$.skip')='object'),0) AS skipped FROM page p LEFT JOIN organization_steps s ON s.job_id=p.id GROUP BY p.id ORDER BY p.created DESC,p.id DESC",(limit,offset))]
        return {'items':rows,'total':total,'offset':offset,'limit':limit}

    def detail(self,job_id,offset=0,limit=50):
        job=self.jobs.get(job_id);offset=max(0,int(offset));limit=min(100,max(1,int(limit)))
        entries=[dict(entry,step=step) for entry,step in zip(job['payload']['entries'],job['steps'])]
        for entry in entries:
            entry['step']['details']['preflight_messages']=[LABELS.get(code,code) for code in entry['step']['details'].get('preflight',[])]
        with self.db() as c:
            inverse=c.execute('SELECT i.inverse_job_id,j.state,COALESCE(control.pause_requested,0) AS paused FROM organization_inverses i LEFT JOIN organization_jobs j ON j.id=i.inverse_job_id LEFT JOIN organization_control control ON control.job_id=j.id WHERE i.original_job_id=?',(job_id,)).fetchone()
            inverse_of=c.execute('SELECT original_job_id FROM organization_inverses WHERE inverse_job_id=?',(job_id,)).fetchone()
            superseded=c.execute('SELECT * FROM organization_superseded WHERE job_id=?',(job_id,)).fetchone()
            archive=c.execute('SELECT original_job_id FROM organization_archive_plans WHERE archive_job_id=?',(job_id,)).fetchone()
            archives=[dict(r) for r in c.execute('SELECT p.archive_job_id AS id,j.state,i.inverse_job_id,reverse.state AS reverse_state FROM organization_archive_plans p JOIN organization_jobs j ON j.id=p.archive_job_id LEFT JOIN organization_inverses i ON i.original_job_id=j.id LEFT JOIN organization_jobs reverse ON reverse.id=i.inverse_job_id WHERE p.original_job_id=? ORDER BY j.created DESC LIMIT 20',(job_id,))]
            if superseded:inverse_of=(superseded['original_job'],)
        capability=None
        if job['payload']['entries'][0]['operation']=='copy' and job['state']=='done':
            try:
                for root in job['payload'].get('source_roots',[job['payload']['source_root']]):self.archive.capability(root)
                capability={'supported':True}
            except (OSError,ValueError) as error:capability={'supported':False,'reason':str(error)}
        return {'archive_capability':capability,'archive_of':archive[0] if archive else None,'source_archives':archives,'superseded':dict(superseded) if superseded else None,'inverse':dict(inverse) if inverse else None,'inverse_of':inverse_of[0] if inverse_of else None,'operation':job['payload']['entries'][0]['operation']}|{key:job[key] for key in ('id','state','created','updated')}|{'items':entries[offset:offset+limit],'total':len(entries),'offset':offset,'limit':limit,'progress':self.jobs.progress(job_id),'paused':self.jobs.pause_requested(job_id)}
