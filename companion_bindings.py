"""Verified companion associations, separate from video/part identities."""
import time
import uuid
from pathlib import Path
from media_locations import fingerprint,binding_key
from organization_jobs import no_links


class CompanionBindings:
    def __init__(self,db,device_identity):
        self.db,self.device_identity=db,device_identity
        with db() as c:
            c.execute('CREATE TABLE IF NOT EXISTS media_resources(id TEXT PRIMARY KEY,asset_id TEXT,device_token TEXT,path TEXT,size INTEGER,mtime_ns INTEGER,sha256 TEXT,origin_task TEXT,UNIQUE(device_token,path))')
            c.execute('CREATE TABLE IF NOT EXISTS media_resource_moves(task_id TEXT,source_path TEXT,resource_id TEXT,PRIMARY KEY(task_id,source_path))')
            c.execute('CREATE TABLE IF NOT EXISTS media_resource_events(resource_id TEXT,task_id TEXT,kind TEXT,created REAL,UNIQUE(resource_id,task_id,kind))')

    def verify(self,entry):
        path=Path(entry['destination']);identity=entry.get('verified_identity') if entry['operation']=='copy' else entry.get('source_identity')
        if no_links(path)!=path or self.device_identity(path)!=entry['target_device']:raise ValueError('Companion target path or device changed')
        before=path.stat()
        if not identity or [before.st_dev,before.st_ino]!=identity:raise ValueError('Companion ownership changed')
        stamp=fingerprint(path);after=path.stat()
        if [after.st_dev,after.st_ino]!=identity or after.st_mtime_ns!=stamp['mtime_ns']:raise ValueError('Companion changed during verification')
        if (stamp['sha256'],stamp['size'])!=(entry['fingerprint']['sha256'],entry['fingerprint']['size']):raise ValueError('Companion differs from approved bytes')
        if not entry.get('parent_asset_id'):raise ValueError('Companion owner missing')
        return stamp

    def bind_copy(self,entry,task_id):
        stamp=self.verify(entry)
        with self.db() as c:
            if not c.execute('SELECT 1 FROM media_assets WHERE id=?',(entry['parent_asset_id'],)).fetchone():raise ValueError('Companion parent identity missing')
            owner=c.execute('SELECT * FROM media_resources WHERE device_token=? AND path=?',(entry['target_device'],entry['destination'])).fetchone()
            if owner and owner['asset_id']!=entry['parent_asset_id']:raise ValueError('Resource already belongs to another video')
            resource_id=owner['id'] if owner else uuid.uuid4().hex
            c.execute('INSERT INTO media_resources VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(device_token,path) DO UPDATE SET size=excluded.size,mtime_ns=excluded.mtime_ns,sha256=excluded.sha256',
                      (resource_id,entry['parent_asset_id'],entry['target_device'],entry['destination'],stamp['size'],stamp['mtime_ns'],stamp['sha256'],task_id))
            c.execute('INSERT OR IGNORE INTO media_resource_events VALUES(?,?,?,?)',(resource_id,task_id,'copy_bound',time.time()))
        return resource_id

    def prepare_moves(self,entries):
        return [(entry,self.verify(entry)) for entry in entries]

    def apply_moves(self,c,checked,task_id):
        updates=[];seen=set()
        for entry,stamp in checked:
            parent=entry['parent_asset_id']
            if not c.execute('SELECT 1 FROM media_assets WHERE id=?',(parent,)).fetchone():raise ValueError('Companion parent missing')
            previous=c.execute('SELECT resource_id FROM media_resource_moves WHERE task_id=? AND source_path=?',(task_id,entry['source'])).fetchone()
            if previous:
                owner=c.execute('SELECT * FROM media_resources WHERE id=?',(previous['resource_id'],)).fetchone()
                if not owner or owner['path']!=entry['destination']:raise ValueError('Resource moved after binding')
            else:owner=c.execute('SELECT * FROM media_resources WHERE device_token=? AND path=?',(entry['source_device'],entry['source'])).fetchone()
            if owner and owner['asset_id']!=parent:raise ValueError('Resource parent conflict')
            resource_id=owner['id'] if owner else uuid.uuid4().hex
            if resource_id in seen:raise ValueError('Resource appears twice')
            seen.add(resource_id);updates.append((entry,stamp,resource_id,bool(owner)))
        for entry,stamp,resource_id,existed in updates:
            if existed:c.execute('UPDATE media_resources SET path=? WHERE id=?',('velvet-resource-db:'+task_id+':'+resource_id,resource_id))
        for entry,stamp,resource_id,existed in updates:
            values=(entry['parent_asset_id'],entry['target_device'],entry['destination'],stamp['size'],stamp['mtime_ns'],stamp['sha256'],task_id)
            if existed:c.execute('UPDATE media_resources SET asset_id=?,device_token=?,path=?,size=?,mtime_ns=?,sha256=?,origin_task=? WHERE id=?',(*values,resource_id))
            else:c.execute('INSERT INTO media_resources VALUES(?,?,?,?,?,?,?,?)',(resource_id,*values))
            c.execute('INSERT OR IGNORE INTO media_resource_moves VALUES(?,?,?)',(task_id,entry['source'],resource_id))
            c.execute('INSERT OR IGNORE INTO media_resource_events VALUES(?,?,?,?)',(resource_id,task_id,'move_bound',time.time()))
        return {binding_key(entry):resource_id for entry,stamp,resource_id,existed in updates}
