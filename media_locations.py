"""Stable file identities and verified physical locations (application data only)."""
import hashlib
import time
import uuid
from pathlib import Path
from storage_inventory import volume_identity


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
            c.execute('CREATE UNIQUE INDEX IF NOT EXISTS media_one_preferred ON media_locations(asset_id) WHERE preferred=1')
            c.execute('CREATE TABLE IF NOT EXISTS media_location_events(id INTEGER PRIMARY KEY,asset_id TEXT,location_id TEXT,kind TEXT,task_id TEXT,created REAL)')

    def migrate(self):
        """Idempotent alpha.5 import; offline files stay unverified, with no disk reads."""
        with self.db() as c:
            rows=[dict(r) for r in c.execute('SELECT id,path,size FROM videos')]
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

    def asset(self,video_id):
        with self.db() as c:row=c.execute('SELECT * FROM media_assets WHERE video_id=?',(int(video_id),)).fetchone()
        if not row:raise ValueError('File identity not registered')
        return dict(row)

    def locations(self,video_id):
        asset=self.asset(video_id)
        with self.db() as c:return [dict(r) for r in c.execute('SELECT * FROM media_locations WHERE asset_id=? ORDER BY preferred DESC,id',(asset['id'],))]

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

    def prefer(self,video_id,location_id):
        asset=self.asset(video_id)
        with self.db() as c:row=c.execute('SELECT * FROM media_locations WHERE id=? AND asset_id=?',(location_id,asset['id'])).fetchone()
        if not row or not row['verified']:raise ValueError('Choose a verified location')
        if self.device_identity(Path(row['path']))!=row['device_token']:raise ValueError('Location device changed')
        stamp=fingerprint(row['path'])
        if stamp['sha256']!=asset['sha256'] or stamp['size']!=row['size']:raise ValueError('Location changed after verification')
        with self.db() as c:
            current=c.execute('SELECT sha256 FROM media_assets WHERE id=?',(asset['id'],)).fetchone()
            if current['sha256']!=stamp['sha256']:raise ValueError('Identity changed during verification')
            c.execute('UPDATE media_locations SET preferred=0 WHERE asset_id=?',(asset['id'],))
            c.execute('UPDATE media_locations SET preferred=1 WHERE id=?',(location_id,))
            self.update_video_location(c,int(video_id),row['path'],stamp)
            c.execute('UPDATE media_locations SET size=?,mtime_ns=?,verified=? WHERE id=?',(stamp['size'],stamp['mtime_ns'],time.time(),location_id))
            c.execute('INSERT INTO media_location_events(asset_id,location_id,kind,task_id,created) VALUES(?,?,?,?,?)',(asset['id'],location_id,'preferred','',time.time()))

    def bind_moves(self,entries,task_id):
        """Atomically rebind same-device moves after their physical targets are verified."""
        checked=[];seen=set()
        for entry in entries:
            video_id=entry['video_id']
            if video_id in seen:raise ValueError('A file identity appears twice in the move')
            seen.add(video_id);asset=self.asset(video_id);target=Path(entry['destination'])
            if self.device_identity(target)!=entry['target_device']:raise ValueError('Target device changed')
            stamp=fingerprint(target)
            if stamp['sha256']!=entry['fingerprint']['sha256'] or stamp['size']!=entry['fingerprint']['size']:raise ValueError('Moved file changed')
            if asset['sha256'] and asset['sha256']!=stamp['sha256']:raise ValueError('Moved content does not match identity')
            checked.append((entry,asset,stamp))
        with self.db() as c:
            updates=[]
            for entry,asset,stamp in checked:
                row=c.execute('SELECT path FROM videos WHERE id=?',(entry['video_id'],)).fetchone()
                if not row or row['path'] not in (entry['source'],entry['destination']):raise ValueError('Preferred path changed; reconcile before binding')
                location=c.execute('SELECT id FROM media_locations WHERE asset_id=? AND path=? ORDER BY preferred DESC LIMIT 1',(asset['id'],row['path'])).fetchone()
                if not location:raise ValueError('Original location missing')
                updates.append((entry,asset,stamp,location['id']))
            # Free unique paths inside the transaction, supporting A->B / B->A cycles.
            for entry,asset,stamp,location_id in updates:
                temporary='velvet-db-move:'+task_id+':'+asset['id']
                c.execute('UPDATE videos SET path=? WHERE id=?',(temporary,entry['video_id']))
                c.execute('UPDATE media_locations SET path=? WHERE id=?',(temporary,location_id))
            for entry,asset,stamp,location_id in updates:
                self.update_video_location(c,entry['video_id'],entry['destination'],stamp)
                c.execute('UPDATE media_assets SET sha256=? WHERE id=?',(stamp['sha256'],asset['id']))
                c.execute('UPDATE media_locations SET path=?,device_token=?,size=?,mtime_ns=?,verified=?,origin_task=? WHERE id=?',
                          (entry['destination'],entry['target_device'],stamp['size'],stamp['mtime_ns'],time.time(),task_id,location_id))
                if not c.execute("SELECT 1 FROM media_location_events WHERE asset_id=? AND location_id=? AND kind='moved' AND task_id=?",(asset['id'],location_id,task_id)).fetchone():
                    c.execute('INSERT INTO media_location_events(asset_id,location_id,kind,task_id,created) VALUES(?,?,?,?,?)',(asset['id'],location_id,'moved',task_id,time.time()))
        return {entry['video_id']:location_id for entry,asset,stamp,location_id in updates}
