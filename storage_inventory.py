"""Read-only disk snapshots and versioned organization plans. No file operations."""
import ctypes
import hashlib
import json
import os
import shutil
import re
import time
import uuid
import unicodedata
from pathlib import Path
from media_libraries import canonical
from media_companions import discover

EXTENSIONS = {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.m4v', '.ts', '.webm', '.mpeg', '.mpg', '.flv'}
SKIP = {'$recycle.bin', 'system volume information', '.git', '.venv', 'node_modules'}


def volume_identity(root):
    if os.name == 'nt':
        volume = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetVolumePathNameW(str(root), volume, len(volume)):
            guid = ctypes.create_unicode_buffer(1024)
            if ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(volume.value, guid, len(guid)):
                return 'windows-volume:' + guid.value.casefold(), True
    try:
        device=os.stat(root).st_dev
        if device:return 'system-device:'+str(device),False
    except OSError:pass
    return 'location:' + hashlib.sha256(str(root).encode()).hexdigest(), False


def volume_details(root):
    result={'label':'','filesystem':'unknown','read_only':None}
    if os.name=='nt':
        volume=ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetVolumePathNameW(str(root),volume,len(volume)):
            label=ctypes.create_unicode_buffer(1024);fs=ctypes.create_unicode_buffer(1024)
            flags=ctypes.c_ulong();serial=ctypes.c_ulong();length=ctypes.c_ulong()
            if ctypes.windll.kernel32.GetVolumeInformationW(volume.value,label,len(label),ctypes.byref(serial),ctypes.byref(length),ctypes.byref(flags),fs,len(fs)):
                result.update(label=label.value,filesystem=fs.value,read_only=bool(flags.value & 0x80000))
    return result


def destination_key(path):return unicodedata.normalize('NFC',str(path)).casefold()


def destination_conflicts(path, destinations, filesystem='unknown'):
    key=destination_key(path);conflicts=[]
    if key in destinations:conflicts.append('planned_collision')
    if path.exists():conflicts.append('target_exists')
    if len(str(path))>240:conflicts.append('long_path')
    if any(safe_component(part)!=part for part in path.parts[1:]):conflicts.append('invalid_component')
    destinations.add(key)
    return conflicts


def target_space(entries,target):
    parent=Path(target)
    while not parent.exists() and parent!=parent.parent:parent=parent.parent
    free=shutil.disk_usage(parent).free if parent.exists() else None
    volume=volume_details(parent);companions={}
    for entry in entries:
        for resource in entry['companions']:companions[str(Path(entry['relative']).parent/resource['name'])]=resource.get('size',0) or 0
        if volume['read_only']:entry['conflicts'].append('target_read_only')
        if volume['filesystem'].upper()=='FAT32' and entry['size']>4294967295:entry['conflicts'].append('fat32_file_limit')
    estimated=sum(entry['size'] for entry in entries)+sum(companions.values())
    reserve=max(100*1024*1024,max((entry['size'] for entry in entries),default=0),int(estimated*.05))
    return {'filesystem':volume['filesystem'],'read_only':volume['read_only'],'free':free,'companions_bytes':sum(companions.values()),'required':estimated,'reserve':reserve,'sufficient':free is not None and free>=estimated+reserve}


def safe_component(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value)).strip(' .')[:120]
    if not value or value in ('.', '..'):
        return '_unnamed'
    if re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', value):
        value = '_' + value
    return value


