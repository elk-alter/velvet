"""Persistent library relationships and reversible application-data maintenance."""
import hashlib,json,re,sqlite3,time,zipfile,tempfile,threading
from contextlib import closing
from restore_safety import require_reconciled_files
from pathlib import Path
class LibraryCore:
 def __init__(self,db,data,presentation):
  self.db,self.data,self.present=db,data,presentation
  with db() as c:
   c.execute('CREATE TABLE IF NOT EXISTS media_links(video_id INTEGER PRIMARY KEY,movie_key TEXT,version_key TEXT,part_no INTEGER,manual INTEGER DEFAULT 0)')
   c.execute('CREATE TRIGGER IF NOT EXISTS media_link_forget AFTER DELETE ON videos BEGIN DELETE FROM media_links WHERE video_id=OLD.id; END')
   c.execute('CREATE TABLE IF NOT EXISTS movie_preferences(movie_key TEXT PRIMARY KEY,version_key TEXT)')
   pref_fields={r[1] for r in c.execute('PRAGMA table_info(movie_preferences)')}
   if 'preferred_id' not in pref_fields:c.execute('ALTER TABLE movie_preferences ADD COLUMN preferred_id INTEGER')
   c.execute('CREATE TABLE IF NOT EXISTS duplicate_preference_history(group_key TEXT PRIMARY KEY,payload TEXT)')
   c.execute('CREATE TABLE IF NOT EXISTS relationship_undo(id INTEGER PRIMARY KEY,created REAL,payload TEXT)')
   c.execute('CREATE TABLE IF NOT EXISTS app_state(name TEXT PRIMARY KEY,value TEXT)')
 def infer(self,row):
  p=Path(row['path']);r=self.present(row);code=row.get('code','').strip().upper()
  stem=re.sub(r'(?i)(?<![a-z0-9])(?:part|pt|cd|disc|disk)[ ._-]*\d{1,2}(?![a-z0-9])|第\s*\d{1,2}\s*(?:部分|段|集)','',p.stem)
  movie='code:'+code if code else 'local:'+hashlib.sha256((str(p.parent)+stem).casefold().encode()).hexdigest()[:20]
  version='v:'+hashlib.sha256((movie+'|'+str(p.parent).casefold()+'|'+stem.casefold()+'|'+str(row.get('height',0))+'|'+str(row.get('codec',''))+'|'+r['censorship']+'|'+str(r['vr'])).encode()).hexdigest()[:20]
  return movie,version,r['part_index'] or 1
 def reconcile(self):
  with self.db() as c:
   occupied=set()
   relation_paths={}
   if c.execute("SELECT 1 FROM sqlite_master WHERE name='media_assets'").fetchone() and 'relation_path' in {r[1] for r in c.execute('PRAGMA table_info(media_assets)')}:
    relation_paths={r['video_id']:r['relation_path'] for r in c.execute("SELECT video_id,relation_path FROM media_assets WHERE video_id IS NOT NULL AND relation_path!=''")}
   for raw in c.execute('SELECT * FROM videos ORDER BY id').fetchall():
    row=dict(raw);old=c.execute('SELECT * FROM media_links WHERE video_id=?',(row['id'],)).fetchone()
    if old and old['manual']:
     occupied.add((old['movie_key'],old['version_key'],old['part_no']));continue
    inference=dict(row,path=relation_paths.get(row['id'],row['path']))
    movie,version,part=self.infer(inference)
    if (movie,version,part) in occupied:version+=':copy:'+str(row['id'])
    occupied.add((movie,version,part))
    if old and (old['movie_key'],old['version_key'],old['part_no'])==(movie,version,part):continue
    c.execute('INSERT INTO media_links VALUES(?,?,?,?,0) ON CONFLICT(video_id) DO UPDATE SET movie_key=excluded.movie_key,version_key=excluded.version_key,part_no=excluded.part_no',(row['id'],movie,version,part))
 def hierarchy(self):
  self.reconcile()
  with self.db() as c:
   links={r['video_id']:dict(r) for r in c.execute('SELECT * FROM media_links')}
   pref_rows=[dict(r) for r in c.execute('SELECT * FROM movie_preferences')];prefs={r['movie_key']:r['version_key'] for r in pref_rows};preferred_ids={r['movie_key']:r['preferred_id'] for r in pref_rows}
   choices=[r[0] for r in c.execute('SELECT preferred_id FROM duplicate_choices')]
   rows=[self.present(r) for r in c.execute('SELECT * FROM videos ORDER BY mtime DESC')]
  excluded=getattr(self,'excluded',lambda:[])()
  rows=[r for r in rows if not any(Path(r['path']).is_relative_to(Path(root)) for root in excluded)]
  rows=getattr(self,'filter_rows',lambda rows:rows)(rows)
  movies={}
  for row in rows:
   link=links[row['id']];row.update(movie_key=link['movie_key'],version_key=link['version_key'],part_index=link['part_no'],relation_manual=link['manual'])
   movies.setdefault(link['movie_key'],{}).setdefault(link['version_key'],[]).append(row)
  result=[]
  for movie,versions in movies.items():
   outputs=[]
   for key,parts in versions.items():
    parts.sort(key=lambda p:(p['part_index'],p['id']));first=next((p for p in parts if not p['missing']),parts[0])
    # Resume the first unfinished part with progress, otherwise first unfinished part.
    playable=[p for p in parts if not p['missing']]
    resume=next((p for p in playable if not p['watched'] and p['position']>0),next((p for p in playable if not p['watched']),first))
    label=' / '.join(dict.fromkeys(p['resolution_label'] for p in parts))+' · '+first['censorship_label']+' · '+(first['codec'] or '未知编码')
    v=dict(first,resolution_label=' / '.join(dict.fromkeys(p['resolution_label'] for p in parts)),height=min(p['height'] or 0 for p in parts),_parts=parts,version_key=key,version_label=label,resume_id=resume['id'],duration=sum(p['duration'] or 0 for p in parts),size=sum(p['size'] or 0 for p in parts),watched=all(p['watched'] for p in parts),position=max(p['position'] for p in parts),missing=all(p['missing'] for p in parts),preferred=any(p['id'] in choices for p in parts))
    outputs.append(v)
   outputs.sort(key=lambda v:(v['missing'],not v['preferred'],-v['height'],-v['size']))
   preferred=next((v for v in outputs if any(p['id']==preferred_ids.get(movie) for p in v['_parts']) and not v['missing']),next((v for v in outputs if v['version_key']==prefs.get(movie) and not v['missing']),outputs[0]))
   # Per-file/part metadata stays on versions; never sum alternative-version durations.
   allparts=[p for v in outputs for p in v['_parts']]
   result.append(dict(preferred,movie_key=movie,_versions=outputs,id=preferred['resume_id'],favorite=any(p['favorite'] for p in allparts),mtime=max(p['mtime'] for p in allparts),part_search=' '.join(p['path'] for p in allparts)))
  return result
 def relation(self,changes):
  if not isinstance(changes,list) or not 1<=len(changes)<=100:raise ValueError('请选择 1–100 个文件')
  self.reconcile()
  with self.db() as c:
   before=[]
   for change in changes:
    vid=int(change['id']);old=c.execute('SELECT * FROM media_links WHERE video_id=?',(vid,)).fetchone()
    if not old:raise ValueError('文件不存在')
    part=int(change.get('part_no',1));movie=str(change['movie_key']).strip();version=str(change['version_key']).strip()
    if not movie or not version or not 1<=part<=999:raise ValueError('影片/版本名称或分段序号无效')
    snapshot=dict(old)
    if c.execute("SELECT 1 FROM sqlite_master WHERE name='media_assets'").fetchone():
     asset=c.execute('SELECT id,relation_path FROM media_assets WHERE video_id=?',(vid,)).fetchone()
     if asset:snapshot.update(asset_id=asset['id'],relation_path=asset['relation_path'])
    before.append(snapshot);c.execute('UPDATE media_links SET movie_key=?,version_key=?,part_no=?,manual=1 WHERE video_id=?',(movie,version,part,vid))
   # Reject ambiguous ordering within affected versions.
   for key in {x['version_key'] for x in changes}:
    conflict=c.execute('SELECT part_no,COUNT(*) FROM media_links WHERE version_key=? GROUP BY movie_key,part_no HAVING COUNT(*)>1',(key,)).fetchone()
    if conflict:raise ValueError('同一版本中有重复分段序号，请分为不同版本或重新排序')
   c.execute('INSERT INTO relationship_undo(created,payload) VALUES(?,?)',(time.time(),json.dumps(before)))
 def reset(self,ids):
  with self.db() as c:
   before=[]
   for vid in ids:
    row=c.execute('SELECT * FROM media_links WHERE video_id=?',(int(vid),)).fetchone()
    if row:
     snapshot=dict(row)
     if c.execute("SELECT 1 FROM sqlite_master WHERE name='media_assets'").fetchone():
      asset=c.execute('SELECT id,relation_path FROM media_assets WHERE video_id=?',(int(vid),)).fetchone()
      if asset:
       snapshot['asset_id']=asset['id'];snapshot['relation_path']=asset['relation_path']
       c.execute('UPDATE media_assets SET relation_path=(SELECT path FROM videos WHERE id=?) WHERE id=?',(int(vid),asset['id']))
     before.append(snapshot);c.execute('UPDATE media_links SET manual=0 WHERE video_id=?',(int(vid),))
   c.execute('INSERT INTO relationship_undo(created,payload) VALUES(?,?)',(time.time(),json.dumps(before)))
  self.reconcile()
 def undo(self):
  with self.db() as c:
   last=c.execute('SELECT * FROM relationship_undo ORDER BY id DESC LIMIT 1').fetchone()
   if not last:raise ValueError('没有可撤销的关系修改')
   for row in json.loads(last['payload']):
    if not c.execute('SELECT 1 FROM videos WHERE id=?',(row['video_id'],)).fetchone():continue
    if 'asset_id' in row:
     asset=c.execute('SELECT id FROM media_assets WHERE video_id=?',(row['video_id'],)).fetchone()
     if not asset or asset['id']!=row['asset_id']:continue
     c.execute('UPDATE media_assets SET relation_path=? WHERE id=?',(row['relation_path'],row['asset_id']))
    c.execute('INSERT OR REPLACE INTO media_links VALUES(?,?,?,?,?)',tuple(row[k] for k in ['video_id','movie_key','version_key','part_no','manual']))
   c.execute('DELETE FROM relationship_undo WHERE id=?',(last['id'],))
 def prefer(self,vid):
  self.reconcile()
  with self.db() as c:
   row=c.execute('SELECT * FROM media_links WHERE video_id=?',(int(vid),)).fetchone()
   if not row:raise ValueError('文件不存在')
   c.execute('INSERT INTO movie_preferences(movie_key,version_key,preferred_id) VALUES(?,?,?) ON CONFLICT(movie_key) DO UPDATE SET version_key=excluded.version_key,preferred_id=excluded.preferred_id',(row['movie_key'],row['version_key'],int(vid)))
 def backup(self,prefix='manual'):
  target=self.data/'backups';target.mkdir(exist_ok=True)
  name=prefix+'-'+time.strftime('%Y%m%d-%H%M%S')+'-'+str(time.time_ns()%1000000)+'.zip';out=target/name
  with tempfile.TemporaryDirectory(dir=self.data) as temp:
   dbpath=Path(temp)/'library.db'
   with self.db() as source:
    with closing(sqlite3.connect(dbpath)) as dest:source.backup(dest)
   with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
    z.write(dbpath,'library.db');z.writestr('manifest.json',json.dumps({'schema':1,'version':Path(__file__).with_name('VERSION').read_text('utf-8-sig').strip(),'created':time.time()}))
    for file in ('settings.json','tag_rules.json'):
     p=self.data/file
     if p.exists():z.write(p,file)
  return name
 def backups(self):
  target=self.data/'backups';return [{'name':p.name,'size':p.stat().st_size,'created':p.stat().st_mtime} for p in sorted(target.glob('*.zip'),key=lambda p:p.stat().st_mtime,reverse=True)]
 def validate_backup(self,name):
  if Path(name).name!=name or not name.endswith('.zip'):raise ValueError('备份名称无效')
  path=self.data/'backups'/name
  with zipfile.ZipFile(path) as z:
   if z.getinfo('library.db').file_size>512_000_000:raise ValueError('备份数据库过大')
   manifest=json.loads(z.read('manifest.json'))
   if manifest.get('schema')!=1:raise ValueError('不支持的备份版本')
   with tempfile.TemporaryDirectory(dir=self.data) as temp:
    p=Path(temp)/'check.db';p.write_bytes(z.read('library.db'))
    with closing(sqlite3.connect(p)) as c:
     if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('备份校验失败')
     tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
     if not {'videos','media_links','movie_preferences','actor_preferences'}.issubset(tables):raise ValueError('备份缺少必要资料表')
     required={'videos':{'id','path','code','locked_fields','amane_snapshot','position'},'media_links':{'video_id','movie_key','version_key','part_no','manual'},'movie_preferences':{'movie_key','version_key'},'actor_preferences':{'name','rating','favorite'}}
     for table,columns in required.items():
      if not columns.issubset({r[1] for r in c.execute('PRAGMA table_info('+table+')')}):raise ValueError('备份表结构不兼容：'+table)
   for filename in ('settings.json','tag_rules.json'):
    if filename in z.namelist():
     value=json.loads(z.read(filename))
     if not isinstance(value,dict if filename=='settings.json' else list):raise ValueError('备份配置格式错误')
  return path
 def restore(self,name):
  with self.db() as c:require_reconciled_files(c)
  path=self.validate_backup(name);rollback=self.backup('before-restore')
  with zipfile.ZipFile(path) as z,tempfile.TemporaryDirectory(dir=self.data) as temp:
   p=Path(temp)/'restore.db';p.write_bytes(z.read('library.db'))
   with closing(sqlite3.connect(p)) as source:
    require_reconciled_files(source)
    with self.db() as dest:source.backup(dest)
   for filename in ('settings.json','tag_rules.json'):
    if filename in z.namelist():(self.data/filename).write_bytes(z.read(filename))
    else:(self.data/filename).unlink(missing_ok=True)
  self.__init__(self.db,self.data,self.present)
  return rollback
