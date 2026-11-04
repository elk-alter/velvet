"""Stable file identities and verified physical locations (application data only)."""
import hashlib
import time
import uuid
from pathlib import Path
from storage_inventory import volume_identity
from archive_policy import archive_roots,is_archive_path


def fingerprint(raw,check=None):
    path=Path(raw)
    if path.is_symlink():raise ValueError('Linked media must be reviewed before binding')
    before=path.stat();digest=hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda:source.read(4*1024*1024),b''):
            if check:check()
            digest.update(block)
    after=path.stat()
    if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise ValueError('Source changed during verification')
    return {'size':after.st_size,'mtime_ns':after.st_mtime_ns,'sha256':digest.hexdigest()}


def binding_key(entry):
    return entry['video_id'] if entry.get('video_id') is not None else entry['source']


class MediaLocations:
    def __init__(self,db,device_identity=None):
        self.db=db
        self.device_identity=device_identity or (lambda path:volume_identity(Path(path).parent)[0])
        self.ensure()

    def ensure(self):
        with self.db() as c:
            c.execute("CREATE TABLE IF NOT EXISTS media_assets(id TEXT PRIMARY KEY,video_id INTEGER UNIQUE,sha256 TEXT DEFAULT '',created REAL NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS media_locations(id TEXT PRIMARY KEY,asset_id TEXT NOT NULL,device_token TEXT NOT NULL,path TEXT NOT NULL,size INTEGER,mtime_ns INTEGER,verified REAL DEFAULT 0,preferred INTEGER DEFAULT 0,origin_task TEXT DEFAULT '',UNIQUE(device_token,path))")
            columns={r[1]:r for r in c.execute('PRAGMA table_info(media_assets)')}
            if 'relation_path' not in columns:c.execute("ALTER TABLE media_assets ADD COLUMN relation_path TEXT DEFAULT ''")
            if columns['video_id'][3]:
                c.execute('DROP TRIGGER IF EXISTS media_asset_detach')
                c.execute('ALTER TABLE media_assets RENAME TO media_assets_legacy')
                c.execute("CREATE TABLE media_assets(id TEXT PRIMARY KEY,video_id INTEGER UNIQUE,sha256 TEXT DEFAULT '',created REAL NOT NULL,relation_path TEXT DEFAULT '')")
                c.execute('INSERT INTO media_assets SELECT * FROM media_assets_legacy')
                c.execute('DROP TABLE media_assets_legacy')
            c.execute('DROP TRIGGER IF EXISTS media_asset_detach')
            c.execute("CREATE TRIGGER media_asset_detach AFTER DELETE ON videos BEGIN UPDATE media_locations SET device_token='detached:'||asset_id||':'||device_token,preferred=0 WHERE asset_id IN (SELECT id FROM media_assets WHERE video_id=OLD.id); UPDATE media_assets SET video_id=NULL WHERE video_id=OLD.id; END")
            c.execute('CREATE INDEX IF NOT EXISTS media_locations_asset ON media_locations(asset_id)')
            c.execute('CREATE INDEX IF NOT EXISTS media_locations_path ON media_locations(path)')
            c.execute('CREATE TABLE IF NOT EXISTS raw_media_assets(asset_id TEXT PRIMARY KEY,metadata TEXT)')
            c.execute('CREATE TABLE IF NOT EXISTS media_move_bindings(task_id TEXT,source_path TEXT,location_id TEXT,PRIMARY KEY(task_id,source_path))')
            c.execute('CREATE UNIQUE INDEX IF NOT EXISTS media_one_preferred ON media_locations(asset_id) WHERE preferred=1')
            c.execute('CREATE TABLE IF NOT EXISTS media_location_events(id INTEGER PRIMARY KEY,asset_id TEXT,location_id TEXT,kind TEXT,task_id TEXT,created REAL)')

    def migrate(self,video_ids=None):
        """Idempotent alpha.5 import; offline files stay unverified, with no disk reads."""
        with self.db() as c:
            if video_ids is not None:
                if not isinstance(video_ids,list) or not video_ids or len(video_ids)>500 or any(type(value) is not int for value in video_ids):raise ValueError('Choose 1-500 video identities')
                rows=[dict(r) for r in c.execute('SELECT id,path,size FROM videos WHERE id IN ('+','.join('?' for _ in video_ids)+')',video_ids)]
            else:rows=[dict(r) for r in c.execute('SELECT id,path,size FROM videos')]
            for row in rows:
                asset=c.execute('SELECT id FROM media_assets WHERE video_id=?',(row['id'],)).fetchone()
                if asset:
                    known={r[0] for r in c.execute('SELECT path FROM media_locations WHERE asset_id=?',(asset['id'],))}
                    if row['path'] in known:
                        c.execute("UPDATE media_assets SET relation_path=? WHERE id=? AND relation_path=''",(row['path'],asset['id']));continue
                    c.execute('UPDATE media_assets SET video_id=NULL WHERE id=?',(asset['id'],))
                asset_id=uuid.uuid4().hex
                c.execute('INSERT INTO media_assets(id,video_id,sha256,created,relation_path) VALUES(?,?,?,?,?)',(asset_id,row['id'],'',time.time(),row['path']))
                c.execute('INSERT INTO media_locations VALUES(?,?,?,?,?,?,0,1,?)',(uuid.uuid4().hex,asset_id,'legacy-unknown',row['path'],row['size'],None,'migration'))
        return len(rows)

    def scan_alias(self,path,stamp):
        """Recognize nonpreferred and provisional positions without duplicate video rows."""
        path=str(Path(path).resolve());device=self.device_identity(Path(path))
        with self.db() as c:
            row=c.execute('SELECT l.*,a.video_id,v.path AS preferred_path FROM media_locations l JOIN media_assets a ON a.id=l.asset_id LEFT JOIN videos v ON v.id=a.video_id WHERE l.path=? AND l.device_token=?',(path,device)).fetchone()
            if not row or row['preferred_path']==path:return None
            changed=(row['size'],row['mtime_ns'])!=(stamp.st_size,stamp.st_mtime_ns)
            if changed and row['verified']:
                c.execute('UPDATE media_locations SET verified=0 WHERE id=?',(row['id'],))
                c.execute('INSERT INTO media_location_events(asset_id,location_id,kind,task_id,created) VALUES(?,?,?,?,?)',(row['asset_id'],row['id'],'source_changed','scan',time.time()))
            raw=row['video_id'] is None
        if changed:return {'status':'changed','video_id':row['video_id']}
        if raw:return {'status':'materialized','video_id':self.materialize(row['asset_id'])}
        return {'status':'alias','video_id':row['video_id']}

    def asset(self,video_id):
        with self.db() as c:row=c.execute('SELECT * FROM media_assets WHERE video_id=?',(int(video_id),)).fetchone()
        if not row:raise ValueError('File identity not registered')
        return dict(row)

    def locations(self,video_id):
        asset=self.asset(video_id)
        with self.db() as c:return [dict(r,archived=is_archive_path(r['path'])) for r in c.execute('SELECT * FROM media_locations WHERE asset_id=? ORDER BY preferred DESC,id',(asset['id'],))]

    def register_verified(self,source,stamp,device_token,video_id=None,metadata=None):
        """Register an already-hashed source without adding a premature library card."""
        import json
        source=Path(source);path=str(source.resolve());current=source.stat()
        if source.is_symlink() or (current.st_size,current.st_mtime_ns)!=(stamp['size'],stamp['mtime_ns']):raise ValueError('Source changed during identity registration')
        if not device_token or device_token=='legacy-unknown' or self.device_identity(source)!=device_token:raise ValueError('Source device changed')
        if video_id is not None:
            asset=self.asset(video_id)
        else:
            with self.db() as c:
                existing=c.execute('SELECT a.* FROM media_assets a JOIN media_locations l ON l.asset_id=a.id WHERE l.device_token=? AND l.path=?',(device_token,path)).fetchone()
            asset=dict(existing) if existing else {'id':uuid.uuid4().hex,'sha256':''}
        if asset['sha256'] and asset['sha256']!=stamp['sha256']:raise ValueError('Existing identity has different bytes; review required')
        with self.db() as c:
            if video_id is not None:
                video=c.execute('SELECT path FROM videos WHERE id=?',(video_id,)).fetchone()
                if not video or video['path']!=path:raise ValueError('Indexed source changed')
            c.execute('INSERT OR IGNORE INTO media_assets(id,video_id,sha256,created,relation_path) VALUES(?,?,?,?,?)',(asset['id'],video_id,stamp['sha256'],time.time(),path))
            c.execute('UPDATE media_assets SET sha256=? WHERE id=?',(stamp['sha256'],asset['id']))
            owner=c.execute('SELECT * FROM media_locations WHERE path=? AND asset_id=? ORDER BY preferred DESC LIMIT 1',(path,asset['id'])).fetchone()
            foreign=c.execute('SELECT asset_id FROM media_locations WHERE device_token=? AND path=?',(device_token,path)).fetchone()
            if foreign and foreign['asset_id']!=asset['id']:raise ValueError('Source already belongs to another identity')
            if owner:
                c.execute('UPDATE media_locations SET device_token=?,size=?,mtime_ns=?,verified=? WHERE id=?',(device_token,stamp['size'],stamp['mtime_ns'],time.time(),owner['id']))
            else:
                c.execute('INSERT INTO media_locations VALUES(?,?,?,?,?,?,?,1,?)',(uuid.uuid4().hex,asset['id'],device_token,path,stamp['size'],stamp['mtime_ns'],time.time(),'registration'))
            if video_id is None:c.execute('INSERT OR IGNORE INTO raw_media_assets VALUES(?,?)',(asset['id'],json.dumps(metadata or {},ensure_ascii=False)))
        return asset['id']

    def materialize(self,asset_id):
        with self.db() as c:return self._materialize(c,asset_id)

    def _materialize(self,c,asset_id,metadata=None):
        import json
        asset=c.execute('SELECT * FROM media_assets WHERE id=?',(asset_id,)).fetchone()
        if not asset:raise ValueError('Unknown raw file identity')
        if asset['video_id'] is not None:
            if not c.execute('SELECT 1 FROM videos WHERE id=?',(asset['video_id'],)).fetchone():raise ValueError('Linked video missing')
            return asset['video_id']
        location=c.execute('SELECT * FROM media_locations WHERE asset_id=? ORDER BY preferred DESC LIMIT 1',(asset_id,)).fetchone()
        raw=c.execute('SELECT metadata FROM raw_media_assets WHERE asset_id=?',(asset_id,)).fetchone()
        if not location or not raw:raise ValueError('Raw registration incomplete')
        existing=c.execute('SELECT id FROM videos WHERE path=?',(location['path'],)).fetchone()
        if existing:
            conflict=c.execute('SELECT id FROM media_assets WHERE video_id=?',(existing['id'],)).fetchone()
            if conflict and conflict['id']!=asset_id:raise ValueError('Source index was reassigned; review required')
            video_id=existing['id']
        else:
            metadata=metadata if metadata is not None else json.loads(raw['metadata']);columns={r[1] for r in c.execute('PRAGMA table_info(videos)')}
            allowed={'title','code','actors','studio','date','plot','width','height','duration','codec','source','locked_fields','tags','censor_override'}
            values={key:value for key,value in metadata.items() if key in allowed and key in columns and isinstance(value,(str,int,float))}
            values.update(path=location['path'],size=location['size'])
            if 'title' in columns:values.setdefault('title',Path(location['path']).stem)
            if 'mtime' in columns:values['mtime']=location['mtime_ns']/1_000_000_000
            if 'missing' in columns:values['missing']=1
            video_id=c.execute('INSERT INTO videos('+','.join(values)+') VALUES('+','.join('?' for _ in values)+')',tuple(values.values())).lastrowid
        c.execute('UPDATE media_assets SET video_id=? WHERE id=? AND video_id IS NULL',(video_id,asset_id))
        return video_id


    def bind_committed_copy(self,asset_id,target,approved,device_token,task_id,metadata=None,expected_identity=None):
        """Bind an executor-owned committed copy even when the original disk is now offline."""
        target=Path(target)
        if self.device_identity(target)!=device_token:raise ValueError('Target device changed')
        before=target.stat()
        if not expected_identity or [before.st_dev,before.st_ino]!=expected_identity:raise ValueError('Committed target ownership changed')
        stamp=fingerprint(target)
        after=target.stat()
        if [after.st_dev,after.st_ino]!=expected_identity or after.st_mtime_ns!=stamp['mtime_ns']:raise ValueError('Committed target changed during binding')
        with self.db() as c:asset=c.execute('SELECT * FROM media_assets WHERE id=?',(asset_id,)).fetchone()
        if not asset or not asset['sha256'] or asset['sha256']!=approved['sha256'] or (stamp['size'],stamp['sha256'])!=(approved['size'],approved['sha256']):raise ValueError('Target does not match approved identity')
        path=str(target.resolve())
        with self.db() as c:
            owner=c.execute('SELECT * FROM media_locations WHERE device_token=? AND path=?',(device_token,path)).fetchone()
            if owner and owner['asset_id']!=asset_id:raise ValueError('Target belongs to another identity')
            video_id=self._materialize(c,asset_id,metadata)
            location_id=owner['id'] if owner else uuid.uuid4().hex
            c.execute('INSERT INTO media_locations VALUES(?,?,?,?,?,?,?,0,?) ON CONFLICT(device_token,path) DO UPDATE SET size=excluded.size,mtime_ns=excluded.mtime_ns,verified=excluded.verified',
                      (location_id,asset_id,device_token,path,stamp['size'],stamp['mtime_ns'],time.time(),task_id))
            if not owner:c.execute('INSERT INTO media_location_events(asset_id,location_id,kind,task_id,created) VALUES(?,?,?,?,?)',(asset_id,location_id,'bound',task_id,time.time()))
        return video_id,location_id

    def bind_verified_copy(self,video_id,target,device_token,task_id):
        """Bind an existing verified copy; never copies, moves or deletes media."""
        if not isinstance(device_token,str) or not device_token or device_token=='legacy-unknown':raise ValueError('Verified device identity required')
        if not isinstance(task_id,str) or not task_id:raise ValueError('Owning task required')
        asset=self.asset(video_id)
        with self.db() as c:video=dict(c.execute('SELECT * FROM videos WHERE id=?',(int(video_id),)).fetchone())
        source=Path(video['path']);destination=Path(target)
        if destination.is_symlink():raise ValueError('Linked target is not supported')
        if self.device_identity(destination)!=device_token:raise ValueError('Target device identity mismatch')
        source_device=self.device_identity(source)
        if not source_device or source_device=='legacy-unknown':raise ValueError('Source device identity required')
        source_stamp=fingerprint(source);target_stamp=fingerprint(destination)
        for file,stamp in ((source,source_stamp),(destination,target_stamp)):
            current=file.stat()
            if (current.st_size,current.st_mtime_ns)!=(stamp['size'],stamp['mtime_ns']):raise ValueError('File changed during binding')
        if source_stamp['sha256']!=target_stamp['sha256'] or source_stamp['size']!=target_stamp['size']:raise ValueError('Copy content does not match source')
        if asset['sha256'] and asset['sha256']!=source_stamp['sha256']:raise ValueError('File identity changed; review required')
        path=str(destination.resolve())
        with self.db() as c:
            current=c.execute('SELECT path FROM videos WHERE id=?',(int(video_id),)).fetchone()
            if not current or current['path']!=video['path']:raise ValueError('Preferred location changed during verification')
            source_owner=c.execute('SELECT * FROM media_locations WHERE asset_id=? AND path=? ORDER BY preferred DESC LIMIT 1',(asset['id'],video['path'])).fetchone()
            if not source_owner:raise ValueError('Source location missing')
            c.execute('UPDATE media_locations SET device_token=?,size=?,mtime_ns=?,verified=? WHERE id=?',
                      (source_device,source_stamp['size'],source_stamp['mtime_ns'],time.time(),source_owner['id']))
            owner=c.execute('SELECT * FROM media_locations WHERE device_token=? AND path=?',(device_token,path)).fetchone()
            if owner and owner['asset_id']!=asset['id']:raise ValueError('Location belongs to another identity')
            location_id=owner['id'] if owner else uuid.uuid4().hex
            c.execute('UPDATE media_assets SET sha256=? WHERE id=?',(source_stamp['sha256'],asset['id']))
            c.execute('INSERT INTO media_locations VALUES(?,?,?,?,?,?,?,0,?) ON CONFLICT(device_token,path) DO UPDATE SET size=excluded.size,mtime_ns=excluded.mtime_ns,verified=excluded.verified',
                      (location_id,asset['id'],device_token,path,target_stamp['size'],target_stamp['mtime_ns'],time.time(),task_id))
            if not owner:c.execute('INSERT INTO media_location_events(asset_id,location_id,kind,task_id,created) VALUES(?,?,?,?,?)',(asset['id'],location_id,'bound',task_id,time.time()))
        return location_id

    def update_video_location(self,c,video_id,path,stamp):
        """Carry evidence only when it still describes the verified original bytes."""
        video=dict(c.execute('SELECT * FROM videos WHERE id=?',(video_id,)).fetchone())
        if 'mtime' in video and c.execute("SELECT 1 FROM sqlite_master WHERE name='file_reviews'").fetchone():
            c.execute('UPDATE file_reviews SET size=?,mtime=? WHERE video_id=? AND size=? AND mtime=?',
                      (stamp['size'],stamp['mtime_ns']/1_000_000_000,video_id,video['size'],video['mtime']))
        values={'path':str(path),'size':stamp['size']}
        if 'mtime' in video:values['mtime']=stamp['mtime_ns']/1_000_000_000
        if 'missing' in video:values['missing']=0
        c.execute('UPDATE videos SET '+','.join(key+'=?' for key in values)+' WHERE id=?',(*values.values(),video_id))

    def prefer(self,video_id,location_id,expected_identity=None):
        asset=self.asset(video_id)
        with self.db() as c:row=c.execute('SELECT * FROM media_locations WHERE id=? AND asset_id=?',(location_id,asset['id'])).fetchone()
        if not row or not row['verified']:raise ValueError('Choose a verified location')
        if is_archive_path(row['path'],archive_roots(self.db)):raise ValueError('归档位置不能直接设为默认播放，请先按任务恢复')
        if self.device_identity(Path(row['path']))!=row['device_token']:raise ValueError('Location device changed')
        stamp=fingerprint(row['path'])
        if expected_identity:
            current=Path(row['path']).stat()
            if [current.st_dev,current.st_ino]!=expected_identity:raise ValueError('Preferred target ownership changed')
        if stamp['sha256']!=asset['sha256'] or stamp['size']!=row['size']:raise ValueError('Location changed after verification')
        with self.db() as c:
            current=c.execute('SELECT sha256 FROM media_assets WHERE id=?',(asset['id'],)).fetchone()
            if current['sha256']!=stamp['sha256']:raise ValueError('Identity changed during verification')
            c.execute('UPDATE media_locations SET preferred=0 WHERE asset_id=?',(asset['id'],))
            c.execute('UPDATE media_locations SET preferred=1 WHERE id=?',(location_id,))
            self.update_video_location(c,int(video_id),row['path'],stamp)
            c.execute('UPDATE media_locations SET size=?,mtime_ns=?,verified=? WHERE id=?',(stamp['size'],stamp['mtime_ns'],time.time(),location_id))
            c.execute('INSERT INTO media_location_events(asset_id,location_id,kind,task_id,created) VALUES(?,?,?,?,?)',(asset['id'],location_id,'preferred','',time.time()))

    def bind_moves(self,entries,task_id,supplemental=None):
        """Atomically rebind same-device moves after their physical targets are verified."""
        checked=[];seen=set()
        for entry in entries:
            key=binding_key(entry)
            if key in seen:raise ValueError('A file identity appears twice in the move')
            seen.add(key)
            if entry.get('asset_id'):
                with self.db() as c:row=c.execute('SELECT * FROM media_assets WHERE id=?',(entry['asset_id'],)).fetchone()
                if not row:raise ValueError('Unknown moved identity')
                asset=dict(row)
            else:asset=self.asset(entry['video_id'])
            target=Path(entry['destination'])
            if self.device_identity(target)!=entry['target_device']:raise ValueError('Target device changed')
            stamp=fingerprint(target)
            info=target.stat()
            if [info.st_dev,info.st_ino]!=entry.get('source_identity') or info.st_mtime_ns!=stamp['mtime_ns']:raise ValueError('Moved file ownership changed')
            if stamp['sha256']!=entry['fingerprint']['sha256'] or stamp['size']!=entry['fingerprint']['size']:raise ValueError('Moved file changed')
            if asset['sha256'] and asset['sha256']!=stamp['sha256']:raise ValueError('Moved content does not match identity')
            checked.append((entry,asset,stamp))
        with self.db() as c:
            updates=[];used=set()
            for entry,asset,stamp in checked:
                entry=dict(entry,video_id=self._materialize(c,asset['id'],entry.get('metadata')))
                row=c.execute('SELECT path FROM videos WHERE id=?',(entry['video_id'],)).fetchone()
                previous=c.execute('SELECT location_id FROM media_move_bindings WHERE task_id=? AND source_path=?',(task_id,entry['source'])).fetchone()
                if previous:
                    location=c.execute('SELECT * FROM media_locations WHERE id=? AND asset_id=?',(previous['location_id'],asset['id'])).fetchone()
                    if not location or location['path']!=entry['destination']:raise ValueError('Moved location changed after binding')
                else:
                    location=c.execute('SELECT * FROM media_locations WHERE asset_id=? AND path=?',(asset['id'],entry['source'])).fetchone()
                    if not location and c.execute("SELECT 1 FROM sqlite_master WHERE name='organization_inverses'").fetchone() and c.execute('SELECT 1 FROM organization_inverses WHERE inverse_job_id=?',(task_id,)).fetchone():
                        # An interrupted original move may never have rebound its DB location.
                        location=c.execute('SELECT * FROM media_locations WHERE asset_id=? AND path=?',(asset['id'],entry['destination'])).fetchone()
                if not location or location['id'] in used:raise ValueError('Original location missing or repeated')
                if location['preferred'] and (not row or row['path'] not in (entry['source'],entry['destination'])):raise ValueError('Preferred path changed; reconcile before binding')
                used.add(location['id']);updates.append((entry,asset,stamp,dict(location)))
            # Free location and preferred-video unique paths atomically, including cycles.
            for entry,asset,stamp,location in updates:
                temporary='velvet-db-move:'+task_id+':'+location['id']
                if location['preferred']:c.execute('UPDATE videos SET path=? WHERE id=?',(temporary,entry['video_id']))
                c.execute('UPDATE media_locations SET path=? WHERE id=?',(temporary,location['id']))
            for entry,asset,stamp,location in updates:
                if location['preferred']:self.update_video_location(c,entry['video_id'],entry['destination'],stamp)
                c.execute('UPDATE media_assets SET sha256=? WHERE id=?',(stamp['sha256'],asset['id']))
                c.execute('UPDATE media_locations SET path=?,device_token=?,size=?,mtime_ns=?,verified=?,origin_task=? WHERE id=?',
                          (entry['destination'],entry['target_device'],stamp['size'],stamp['mtime_ns'],time.time(),task_id,location['id']))
                if not c.execute('SELECT 1 FROM media_move_bindings WHERE task_id=? AND source_path=?',(task_id,entry['source'])).fetchone():
                    c.execute('INSERT INTO media_move_bindings VALUES(?,?,?)',(task_id,entry['source'],location['id']))
                    c.execute('INSERT INTO media_location_events(asset_id,location_id,kind,task_id,created) VALUES(?,?,?,?,?)',(asset['id'],location['id'],'moved',task_id,time.time()))
            if supplemental:supplemental(c)
        return {binding_key(original):update[3]['id'] for original,update in zip(entries,updates)}