class StorageInventory:
    def __init__(self, db, metadata, excluded):
        self.db, self.metadata, self.excluded = db, metadata, excluded
        self.ensure()

    def ensure(self):
        with self.db() as c:
            c.execute('CREATE TABLE IF NOT EXISTS storage_devices(id TEXT PRIMARY KEY,identity TEXT,root TEXT,alias TEXT,stable INTEGER,updated REAL)')
            c.execute('CREATE TABLE IF NOT EXISTS inventory_files(device_id TEXT,relative_path TEXT,size INTEGER,mtime_ns INTEGER,digest TEXT,metadata TEXT,companions TEXT,snapshot TEXT,PRIMARY KEY(device_id,relative_path,snapshot))')
            primary=[r[1] for r in c.execute('PRAGMA table_info(inventory_files)') if r[5]]
            if primary==['device_id','relative_path']:
                c.execute('ALTER TABLE inventory_files RENAME TO inventory_files_legacy')
                c.execute('CREATE TABLE inventory_files(device_id TEXT,relative_path TEXT,size INTEGER,mtime_ns INTEGER,digest TEXT,metadata TEXT,companions TEXT,snapshot TEXT,PRIMARY KEY(device_id,relative_path,snapshot))')
                c.execute('INSERT INTO inventory_files SELECT * FROM inventory_files_legacy')
                if c.execute('SELECT COUNT(*) FROM inventory_files').fetchone()[0]!=c.execute('SELECT COUNT(*) FROM inventory_files_legacy').fetchone()[0]:raise ValueError('Inventory migration did not preserve all rows')
                c.execute('DROP TABLE inventory_files_legacy')
            c.execute('CREATE TABLE IF NOT EXISTS inventory_snapshots(id TEXT PRIMARY KEY,device_id TEXT,state TEXT,processed INTEGER,failed INTEGER,created REAL,finished REAL)')
            if 'processed_bytes' not in {r[1] for r in c.execute('PRAGMA table_info(inventory_snapshots)')}:c.execute('ALTER TABLE inventory_snapshots ADD COLUMN processed_bytes INTEGER DEFAULT 0')
            c.execute('CREATE TABLE IF NOT EXISTS inventory_errors(snapshot TEXT,relative_path TEXT,kind TEXT)')
            c.execute('CREATE TABLE IF NOT EXISTS organization_plans(id TEXT PRIMARY KEY,device_id TEXT,target TEXT,snapshot TEXT,state TEXT,payload TEXT,created REAL)')
            c.execute("CREATE TABLE IF NOT EXISTS inventory_preferences(device_id TEXT,relative_path TEXT,preferred INTEGER DEFAULT 0,note TEXT DEFAULT '',PRIMARY KEY(device_id,relative_path))")
            c.execute('CREATE TABLE IF NOT EXISTS inventory_annotations(device_id TEXT,relative_path TEXT,payload TEXT,PRIMARY KEY(device_id,relative_path))')
            c.execute("UPDATE inventory_snapshots SET state='interrupted' WHERE state='running'")

    def register(self, raw, alias):
        if not isinstance(raw, str) or not raw.strip() or not isinstance(alias, str) or not 1 <= len(alias.strip()) <= 100:
            raise ValueError('请填写目录和1–100字设备别名')
        root = Path(raw).expanduser().resolve()
        if not root.is_dir():
            raise ValueError('盘点目录不存在或离线')
        identity, stable = volume_identity(root)
        # Same physical disk may have several independent scan roots.
        with self.db() as c:
            old = c.execute('SELECT * FROM storage_devices WHERE root=?', (str(root),)).fetchone()
            if old and old['identity'] != identity:
                raise ValueError('设备身份变化，请使用新的盘点登记，不覆盖原快照')
            device_id = old['id'] if old else uuid.uuid4().hex
            c.execute('INSERT OR REPLACE INTO storage_devices VALUES(?,?,?,?,?,?)', (device_id, identity, str(root), alias.strip(), int(stable), time.time()))
        return device_id

    def devices(self):
        with self.db() as c:
            rows = [dict(r) for r in c.execute('SELECT * FROM storage_devices ORDER BY alias')]
        for row in rows:
            root = Path(row['root']); row['online'] = root.is_dir()
            if row['online']:
                row['online'] = volume_identity(root)[0] == row['identity']
            if row['online']:
                capacity=shutil.disk_usage(root);row['capacity']={'total':capacity.total,'free':capacity.free,'used':capacity.used}
            else:row['capacity']=None
            row['volume']=volume_details(root) if row['online'] else {'label':'','filesystem':'unknown','read_only':None}
            row['identity_note'] = '卷身份已记录' if row['stable'] else '位置标识，跨设备或挂载变化需人工核对'
            with self.db() as c:
                row['latest_snapshot'] = next((dict(r) for r in c.execute('SELECT * FROM inventory_snapshots WHERE device_id=? ORDER BY created DESC LIMIT 1', (row['id'],))), None)
                row['files'] = c.execute("SELECT COUNT(*) FROM inventory_files WHERE device_id=? AND snapshot=(SELECT id FROM inventory_snapshots WHERE device_id=? AND state IN ('done','partial') ORDER BY created DESC LIMIT 1)", (row['id'],row['id'])).fetchone()[0]
        return rows

    def device(self, device_id):
        with self.db() as c:
            row = c.execute('SELECT * FROM storage_devices WHERE id=?', (device_id,)).fetchone()
        if not row:
            raise ValueError('未知盘点设备')
        return dict(row)

    def annotate(self, device_id, relative, annotation):
        if not isinstance(annotation,dict) or set(annotation)-{'title','kind','number','collection'}:raise ValueError('只允许修正整理建议，不修改片库关系')
        if annotation.get('kind','unconfirmed') not in ('unconfirmed','movie','version','part','episode','collection'):raise ValueError('关系类型无效')
        for key in ('title','collection'):
            if key in annotation and (not isinstance(annotation[key],str) or len(annotation[key])>200):raise ValueError('建议文字过长')
        if annotation.get('number') is not None and (type(annotation['number']) is not int or not 1<=annotation['number']<=999):raise ValueError('序号应为1–999')
        with self.db() as c:
            if not c.execute('SELECT 1 FROM inventory_files WHERE device_id=? AND relative_path=?',(device_id,relative)).fetchone():raise ValueError('未知快照条目')
            c.execute('INSERT OR REPLACE INTO inventory_annotations VALUES(?,?,?)',(device_id,relative,json.dumps(annotation)))

    def effective_metadata(self, row):
        metadata=json.loads(row['metadata'])
        with self.db() as c:annotation=c.execute('SELECT payload FROM inventory_annotations WHERE device_id=? AND relative_path=?',(row['device_id'],row['relative_path'])).fetchone()
        if annotation:
            correction=json.loads(annotation['payload']);metadata['organization_annotation']=correction
            # Keep locked library metadata intact. This annotation belongs only to the proposal.
            metadata['relationship']=dict(metadata.get('relationship',{}),**{k:v for k,v in correction.items() if k!='title'},manual_required=False)
        return metadata

    def remap(self, device_id, raw, confirm_identity):
        device=self.device(device_id);root=Path(raw).expanduser().resolve()
        if not root.is_dir():raise ValueError('映射位置不存在或离线')
        identity,stable=volume_identity(root)
        if confirm_identity!=device['identity']:raise ValueError('请核对原设备身份后确认映射')
        if device['stable'] and identity!=device['identity']:raise ValueError('卷身份不匹配，不能绑定旧快照')
        with self.db() as c:
            c.execute('UPDATE storage_devices SET root=?,identity=?,stable=?,updated=? WHERE id=?',(str(root),identity,int(stable),time.time(),device_id))
        return self.device(device_id)

    def scan(self, device_id, cancel=lambda: False, progress=lambda **_: None, hashing=False):
        device = self.device(device_id); root = Path(device['root'])
        if not root.is_dir() or volume_identity(root)[0] != device['identity']:
            raise ValueError('设备离线或身份不匹配')
        snapshot = uuid.uuid4().hex; processed = failed = processed_bytes = 0
        with self.db() as c:
            c.execute('INSERT INTO inventory_snapshots(id,device_id,state,processed,failed,created,finished) VALUES(?,?,?,?,?,?,?)', (snapshot, device_id, 'running', 0, 0, time.time(), None))
        excluded = [Path(p).resolve() for p in self.excluded()]
        def allowed(path):
            return not any(canonical(path).is_relative_to(p) for p in excluded)
        state = 'done'
        try:
            def walk_error(error):
                nonlocal failed
                failed += 1
                with self.db() as c:c.execute('INSERT INTO inventory_errors VALUES(?,?,?)',(snapshot,'.',type(error).__name__))
            for base, directories, names in os.walk(root, followlinks=False, onerror=walk_error):
                base = Path(base)
                if cancel():
                    state = 'cancelled'; break
                if not allowed(base):
                    directories[:] = []; continue
                safe_dirs = []
                for name in directories:
                    path = base/name
                    try:
                        if name.casefold() not in SKIP and not path.is_symlink() and not getattr(path.lstat(), 'st_file_attributes', 0) & 1024 and allowed(path):
                            safe_dirs.append(name)
                    except OSError:
                        failed += 1
                directories[:] = safe_dirs
                resources = discover(base,names,EXTENSIONS,allowed,hashing,cancel)
                for name in names:
                    if cancel():
                        state = 'cancelled'; break
                    path = base/name
                    if path.suffix.lower() not in EXTENSIONS or path.is_symlink() or not allowed(path):
                        continue
                    try:
                        before = path.stat(); relative = path.relative_to(root).as_posix()
                        with self.db() as c:
                            old = c.execute('SELECT * FROM inventory_files WHERE device_id=? AND relative_path=? ORDER BY rowid DESC LIMIT 1', (device_id, relative)).fetchone()
                        unchanged = old and (old['size'], old['mtime_ns']) == (before.st_size, before.st_mtime_ns)
                        digest = old['digest'] if unchanged else ''
                        if hashing:
                            h = hashlib.sha256()
                            with path.open('rb') as source:
                                for block in iter(lambda: source.read(4*1024*1024), b''):
                                    if cancel():break
                                    h.update(block)
                            if cancel():
                                state = 'cancelled'; break
                            digest = h.hexdigest()
                        metadata = self.metadata(path)
                        metadata.setdefault('candidates',{'filename':path.stem,'directory':path.parent.name})
                        metadata.setdefault('confidence','manual' if metadata.get('locked_fields') not in (None,'[]',[]) else 'matched' if metadata.get('source') not in ('filename','',None) else 'needs_review')
                        match=re.search(r'(?i)(?:part|cd|disc|episode|ep|第)[ ._-]*(\d{1,3})',path.stem)
                        metadata.setdefault('relationship',{'kind':'episode_or_part_candidate' if match else 'unconfirmed','number':int(match[1]) if match else None,'collection':path.parent.name,'manual_required':True})
                        metadata['hash_verified_at']=time.time() if hashing and digest else (json.loads(old['metadata']).get('hash_verified_at') if unchanged and old else None)
                        after = path.stat()
                        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                            failed += 1
                            with self.db() as c:c.execute('INSERT INTO inventory_errors VALUES(?,?,?)',(snapshot,relative,'SourceChanged'))
                            continue
                        companions = resources.get(name,[])
                        with self.db() as c:
                            c.execute('INSERT OR REPLACE INTO inventory_files VALUES(?,?,?,?,?,?,?,?)', (device_id, relative, before.st_size, before.st_mtime_ns, digest, json.dumps(metadata), json.dumps(companions), snapshot))
                            c.execute('UPDATE inventory_snapshots SET processed=?,failed=?,processed_bytes=? WHERE id=?', (processed+1, failed, processed_bytes+before.st_size, snapshot))
                        processed += 1;processed_bytes+=before.st_size
                        if processed % 10 == 0:progress(processed=processed, failed=failed,bytes_processed=processed_bytes)
                    except Exception as error:
                        failed += 1
                        with self.db() as c:c.execute('INSERT INTO inventory_errors VALUES(?,?,?)',(snapshot,path.relative_to(root).as_posix(),type(error).__name__))
                if state == 'cancelled':break
        except BaseException:
            state = 'interrupted'; raise
        finally:
            with self.db() as c:
                c.execute('UPDATE inventory_snapshots SET state=?,processed=?,failed=?,finished=? WHERE id=?', ('partial' if state=='done' and failed else state, processed, failed, time.time(), snapshot))
        return {'snapshot': snapshot, 'state': 'partial' if state=='done' and failed else state, 'processed': processed, 'failed': failed,'bytes_processed':processed_bytes}

    def files(self, device_id, offset=0, limit=50, status='all'):
        self.device(device_id)
        if status not in ('all','unknown','issue'):raise ValueError('未知盘点筛选')
        condition=" AND (COALESCE(json_extract(metadata,'$.code'),'')='' OR json_extract(metadata,'$.confidence')='needs_review')" if status=='unknown' else " AND COALESCE(json_array_length(json_extract(metadata,'$.issues')),0)>0" if status=='issue' else ''
        with self.db() as c:
            rows = [dict(r) for r in c.execute('SELECT * FROM inventory_files WHERE device_id=? AND snapshot=(SELECT id FROM inventory_snapshots WHERE device_id=? AND state IN ("done","partial") ORDER BY created DESC LIMIT 1)'+condition+' ORDER BY relative_path LIMIT ? OFFSET ?', (device_id, device_id, min(100,max(1,int(limit))), max(0,int(offset))))]
        for row in rows:
            row['metadata'] = self.effective_metadata(row); row['companions'] = json.loads(row['companions'])
        return rows

    def plan(self, device_id, raw_target, layout='title'):
        if layout not in ('title', 'preserve', 'version', 'episode'):
            raise ValueError('未知整理模板')
        if not isinstance(raw_target,str) or not raw_target.strip():raise ValueError('请填写独立目标目录')
        device = self.device(device_id); source = Path(device['root'])
        target = Path(raw_target).expanduser().resolve()
        if source.is_relative_to(target) or target.is_relative_to(source):
            raise ValueError('目标必须与源目录相互独立')
        with self.db() as c:
            latest = c.execute("SELECT * FROM inventory_snapshots WHERE device_id=? AND state IN ('done','partial') ORDER BY created DESC LIMIT 1", (device_id,)).fetchone()
            if not latest:raise ValueError('先完成一次盘点')
            rows = [dict(r) for r in c.execute('SELECT * FROM inventory_files WHERE device_id=? AND snapshot=? ORDER BY relative_path', (device_id, latest['id']))]
        entries = []; destinations = set()
        for row in rows:
            metadata = self.effective_metadata(row); relative = Path(row['relative_path'])
            title = metadata.get('organization_annotation',{}).get('title') or metadata.get('title') or relative.parent.name or relative.stem
            destination = target/relative if layout == 'preserve' else target/safe_component(title)/safe_component(relative.name)
            if layout=='version':destination=target/safe_component(title)/safe_component(str(metadata.get('height') or 'unknown')+'p-'+str(metadata.get('codec') or 'unknown'))/safe_component(relative.name)
            if layout=='episode':destination=target/safe_component(metadata.get('relationship',{}).get('collection') or relative.parent.name or title)/('Episode '+str(metadata.get('relationship',{}).get('number') or 'unknown').zfill(2))/safe_component(relative.name)
            conflicts=destination_conflicts(destination,destinations)
            try:
                stamp=(source/relative).stat()
                if (stamp.st_size,stamp.st_mtime_ns)!=(row['size'],row['mtime_ns']):conflicts.append('source_changed')
                if volume_identity(source)[0]!=device['identity']:conflicts.append('device_mismatch')
            except OSError:conflicts.append('source_unavailable')
            entries.append({'relative': row['relative_path'], 'source':str(source/relative), 'destination': str(destination), 'size': row['size'], 'mtime_ns': row['mtime_ns'], 'digest': row['digest'], 'operation': 'copy_proposal_only', 'conflicts': conflicts,
                            'reason': 'preserve_original' if layout=='preserve' else 'metadata_or_directory_title', 'metadata_source':metadata.get('source','unknown'), 'needs_review': True, 'source_metadata':metadata, 'companions': json.loads(row['companions'])})
        space=target_space(entries,target)
        plan_id = uuid.uuid4().hex
        payload = {'entries': entries, 'bytes': sum(r['size'] for r in rows), 'source_unchanged': not any(any(issue in entry['conflicts'] for issue in ('source_changed','source_unavailable','device_mismatch')) for entry in entries), 'source_root':str(source),'device_id':device_id,'device_alias':device['alias'],'source_device_identity':device['identity'], 'snapshot_created':latest['created'], 'executable': False, 'layout': layout,'space':space}
        payload['digest'] = hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
        with self.db() as c:
            c.execute('INSERT INTO organization_plans VALUES(?,?,?,?,?,?,?)', (plan_id, device_id, str(target), latest['id'], 'preview_only', json.dumps(payload), time.time()))
        return dict(payload, id=plan_id,target=str(target))

    def plans(self, device_id):
        with self.db() as c:
            rows = [dict(r) for r in c.execute('SELECT * FROM organization_plans WHERE device_id=? ORDER BY created DESC LIMIT 20', (device_id,))]
        for row in rows:row['payload'] = json.loads(row['payload'])
        return rows

    def comparisons(self):
        with self.db() as c:
            snapshots=[dict(r) for r in c.execute("SELECT * FROM inventory_snapshots WHERE state IN ('done','partial') ORDER BY created DESC")]
            latest={}
            for snap in snapshots:latest.setdefault(snap['device_id'],snap['id'])
            rows=[dict(r) for r in c.execute('SELECT * FROM inventory_files') if latest.get(r['device_id'])==r['snapshot']]
            preferences={(r['device_id'],r['relative_path']):dict(r) for r in c.execute('SELECT * FROM inventory_preferences')}
        groups={}
        for row in rows:
            row['metadata']=json.loads(row['metadata']);row['companions']=json.loads(row['companions'])
            row['preference']=preferences.get((row['device_id'],row['relative_path']),{'preferred':0,'note':''})
            if row['digest']:groups.setdefault(('exact',row['digest']),[]).append(row)
            code=str(row['metadata'].get('code') or '').strip().upper()
            if code:groups.setdefault(('candidate',code),[]).append(row)
        return [{'kind':kind,'key':key,'files':items,'current_content_verified':False} for (kind,key),items in groups.items() if len(items)>1][:100]

    def prefer(self, device_id, relative, preferred, note=''):
        if type(preferred) is not bool or not isinstance(note,str) or len(note)>1000:raise ValueError('无效偏好')
        with self.db() as c:
            if not c.execute('SELECT 1 FROM inventory_files WHERE device_id=? AND relative_path=?',(device_id,relative)).fetchone():raise ValueError('快照条目不存在')
            c.execute('INSERT OR REPLACE INTO inventory_preferences VALUES(?,?,?,?)',(device_id,relative,int(preferred),note))

    def revise_plan(self, plan_id, expected_digest, selected, edits=None):
        with self.db() as c:row=c.execute('SELECT * FROM organization_plans WHERE id=?',(plan_id,)).fetchone()
        if not row:raise ValueError('未知方案')
        payload=json.loads(row['payload'])
        if payload['digest']!=expected_digest:raise ValueError('方案已变化，请重新载入')
        if not isinstance(selected,list) or not selected or any(type(i) is not int or not 0<=i<len(payload['entries']) for i in selected):raise ValueError('选择条目无效')
        edits=edits or {}
        if not isinstance(edits,dict):raise ValueError('目标修改无效')
        root=Path(row['target']);entries=[];destinations=set()
        for i in sorted(set(selected)):
            entry=dict(payload['entries'][i]);replacement=edits.get(str(i))
            if replacement is not None:
                if not isinstance(replacement,str) or not replacement:raise ValueError('目标相对路径无效')
                relative=Path(replacement)
                if relative.is_absolute() or relative.drive or '..' in relative.parts:raise ValueError('目标路径越界')
                target=root/relative
                if not target.resolve().is_relative_to(root.resolve()):raise ValueError('目标路径越界')
                entry['destination']=str(target)
            entry['conflicts']=destination_conflicts(Path(entry['destination']),destinations)
            device=self.device(row['device_id']);source=Path(device['root'])/entry['relative'];entry['source']=str(source)
            if device['identity']!=payload.get('source_device_identity'):entry['conflicts'].append('device_mismatch')
            try:
                stat=source.stat()
                if (stat.st_size,stat.st_mtime_ns)!=(entry['size'],entry['mtime_ns']):entry['conflicts'].append('source_changed')
            except OSError:entry['conflicts'].append('source_unavailable')
            entries.append(entry)
        updated=dict(payload,entries=entries,bytes=sum(e['size'] for e in entries),parent_plan_id=plan_id)
        updated['space']=target_space(entries,root)
        updated['source_unchanged']=not any(any(issue in entry['conflicts'] for issue in ('source_changed','source_unavailable','device_mismatch')) for entry in entries)
        updated['source_root']=self.device(row['device_id'])['root']
        updated.pop('digest',None);updated['digest']=hashlib.sha256(json.dumps(updated,sort_keys=True).encode()).hexdigest()
        new_id=uuid.uuid4().hex
        with self.db() as c:
            c.execute('INSERT INTO organization_plans VALUES(?,?,?,?,?,?,?)',(new_id,row['device_id'],row['target'],row['snapshot'],'preview_only',json.dumps(updated),time.time()))
        return dict(updated,id=new_id,parent=plan_id,target=row['target'])

    def errors(self, device_id):
        self.device(device_id)
        with self.db() as c:
            latest=c.execute('SELECT id FROM inventory_snapshots WHERE device_id=? ORDER BY created DESC LIMIT 1',(device_id,)).fetchone()
            return [dict(r) for r in c.execute('SELECT relative_path,kind FROM inventory_errors WHERE snapshot=? LIMIT 500',(latest['id'],))] if latest else []
