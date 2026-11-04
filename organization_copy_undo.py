"""Approved, atomic copy-binding rollback. Physical files always stay in place."""
import json
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from media_locations import fingerprint
from organization_jobs import digest,no_links
from organization_executor import CopyCancelled,CopyPaused,file_identity
from resource_leases import ResourceBusy
from archive_policy import is_archive_path


class CopyUndo:
    def __init__(self,jobs,locations,checkpoint=lambda *args:None):
        self.jobs,self.locations,self.checkpoint=jobs,locations,checkpoint

    @contextmanager
    def exclusive(self,original):
        """Freeze the stopped original, including its own reconciliation quarantine."""
        owner='copy-undo:'+uuid.uuid4().hex
        with self.jobs.db() as c:
            c.execute('BEGIN IMMEDIATE')
            job=c.execute('SELECT state FROM organization_jobs WHERE id=?',(original,)).fetchone()
            if not job or job['state'] in ('running','queued'):raise ValueError('请先暂停复制任务，再审阅撤销')
            control=c.execute('SELECT pause_requested FROM organization_control WHERE job_id=?',(original,)).fetchone()
            if job['state']=='waiting' and not (control and control[0]):raise ValueError('请先暂停等待中的复制任务，再审阅撤销')
            claims=c.execute('SELECT * FROM resource_claims').fetchall()
            if any(row['owner']!='reconcile:'+original or row['kind']!='organize-reconcile' for row in claims):raise ResourceBusy()
            c.execute('INSERT INTO organization_maintenance VALUES(?)',(original,))
            c.execute('INSERT INTO resource_claims VALUES(?,?,?,?)',(owner,'copy-undo-preview','write',time.time()))
        try:yield
        finally:
            with self.jobs.db() as c:
                c.execute('DELETE FROM resource_claims WHERE owner=?',(owner,))
                c.execute('DELETE FROM organization_maintenance WHERE job_id=?',(original,))

    def verify_file(self,path,device,expected,identity=None,check=None):
        path=Path(path)
        if no_links(path)!=path or self.jobs.device_identity(path)!=device:raise ValueError('撤销文件路径或设备已变化')
        before=file_identity(path)
        if identity is not None and before!=identity:raise ValueError('撤销文件已被替换')
        stamp=fingerprint(path,check)
        if before!=file_identity(path) or path.stat().st_mtime_ns!=stamp['mtime_ns']:raise ValueError('撤销校验期间文件已变化')
        if (stamp['size'],stamp['sha256'])!=(expected['size'],expected['sha256']):raise ValueError('撤销文件内容已变化')
        return {'path':str(path),'device':device,'fingerprint':stamp,'identity':before}

    def prepare(self,original):
        with self.exclusive(original):
            job=self.jobs.get(original)
            if any(e['operation']!='copy' for e in job['payload']['entries']):raise ValueError('此入口只撤销复制绑定')
            with self.jobs.db() as c:
                previous=c.execute('SELECT * FROM organization_inverses WHERE original_job_id=?',(original,)).fetchone()
                obsolete=previous['inverse_job_id'] if previous else None
                replaced_job=obsolete
                if previous and not obsolete:
                    prior=c.execute('SELECT payload FROM organization_drafts WHERE id=?',(previous['draft_id'],)).fetchone()
                    replaced_job=json.loads(prior[0]).get('replaces_job') if prior else None
                if obsolete:
                    old=self.jobs.get(obsolete)
                    if old['state']=='done':raise ValueError('复制绑定已撤销')
                    if old['state'] in ('running','queued') or (old['state']=='waiting' and not self.jobs.pause_requested(obsolete)):raise ValueError('请先暂停或取消旧撤销任务，再重新审阅')
            entries=[];retained=[]
            removed_paths={(e['target_device'],e['destination']) for e in job['payload']['entries']}
            for entry,step in zip(job['payload']['entries'],job['steps']):
                companion=entry.get('role')=='companion';table='media_resources' if companion else 'media_locations'
                asset_id=entry.get('parent_asset_id') if companion else entry.get('asset_id')
                with self.jobs.db() as c:
                    owner=c.execute('SELECT * FROM '+table+' WHERE path=? AND device_token=?',(entry['destination'],entry['target_device'])).fetchone()
                    if not owner:
                        retained.append({'source':entry['destination'],'operation':'retain','reason':'未绑定目标不在撤销范围；文件及暂存保持原状'})
                        continue
                    owner=dict(owner)
                    if owner['origin_task']!=original or owner['asset_id']!=asset_id:raise ValueError('目标绑定不再属于此任务，请核对后重新规划')
                    asset=c.execute('SELECT id,video_id,sha256 FROM media_assets WHERE id=?',(asset_id,)).fetchone()
                    if not asset or (not companion and asset['sha256']!=entry['fingerprint']['sha256']):raise ValueError('作品身份已变化')
                    asset=dict(asset)
                    video=c.execute('SELECT id,path FROM videos WHERE id=?',(asset['video_id'],)).fetchone()
                    if not video:raise ValueError('作品已移除，不能使用旧撤销计划')
                    video=dict(video)
                    if not companion and bool(owner['preferred'])!=(video['path']==owner['path']):raise ValueError('播放位置与绑定不一致，请先核对作品位置')
                    candidates=[dict(r) for r in c.execute('SELECT * FROM media_locations WHERE asset_id=? AND id!=? ORDER BY preferred DESC,id',(asset_id,owner['id'])) if (r['device_token'],r['path']) not in removed_paths] if not companion else []
                proof=self.verify_file(entry['destination'],entry['target_device'],entry['fingerprint'],step['details'].get('file_identity'))
                # A committed copy records target identity in details (not source identity).
                if not step['details'].get('file_identity'):raise ValueError('任务缺少已提交文件身份，请人工核对')
                replacement=None
                if not companion and owner['preferred']:
                    for candidate in sorted(candidates,key=lambda r:r['path']!=entry['source']):
                        if is_archive_path(candidate['path']):continue
                        try:verified=self.verify_file(candidate['path'],candidate['device_token'],entry['fingerprint'])
                        except (OSError,ValueError):continue
                        replacement={'row':candidate,'proof':verified};break
                    if not replacement:raise ValueError('请连接原盘或另一份有效副本后撤销默认播放位置')
                entries.append(dict(source=entry['destination'],destination=replacement['row']['path'] if replacement else video['path'],
                    operation='unbind_copy',source_device=entry['target_device'],target_device=replacement['row']['device_token'] if replacement else entry['target_device'],
                    role=entry.get('role','media'),video_id=asset['video_id'],asset_id=asset_id,fingerprint=entry['fingerprint'],
                    binding_table=table,binding=owner,asset=asset,video=video,proof=proof,replacement=replacement))
            if not entries:raise ValueError('没有属于此复制任务的绑定可撤销；目标文件保持原状')
            payload={'entries':entries,'original_job':original,'retained':retained,'rules_version':1,'files_retained':True,'replaces_job':replaced_job}
            stamp=digest(payload);draft_id=uuid.uuid4().hex
            with self.jobs.db() as c:
                c.execute('BEGIN IMMEDIATE')
                c.execute('INSERT OR IGNORE INTO organization_drafts VALUES(?,?,?,?)',(draft_id,stamp,json.dumps(payload),time.time()))
                draft_id=c.execute('SELECT id FROM organization_drafts WHERE digest=?',(stamp,)).fetchone()[0]
                c.execute('INSERT OR REPLACE INTO organization_inverses VALUES(?,?,NULL)',(original,draft_id))
                if obsolete:
                    c.execute('INSERT INTO organization_superseded VALUES(?,?,?)',(obsolete,draft_id,original))
                    c.execute("UPDATE organization_jobs SET state='cancelled',updated=? WHERE id=?",(time.time(),obsolete))
            if obsolete:self.jobs.observe_progress(obsolete,'cancelled',reason='superseded_approval')
            return {'id':draft_id,'digest':stamp,'payload':payload}

    def check_database(self,c,entry):
        for table,expected in ((entry['binding_table'],entry['binding']),('media_assets',entry['asset']),('videos',entry['video'])):
            columns=','.join(expected)
            current=c.execute('SELECT '+columns+' FROM '+table+' WHERE id=?',(expected['id'],)).fetchone()
            if not current or dict(current)!=expected:raise ValueError('绑定或播放位置已变化，请重新审阅撤销计划')
        if entry['replacement']:
            expected=entry['replacement']['row']
            current=c.execute('SELECT * FROM media_locations WHERE id=?',(expected['id'],)).fetchone()
            if not current or dict(current)!=expected:raise ValueError('撤销后的播放位置已变化')

    def validate(self,payload,check=None):
        for entry in payload['entries']:
            for proof in (entry['proof'],entry['replacement']['proof'] if entry['replacement'] else None):
                if proof and self.verify_file(proof['path'],proof['device'],proof['fingerprint'],proof['identity'],check)!=proof:raise ValueError('文件在撤销预览后发生变化')
            with self.jobs.db() as c:self.check_database(c,entry)

    def approve(self,original,expected_digest):
        with self.exclusive(original):
            with self.jobs.db() as c:
                inverse=c.execute('SELECT * FROM organization_inverses WHERE original_job_id=?',(original,)).fetchone()
                if not inverse:raise ValueError('请先预览撤销范围')
                draft=c.execute('SELECT * FROM organization_drafts WHERE id=?',(inverse['draft_id'],)).fetchone()
                if not draft or draft['digest']!=expected_digest:raise ValueError('撤销计划摘要不匹配')
                payload=json.loads(draft['payload'])
                if digest(payload)!=expected_digest:raise ValueError('撤销计划已被修改')
                if inverse['inverse_job_id']:return inverse['inverse_job_id']
            self.validate(payload)
            job_id=uuid.uuid4().hex;now=time.time()
            with self.jobs.db() as c:
                c.execute('BEGIN IMMEDIATE')
                for entry in payload['entries']:self.check_database(c,entry)
                c.execute('INSERT INTO organization_jobs VALUES(?,?,?,?,?,?)',(job_id,expected_digest,json.dumps(payload),'queued',now,now))
                for ordinal in range(len(payload['entries'])):c.execute('INSERT INTO organization_steps(job_id,ordinal,state,updated) VALUES(?,?,?,?)',(job_id,ordinal,'pending',now))
                c.execute('UPDATE organization_inverses SET inverse_job_id=? WHERE original_job_id=?',(job_id,original))
            self.jobs.observe_progress(job_id,'queued',phase='unbind_copy')
            return job_id

    def run(self,job_id,cancel=lambda:False):
        job=self.jobs.get(job_id);payload=job['payload']
        if job['state']=='done':return job
        if digest(payload)!=job['approval_digest'] or any(e['operation']!='unbind_copy' for e in payload['entries']):raise ValueError('Invalid copy undo approval')
        self.jobs.claim(job_id)
        def check():
            if cancel() or self.jobs.cancel_requested(job_id):raise CopyCancelled()
            if self.jobs.pause_requested(job_id):raise CopyPaused()
        try:
            check();self.validate(payload,check);check()
            self.checkpoint('verified',job_id,None)
            # All metadata and durable completion advance together; no file is mutated.
            with self.jobs.db() as c:
                c.execute('BEGIN IMMEDIATE')
                for entry in payload['entries']:
                    self.check_database(c,entry)
                    for proof in (entry['proof'],entry['replacement']['proof'] if entry['replacement'] else None):
                        if proof:
                            path=Path(proof['path']);stat=path.stat()
                            if no_links(path)!=path or file_identity(path)!=proof['identity'] or (stat.st_size,stat.st_mtime_ns)!=(proof['fingerprint']['size'],proof['fingerprint']['mtime_ns']) or self.jobs.device_identity(path)!=proof['device']:raise ValueError('撤销提交前文件发生变化')
                for ordinal,entry in enumerate(payload['entries']):
                    replacement=entry['replacement'];binding=entry['binding']
                    c.execute('DELETE FROM '+entry['binding_table']+' WHERE id=?',(binding['id'],))
                    if replacement:
                        row=replacement['row'];stamp=replacement['proof']['fingerprint']
                        c.execute('UPDATE media_locations SET preferred=1,size=?,mtime_ns=?,verified=? WHERE id=?',(stamp['size'],stamp['mtime_ns'],time.time(),row['id']))
                        self.locations.update_video_location(c,entry['video_id'],row['path'],stamp)
                    if entry['binding_table']=='media_resources':
                        c.execute('INSERT INTO media_resource_events VALUES(?,?,?,?)',(binding['id'],job_id,'copy_unbound',time.time()))
                    else:c.execute('INSERT INTO media_location_events(asset_id,location_id,kind,task_id,created) VALUES(?,?,?,?,?)',(entry['asset_id'],binding['id'],'copy_unbound',job_id,time.time()))
                    c.execute("UPDATE organization_steps SET state='done',details=?,updated=? WHERE job_id=? AND ordinal=?",(json.dumps({'files_retained':True,'binding_id':binding['id']}),time.time(),job_id,ordinal))
                    c.execute("INSERT INTO organization_step_events(job_id,ordinal,state,created) VALUES(?,?,'done',?)",(job_id,ordinal,time.time()))
                self.checkpoint('before_commit',job_id,None)
                c.execute("UPDATE organization_jobs SET state='done',updated=? WHERE id=?",(time.time(),job_id))
                c.execute('DELETE FROM organization_leases WHERE job_id=?',(job_id,))
                c.execute('DELETE FROM resource_claims WHERE owner=?',('organize:'+job_id,))
                self.jobs.refresh_reconciliation(c)
            self.checkpoint('committed',job_id,None)
            self.jobs.observe_progress(job_id,'done',phase='unbind_copy')
        except CopyPaused:self.jobs.finish(job_id,'waiting')
        except CopyCancelled:self.jobs.finish(job_id,'cancelled')
        except Exception as error:
            if self.jobs.get(job_id)['state']!='done':
                with self.jobs.db() as c:c.execute('UPDATE organization_steps SET details=? WHERE job_id=?',(json.dumps({'error':str(error),'files_retained':True}),job_id))
                self.jobs.finish(job_id,'failed')
            self.jobs.observe_progress(job_id,self.jobs.get(job_id)['state'],reason=type(error).__name__)
        return self.jobs.get(job_id)
