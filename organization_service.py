"""Organization application service: frozen inventory plans to persistent execution."""
import json
import hashlib
import time
import threading
from pathlib import Path
from media_locations import MediaLocations
from organization_jobs import OrganizationJobs,identify,no_links
from organization_executor import CopyExecutor
from organization_moves import MoveExecutor
from organization_cleanup import CopyCleanup
from resource_leases import ResourceBusy


class OrganizationService:
    def __init__(self,db,inventory,device_identity=identify):
        self.db,self.inventory=db,inventory
        self.jobs=OrganizationJobs(db,device_identity)
        self.locations=MediaLocations(db,device_identity)
        self.copy=CopyExecutor(self.jobs,self.bind_copy)
        self.move=MoveExecutor(self.jobs,self.locations.bind_moves)
        self.cleanup=CopyCleanup(self.jobs)
        self.wake=threading.Event();self.stopping=threading.Event()
        self.dispatch=threading.Lock();self.thread=None

    def prepare(self,plan_id,expected_digest,selected,operation='copy'):
        if operation not in ('copy','move'):raise ValueError('请选择复制或同盘移动')
        with self.jobs.leases.hold('organization-preview'):
            with self.db() as c:row=c.execute('SELECT * FROM organization_plans WHERE id=?',(plan_id,)).fetchone()
            if not row:raise ValueError('整理预览不存在')
            payload=json.loads(row['payload'])
            unsigned={key:value for key,value in payload.items() if key!='digest'}
            if payload['digest']!=expected_digest or hashlib.sha256(json.dumps(unsigned,sort_keys=True).encode()).hexdigest()!=expected_digest:raise ValueError('整理预览已变化，请重新载入')
            if not isinstance(selected,list) or not selected or any(type(i) is not int or not 0<=i<len(payload['entries']) for i in selected):raise ValueError('请选择当前预览中的文件')
            if len(selected)!=len(set(selected)):raise ValueError('选择包含重复条目')
            device=self.inventory.device(row['device_id']);source_root=no_links(device['root'])
            if str(source_root)!=payload['source_root'] or device['identity']!=payload['source_device_identity'] or self.jobs.device_identity(source_root)!=device['identity']:raise ValueError('源设备或根目录变化，请重新生成预览')
            self.locations.migrate()
            entries=[];originals={}
            for index in sorted(selected):
                original=payload['entries'][index]
                if original.get('conflicts'):raise ValueError('请先解决预览中的冲突')
                if original.get('companions'):raise ValueError('包含伴随资源的执行尚未接入，请保留预览')
                source=no_links(source_root/original['relative'])
                if str(source)!=original['source']:raise ValueError('预览源路径变化')
                with self.db() as c:video=c.execute('SELECT id FROM videos WHERE path=?',(str(source),)).fetchone()
                metadata=dict(original.get('source_metadata') or {})
                annotation=metadata.get('organization_annotation') or {}
                if annotation.get('title'):
                    metadata['title']=annotation['title'];metadata['locked_fields']=json.dumps(['title'])
                entries.append({'metadata':metadata,'source':str(source),'destination':original['destination'],'video_id':video['id'] if video else None,'operation':operation})
                originals[str(source)]=original
            def register(entry,stamp,device_token):
                original=originals[str(no_links(entry['source']))]
                if (stamp['size'],stamp['mtime_ns'])!=(original['size'],original['mtime_ns']) or (original.get('digest') and original['digest']!=stamp['sha256']):raise ValueError('源文件已变化，请重新盘点')
                return self.locations.register_verified(entry['source'],stamp,device_token,entry.get('video_id'),entry['metadata'])
            return self.jobs.prepare(entries,source_root,row['target'],register=register)

    def bind_copy(self,entry,job_id):
        video_id,location_id=self.locations.bind_committed_copy(entry['asset_id'],entry['destination'],entry['fingerprint'],entry['target_device'],job_id,entry.get('metadata'),entry.get('verified_identity'))
        self.locations.prefer(video_id,location_id,entry.get('verified_identity'))
        return location_id

    def approve(self,draft_id,expected_digest):
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
                row=c.execute("SELECT j.id FROM organization_jobs j LEFT JOIN organization_control c ON c.job_id=j.id WHERE j.state IN ('queued','waiting') AND COALESCE(c.pause_requested,0)=0 ORDER BY j.created,j.id LIMIT 1").fetchone()
            if not row:self.wake.wait(1);self.wake.clear();continue
            try:result=self.run_once(row['id'])
            except Exception as error:
                current=self.jobs.get(row['id'])
                if current['state'] in ('done','cancelled') or self.jobs.pause_requested(row['id']):continue
                self.jobs.finish(row['id'],'failed')
                self.jobs.observe_progress(row['id'],'failed',reason=type(error).__name__)
                continue
            if result['state']=='waiting':self.wake.wait(1);self.wake.clear()

    def listing(self,offset=0,limit=25):
        offset=max(0,int(offset));limit=min(100,max(1,int(limit)))
        with self.db() as c:
            total=c.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0]
            rows=[dict(r) for r in c.execute('SELECT id,state,created,updated FROM organization_jobs ORDER BY created DESC,id DESC LIMIT ? OFFSET ?',(limit,offset))]
        return {'items':rows,'total':total,'offset':offset,'limit':limit}

    def detail(self,job_id,offset=0,limit=50):
        job=self.jobs.get(job_id);offset=max(0,int(offset));limit=min(100,max(1,int(limit)))
        entries=[dict(entry,step=step) for entry,step in zip(job['payload']['entries'],job['steps'])]
        return {key:job[key] for key in ('id','state','created','updated')}|{'items':entries[offset:offset+limit],'total':len(entries),'offset':offset,'limit':limit,'progress':self.jobs.progress(job_id)}
