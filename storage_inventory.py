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
import stat
from pathlib import Path
from media_libraries import canonical
from organization_preflight import no_links,owned_path
from media_companions import discover
from archive_policy import archive_roots,is_archive_path,ARCHIVE_NAME

EXTENSIONS = {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.m4v', '.ts', '.webm', '.mpeg', '.mpg', '.flv'}
SKIP = {'$recycle.bin', 'system volume information', '.git', '.venv', 'node_modules',ARCHIVE_NAME}

class DeviceChangedError(ValueError):pass
class DirectoryChangedError(DeviceChangedError):pass

def root_file_id(root):
    info=Path(root).stat()
    if not stat.S_ISDIR(info.st_mode):raise ValueError('盘点位置不是目录')
    return str(info.st_ino) if info.st_ino else ''


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


def destination_conflicts(path, destinations, filesystem='unknown',allowed=(),cache=None):
    from organization_preflight import path_conflicts
    return path_conflicts(path,destinations,allowed=allowed,cache=cache)


def target_space(entries,target,operation='copy'):
    parent=Path(target)
    while not parent.exists() and parent!=parent.parent:parent=parent.parent
    free=shutil.disk_usage(parent).free if parent.exists() else None
    volume=volume_details(parent);companions={}
    for entry in entries:
        for resource in entry['companions']:
            if resource.get('shared') or not resource.get('available',True):continue
            companions[str(Path(entry.get('source',entry['relative'])).parent/resource['name'])]=resource.get('size',0) or 0
            if volume['filesystem'].upper()=='FAT32' and (resource.get('size',0) or 0)>4294967295:entry['conflicts'].append('fat32_file_limit')
        if volume['read_only']:entry['conflicts'].append('target_read_only')
        if volume['filesystem'].upper()=='FAT32' and entry['size']>4294967295:entry['conflicts'].append('fat32_file_limit')
    estimated=sum(entry['size'] for entry in entries)+sum(companions.values())
    required=0 if operation=='move' else estimated
    reserve=16*1024*1024 if operation=='move' else max(100*1024*1024,max([entry['size'] for entry in entries]+list(companions.values()),default=0),int(required*.05))
    return {'filesystem':volume['filesystem'],'read_only':volume['read_only'],'free':free,'companions_bytes':sum(companions.values()),'required':required,'data_bytes':estimated,'operation':operation,'reserve':reserve,'sufficient':free is not None and free>=required+reserve}


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
            c.execute('CREATE TABLE IF NOT EXISTS storage_root_proofs(device_id TEXT PRIMARY KEY,identity TEXT,file_id TEXT,root TEXT,recorded REAL)')
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

    def register(self, raw, alias, new_location=False):
        if not isinstance(raw, str) or not raw.strip() or not isinstance(alias, str) or not 1 <= len(alias.strip()) <= 100:
            raise ValueError('请填写目录和1–100字设备别名')
        if type(new_location) is not bool:raise ValueError('新位置确认无效')
        root = no_links(Path(raw).expanduser())
        if is_archive_path(root,archive_roots(self.db)):raise ValueError('归档目录不能作为默认盘点位置，请按任务恢复文件')
        if not root.is_dir():
            raise ValueError('盘点目录不存在或离线')
        identity, stable = volume_identity(root)
        file_id=root_file_id(root)
        # Same physical disk may have several independent scan roots.
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            existing=c.execute('SELECT d.*,p.file_id AS root_file_id FROM storage_devices d LEFT JOIN storage_root_proofs p ON p.device_id=d.id WHERE d.root=? ORDER BY d.updated DESC,d.id',(str(root),)).fetchall()
            old=next((row for row in existing if row['identity']==identity and (row['root_file_id']==file_id if row['root_file_id'] else not new_location)),None)
            if existing and not old and not new_location:raise ValueError('设备或目录身份变化；请明确登记为新位置，旧快照会保留')
            device_id = old['id'] if old else uuid.uuid4().hex
            c.execute('INSERT OR REPLACE INTO storage_devices VALUES(?,?,?,?,?,?)', (device_id, identity, str(root), alias.strip(), int(stable), time.time()))
            if not old:c.execute('INSERT INTO storage_root_proofs VALUES(?,?,?,?,?)',(device_id,identity,file_id,str(root),time.time()))
        return device_id

    def devices(self,device_id=None):
        with self.db() as c:
            rows = [dict(r) for r in c.execute('SELECT * FROM storage_devices'+(' WHERE id=?' if device_id is not None else '')+' ORDER BY alias',(device_id,) if device_id is not None else ())]
            proofs={r['device_id']:dict(r) for r in c.execute('SELECT * FROM storage_root_proofs')}
        if device_id is not None and not rows:raise ValueError('未知盘点设备')
        for row in rows:
            root=Path(row['root']);row.update(online=False,status='offline',capacity=None,volume={'label':'','filesystem':'unknown','read_only':None},directory_verified=False)
            proof=proofs.get(row['id'])
            try:
                file_id=root_file_id(root)
                if root.is_symlink() or getattr(root.lstat(),'st_file_attributes',0)&1024:row['status']='linked'
                elif volume_identity(root)[0]!=row['identity']:row['status']='identity_changed'
                elif proof and proof['file_id'] and proof['file_id']!=file_id:row['status']='directory_changed'
                else:
                    row.update(online=True,status='online',directory_verified=bool(proof and proof['file_id']))
                    capacity=shutil.disk_usage(root);row['capacity']={'total':capacity.total,'free':capacity.free,'used':capacity.used};row['volume']=volume_details(root)
            except FileNotFoundError:pass
            except PermissionError:row.update(online=False,status='permission_denied')
            except OSError:row.update(online=False,status='unavailable')
            except ValueError:row.update(online=False,status='not_directory')
            row['identity_note']='卷及目录身份已记录' if row['stable'] and proof and proof['file_id'] else '卷身份已记录；旧登记无目录证明，重连需人工核对' if row['stable'] else '位置标识，跨设备或挂载变化需人工核对'
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

    def verify_root(self,device):
        root=no_links(device['root'])
        if volume_identity(root)[0]!=device['identity']:raise DeviceChangedError('设备身份不匹配，请连接原设备')
        try:file_id=root_file_id(root)
        except (FileNotFoundError,NotADirectoryError) as error:raise DeviceChangedError('原盘点目录已离线，请重连后重试') from error
        with self.db() as c:proof=c.execute('SELECT * FROM storage_root_proofs WHERE device_id=?',(device['id'],)).fetchone()
        if proof and proof['file_id'] and proof['file_id']!=file_id:raise DirectoryChangedError('盘点目录身份已变化，请保留旧快照并登记新位置')
        return file_id

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

    def _remap_preview(self,c,device_id,raw):
        if not isinstance(raw,str) or not raw.strip():raise ValueError('请选择新的挂载目录')
        row=c.execute('SELECT * FROM storage_devices WHERE id=?',(device_id,)).fetchone()
        if not row:raise ValueError('未知盘点设备')
        device=dict(row);root=no_links(Path(raw).expanduser())
        if is_archive_path(root,archive_roots(self.db)):raise ValueError('归档目录不能作为盘点位置')
        if not root.is_dir():raise ValueError('映射位置不存在或离线')
        if c.execute("SELECT 1 FROM inventory_snapshots WHERE device_id=? AND state='running'",(device_id,)).fetchone():raise ValueError('正在盘点，请停止后再重连')
        identity,stable=volume_identity(root)
        if device['stable'] and identity!=device['identity']:raise ValueError('卷身份不匹配，不能绑定旧快照')
        file_id=root_file_id(root);proof=c.execute('SELECT * FROM storage_root_proofs WHERE device_id=?',(device_id,)).fetchone()
        matches=bool(proof and proof['identity']==identity and proof['file_id'] and proof['file_id']==file_id)
        if device['stable'] and proof and proof['file_id'] and not matches:raise ValueError('卷相同但目录身份不匹配，不能绑定旧快照')
        conflict=c.execute("SELECT d.id FROM storage_devices d LEFT JOIN storage_root_proofs p ON p.device_id=d.id WHERE d.root=? AND d.identity=? AND d.id!=? AND (p.file_id IS NULL OR p.file_id='' OR p.file_id=?)",(str(root),identity,device_id,file_id)).fetchone()
        if conflict:raise ValueError('此目录已有独立盘点登记，请保留各自快照，不合并位置')
        candidate={'device':device,'root':str(root),'identity':identity,'stable':bool(stable),'file_id':file_id,'proof':dict(proof) if proof else None}
        latest=c.execute('SELECT * FROM inventory_snapshots WHERE device_id=? ORDER BY created DESC LIMIT 1',(device_id,)).fetchone()
        candidate['snapshot']=dict(latest) if latest else None
        digest=hashlib.sha256(json.dumps(candidate,sort_keys=True).encode()).hexdigest()
        count=c.execute('SELECT COUNT(*) FROM inventory_files WHERE device_id=? AND snapshot=(SELECT id FROM inventory_snapshots WHERE device_id=? AND state IN (\'done\',\'partial\') ORDER BY created DESC LIMIT 1)',(device_id,device_id)).fetchone()[0]
        return {'id':device_id,'alias':device['alias'],'old_root':device['root'],'root':str(root),'identity':identity,'previous_identity':device['identity'],'stable':bool(stable),'directory_matches':matches,'manual_required':not device['stable'] or not matches,'digest':digest,'snapshot_files':count},candidate

    def remap_preview(self,device_id,raw):
        with self.db() as c:return self._remap_preview(c,device_id,raw)[0]

    def remap(self, device_id, raw, confirm_identity,expected_digest=None,manual_confirmed=False):
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE');preview,candidate=self._remap_preview(c,device_id,raw)
            if confirm_identity!=preview['previous_identity']:raise ValueError('请核对原设备身份后确认映射')
            if expected_digest is not None and expected_digest!=preview['digest']:raise ValueError('重连范围或目录已变化，请重新审阅')
            if preview['manual_required'] and manual_confirmed is not True:raise ValueError('缺少可靠目录证明，请单独确认已人工核对原位置')
            c.execute('UPDATE storage_devices SET root=?,identity=?,stable=?,updated=? WHERE id=?',(candidate['root'],candidate['identity'],int(candidate['stable']),time.time(),device_id))
            c.execute('INSERT OR REPLACE INTO storage_root_proofs VALUES(?,?,?,?,?)',(device_id,candidate['identity'],candidate['file_id'],candidate['root'],time.time()))
        return self.device(device_id)

    def scan(self, device_id, cancel=lambda: False, progress=lambda **_: None, hashing=False):
        device = self.device(device_id); root = Path(device['root'])
        if is_archive_path(root,archive_roots(self.db)):raise ValueError('归档目录不参与默认盘点，请按归档任务恢复')
        if not root.is_dir() or volume_identity(root)[0] != device['identity']:
            raise ValueError('设备离线或身份不匹配')
        initial_root_id=self.verify_root(device)
        def check_device():
            try:matches=volume_identity(root)[0]==device['identity'] and root_file_id(root)==initial_root_id
            except OSError:matches=False
            if not matches:raise DeviceChangedError('盘点期间设备或目录已变化，旧快照保留')
        snapshot = uuid.uuid4().hex; processed = failed = processed_bytes = 0
        with self.db() as c:
            c.execute('INSERT INTO inventory_snapshots(id,device_id,state,processed,failed,created,finished) VALUES(?,?,?,?,?,?,?)', (snapshot, device_id, 'running', 0, 0, time.time(), None))
        excluded = [Path(p).resolve() for p in self.excluded()+archive_roots(self.db)]
        def allowed(path):
            return not any(canonical(path).is_relative_to(p) for p in excluded)
        state = 'done'
        try:
            def walk_error(error):
                nonlocal failed
                failed += 1
                with self.db() as c:c.execute('INSERT INTO inventory_errors VALUES(?,?,?)',(snapshot,'.',type(error).__name__))
            for base, directories, names in os.walk(root, followlinks=False, onerror=walk_error):
                check_device()
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
                        check_device()
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
                    except DeviceChangedError:raise
                    except Exception as error:
                        check_device()
                        failed += 1
                        with self.db() as c:c.execute('INSERT INTO inventory_errors VALUES(?,?,?)',(snapshot,path.relative_to(root).as_posix(),type(error).__name__))
                if state == 'cancelled':break
            if state=='done':check_device()
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

    def plan(self, device_id, raw_target, layout='title',operation='copy',*,snapshot_id=None,relatives=None,persist=True):
        if operation not in ('copy','move','keep'):raise ValueError('未知整理操作')
        keeping=operation=='keep'
        if layout not in ('title', 'preserve', 'version', 'episode'):
            raise ValueError('未知整理模板')
        if not keeping and (not isinstance(raw_target,str) or not raw_target.strip()):raise ValueError('请填写独立目标目录')
        device = self.device(device_id); source = Path(device['root'])
        try:self.verify_root(device);root_matches=True
        except (ValueError,OSError):root_matches=False
        target = source if keeping else no_links(raw_target)
        if operation=='copy' and (source.is_relative_to(target) or target.is_relative_to(source)):
            raise ValueError('目标必须与源目录相互独立')
        with self.db() as c:
            latest = c.execute("SELECT * FROM inventory_snapshots WHERE device_id=? AND state IN ('done','partial')"+(' AND id=?' if snapshot_id else '')+' ORDER BY created DESC LIMIT 1', (device_id,snapshot_id) if snapshot_id else (device_id,)).fetchone()
            if not latest:raise ValueError('先完成一次盘点')
            rows = [dict(r) for r in c.execute('SELECT * FROM inventory_files WHERE device_id=? AND snapshot=? ORDER BY relative_path', (device_id, latest['id']))]
        if relatives is not None:
            allowed=set(relatives);rows=[row for row in rows if row['relative_path'] in allowed]
            if len(rows)!=len(allowed):raise ValueError('固定盘点中的所选条目已失效')
        entries = []; destinations = set();path_cache={}
        allowed_sources={owned_path(source/row['relative_path']) for row in rows} if operation=='move' else set()
        for row in rows:
            metadata = self.effective_metadata(row); relative = Path(row['relative_path'])
            title = metadata.get('organization_annotation',{}).get('title') or metadata.get('title') or relative.parent.name or relative.stem
            destination = target/relative if layout == 'preserve' else target/safe_component(title)/safe_component(relative.name)
            if layout=='version':destination=target/safe_component(title)/safe_component(str(metadata.get('height') or 'unknown')+'p-'+str(metadata.get('codec') or 'unknown'))/safe_component(relative.name)
            if layout=='episode':destination=target/safe_component(metadata.get('relationship',{}).get('collection') or relative.parent.name or title)/('Episode '+str(metadata.get('relationship',{}).get('number') or 'unknown').zfill(2))/safe_component(relative.name)
            if keeping:destination=source/relative
            conflicts=[] if keeping else destination_conflicts(destination,destinations,allowed=allowed_sources,cache=path_cache)
            try:
                stamp=(source/relative).stat()
                if (stamp.st_size,stamp.st_mtime_ns)!=(row['size'],row['mtime_ns']):conflicts.append('source_changed')
                if not root_matches or volume_identity(source)[0]!=device['identity']:conflicts.append('device_mismatch')
            except OSError:conflicts.append('source_unavailable')
            entries.append({'relative': row['relative_path'], 'source':str(source/relative), 'destination': str(destination), 'size': row['size'], 'mtime_ns': row['mtime_ns'], 'digest': row['digest'], 'operation': 'keep_proposal_only' if keeping else 'copy_proposal_only', 'conflicts': conflicts,
                            'reason': 'keep_original_location' if keeping else 'preserve_original' if layout=='preserve' else 'metadata_or_directory_title', 'metadata_source':metadata.get('source','unknown'), 'needs_review': True, 'source_metadata':metadata, 'companions': json.loads(row['companions'])})
        space={'required':0,'reserve':0,'sufficient':True,'files_unchanged':True} if keeping else target_space(entries,target,operation)
        plan_id = uuid.uuid4().hex
        payload = {'entries': entries, 'bytes': sum(r['size'] for r in rows), 'source_unchanged': not any(any(issue in entry['conflicts'] for issue in ('source_changed','source_unavailable','device_mismatch')) for entry in entries), 'source_root':str(source),'device_id':device_id,'device_alias':device['alias'],'source_device_identity':device['identity'], 'snapshot_created':latest['created'], 'executable': False, 'layout': layout,'space':space}
        payload['operation']='keep' if keeping else 'relocate'
        payload['requested_operation']=operation
        payload['digest'] = hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
        if persist:
            with self.db() as c:c.execute('INSERT INTO organization_plans VALUES(?,?,?,?,?,?,?)', (plan_id, device_id, str(target), latest['id'], 'preview_only', json.dumps(payload), time.time()))
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
        if payload.get('operation')=='keep':raise ValueError('原位保留不修改目标路径，请返回规则页更换操作')
        if payload['digest']!=expected_digest:raise ValueError('方案已变化，请重新载入')
        if not isinstance(selected,list) or not selected or any(type(i) is not int or not 0<=i<len(payload['entries']) for i in selected):raise ValueError('选择条目无效')
        edits=edits or {}
        if not isinstance(edits,dict):raise ValueError('目标修改无效')
        root=Path(row['target']);entries=[];destinations=set();path_cache={}
        allowed_sources={owned_path(payload['entries'][i]['source']) for i in set(selected)} if payload.get('requested_operation')=='move' else set()
        for i in sorted(set(selected)):
            entry=dict(payload['entries'][i]);replacement=edits.get(str(i))
            if replacement is not None:
                if not isinstance(replacement,str) or not replacement:raise ValueError('目标相对路径无效')
                relative=Path(replacement)
                if relative.is_absolute() or relative.drive or '..' in relative.parts:raise ValueError('目标路径越界')
                target=root/relative
                if not target.resolve().is_relative_to(root.resolve()):raise ValueError('目标路径越界')
                entry['destination']=str(target)
            entry['conflicts']=destination_conflicts(Path(entry['destination']),destinations,allowed=allowed_sources,cache=path_cache)
            device=self.device(entry.get('device_id',row['device_id']));source=Path(device['root'])/entry['relative'];entry['source']=str(source)
            expected=entry.get('source_device_identity',payload.get('source_device_identity'))
            if device['identity']!=expected or (entry.get('source_root') and device['root']!=entry['source_root']):entry['conflicts'].append('device_mismatch')
            try:
                stat=source.stat()
                if (stat.st_size,stat.st_mtime_ns)!=(entry['size'],entry['mtime_ns']):entry['conflicts'].append('source_changed')
            except OSError:entry['conflicts'].append('source_unavailable')
            entries.append(entry)
        updated=dict(payload,entries=entries,bytes=sum(e['size'] for e in entries),parent_plan_id=plan_id)
        updated['space']=target_space(entries,root,payload.get('requested_operation','copy'))
        if payload.get('selection'):updated['selection']=dict(payload['selection'],scope='revised',origin_total=payload['selection']['total'],total=len(entries),bytes=updated['bytes'])
        updated['source_unchanged']=not any(any(issue in entry['conflicts'] for issue in ('source_changed','source_unavailable','device_mismatch')) for entry in entries)
        updated['source_root']=payload['source_root'] if payload.get('source_devices') else self.device(row['device_id'])['root']
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
