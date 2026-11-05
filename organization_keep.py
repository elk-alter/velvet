"""Verify and associate files at their current locations; never write media files."""
import json
import time
from organization_jobs import digest,no_links
from organization_executor import CopyCancelled,CopyPaused,file_identity
from media_locations import fingerprint


class KeepExecutor:
    def __init__(self,jobs,binder,checkpoint=lambda *args:None):
        self.jobs,self.binder,self.checkpoint=jobs,binder,checkpoint

    def verify(self,entry,check):
        path=no_links(entry['source'])
        if str(path)!=entry['source'] or entry['source']!=entry['destination']:raise ValueError('原位保留路径已变化')
        if self.jobs.device_identity(path)!=entry['source_device'] or entry['target_device']!=entry['source_device']:raise ValueError('原位文件设备已变化')
        if file_identity(path)!=entry['source_identity'] or fingerprint(path,check)!=entry['fingerprint'] or file_identity(path)!=entry['source_identity']:raise ValueError('原位文件已变化')

    def run(self,job_id,cancel=lambda:False):
        job=self.jobs.get(job_id)
        if job['state']=='done':return job
        if digest(job['payload'])!=job['approval_digest'] or any(e['operation']!='keep' for e in job['payload']['entries']):raise ValueError('Invalid keep approval')
        self.jobs.claim(job_id);job=self.jobs.get(job_id);current=None
        def check():
            if cancel() or self.jobs.cancel_requested(job_id):raise CopyCancelled()
            if self.jobs.pause_requested(job_id):raise CopyPaused()
        try:
            for ordinal,entry in enumerate(job['payload']['entries']):
                check();current=ordinal;step=self.jobs.get(job_id)['steps'][ordinal]
                if step['state']=='done' or step['details'].get('skip'):continue
                self.verify(entry,check)
                if step['state'] in ('pending','needs_binding'):
                    self.jobs.transition(job_id,ordinal,step['state'],'binding',{'files_unchanged':True})
                elif step['state']!='binding':raise ValueError('原位关联步骤需要核对')
                self.checkpoint('binding',job_id,ordinal)
                binding=self.binder(entry,job_id)
                if not binding:raise ValueError('原位关联未完成')
                self.verify(entry,check);self.checkpoint('bound',job_id,ordinal)
                self.jobs.transition(job_id,ordinal,'binding','done',{'files_unchanged':True,'binding_id':binding})
            self.jobs.finish(job_id,'cancelled' if any(s['details'].get('skip') for s in job['steps']) else 'done')
        except CopyPaused:self.jobs.finish(job_id,'waiting')
        except CopyCancelled:self.jobs.finish(job_id,'cancelled')
        except Exception as error:
            if current is not None:
                step=self.jobs.get(job_id)['steps'][current]
                details=dict(step['details'],error=str(error),files_unchanged=True)
                if step['state']=='binding':self.jobs.transition(job_id,current,'binding','needs_binding',details)
                else:
                    with self.jobs.db() as c:c.execute('UPDATE organization_steps SET details=?,updated=? WHERE job_id=? AND ordinal=? AND state=?',(json.dumps(details),time.time(),job_id,current,step['state']))
            done=any(s['state']=='done' for s in self.jobs.get(job_id)['steps'])
            self.jobs.finish(job_id,'partial' if done else 'failed')
            self.jobs.observe_progress(job_id,'partial' if done else 'failed',reason=type(error).__name__)
        return self.jobs.get(job_id)
