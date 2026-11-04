"""Explicit, reversible same-volume source archival after verified copy binding."""
import json
import os
import time
from pathlib import Path
from archive_policy import ARCHIVE_NAME,is_archive_path
from organization_jobs import no_links,digest
from organization_executor import file_identity
from media_locations import fingerprint
from storage_inventory import volume_details


class SourceArchive:
    def __init__(self,jobs):
        self.jobs=jobs
        with jobs.db() as c:
            c.execute('CREATE TABLE IF NOT EXISTS organization_archive_roots(path TEXT PRIMARY KEY,device_token TEXT,created REAL)')
            c.execute('CREATE TABLE IF NOT EXISTS organization_archive_plans(draft_id TEXT PRIMARY KEY,original_job_id TEXT,archive_job_id TEXT,root TEXT,device_token TEXT)')

    def capability(self,source_root):
        source_root=no_links(source_root)
        volume=volume_details(source_root)
        if os.name!='nt' or volume['filesystem'].upper() not in ('NTFS','REFS') or volume['read_only'] is not False:
            raise ValueError('此设备未确认支持可靠的同盘可恢复归档；请保留源文件。当前归档支持可写Windows NTFS/ReFS卷')
        token=self.jobs.device_identity(source_root)
        if not token.startswith('windows-volume:'):raise ValueError('源设备缺少稳定卷身份，不能归档')
        root=no_links(source_root/ARCHIVE_NAME)
        if self.jobs.device_identity(root)!=token:raise ValueError('归档位置不在原设备')
        with self.jobs.db() as c:record=c.execute('SELECT device_token FROM organization_archive_roots WHERE path=?',(str(root),)).fetchone()
        if record and record[0]!=token:raise ValueError('归档根设备身份发生变化')
        if root.exists() and not record:raise ValueError('归档目录已存在但无应用归属记录，请使用其它源范围并人工核对')
        return root,token

    def validate_guard(self,entry,check=None):
        """Never archive an only usable copy or adopt an external same-byte target."""
        guard=entry.get('archive_guard')
        if not isinstance(guard,dict):raise ValueError('缺少归档前目标副本证明')
        path=Path(guard['path'])
        if no_links(path)!=path or self.jobs.device_identity(path)!=guard['device']:raise ValueError('目标副本设备或路径发生变化')
        if file_identity(path)!=guard['identity'] or fingerprint(path,check)!=guard['fingerprint'] or file_identity(path)!=guard['identity']:raise ValueError('目标副本变化，停止源归档')
        with self.jobs.db() as c:
            table=guard['table']
            if table not in ('media_locations','media_resources'):raise ValueError('无效归档证明')
            binding=c.execute('SELECT id,asset_id,path,device_token,origin_task FROM '+table+' WHERE id=?',(guard['binding']['id'],)).fetchone()
            asset=c.execute('SELECT id,video_id,sha256 FROM media_assets WHERE id=?',(guard['asset']['id'],)).fetchone()
            video=c.execute('SELECT id,path FROM videos WHERE id=?',(guard['video']['id'],)).fetchone()
        if not binding or dict(binding)!=guard['binding'] or not asset or dict(asset)!=guard['asset'] or not video or dict(video)!=guard['video']:raise ValueError('目标绑定、作品身份或默认播放位置已变化，请重新审阅')
        self.capability(guard['source_root'])

    def prepare(self,original):
        with self.jobs.leases.hold('source-archive-preview'):
            job=self.jobs.get(original)
            if job['state']!='done' or any(e['operation']!='copy' for e in job['payload']['entries']):raise ValueError('请先完成复制、校验和片库绑定，再单独归档源文件')
            source_root=no_links(job['payload']['source_root']);root,token=self.capability(source_root)
            original_sources={e['source'] for e in job['payload']['entries']}
            with self.jobs.db() as c:
                restored=c.execute("SELECT i.inverse_job_id FROM organization_archive_plans p JOIN organization_inverses i ON i.original_job_id=p.archive_job_id JOIN organization_jobs j ON j.id=i.inverse_job_id WHERE p.original_job_id=? AND j.state='done' ORDER BY j.created DESC LIMIT 1",(original,)).fetchone()
            entries=[]
            for old,step in zip(job['payload']['entries'],job['steps']):
                if step['state']!='done':raise ValueError('复制步骤尚未完成')
                companion=old.get('role')=='companion';asset_id=old.get('parent_asset_id') if companion else old.get('asset_id')
                table='media_resources' if companion else 'media_locations'
                with self.jobs.db() as c:
                    owner=c.execute('SELECT id,asset_id,path,device_token,origin_task FROM '+table+' WHERE path=? AND device_token=?',(old['destination'],old['target_device'])).fetchone()
                    asset=c.execute('SELECT id,video_id,sha256 FROM media_assets WHERE id=?',(asset_id,)).fetchone()
                    video=c.execute('SELECT id,path FROM videos WHERE id=?',(asset['video_id'],)).fetchone() if asset else None
                if not owner or owner['asset_id']!=asset_id or owner['origin_task']!=original or not asset or not video:raise ValueError('目标副本未保持本次复制绑定，不能归档源文件')
                if video['path'] in original_sources or is_archive_path(video['path']):raise ValueError('请先将默认播放位置切换到保留的有效副本，再归档源文件')
                target=Path(old['destination']);stamp=fingerprint(target)
                if (stamp['size'],stamp['sha256'])!=(old['fingerprint']['size'],old['fingerprint']['sha256']):raise ValueError('目标副本内容已变化')
                if not step['details'].get('file_identity') or file_identity(target)!=step['details']['file_identity']:raise ValueError('目标副本已被替换')
                guard={'source_root':str(source_root),'path':str(target),'device':old['target_device'],'identity':step['details']['file_identity'],'fingerprint':stamp,'binding':dict(owner),'asset':dict(asset),'video':dict(video),'table':table,'copy_job':original}
                guard['after_restore']=restored[0] if restored else None
                source=no_links(old['source'])
                if is_archive_path(source) or not source.is_relative_to(source_root):raise ValueError('原位置不属于源范围')
                entry={key:old.get(key) for key in ('video_id','asset_id','metadata','role','parent_asset_id')}
                entry.update(source=str(source),destination=str(root/original/source.relative_to(source_root)),operation='archive',archive_guard=guard)
                self.validate_guard(entry)
                entries.append(entry)
            draft=self.jobs.prepare(entries,source_root,root,retained=job['payload'].get('retained',[]))
            with self.jobs.db() as c:c.execute('INSERT OR IGNORE INTO organization_archive_plans VALUES(?,?,NULL,?,?)',(draft['id'],original,str(root),token))
            return draft

    def approve(self,original,draft_id,expected_digest):
        with self.jobs.leases.hold('source-archive-approval','write'):
            with self.jobs.db() as c:
                row=c.execute('SELECT * FROM organization_archive_plans WHERE draft_id=? AND original_job_id=?',(draft_id,original)).fetchone()
                draft=c.execute('SELECT * FROM organization_drafts WHERE id=?',(draft_id,)).fetchone()
            if not row or not draft or draft['digest']!=expected_digest:raise ValueError('归档计划摘要或原任务不匹配')
            payload=json.loads(draft['payload'])
            if digest(payload)!=expected_digest:raise ValueError('归档计划内容发生变化')
            if row['archive_job_id']:return row['archive_job_id']
            original_job=self.jobs.get(original)
            if original_job['state']!='done':raise ValueError('原复制任务状态已变化')
            for entry in payload['entries']:self.validate_guard(entry)
            # Reserve the mandatory exclusion before any media mutation can run.
            with self.jobs.db() as c:c.execute('INSERT OR IGNORE INTO organization_archive_roots VALUES(?,?,?)',(row['root'],row['device_token'],time.time()))
            job_id=self.jobs.approve(draft_id,expected_digest)
            with self.jobs.db() as c:c.execute('UPDATE organization_archive_plans SET archive_job_id=? WHERE draft_id=?',(job_id,draft_id))
            return job_id
