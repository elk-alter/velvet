"""Explicit, journaled cleanup of proven copy fragments, never move staging."""
import json
import os
import hashlib
import time
from contextlib import contextmanager
from pathlib import Path
from media_locations import fingerprint
from organization_executor import file_identity
from organization_jobs import digest,no_links


def remove_verified_fragment(path,record,stamp):
    """Windows handle ownership prevents a final check/unlink replacement race."""
    if os.name!='nt':raise OSError('Safe fragment removal is not validated on this platform')
    import ctypes
    import msvcrt
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    create=kernel.CreateFileW
    create.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    create.restype=wintypes.HANDLE
    # DELETE + GENERIC_READ, share read only; refuse concurrent writers/deleters.
    handle=create(str(path),0x80010000,1,None,3,0x00200000,None)
    if handle==wintypes.HANDLE(-1).value:raise ctypes.WinError(ctypes.get_last_error())
    try:descriptor=msvcrt.open_osfhandle(handle,os.O_RDONLY|os.O_BINARY)
    except BaseException:
        close=kernel.CloseHandle;close.argtypes=[wintypes.HANDLE];close(handle);raise
    with os.fdopen(descriptor,'rb') as source:
        info=os.fstat(source.fileno())
        if [info.st_dev,info.st_ino]!=record['file_identity'] or getattr(info,'st_file_attributes',0)&1024:raise ValueError('Fragment handle ownership changed')
        actual=hashlib.sha256()
        for block in iter(lambda:source.read(4*1024*1024),b''):actual.update(block)
        if (info.st_size,info.st_mtime_ns,actual.hexdigest())!=(stamp['size'],stamp['mtime_ns'],stamp['sha256']):raise ValueError('Fragment changed before removal')
        disposition=ctypes.c_byte(1)
        remove=kernel.SetFileInformationByHandle
        remove.argtypes=[wintypes.HANDLE,ctypes.c_int,wintypes.LPVOID,wintypes.DWORD];remove.restype=wintypes.BOOL
        if not remove(handle,4,ctypes.byref(disposition),1):raise ctypes.WinError(ctypes.get_last_error())


class CopyCleanup:
    def __init__(self,jobs):
        self.jobs=jobs
        with jobs.db() as c:
            c.execute('CREATE TABLE IF NOT EXISTS organization_cleanups(job_id TEXT,path TEXT,identity TEXT,state TEXT,updated REAL,PRIMARY KEY(job_id,path))')

    @contextmanager
    def exclusive(self,job_id):
        owner='cleanup:'+job_id
        with self.jobs.db() as c:
            c.execute('BEGIN IMMEDIATE')
            job=c.execute('SELECT * FROM organization_jobs WHERE id=?',(job_id,)).fetchone()
            if not job or job['state'] in ('running','queued'):raise ValueError('Stop the task before cleaning fragments')
            resources=sorted({e['target_device'] for e in json.loads(job['payload'])['entries']})
            c.execute('INSERT INTO organization_maintenance VALUES(?)',(job_id,))
            for resource in resources:
                if c.execute('SELECT 1 FROM organization_leases WHERE resource=?',(resource,)).fetchone():raise ValueError('Device in use')
                c.execute('INSERT INTO organization_leases VALUES(?,?)',(resource,owner))
        try:yield
        finally:
            with self.jobs.db() as c:
                c.execute('DELETE FROM organization_leases WHERE job_id=?',(owner,))
                c.execute('DELETE FROM organization_maintenance WHERE job_id=?',(job_id,))

    def verify(self,job_id,entry,record):
        raw=record.get('path')
        if not raw or not record.get('file_identity') or record.get('bytes') is None or not record.get('sha256'):
            raise ValueError('Fragment ownership evidence is incomplete')
        path=Path(raw);target=Path(entry['destination'])
        if path.parent!=target.parent or not path.name.startswith('.velvet-'+job_id+'-') or not path.name.endswith('.partial'):
            raise ValueError('Fragment is outside the approved staging scope')
        if no_links(path)!=path or self.jobs.device_identity(path.parent)!=entry['target_device']:raise ValueError('Fragment device or path changed')
        if file_identity(path)!=record['file_identity']:raise ValueError('Fragment ownership changed')
        stamp=fingerprint(path)
        if stamp['size']!=record['bytes'] or stamp['sha256']!=record['sha256']:raise ValueError('Fragment content changed')
        return stamp

    def preview(self,job_id):
        job=self.jobs.get(job_id)
        if job['state'] in ('running','queued'):raise ValueError('Stop the task before previewing cleanup')
        rows=[]
        for entry,step in zip(job['payload']['entries'],job['steps']):
            if entry['operation']!='copy':continue
            details=step['details'];records=list(details.get('previous_temporaries',[]))
            current=None
            if step['state'] in ('copying','interrupted') and details.get('temporary'):
                current=details['temporary']
                records.append({'path':current,'file_identity':details.get('file_identity'),'bytes':details.get('bytes'),'sha256':details.get('partial_sha256')})
            for record in records:
                if isinstance(record,str):record={'path':record}
                row={'ordinal':step['ordinal'],'current':record.get('path')==current,'record':record,'status':'review'}
                try:
                    row['fingerprint']=self.verify(job_id,entry,record);row['status']='ready'
                except FileNotFoundError:row['status']='absent'
                except (OSError,ValueError):pass
                rows.append(row)
        payload={'job_id':job_id,'entries':rows}
        return dict(payload,digest=digest(payload),bytes=sum(r['record']['bytes'] for r in rows if r['status']=='ready'))

    def clean(self,job_id,expected_digest):
        removed=0
        with self.exclusive(job_id):
            plan=self.preview(job_id)
            payload={key:plan[key] for key in ('job_id','entries')}
            if digest(payload)!=expected_digest:raise ValueError('Cleanup preview changed; review again')
            job=self.jobs.get(job_id)
            for row in plan['entries']:
                if row['status']!='ready':continue
                entry=job['payload']['entries'][row['ordinal']];record=row['record'];path=Path(record['path'])
                stamp=self.verify(job_id,entry,record)
                if stamp!=row['fingerprint']:raise ValueError('Fragment changed since cleanup preview')
                with self.jobs.db() as c:
                    c.execute('INSERT OR REPLACE INTO organization_cleanups VALUES(?,?,?,?,?)',(job_id,str(path),json.dumps(record['file_identity']),'removing',time.time()))
                # Recheck after writing the intent. Never follow a replacement link.
                if self.verify(job_id,entry,record)!=stamp:raise ValueError('Fragment changed before removal')
                remove_verified_fragment(path,record,stamp);removed+=1
                with self.jobs.db() as c:
                    c.execute("UPDATE organization_cleanups SET state='removed',updated=? WHERE job_id=? AND path=?",(time.time(),job_id,str(path)))
                    if row['current']:
                        preserved={'previous_temporaries':job['steps'][row['ordinal']]['details'].get('previous_temporaries',[])}
                        c.execute("UPDATE organization_steps SET state='pending',details=?,updated=? WHERE job_id=? AND ordinal=? AND state IN ('copying','interrupted')",(json.dumps(preserved),time.time(),job_id,row['ordinal']))
        return {'removed':removed,'review':sum(row['status']=='review' for row in plan['entries'])}
