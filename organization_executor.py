"""Streaming copy executor for explicitly approved organization jobs."""
import ctypes
import hashlib
import os
import shutil
import time
import uuid
from pathlib import Path
from media_locations import fingerprint
from organization_jobs import digest, no_links


class CopyCancelled(Exception):pass


def publish_no_replace(temporary,target):
    if os.name=='nt':
        from ctypes import wintypes
        move=ctypes.windll.kernel32.MoveFileW
        move.argtypes=[wintypes.LPCWSTR,wintypes.LPCWSTR];move.restype=wintypes.BOOL
        if not move(str(temporary),str(target)):raise ctypes.WinError(ctypes.windll.kernel32.GetLastError())
    else:
        # Both paths share a parent filesystem; link is atomic and rejects existing names.
        os.link(temporary,target);temporary.unlink()
        descriptor=os.open(target.parent,os.O_RDONLY)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)


def file_identity(path):
    stat=Path(path).stat()
    if not stat.st_ino:raise ValueError('Stable file identity unavailable on this filesystem')
    return [stat.st_dev,stat.st_ino]


class CopyExecutor:
    def __init__(self,jobs,binder,checkpoint=lambda *args:None):
        self.jobs,self.binder,self.checkpoint=jobs,binder,checkpoint

    def save_details(self,job_id,ordinal,state,details):
        import json
        with self.jobs.db() as c:
            changed=c.execute('UPDATE organization_steps SET details=?,updated=? WHERE job_id=? AND ordinal=? AND state=?',
                              (json.dumps(details),time.time(),job_id,ordinal,state)).rowcount
            if changed!=1:raise ValueError('Execution checkpoint changed')
        self.jobs.journal.observe('organize',job_id,'running',{'phase':state,'bytes_processed':details.get('bytes',0),'reason':details.get('error','executor_checkpoint'),'cancellable':True})

    def check_paths(self,entry):
        source=no_links(entry['source']);target=no_links(entry['destination'])
        if str(source)!=entry['source'] or str(target)!=entry['destination']:raise ValueError('Approved paths changed')
        if self.jobs.device_identity(source)!=entry['source_device'] or self.jobs.device_identity(target.parent)!=entry['target_device']:raise ValueError('Device identity changed')
        return source,target

    def verify_target(self,entry,details):
        _,target=self.check_paths(entry)
        if not details.get('file_identity') or file_identity(target)!=details['file_identity']:raise ValueError('Target ownership requires review')
        stamp=fingerprint(target)
        if (stamp['size'],stamp['sha256'])!=(entry['fingerprint']['size'],entry['fingerprint']['sha256']):raise ValueError('Committed target changed')

    def copy(self,job_id,ordinal,entry,details,cancel):
        def check():
            if cancel():raise CopyCancelled()
        source,target=self.check_paths(entry);check()
        if fingerprint(source,check)!=entry['fingerprint']:raise ValueError('Source changed after approval')
        if target.exists():raise ValueError('Target appeared after approval')
        parent=target.parent
        while not parent.exists() and parent!=parent.parent:parent=parent.parent
        reserve=max(100*1024*1024,int(entry['fingerprint']['size']*.05))
        if shutil.disk_usage(parent).free<entry['fingerprint']['size']+reserve:raise OSError('Insufficient target space')
        temporary=target.with_name('.velvet-'+job_id+'-'+uuid.uuid4().hex+'.partial')
        previous=details.get('previous_temporaries',[])+([details['temporary']] if details.get('temporary') else [])
        details={'temporary':str(temporary),'bytes':0,'previous_temporaries':previous}
        self.jobs.transition(job_id,ordinal,'pending','copying',details)
        target.parent.mkdir(parents=True,exist_ok=True)
        self.check_paths(entry)
        digest_copy=hashlib.sha256()
        with temporary.open('xb') as output:
            info=os.fstat(output.fileno())
            if not info.st_ino:raise ValueError('Stable temporary identity unavailable')
            details['file_identity']=[info.st_dev,info.st_ino]
            self.save_details(job_id,ordinal,'copying',details)
            with source.open('rb') as input_file:
                while True:
                    check();block=input_file.read(4*1024*1024)
                    if not block:break
                    output.write(block);digest_copy.update(block);details['bytes']+=len(block)
                    self.save_details(job_id,ordinal,'copying',details)
                    self.checkpoint('copying',job_id,ordinal)
            output.flush();os.fsync(output.fileno())
        check()
        if digest_copy.hexdigest()!=entry['fingerprint']['sha256']:raise ValueError('Copied bytes differ from approved identity')
        if fingerprint(source,check)!=entry['fingerprint']:raise ValueError('Source changed during copy')
        verified=fingerprint(temporary,check)
        if (verified['size'],verified['sha256'])!=(entry['fingerprint']['size'],entry['fingerprint']['sha256']):raise ValueError('Temporary copy verification failed')
        os.utime(temporary,ns=(source.stat().st_atime_ns,entry['fingerprint']['mtime_ns']))
        self.jobs.transition(job_id,ordinal,'copying','verified',details)
        self.checkpoint('verified',job_id,ordinal)
        return details

    def execute_step(self,job_id,step,entry,cancel):
        ordinal=step['ordinal'];state=step['state'];details=step['details']
        if entry['operation']!='copy':raise ValueError('Unsupported operation')
        if state=='done':return
        if state in ('copying','interrupted'):
            if state=='copying':self.jobs.transition(job_id,ordinal,'copying','interrupted',details)
            self.jobs.transition(job_id,ordinal,'interrupted','pending',details);state='pending'
        if state=='pending':details=self.copy(job_id,ordinal,entry,details,cancel);state='verified'
        if state=='verified':
            source,_=self.check_paths(entry)
            if fingerprint(source)!=entry['fingerprint']:raise ValueError('Source changed before commit')
            if cancel():raise CopyCancelled()
            temporary=Path(details['temporary'])
            if file_identity(temporary)!=details['file_identity']:raise ValueError('Temporary ownership changed')
            stamp=fingerprint(temporary)
            if (stamp['size'],stamp['sha256'])!=(entry['fingerprint']['size'],entry['fingerprint']['sha256']):raise ValueError('Verified temporary changed')
            self.jobs.transition(job_id,ordinal,'verified','committing',details);state='committing'
            self.checkpoint('committing',job_id,ordinal)
        if state=='committing':
            _,target=self.check_paths(entry);temporary=Path(details['temporary'])
            if not target.exists():
                if fingerprint(entry['source'])!=entry['fingerprint']:raise ValueError('Source changed before commit')
                if file_identity(temporary)!=details['file_identity']:raise ValueError('Temporary ownership changed')
                stamp=fingerprint(temporary)
                if stamp['sha256']!=entry['fingerprint']['sha256'] or stamp['size']!=entry['fingerprint']['size']:raise ValueError('Temporary changed before commit')
                publish_no_replace(temporary,target)
            self.verify_target(entry,details)
            self.jobs.transition(job_id,ordinal,'committing','committed',details);state='committed'
            self.checkpoint('committed',job_id,ordinal)
        if state in ('committed','needs_binding','binding'):
            self.verify_target(entry,details)
            if state!='binding':self.jobs.transition(job_id,ordinal,state,'binding',details)
            try:
                if not self.binder:raise ValueError('Library binder not configured')
                binding=self.binder(entry,job_id)
                if not binding:raise ValueError('Library binding not confirmed')
            except Exception as error:
                details['error']=type(error).__name__
                self.jobs.transition(job_id,ordinal,'binding','needs_binding',details)
                raise
            details['binding_id']=binding;details.pop('error',None)
            self.jobs.transition(job_id,ordinal,'binding','done',details)
            self.checkpoint('done',job_id,ordinal)

    def run(self,job_id,cancel=lambda:False):
        job=self.jobs.get(job_id)
        if job['state']=='done':return job
        if digest(job['payload'])!=job['approval_digest']:raise ValueError('Approved manifest changed')
        self.jobs.claim(job_id)
        external_cancel=cancel
        cancel=lambda:external_cancel() or self.jobs.cancel_requested(job_id)
        cancelled=False
        for step in job['steps']:
            if step['state']=='done':continue
            if cancel():cancelled=True;break
            try:self.execute_step(job_id,step,job['payload']['entries'][step['ordinal']],cancel)
            except CopyCancelled:cancelled=True;break
            except Exception as error:
                current=self.jobs.get(job_id)['steps'][step['ordinal']]
                details=dict(current['details'],error=type(error).__name__)
                self.save_details(job_id,step['ordinal'],current['state'],details)
        states=[step['state'] for step in self.jobs.get(job_id)['steps']]
        outcome='done' if all(state=='done' for state in states) else 'cancelled' if cancelled else 'partial' if any(state in ('committed','binding','needs_binding','done') for state in states) else 'failed'
        self.jobs.finish(job_id,outcome)
        return self.jobs.get(job_id)
