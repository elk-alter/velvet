import os,json,sqlite3,threading,subprocess,shutil,time,webbrowser,sys,re,mimetypes,secrets
import xml.etree.ElementTree as ET
import base64,hmac,hashlib,socket,datetime
from urllib.error import HTTPError
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.parse import urlparse,quote,parse_qs
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from media_naming import fallback_title, generic_name
from runtime_config import load_runtime, diagnostics
RUNTIME=load_runtime(sys.argv[1:] if __name__=='__main__' else ())
ROOT=Path(__file__).resolve().parent
APP_VERSION=(ROOT/'VERSION').read_text('utf-8-sig').strip()
UI_ASSETS={
 '/':('index.html','text/html'),
 '/management.js':('management.js','text/javascript'),
 '/disk_manager.js':('disk_manager.js','text/javascript'),
 '/disk_selection.js':('disk_selection.js','text/javascript'),
 '/manager_routes.js':('manager_routes.js','text/javascript'),
 '/management.css':('management.css','text/css'),
 '/browse.js':('browse.js','text/javascript'),
 '/server_browse.js':('server_browse.js','text/javascript'),
 '/film_detail.js':('film_detail.js','text/javascript'),
 '/browse.css':('browse.css','text/css'),
 '/file_review.js':('file_review.js','text/javascript'),
}
DATA=Path(os.environ.get('VELVET_DATA',str(ROOT/'data'))).resolve();DATA.mkdir(parents=True,exist_ok=True)
if __name__=='__main__':
 from data_guard import DataGuard
 import atexit
 try:DATA_GUARD=DataGuard(DATA)
 except ValueError as error:raise SystemExit(str(error)) from None
 atexit.register(DATA_GUARD.close)
DB_REVISION=0
DB_MUTEX=threading.RLock()
SHUTTING_DOWN=threading.Event()
POST_LEASE=threading.Lock();ACTIVE_POSTS=0
SCAN_CANCEL=threading.Event();SYNC_CANCEL=threading.Event();DUP_CANCEL=threading.Event();RESTORING=threading.Event()
DB=DATA/'library.db'; LOCK=threading.Lock(); TOKEN=secrets.token_urlsafe(32)
STATUS={'running':False,'message':'准备就绪','processed':0,'errors':0}
NAS=os.environ.get('VELVET_SERVER','0')=='1'
BIND=RUNTIME.bind
USER=os.environ.get('VELVET_USER','admin'); PASSWORD=os.environ.get('VELVET_PASSWORD','')
DEFAULT={'interval':300,'online':False,'provider':'','api_key':'','amane_url':'http://127.0.0.1:18000','amane_token':'','tag_aliases':{},'auto_scrape':True,'scrape_engine':'external','scrape_proxy':'','scrape_sources':['javbus','jav321','fc2','fc2club','fc2ppvdb','freejavbt','airav','xcity'],'scrape_field_priority':{}}
class Connection(sqlite3.Connection):
 def __enter__(self):
  DB_MUTEX.acquire()
  try:return super().__enter__()
  except BaseException:DB_MUTEX.release();raise
 def __exit__(self,*args):
  global DB_REVISION
  if not args[0] and self.total_changes:DB_REVISION+=1
  try:return super().__exit__(*args)
  finally:self.close();DB_MUTEX.release()
def db():
 c=sqlite3.connect(DB,timeout=30,factory=Connection);c.row_factory=sqlite3.Row;return c
def config():
 try:return DEFAULT|json.loads((DATA/'settings.json').read_text('utf-8'))
 except (OSError,ValueError):return DEFAULT.copy()
with db() as c:
 c.execute('PRAGMA journal_mode=WAL')
 c.execute('CREATE TABLE IF NOT EXISTS videos(id INTEGER PRIMARY KEY AUTOINCREMENT,path TEXT UNIQUE,title TEXT,size INTEGER,mtime REAL,duration REAL DEFAULT 0,favorite INTEGER DEFAULT 0,tags TEXT DEFAULT "",error TEXT DEFAULT "")')
 c.execute('CREATE TABLE IF NOT EXISTS folders(path TEXT PRIMARY KEY)')
 c.execute('CREATE TABLE IF NOT EXISTS actor_preferences(name TEXT PRIMARY KEY,rating INTEGER DEFAULT 0,favorite INTEGER DEFAULT 0)')
 c.execute('CREATE TABLE IF NOT EXISTS duplicate_choices(group_key TEXT PRIMARY KEY,preferred_id INTEGER)')
 c.execute('CREATE TABLE IF NOT EXISTS scrape_jobs(id INTEGER PRIMARY KEY,code TEXT,remote_id INTEGER,status TEXT,error TEXT DEFAULT "",details TEXT DEFAULT "",created REAL,updated REAL)')
 fields={r[1] for r in c.execute('PRAGMA table_info(videos)')}
 for name,definition in {'code':'TEXT DEFAULT ""','actors':'TEXT DEFAULT ""','studio':'TEXT DEFAULT ""','date':'TEXT DEFAULT ""','plot':'TEXT DEFAULT ""','width':'INTEGER DEFAULT 0','height':'INTEGER DEFAULT 0','codec':'TEXT DEFAULT ""','missing':'INTEGER DEFAULT 0','watched':'INTEGER DEFAULT 0','rating':'INTEGER DEFAULT 0','source':'TEXT DEFAULT "filename"','scraped':'REAL DEFAULT 0','manual':'INTEGER DEFAULT 0','censor_override':'TEXT DEFAULT "auto"','file_hash':'TEXT DEFAULT ""','hash_stamp':'TEXT DEFAULT ""','locked_fields':'TEXT DEFAULT "[]"','amane_snapshot':'TEXT DEFAULT "{}"','cover_origin':'TEXT DEFAULT ""','poster_origin':'TEXT DEFAULT ""','source_tags':'TEXT DEFAULT ""','amane_id':'INTEGER DEFAULT 0','auto_tags':'TEXT DEFAULT ""','tag_reasons':'TEXT DEFAULT "{}"','metadata_stamp':'TEXT DEFAULT ""','position':'REAL DEFAULT 0'}.items():
  if name not in fields:c.execute(f'ALTER TABLE videos ADD COLUMN {name} {definition}')
with db() as c:
 job_fields={r[1] for r in c.execute('PRAGMA table_info(scrape_jobs)')}
 if 'retry_kind' not in job_fields:c.execute("ALTER TABLE scrape_jobs ADD COLUMN retry_kind TEXT DEFAULT ''")
 if 'engine' not in job_fields:c.execute("ALTER TABLE scrape_jobs ADD COLUMN engine TEXT DEFAULT ''")
 if 'video_id' not in job_fields:c.execute('ALTER TABLE scrape_jobs ADD COLUMN video_id INTEGER')

from video_ids import ensure_video_ids
ensure_video_ids(db,DATA)

from media_libraries import MediaLibraries
from network_checks import NetworkChecks
from source_catalog import SourceCatalog,NATIVE_SOURCES
from people_catalog import PeopleCache,FacetCatalog,actor_key
from artwork_store import ArtworkStore
LIBRARIES=MediaLibraries(db)
from directory_policy import DirectoryPolicy
DIRECTORY_POLICY=DirectoryPolicy(db,LIBRARIES)
SOURCES=SourceCatalog(lambda path:external_amane_request(path),config)
NETWORK=NetworkChecks(config,lambda:external_amane_request('metadata?limit=1'),external_probe=lambda ids:external_amane_request('network/check',{'source_ids':ids} if ids else {}),external_sources=lambda:list(dict.fromkeys(s for route in SOURCES.snapshot()['upstream']['routes'].values() for s in route)))
PEOPLE=PeopleCache(db,DATA,config,lambda path:external_amane_request(path),lambda url:actor_image_bytes(url))
CATEGORIES=FacetCatalog(db,config)
CATEGORIES.filter_rows=LIBRARIES.filter_rows
ARTWORK=ArtworkStore(db,DATA,lambda url,target,engine,referer:download_still(url,target,engine,referer))
ARTWORK.allowed=lambda vid:video_included(vid)

def binary(name):
 found=shutil.which(name)
 if found:return found
 filename=name+('.exe' if os.name=='nt' else '')
 roots=[ROOT/'tools']
 if os.name=='nt':
  local=Path(os.environ.get('LOCALAPPDATA',''))
  alias=local/'Microsoft'/'WinGet'/'Links'/filename
  if alias.is_file():return str(alias)
  packages=local/'Microsoft'/'WinGet'/'Packages'
  if packages.exists():roots.extend(packages.glob('Gyan.FFmpeg*'))
 for root in roots:
  found=next((str(p) for p in root.glob('**/'+filename) if p.is_file()),None)
  if found:return found
 return None

def run(args,timeout=90):
 return subprocess.run(args,capture_output=True,timeout=timeout,creationflags=0x08000000 if os.name=='nt' else 0)
def identify(stem):
 m=re.search(r'(?i)(?<![a-z0-9])([a-z]{2,10})[-_ ](\d{2,7})(?!\d)',stem)
 return (m[1]+'-'+m[2]).upper() if m else ''
def censorship(row,metadata=None):
 override=row.get('censor_override','auto')
 if override in ('censored','uncensored','unknown'):return override,'手动指定'
 p=Path(row['path']);text=p.stem+' '+p.parent.name
 uncensored=bool(re.search(r'无码|無碼|无修正|無修正|无码破解|無碼破解|(?i:\buncensored\b)',text))
 censored=bool(re.search(r'有码|有碼|(?i:\bcensored\b)',text))
 if uncensored and censored:return 'unknown','文件名或目录标记冲突，请手动确认'
 if uncensored:return 'uncensored','文件名/目录明确标记'
 if censored:return 'censored','文件名/目录明确标记'
 if metadata is None:
  try:metadata=json.loads(row.get('amane_snapshot') or '{}')
  except ValueError:metadata={}
 kind=metadata.get('content_type')
 if kind in ('censored','uncensored'):return kind,'Amane 内容类型（未验证实际画面）'
 return 'unknown','缺少可靠资料，请手动确认'

def presentation(row):
 row=dict(row);p=Path(row['path'])
 if row.get('source') in ('','filename') and not row.get('manual') and not row.get('amane_id') and 'title' not in json.loads(row.get('locked_fields') or '[]') and generic_name(row.get('title')):row['title']=fallback_title(p)
 pattern=r'(?i)(?<![a-z0-9])(?:part|pt|cd|disc|disk)[ ._-]*(\d{1,2})(?![a-z0-9])|第\s*(\d{1,2})\s*(?:部分|段|集)'
 match=re.search(pattern,p.stem)
 row['part_index']=int(match[1] or match[2]) if match else 0
 base=re.sub(pattern,' ',p.stem);base=re.sub(r'[ ._\-]+',' ',base).strip().casefold()
 row['part_key']=str(p.parent).casefold()+'|'+base if match else ''
 try:metadata=json.loads(row.get('amane_snapshot') or '{}')
 except ValueError:metadata={}
 named=bool(re.search(r'(?i)(?<![a-z])vr(?![a-z])|虚拟现实|虛擬實境',p.stem+' '+p.parent.name))
 row['vr']=bool(metadata.get('vr')) or named
 row['vr_source']='Amane' if metadata.get('vr') else '文件名/目录' if named else ''
 row['censorship'],row['censorship_source']=censorship(row,metadata)
 row['censorship_label']={'censored':'有码','uncensored':'无码','unknown':'未知'}[row['censorship']]
 h=int(row.get('height') or 0)
 row.pop('amane_snapshot',None)
 row['resolution_label']='8K' if h>=4320 else '4K' if h>=2160 else '1440p' if h>=1440 else '1080p' if h>=1080 else '720p' if h>=720 else str(h)+'p' if h else '画质待解析'
 return row

sys.path.insert(0,str(ROOT))
from resource_leases import ResourceLeases,ResourceBusy,path_resource,video_resources
LEASES=ResourceLeases(db)
ARTWORK.leases=LEASES
if __name__=='__main__':LEASES.recover()
from library_core import LibraryCore
CORE=LibraryCore(db,DATA,presentation)
CORE.excluded=LIBRARIES.exclusions
CORE.filter_rows=LIBRARIES.filter_rows
from file_review import FileReview
REVIEW=FileReview(db,binary,DATA)
REVIEW.leases=LEASES
REVIEW.excluded=LIBRARIES.exclusions
REVIEW.allowed=lambda row:bool(LIBRARIES.filter_rows([row]))
from task_journal import TaskJournal
JOURNAL=TaskJournal(db)
ARTWORK.observe=lambda vid,ordinal,state:JOURNAL.observe('images',str(vid)+':'+str(ordinal),state)
REVIEW.observe=lambda vid,state,result='':JOURNAL.observe('review',str(vid)+':',('failed' if state=='done' and result not in ('passed','') else state),{'reason':result or 'executor_state'})
from storage_inventory import StorageInventory
INVENTORY=StorageInventory(db,lambda path:inventory_metadata(path),LIBRARIES.exclusions)
from organization_service import OrganizationService
ORGANIZATION=OrganizationService(db,INVENTORY)
from inventory_query import InventoryQuery,InventoryQueryChanged
INVENTORY_QUERY=InventoryQuery(INVENTORY)
from library_query import LibraryQuery,LibraryQueryChanged
BROWSE=LibraryQuery(db,CORE,LIBRARIES,DIRECTORY_POLICY,config)
with db() as c:c.execute("UPDATE videos SET locked_fields=?,manual=0 WHERE manual=1 AND locked_fields='[]'",(json.dumps(['title','code','actors','studio','date','plot']),))
WORK_STATUS={'sync':{'running':False,'phase':'idle'},'images':{'running':False,'failed':0},'scan':STATUS}

def read_metadata(p,nfo_path=None):
 result={'code':identify(p.stem) or identify(p.parent.name),'title':fallback_title(p),'source':'filename'}
 nfo=nfo_path or p.with_suffix('.nfo')
 if nfo.is_file():
  if nfo.is_symlink() or getattr(nfo.lstat(),'st_file_attributes',0)&1024:raise ValueError('NFO链接需要人工核对')
  if nfo.stat().st_size>2_000_000:raise ValueError('NFO 过大')
  tree=ET.fromstring(nfo.read_bytes())
  for target,tag in [('title','title'),('studio','studio'),('date','premiered'),('plot','plot'),('code','id')]:
   value=tree.findtext(tag)
   if value:result[target]=value
  result['code']=tree.findtext('num') or tree.findtext('id') or result['code']
  result['actors']=', '.join(x.text for x in tree.findall('actor/name') if x.text)
  result['tags']=', '.join(dict.fromkeys(x.text for x in tree.findall('tag')+tree.findall('genre') if x.text))
  result['source']='nfo'
 return result
def remote_metadata(code):
 cfg=config()
 if not cfg['online'] or not cfg['provider'] or not code:return {}
 url=cfg['provider'].replace('{code}',quote(code,safe=''))
 if urlparse(url).scheme!='https':raise ValueError('资料接口必须使用 HTTPS')
 headers={'Accept':'application/json','User-Agent':'Velvet/2'}
 if cfg['api_key']:headers['Authorization']='Bearer '+cfg['api_key']
 with urlopen(Request(url,headers=headers),timeout=15) as response:
  payload=response.read(2_000_001)
 if len(payload)>2_000_000:raise ValueError('接口响应过大')
 result=json.loads(payload)
 if not isinstance(result,dict) or str(result.get('code','')).upper()!=code.upper():raise ValueError('接口编号不一致，未自动采用')
 clean={k:str(result[k])[:10000] for k in ['code','title','studio','date','plot'] if result.get(k)}
 for k in ['actors','tags']:
  if k in result:clean[k]=', '.join(map(str,result[k])) if isinstance(result[k],list) else str(result[k])
 clean['source']='online';return clean

def classify(row):
 reasons={};aliases=config().get('tag_aliases',{})
 def add(tag,why):
  tag=str(tag).strip()[:80]
  if tag:reasons.setdefault(tag,why)
 for tag in re.split(r'[,，]',row.get('source_tags') or ''):
  if tag.strip():add('题材/'+aliases.get(tag.strip(),tag.strip()),'Amane 资料分类')
 status,why=censorship(row)
 add('制式/'+{'censored':'有码','uncensored':'无码','unknown':'未知'}[status],why)
 p=Path(row['path']);height=int(row.get('height') or 0);duration=float(row.get('duration') or 0)
 if height:add('画质/'+('4K' if height>=2160 else '1080p' if height>=1080 else '720p' if height>=720 else '标清'),'媒体分辨率')
 if duration:add('时长/'+('短片' if duration<1200 else '中片' if duration<3600 else '长片'),'媒体时长')
 if row.get('codec'):add('编码/'+row['codec'].upper(),'视频编码')
 if row.get('code'):add('资料/有编号','文件编号')
 if row.get('source') in ('nfo','online','amane'):add('资料/已匹配','NFO 或联网匹配')
 else:add('资料/待刮削','尚无 NFO 或联网匹配')
 # Only explicit folder names and bracketed labels, never infer intimate traits from images.
 if p.parent.name and p.parent.name.lower() not in {'videos','video','movies','media','downloads'}:add('目录/'+p.parent.name,'直接父目录')
 for label in re.findall(r'\[([^\[\]]{1,40})\]|【([^【】]{1,40})】',p.stem):
  value=label[0] or label[1]
  if not re.search(r'(?i)\d{3,4}[xp]|fps|\d{3,4}p',value):add('文件标签/'+value,'文件名括号标签')
 try:rules=json.loads((DATA/'tag_rules.json').read_text('utf-8'))
 except (OSError,ValueError):rules=[]
 text=' '.join(str(row.get(k,'') or '') for k in ['title','path','tags','actors','studio']).casefold()
 for rule in rules:
  if isinstance(rule,dict) and rule.get('tag') and any(str(word).casefold() in text for word in rule.get('keywords',[]) if str(word).strip()):add(rule['tag'],'自定义关键词规则')
 return ', '.join(reasons),json.dumps(reasons,ensure_ascii=False)

def retag_all():
 with db() as c:
  rows=LIBRARIES.filter_rows([dict(r) for r in c.execute('SELECT * FROM videos').fetchall()])
  for row in rows:
   tags,reasons=classify(dict(row));c.execute('UPDATE videos SET auto_tags=?,tag_reasons=? WHERE id=?',(tags,reasons,row['id']))
 return len(rows)

def scan(force=False,folder_path=None,automatic=False):
 if RESTORING.is_set() or SHUTTING_DOWN.is_set():return
 policies=[policy for policy in LIBRARIES.policies() if (not folder_path or policy['path']==folder_path) and (not automatic or policy['automation']!='off')]
 lease=LEASES.try_acquire('scan',resources=[path_resource(policy['path'],tree=True) for policy in policies] or ['metadata'])
 if lease is None:return
 try:
  if RESTORING.is_set() or SHUTTING_DOWN.is_set() or not LOCK.acquire(False):return
  try:task_key=JOURNAL.start('scan')
  except BaseException:LOCK.release();raise
  SCAN_CANCEL.clear()
  STATUS.update(running=True,message='扫描目录…',processed=0,errors=0)
  try:
   for policy in policies:
    folder=policy['path']
    if folder_path and folder!=folder_path:continue
    if automatic and policy['automation']=='off':continue
    if not Path(folder).is_dir():LIBRARIES.finish(folder,'目录离线或不可访问，保留现有记录');continue
    seen=set()
    walk_errors=[]
    for base,dirs,files in os.walk(folder,onerror=lambda error:walk_errors.append('目录读取失败')):
     dirs[:]=[d for d in dirs if not any((Path(base)/d).is_relative_to(Path(root)) for root in policy.get('excluded_directories',[]))]
     for name in files:
      if SCAN_CANCEL.is_set():STATUS['message']='扫描已取消（已完成项目保留）';return
      p=Path(base)/name
      if p.suffix.lower() not in {'.mp4','.mkv','.avi','.mov','.webm','.wmv','.m4v','.ts','.flv','.mpeg','.mpg'}:continue
      seen.add(str(p))
      try:
       stat=p.stat()
       if LIBRARIES.owner(p,policies)!=folder:continue
       if not LIBRARIES.included(p,policy,stat.st_size) or time.time()-stat.st_mtime<policy['settle_seconds']:continue
       if not LIBRARIES.filter_rows([{'path':str(p),'size':stat.st_size}]):continue
       alias=ORGANIZATION.locations.scan_alias(p,stat)
       if alias and alias['status']!='materialized':
        if alias['status']=='changed':STATUS['errors']+=1
        continue
       with db() as c:
        old=c.execute('SELECT * FROM videos WHERE path=?',(str(p),)).fetchone()
        c.execute('INSERT INTO videos(path,title,size,mtime) VALUES(?,?,?,?) ON CONFLICT(path) DO UPDATE SET size=excluded.size,mtime=excluded.mtime,missing=0',(str(p),p.stem,stat.st_size,stat.st_mtime))
        vid=c.execute('SELECT id FROM videos WHERE path=?',(str(p),)).fetchone()[0]
       unchanged=old and old['mtime']==stat.st_mtime and old['size']==stat.st_size
       sidecar=p.with_suffix('.nfo');stamp=str(sidecar.stat().st_mtime_ns) if sidecar.exists() else ''
       if unchanged and old['metadata_stamp']==stamp and not force and old['scraped'] and time.time()-old['scraped']<86400 and (not binary('ffmpeg') or (DATA/f'{vid}-5.jpg').exists()):continue
       STATUS['message']='正在整理：'+p.name
       err=[]
       try:meta=read_metadata(p)
       except Exception as e:
        meta={'code':identify(p.stem) or identify(p.parent.name),'title':fallback_title(p),'source':'filename'};err.append('NFO 读取失败：'+str(e))
       if meta['source']!='nfo':
        try:meta.update(remote_metadata(meta['code']))
        except Exception as e:err.append('联网匹配失败：'+str(e))
       probe=binary('ffprobe');ff=binary('ffmpeg');duration=old['duration'] if old else 0
       technical={}
       if probe and (force or not unchanged or not duration):
        r=run([probe,'-v','error','-show_format','-show_streams','-of','json',str(p)],45)
        if r.returncode:raise ValueError('无法解析视频')
        info=json.loads(r.stdout);duration=float(info.get('format',{}).get('duration',0));technical['duration']=duration
        stream=next((s for s in info.get('streams',[]) if s.get('codec_type')=='video'),{})
        technical.update(width=stream.get('width',0),height=stream.get('height',0),codec=stream.get('codec_name',''))
       if ff:
        for i in range(6):
         target=DATA/(f'{vid}.jpg' if i==0 else f'{vid}-{i}.jpg')
         if unchanged and not force and target.exists():continue
         r=run([ff,'-y','-loglevel','error','-ss',str(duration*(i+1)/8),'-i',str(p),'-frames:v','1','-vf','scale=640:-2',str(target)])
         if r.returncode:err.append('截图失败');break
       else:err.append('缺少 FFmpeg：截图与媒体信息不可用')
       # 本地同名海报和 Kodi 风格海报优先。
       cover=next((x for x in [p.with_suffix('.jpg'),p.with_name(p.stem+'-poster.jpg'),p.parent/'poster.jpg'] if x.is_file()),None)
       if cover and not (old and old['poster_origin']):shutil.copyfile(cover,DATA/f'{vid}-poster.jpg')
       if old and (old['manual'] or old['source']=='amane'):meta={}
       elif old:
        protected=set(json.loads(old['locked_fields'] or '[]'));meta={k:v for k,v in meta.items() if k not in protected}
       updates=meta|technical|{'scraped':time.time(),'metadata_stamp':stamp,'error':'; '.join(err)}
       if not LIBRARIES.filter_rows([{'path':str(p),'size':stat.st_size}]):continue
       merged=(dict(old) if old else {'path':str(p)})|updates
       tags,reasons=classify(merged);updates.update(auto_tags=tags,tag_reasons=reasons)
       with db() as c:c.execute('UPDATE videos SET '+','.join(k+'=?' for k in updates)+' WHERE id=?',[*updates.values(),vid])
       STATUS['processed']+=1
       if err:STATUS['errors']+=1
      except Exception as e:
       STATUS['errors']+=1
       with db() as c:c.execute('UPDATE videos SET error=? WHERE path=?',(str(e)[:500],str(p)))
    with db() as c:
     for row in LIBRARIES.filter_rows([dict(r) for r in c.execute('SELECT id,path,size FROM videos').fetchall()]):
      try:inside=Path(row['path']).is_relative_to(Path(folder))
      except ValueError:inside=False
      if inside and LIBRARIES.owner(row['path'],policies)==folder and row['path'] not in seen and not walk_errors and Path(folder).is_dir():c.execute('UPDATE videos SET missing=1 WHERE id=?',(row['id'],))
    LIBRARIES.finish(folder,'部分目录读取失败，未更新缺失标记' if walk_errors else '')
   STATUS['message']='扫描完成'
  finally:
   try:JOURNAL.observe('scan',task_key,'cancelled' if SCAN_CANCEL.is_set() else 'failed' if sys.exc_info()[0] else 'partial' if STATUS['errors'] else 'done',{'processed':STATUS['processed'],'failed':STATUS['errors']})
   finally:STATUS['running']=False;LOCK.release()
 finally:LEASES.release(lease)

AMANE_STATUS={'connected':False,'synced':0,'message':'等待连接','last_sync':0}
AMANE_LOCK=threading.Lock()
SUBMIT_LOCK=threading.Lock()
POLL_LOCK=threading.Lock()
BUILTIN_LOCK=threading.Lock()
BUILTIN_ENGINES={}

def builtin_engine():
 from builtin_scraper import Engine
 with BUILTIN_LOCK:
  key=str(DATA)
  if key not in BUILTIN_ENGINES:
   BUILTIN_ENGINES[key]=Engine(DATA,config);BUILTIN_ENGINES[key].leases=LEASES
  return BUILTIN_ENGINES[key]
def amane_headers():
 cfg=config();token=cfg.get('amane_token','')
 if not token:
  token_file=Path(os.environ.get('LOCALAPPDATA',''))/'Amane'/'token'
  if not token_file.is_file():raise ValueError('请在设置中填写 Amane Token')
  token=token_file.read_text('utf-8').strip()
 return {'Authorization':'Bearer '+token,'Content-Type':'application/json'}
def amane_base():
 base=config()['amane_url'].rstrip('/');parsed=urlparse(base)
 if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username:raise ValueError('Amane 地址格式无效')
 if parsed.scheme=='http' and parsed.hostname not in ('127.0.0.1','localhost','::1'):raise ValueError('远程 Amane 请使用 HTTPS')
 return base
def amane_request(path,data=None):
 if config().get('scrape_engine')=='builtin':return builtin_engine().request(path,data)
 return external_amane_request(path,data)

def external_amane_request(path,data=None):
 req=Request(amane_base()+'/api/'+path,headers=amane_headers(),data=json.dumps(data).encode() if data is not None else None)
 try:
  with urlopen(req,timeout=90 if path=='network/check' else 25) as r:return json.load(r)
 except HTTPError as e:
  if e.code in (401,403):raise ValueError('Amane 认证失败，请检查 Token') from None
  raise ValueError('Amane 接口错误 HTTP '+str(e.code)) from None

def cache_artwork(url,vid,art='poster'):
 if art not in ('poster','cover'):raise ValueError('图片类型无效')
 if not video_included(vid):return False
 # Use Amane's authenticated cached-resource endpoint, never fetch upstream URLs here.
 if not url:return False
 target=DATA/f'{vid}-{art}.jpg'
 with db() as c:old=c.execute('SELECT '+art+'_origin FROM videos WHERE id=?',(vid,)).fetchone()
 if old and old[0]==url and target.exists():return True
 if config().get('scrape_engine')=='builtin':
  with db() as c:snapshot=c.execute('SELECT amane_snapshot FROM videos WHERE id=?',(vid,)).fetchone()
  referer=json.loads(snapshot[0] or '{}').get('source_url','') if snapshot else ''
  ok=builtin_engine().cache_artwork(url,target,referer)
  if ok:
   with db() as c:c.execute('UPDATE videos SET '+art+'_origin=? WHERE id=?',(url,vid))
  return ok
 parsed=urlparse(url)
 digest=parsed.path.rsplit('/',1)[-1] if parsed.path.startswith('/api/resources/') and re.fullmatch(r'[a-f0-9]{16}',parsed.path.rsplit('/',1)[-1]) else hashlib.sha256(url.encode()).hexdigest()[:16]
 req=Request(amane_base()+'/api/resources/'+digest,headers=amane_headers())
 try:
  try:response=urlopen(req,timeout=15)
  except HTTPError as e:
   if e.code!=404 or urlparse(url).scheme not in ('http','https'):raise
   response=urlopen(Request(amane_base()+'/api/resources/proxy?url='+quote(url,safe=''),headers=amane_headers()),timeout=40)
  with response:
   kind=response.headers.get('Content-Type','');payload=response.read(12_000_001)
  if not kind.startswith('image/') or len(payload)>12_000_000:return False
  if not (payload.startswith(b'\xff\xd8') or payload.startswith(b'\x89PNG') or payload.startswith(b'RIFF')):return False
  tmp=DATA/f'{vid}-{art}.part';tmp.write_bytes(payload);tmp.replace(target)
  with db() as c:c.execute('UPDATE videos SET '+art+'_origin=? WHERE id=?',(url,vid))
  return True
 except (OSError,ValueError):return False

def cache_poster(url,vid):return cache_artwork(url,vid,'poster')

def actor_image_bytes(url):
 parsed=urlparse(url);digest=parsed.path.rsplit('/',1)[-1]
 if parsed.path.startswith('/api/resources/') and re.fullmatch(r'[a-f0-9]{16}',digest):path='resources/'+digest
 else:path='resources/proxy?url='+quote(url,safe='')
 with urlopen(Request(amane_base()+'/api/'+path,headers=amane_headers()),timeout=25) as response:
  payload=response.read(12_000_001)
  if not response.headers.get('Content-Type','').startswith('image/'):raise ValueError('头像响应不是图片')
  return payload

def download_still(url,target,engine,referer):
 if engine=='builtin':return builtin_engine().cache_artwork(url,target,referer)
 from people_catalog import image_payload
 try:
  payload=actor_image_bytes(url)
  if not image_payload(payload):return False
  temporary=target.with_suffix('.part');temporary.write_bytes(payload);temporary.replace(target);return True
 except (OSError,ValueError):return False

def sync_amane():
 lease=LEASES.try_acquire('sync')
 if lease is None:return AMANE_STATUS['synced']
 try:
  if RESTORING.is_set() or SHUTTING_DOWN.is_set() or not AMANE_LOCK.acquire(False):return AMANE_STATUS['synced']
  try:task_key=JOURNAL.start('sync')
  except BaseException:AMANE_LOCK.release();raise
  SYNC_CANCEL.clear();WORK_STATUS['sync'].update(running=True,phase='metadata');WORK_STATUS['images'].update(running=False,failed=0)
  synced=0;offset=0;posters=[];covers=[];stills=[]
  try:
   while True:
    if SYNC_CANCEL.is_set():AMANE_STATUS['message']='同步已取消';return synced
    page=amane_request('metadata?limit=200&offset='+str(offset));items=page.get('items',[])
    with db() as c:
     for item in items:
      if not item.get('title') or not item.get('field_sources'):continue
      rows=c.execute('SELECT * FROM videos WHERE UPPER(code)=?',(str(item['number']).upper(),)).fetchall()
      for old in LIBRARIES.filter_rows([dict(r) for r in rows]):
       updates={'source_tags':', '.join(dict.fromkeys(item.get('tags') or [])),'amane_id':item['id'],'amane_snapshot':json.dumps(dict(item,_velvet_engine=config().get('scrape_engine','external')),ensure_ascii=False),'source':'amane'}
       locked=set(json.loads(old['locked_fields'] or '[]'))
       if old['manual'] and not locked:locked.update(['title','code','actors','studio','date','plot'])
       values={'code':item['number'],'title':item['title'],'actors':', '.join(item.get('actors') or []),'studio':item.get('studio') or '', 'date':item.get('release') or '', 'plot':item.get('plot') or ''}
       updates.update({k:v for k,v in values.items() if k not in locked and v})
       tags,reasons=classify(dict(old)|updates);updates.update(auto_tags=tags,tag_reasons=reasons)
       c.execute('UPDATE videos SET '+','.join(k+'=?' for k in updates)+' WHERE id=?',[*updates.values(),old['id']]);synced+=1
       if item.get('poster_url'):posters.append((item['poster_url'],old['id']))
       if item.get('thumb_url') or item.get('poster_url'):covers.append((item.get('thumb_url') or item['poster_url'],old['id']))
       stills.append((old['id'],item))
    offset+=len(items)
    if not items or offset>=page.get('total',0):break
   WORK_STATUS['sync']['phase']='images';WORK_STATUS['images']['running']=True
   for vid,item in stills:ARTWORK.queue(vid,item,config().get('scrape_engine','external'))
   cached=covered=0
   for art,images in [('poster',posters),('cover',covers)]:
    for url,vid in images:
     if SYNC_CANCEL.is_set():AMANE_STATUS['message']='图片同步已取消';return synced
     ok=cache_artwork(url,vid,art)
     if art=='poster':cached+=bool(ok)
     else:covered+=bool(ok)
     if not ok:WORK_STATUS['images']['failed']+=1
   ARTWORK.process(SYNC_CANCEL)
   AMANE_STATUS.update(connected=True,synced=synced,message=f'封面 {covered}/{len(covers)} · 海报 {cached}/{len(posters)} · 本批剧照 {ARTWORK.status["processed"]-ARTWORK.status["failed"]}/{ARTWORK.status["processed"]}',last_sync=time.time())
   return synced
  finally:
   try:JOURNAL.observe('sync',task_key,'cancelled' if SYNC_CANCEL.is_set() else 'failed' if sys.exc_info()[0] else 'partial' if WORK_STATUS['images']['failed'] else 'done',{'failed':WORK_STATUS['images']['failed']})
   finally:WORK_STATUS['sync'].update(running=False,phase='idle');WORK_STATUS['images']['running']=False;AMANE_LOCK.release()
 finally:LEASES.release(lease)

def submit_scrapes(ids,use_cache=False):
 if not SUBMIT_LOCK.acquire(False):raise ValueError('另一批提交进行中，请稍后重试')
 try:return _submit_scrapes(ids,use_cache)
 finally:SUBMIT_LOCK.release()

def _submit_scrapes(ids,use_cache=False):
 if not isinstance(ids,list) or not 1<=len(ids)<=50:raise ValueError('每次请选择 1–50 部视频')
 with db() as c:
  rows=[c.execute('SELECT * FROM videos WHERE id=?',(int(vid),)).fetchone() for vid in dict.fromkeys(ids)]
 if any(not r or not r['code'].strip() for r in rows):raise ValueError('有视频缺少编号，请先在详情中填写并保存编号')
 rows=LIBRARIES.filter_rows([dict(r) for r in rows])
 codes=list(dict.fromkeys(r['code'].strip().upper() for r in rows));submitted=[];skipped=0;failures=[]
 for code in codes:
  with db() as c:active=c.execute("SELECT id FROM scrape_jobs WHERE code=? AND status IN ('queued','running','submitting','cancelling')",(code,)).fetchone()
  if active:skipped+=1;continue
  with db() as c:job_id=c.execute('INSERT INTO scrape_jobs(code,status,created,updated,engine) VALUES(?,?,?,?,?)',(code,'submitting',time.time(),time.time(),config().get('scrape_engine','external'))).lastrowid
  try:
   JOURNAL.observe('scrape',job_id,'waiting',{'reason':'submitting'})
   remote=amane_request('tasks',{'type':'scrape','number':code,'use_cache':['metadata','trans'] if use_cache else []})
   with db() as c:c.execute('UPDATE scrape_jobs SET remote_id=?,status=?,updated=? WHERE id=?',(remote['id'],remote['status'],time.time(),job_id))
   submitted.append(remote['id'])
  except Exception as e:
   with db() as c:c.execute('UPDATE scrape_jobs SET error=? WHERE id=?',('提交结果待核对：'+str(e)[:150],job_id))
   failures.append({'code':code,'error':str(e)[:200]})
 return {'submitted':len(submitted),'skipped':skipped,'failures':failures}

def poll_scrapes():
 if RESTORING.is_set() or SHUTTING_DOWN.is_set() or not POLL_LOCK.acquire(False):return
 try:return _poll_scrapes()
 finally:POLL_LOCK.release()

def _poll_scrapes():
 with db() as c:jobs=c.execute("SELECT * FROM scrape_jobs WHERE status IN ('queued','running','submitting','cancelling')").fetchall()
 for job in jobs:
  if job['engine']!=config().get('scrape_engine','external'):
   with db() as c:c.execute("UPDATE scrape_jobs SET status='failed',retry_kind='needs_review',error='任务来源未确认或已切换，请核对原引擎后重试' WHERE id=?",(job['id'],))
   continue
  remote_id=job['remote_id']
  if remote_id is None:
   entries=amane_request('tasks?limit=200').get('items',[])
   def recent(task):
    try:return datetime.datetime.fromisoformat(task.get('created_at','').replace('Z','+00:00')).timestamp()>=job['created']-5
    except ValueError:return False
   found=next((t for t in entries if str(t.get('title','')).upper()==job['code'].upper() and recent(t)),None)
   if not found:
    with db() as c:c.execute("UPDATE scrape_jobs SET status='failed',retry_kind='needs_review',error=? WHERE id=?",('远程提交结果未确认，请查看 Amane 后再决定重试',job['id']))
    continue
   remote_id=found['id']
   with db() as c:c.execute('UPDATE scrape_jobs SET remote_id=? WHERE id=?',(remote_id,job['id']))
  try:remote=amane_request('tasks/'+str(remote_id))
  except ValueError as e:
   if '404' not in str(e):raise
   with db() as c:c.execute("UPDATE scrape_jobs SET status='failed',retry_kind='needs_review',error=? WHERE id=?",('远程任务不存在，请核对后重试',job['id']))
   continue
  result=remote.get('result') or {};details=[]
  for out in result.get('outcomes',[]):
   if out.get('outcome')=='failed':details.append(str(out.get('site',''))+': '+str(out.get('reason','unknown')))
   elif out.get('outcome')=='success':details.append(str(out.get('site',''))+': '+('缓存命中' if out.get('cached') else '获取成功'))
   elif out.get('outcome')=='not_found':details.append(str(out.get('site',''))+': 未匹配')
  reasons=[x.get('reason') for x in result.get('outcomes',[]) if x.get('outcome')=='failed']
  retry='retryable' if any(x in ('network','timeout','dns','rate_limited','server_error') for x in reasons) else 'needs_review' if remote['status']=='failed' else ''
  with db() as c:c.execute('UPDATE scrape_jobs SET status=?,error=?,details=?,updated=?,retry_kind=? WHERE id=?',(remote['status'],remote.get('error') or '', '; '.join(details),time.time(),retry,job['id']))

AUTO_STATUS={'enabled':True,'message':'等待自动刮削','submitted':0}
def auto_scrape_tick():
 enabled=bool(config().get('auto_scrape'));AUTO_STATUS['enabled']=enabled
 if not enabled:AUTO_STATUS['message']='自动刮削已暂停';return
 with db() as c:
  active=c.execute("SELECT COUNT(*) FROM scrape_jobs WHERE status IN ('queued','running','submitting','cancelling')").fetchone()[0]
  rows=c.execute("SELECT id,path,size,mtime,UPPER(code) AS code FROM videos WHERE missing=0 AND amane_id=0 AND code!='' ORDER BY id").fetchall()
  policies=LIBRARIES.policies()
  candidates=[];seen=set()
  for row in rows:
   owner=LIBRARIES.owner(row['path'],policies)
   policy=next((p for p in policies if p['path']==owner),None)
   if policy and (policy['automation']!='scrape' or not Path(policy['path']).is_dir() or not LIBRARIES.included(row['path'],policy,row['size'] or 0) or time.time()-(row['mtime'] or 0)<policy['settle_seconds']):continue
   if not policy:continue
   code=row['code']
   if code in seen:continue
   seen.add(code)
   jobs=c.execute('SELECT status,created,retry_kind FROM scrape_jobs WHERE code=? ORDER BY id DESC',(code,)).fetchall()
   if jobs and (jobs[0]['retry_kind']=='needs_review' or jobs[0]['status'] in ('queued','running','submitting','cancelling','done','cancelled','canceled') or time.time()-jobs[0]['created']<86400 or len(jobs)>=3):continue
   candidates.append(row['id'])
 if active>=3:AUTO_STATUS['message']='等待当前刮削任务完成';return
 batch=candidates[:min(2,3-active)]
 if not batch:AUTO_STATUS['message']='暂无可提交的编号（失败项 24 小时后重试，最多 3 次）';return
 result=submit_scrapes(batch,use_cache=True);AUTO_STATUS['submitted']+=result['submitted'];AUTO_STATUS['message']='已自动提交 '+str(result['submitted'])+' 个编号'

DUP_STATUS={'running':False,'processed':0,'total':0,'errors':0,'message':'尚未检测'}
DUP_LOCK=threading.Lock()
def detect_duplicates():
 lease=LEASES.try_acquire('duplicates')
 if lease is None:return
 try:
  if RESTORING.is_set() or not DUP_LOCK.acquire(False):return
  try:task_key=JOURNAL.start('duplicates')
  except BaseException:DUP_LOCK.release();raise
  DUP_CANCEL.clear()
  DUP_STATUS.update(running=True,processed=0,total=0,errors=0,message='查找大小相同的文件…')
  try:
   with db() as c:
    rows=c.execute('SELECT * FROM videos WHERE missing=0 AND size IN (SELECT size FROM videos WHERE missing=0 GROUP BY size HAVING COUNT(*)>1)').fetchall()
   rows=LIBRARIES.filter_rows([dict(r) for r in rows])
   DUP_STATUS['total']=len(rows)
   for row in rows:
    if not LIBRARIES.filter_rows([dict(row)]):continue
    if DUP_CANCEL.is_set():DUP_STATUS['message']='重复校验已取消';return
    try:
     p=Path(row['path']);before=p.stat();stamp=f'{before.st_size}:{before.st_mtime_ns}'
     if row['hash_stamp']!=stamp or not row['file_hash']:
      h=hashlib.sha256()
      with p.open('rb') as f:
       for block in iter(lambda:f.read(4194304),b''):
        if DUP_CANCEL.is_set():DUP_STATUS['message']='重复校验已取消';return
        h.update(block)
      after=p.stat()
      if before.st_size!=after.st_size or before.st_mtime_ns!=after.st_mtime_ns:raise ValueError('文件读取期间发生变化')
      if not LIBRARIES.filter_rows([dict(row)]):continue
      with db() as c:c.execute('UPDATE videos SET file_hash=?,hash_stamp=? WHERE id=?',(h.hexdigest(),stamp,row['id']))
     DUP_STATUS['processed']+=1;DUP_STATUS['message']='正在校验重复候选 '+str(DUP_STATUS['processed'])+'/'+str(len(rows))
    except (OSError,ValueError):DUP_STATUS['errors']+=1
   DUP_STATUS['message']='检测完成；同编号组仅为疑似重复，分集与多版本请人工核对'
  finally:
   try:JOURNAL.observe('duplicates',task_key,'cancelled' if DUP_CANCEL.is_set() else 'failed' if sys.exc_info()[0] else 'partial' if DUP_STATUS['errors'] else 'done',{'processed':DUP_STATUS['processed'],'total':DUP_STATUS['total'],'failed':DUP_STATUS['errors']})
   finally:DUP_STATUS['running']=False;DUP_LOCK.release()
 finally:LEASES.release(lease)

def actor_library():
 with db() as c:
  videos=[dict(r) for r in c.execute('SELECT * FROM videos WHERE missing=0')]
  prefs={r['name']:dict(r) for r in c.execute('SELECT * FROM actor_preferences')}
 videos=LIBRARIES.filter_rows(videos)
 people={}
 for video in videos:
  for name in set(n.strip() for n in re.split(r'[,，;；\n]',video.get('actors') or '') if n.strip()):
   person=people.setdefault(name,{'name':name,'rating':prefs.get(name,{}).get('rating',0),'favorite':prefs.get(name,{}).get('favorite',0),'movie_keys':set(),'file_count':0})
   person['file_count']+=1
   movie=video['code'].strip().upper() or presentation(video)['part_key'] or video['path'];person['movie_keys'].add(movie)
 for name,pref in prefs.items():
  if name not in people and (pref['favorite'] or pref['rating']):people[name]=dict(pref,movie_keys=set(),file_count=0)
 result=[];profiles=PEOPLE.profiles()
 for person in people.values():
  profile=profiles.get(person['name'],{})
  try:person.update(json.loads(profile.get('profile') or '{}'))
  except ValueError:pass
  key=actor_key(person['name']);portrait=DATA/'actors'/(key+'.jpg')
  person['portrait']='/actor-image/'+key+'?v='+str(int(profile.get('updated',0))) if portrait.is_file() else ''
  person['portrait_source']=profile.get('origin','');person['portrait_error']=profile.get('error','')
  person['movie_count']=len(person.pop('movie_keys'));result.append(person)
 return sorted(result,key=lambda p:(-p['favorite'],-p['rating'],-p['movie_count'],p['name']))

def duplicate_groups():
 CORE.reconcile()
 with db() as c:
  links={r['video_id']:dict(r) for r in c.execute('SELECT * FROM media_links')}
  rows=[dict(r) for r in c.execute('SELECT * FROM videos WHERE missing=0')];choices={r[0]:r[1] for r in c.execute('SELECT * FROM duplicate_choices')}
 hashes={};codes={}
 for row in rows:
  if row['file_hash']:
   try:
    st=Path(row['path']).stat()
    if row['hash_stamp']==f'{st.st_size}:{st.st_mtime_ns}':hashes.setdefault(row['file_hash'],[]).append(row)
    else:row['file_hash']=''
   except OSError:row['file_hash']=''
  if row['code']:codes.setdefault(row['code'].strip().upper(),[]).append(row)
 result=[]
 for kind,buckets in [('exact',hashes),('candidate',codes)]:
  for key,items in buckets.items():
   if len(items)<2:continue
   segments=[presentation(x) for x in items]
   if kind=='candidate' and len({links[x['id']]['version_key'] for x in items})==1 and len({links[x['id']]['part_no'] for x in items})==len(items):continue
   if kind=='candidate' and all(x['part_index'] for x in segments) and len({x['part_key'] for x in segments})==1 and len({x['part_index'] for x in segments})==len(segments):continue
   if kind=='candidate' and len({x['file_hash'] for x in items})==1 and items[0]['file_hash']:continue
   group=kind+':'+key
   safe=[dict({k:x[k] for k in ['id','title','path','size','duration','width','height','codec','code','file_hash']},part_index=links[x['id']]['part_no'],version_key=links[x['id']]['version_key']) for x in items]
   result.append({'key':group,'kind':kind,'classification':'exact' if kind=='exact' else 'versions' if len({links[x['id']]['movie_key'] for x in items})==1 else 'candidate','videos':safe,'preferred_id':choices.get(group)})
 return result

def background_sync():
 try:sync_amane()
 except Exception as e:AMANE_STATUS.update(connected=False,message=str(e)[:200])

def amane_watcher():
 last=0;auto_last=0
 while True:
  if RESTORING.is_set() or SHUTTING_DOWN.is_set():time.sleep(1);continue
  try:
   with LEASES.hold('scrape-poll'):
    poll_scrapes();JOURNAL.reconcile_sources()
  except Exception as e:AMANE_STATUS.update(connected=False,message=str(e)[:200])
  # A blocked metadata poll must not stall independent file-review/cache queues.
  if not REVIEW.cancel.is_set():REVIEW.resume()
  for engine in BUILTIN_ENGINES.values():engine.start()
  if time.time()-last>=60 and not AMANE_LOCK.locked():
   threading.Thread(target=background_sync,daemon=True).start();last=time.time()
  if time.time()-auto_last>=60:
   try:auto_scrape_tick()
   except Exception as e:AUTO_STATUS['message']='自动刮削等待连接：'+str(e)[:150]
   try:
    if not LOCK.locked():
     with LEASES.hold('automatic-review',resources=['metadata']):REVIEW.auto_tick()
   except Exception:pass
   auto_last=time.time()
  time.sleep(15)

def watcher():
 while True:
  try:
   if not RESTORING.is_set() and not SHUTTING_DOWN.is_set():
    scan(automatic=True)
    if not any((DATA/'backups').glob('auto-'+time.strftime('%Y%m%d')+'*.zip')):
     with LEASES.hold('backup','write'):CORE.backup('auto-'+time.strftime('%Y%m%d'))
  except Exception as e:STATUS['message']='扫描失败：'+str(e)
  time.sleep(max(30,int(config()['interval'])))
PICKER_LOCK=threading.Lock()
def choose_folder():
 if NAS:raise ValueError('NAS 模式请使用服务器目录路径')
 if not PICKER_LOCK.acquire(False):raise ValueError('文件夹选择器已打开，请切换到系统窗口')
 try:
  if sys.platform=='win32':
   script="""[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = '选择要导入 Velvet 的视频文件夹'
$dialog.ShowNewFolderButton = $false
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.Opacity = 0
$owner.Show()
try { if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Write($dialog.SelectedPath) } }
finally { $dialog.Dispose(); $owner.Close(); $owner.Dispose() }
"""
   args=['powershell.exe','-NoProfile','-STA','-Command',script]
  else:
   script="from tkinter import Tk,filedialog; r=Tk(); r.withdraw(); r.attributes('-topmost',True); p=filedialog.askdirectory(title='选择媒体文件夹'); print(p,end=''); r.destroy()"
   args=[sys.executable,'-c',script]
  result=run(args,600)
  if result.returncode:raise ValueError('无法打开系统选择器，请使用备用路径输入')
  return result.stdout.decode('utf-8-sig').strip()
 finally:PICKER_LOCK.release()

def launch(path):
 if sys.platform=='win32':os.startfile(path)
 else:subprocess.Popen(['open' if sys.platform=='darwin' else 'xdg-open',path],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
def browser_context():
 cfg=config();cfg['has_key']=bool(cfg.get('api_key'));cfg['api_key']='';cfg['has_amane_token']=bool(cfg.get('amane_token'));cfg['amane_token']='';cfg.pop('scrape_site_config',None)
 with db() as c:folders=[row[0] for row in c.execute('SELECT path FROM folders ORDER BY path')]
 return {'settings':cfg,'folders':folders,'collection_rules':DIRECTORY_POLICY.rules(),'excluded_directories':LIBRARIES.exclusions(),
         'status':dict(STATUS),'ffmpeg':bool(binary('ffmpeg')),'ffprobe':bool(binary('ffprobe')),'platform':sys.platform,'server_mode':NAS,'amane':dict(AMANE_STATUS),'token':TOKEN,'transport':'context-v1'}

class Handler(BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def send(self,data,kind='application/json',status=200):
  if not isinstance(data,bytes):data=json.dumps(data,ensure_ascii=False).encode()
  try:
   self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(data)));self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(data)
  except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):self.close_connection=True
 def video(self,vid):
  with db() as c:r=c.execute('SELECT * FROM videos WHERE id=?',(int(vid),)).fetchone()
  if not r:raise ValueError('视频记录不存在')
  return dict(r)
 def authorize(self):
  if not NAS:return True
  expected='Basic '+base64.b64encode((USER+':'+PASSWORD).encode()).decode()
  if hmac.compare_digest(self.headers.get('Authorization',''),expected):return True
  self.send_response(401);self.send_header('WWW-Authenticate','Basic realm="Velvet", charset="UTF-8"');self.send_header('Content-Length','0');self.end_headers();return False
 def do_GET(self):
  if not self.authorize():return
  if not NAS and not RUNTIME.accepts_host(self.headers.get('Host')):return self.send({},status=403)
  path=urlparse(self.path).path
  try:
   if path in UI_ASSETS:
    filename,kind=UI_ASSETS[path]
    return self.send((ROOT/filename).read_bytes(),kind+'; charset=utf-8')
   if path.startswith('/api/review-evidence/'):
    match=re.fullmatch(r'/api/review-evidence/(\d+)/(\d+)',path)
    if not match or not 0<=int(match[2])<32:return self.send({},status=404)
    row=self.video(match[1])
    with db() as c:record=c.execute('SELECT size,mtime,auto_generation,result,auto_status FROM file_reviews WHERE video_id=?',(row['id'],)).fetchone()
    if not record or tuple(record)[:2]!=(row['size'],row['mtime']) or not re.fullmatch(r'[a-f0-9]{32}',record[2] or '') or record[3]=='changed' or record[4]=='stale':return self.send({},status=404)
    try:
     stamp=Path(row['path']).stat()
     if stamp.st_size!=row['size'] or abs(stamp.st_mtime-row['mtime'])>.01:return self.send({},status=404)
    except OSError:return self.send({},status=404)
    image=DATA/'analysis'/str(row['id'])/record[2]/(match[2]+'.jpg')
    if not image.is_file():return self.send({},status=404)
    return self.send(image.read_bytes(),'image/jpeg')
   if path=='/api/file-review':
    with db() as c:return self.send(REVIEW.snapshot(LIBRARIES.filter_rows([dict(r) for r in c.execute('SELECT * FROM videos')]),CORE.hierarchy()))
   if path=='/api/network':return self.send(NETWORK.status())
   if path=='/api/scrape-sites':
    options=config().get('scrape_site_config',{})
    return self.send({'items':[{'id':source,'base_url':options.get(source,{}).get('base_url',''),'timeout':options.get(source,{}).get('timeout',20),'retries':options.get(source,{}).get('retries',1),'cookie_configured':bool(options.get(source,{}).get('cookie'))} for source in NATIVE_SOURCES]})
   if path.startswith('/api/video-artwork/'):
    row=self.video(path.rsplit('/',1)[-1]);return self.send(ARTWORK.manifest(row['id']))
   if path=='/api/source-catalog':return self.send(SOURCES.snapshot(refresh=parse_qs(urlparse(self.path).query).get('refresh')==['1']))
   if path=='/api/libraries':return self.send({'items':LIBRARIES.list()})
   if path=='/api/library-files':
    query=parse_qs(urlparse(self.path).query)
    return self.send(LIBRARIES.files(query.get('path',[''])[0],query.get('status',['all'])[0],query.get('search',[''])[0],int(query.get('offset',['0'])[0]),int(query.get('limit',['30'])[0])))
   if path=='/api/hierarchy':return self.send({'movies':CORE.hierarchy()})
   if path=='/api/backups':return self.send({'backups':CORE.backups()})
   if path.startswith('/api/field-diff/'):
    row=self.video(path.rsplit('/',1)[-1]);snapshot=json.loads(row.get('amane_snapshot') or '{}');values={'title':snapshot.get('title'),'actors':', '.join(snapshot.get('actors') or []),'studio':snapshot.get('studio'),'date':snapshot.get('release'),'plot':snapshot.get('plot'),'code':snapshot.get('number')}
    return self.send({'fields':[{'field':k,'current':row.get(k),'incoming':v,'locked':k in json.loads(row['locked_fields'] or '[]'),'source':(snapshot.get('field_sources') or {}).get(k,'Amane')} for k,v in values.items()]})
   if path=='/api/actors':return self.send({'actors':actor_library()})
   if path=='/api/actor-sync-status':return self.send(dict(PEOPLE.state))
   if path=='/api/categories':return self.send({'items':CATEGORIES.list()})
   if path.startswith('/actor-image/'):
    key=path.rsplit('/',1)[-1]
    if not re.fullmatch(r'[a-f0-9]{64}',key):raise ValueError('头像标识无效')
    image_path=DATA/'actors'/(key+'.jpg')
    if not image_path.is_file():return self.send({},status=404)
    payload=image_path.read_bytes();kind='image/png' if payload.startswith(b'\x89PNG') else 'image/webp' if payload.startswith(b'RIFF') else 'image/jpeg'
    return self.send(payload,kind)
   if path=='/api/duplicates':return self.send({'groups':duplicate_groups(),'status':DUP_STATUS})
   if path=='/api/amane-jobs':
    with db() as c:
     jobs=[dict(r) for r in c.execute('SELECT * FROM scrape_jobs ORDER BY id DESC LIMIT 100')]
     for job in jobs:
      row=c.execute('SELECT * FROM videos WHERE UPPER(code)=? LIMIT 1',(job['code'].upper(),)).fetchone()
      job['completeness']='awaiting_sync' if job['status']=='done' and row and not row['amane_id'] else 'partial' if job['status']=='done' and row and (not row['actors'] or not row['studio'] or not row['source_tags']) else ''
    return self.send({'jobs':jobs,'status':AMANE_STATUS,'auto':AUTO_STATUS,'work':WORK_STATUS,'duplicates':DUP_STATUS,'file_review':{'running':REVIEW.lock.locked()}})
   if path=='/api/tag-rules':
    try:rules=json.loads((DATA/'tag_rules.json').read_text('utf-8'))
    except (OSError,ValueError):rules=[]
    return self.send({'rules':rules})
   if path=='/api/context':return self.send(browser_context())
   if path=='/api/library/lookup':return self.send(BROWSE.lookup(parse_qs(urlparse(self.path).query).get('code',[''])[0]))
   if path=='/api/library/relationship-options':return self.send(BROWSE.relationship_options(parse_qs(urlparse(self.path).query).get('q',[''])[0]))
   if path=='/api/library/query-revision':return self.send(BROWSE.revision(parse_qs(urlparse(self.path).query).get('quality',[''])[0]))
   if path=='/api/library/facets':
    query=parse_qs(urlparse(self.path).query);return self.send(BROWSE.facets(query.get('kind',['tags'])[0],query.get('q',[''])[0]))
   if path=='/api/library/random':return self.send(BROWSE.random(parse_qs(urlparse(self.path).query).get('snapshot',[''])[0]))
   if path=='/api/library/query':return self.send(BROWSE.query({key:values[0] for key,values in parse_qs(urlparse(self.path).query).items()}))
   if path=='/api/library/movie':
    movie=BROWSE.movie(parse_qs(urlparse(self.path).query).get('id',['0'])[0]);videos=[part for version in movie['_versions'] for part in version['_parts']]
    return self.send({'movie':movie,'review':REVIEW.snapshot(videos,[movie])})
   if path=='/api/library-revision':return self.send({'instance':hashlib.sha256(TOKEN.encode()).hexdigest(),'revision':DB_REVISION,'scan':dict(STATUS),'review_running':REVIEW.lock.locked()})
   if path.startswith('/api/organization/'):
    query=parse_qs(urlparse(self.path).query);value=lambda key,default='':query.get(key,[default])[0]
    if path=='/api/organization/preview':return self.send(ORGANIZATION.preview(value('id'),value('offset',0),value('limit',50)))
    if path=='/api/organization/draft':return self.send(ORGANIZATION.draft(value('id'),value('offset',0),value('limit',50),value('retained_offset',0)))
    if path=='/api/organization/jobs':return self.send(ORGANIZATION.listing(value('offset',0),value('limit',25)))
    if path=='/api/organization/job':return self.send(ORGANIZATION.detail(value('id'),value('offset',0),value('limit',50)))
    if path=='/api/organization/locations':
     video_id=int(value('id'));ORGANIZATION.locations.migrate([video_id])
     return self.send({'items':ORGANIZATION.locations.locations(video_id)})
   if path=='/api/storage':return self.send({'devices':INVENTORY.devices()})
   if path=='/api/storage/query':return self.send(INVENTORY_QUERY.query({key:values[0] for key,values in parse_qs(urlparse(self.path).query).items()}))
   if path=='/api/storage/files':
    query=parse_qs(urlparse(self.path).query);return self.send({'files':INVENTORY.files(query.get('id',[''])[0],query.get('offset',[0])[0],status=query.get('status',['all'])[0])})
   if path=='/api/storage/errors':
    query=parse_qs(urlparse(self.path).query);return self.send({'errors':INVENTORY.errors(query.get('id',[''])[0])})
   if path=='/api/storage/comparisons':return self.send({'groups':INVENTORY.comparisons()})
   if path=='/api/storage/plans':
    query=parse_qs(urlparse(self.path).query);return self.send({'plans':INVENTORY.plans(query.get('id',[''])[0])})
   if path=='/api/tasks':
    JOURNAL.reconcile_sources();return self.send({'tasks':JOURNAL.list(),'resources':task_resources()})
   if path=='/api/diagnostics':
    result=diagnostics(APP_VERSION,RUNTIME,binary,{**WORK_STATUS,'duplicates':DUP_STATUS,'file_review':{'running':REVIEW.lock.locked()}})
    result['task_ids']=[{k:row[k] for k in ('id','kind','state')} for row in JOURNAL.list(20)]
    result['resources']=task_resources();return self.send(result)
   if path=='/api/health':
    with db() as c:
     counts=dict(c.execute("SELECT COUNT(*) AS videos,SUM(missing) AS missing,SUM(CASE WHEN error!='' THEN 1 ELSE 0 END) AS errors FROM videos").fetchone())
    return self.send({'ok':True,'application':'velvet','instance_id':hashlib.sha256(TOKEN.encode()).hexdigest(),'draining':SHUTTING_DOWN.is_set(),'port':RUNTIME.port,'version':APP_VERSION,'platform':sys.platform,'server_mode':NAS,'ffmpeg':bool(binary('ffmpeg')),'ffprobe':bool(binary('ffprobe')),'scan':STATUS,'library':counts})
   if path=='/api/library':
    cfg=config();cfg['has_key']=bool(cfg['api_key']);cfg['api_key']='';cfg['has_amane_token']=bool(cfg['amane_token']);cfg['amane_token']='';cfg.pop('scrape_site_config',None)
    excluded=LIBRARIES.exclusions()
    movies=CORE.hierarchy()
    with db() as c:
     videos=LIBRARIES.filter_rows([dict(r) for r in c.execute('SELECT * FROM videos ORDER BY mtime DESC')]);folders=[r[0] for r in c.execute('SELECT path FROM folders')]
    payload={'movies':movies,'videos':[presentation(r) for r in videos],'collection_rules':DIRECTORY_POLICY.rules(),'excluded_directories':excluded,'folders':folders,'status':STATUS,'ffmpeg':bool(binary('ffmpeg')),'ffprobe':bool(binary('ffprobe')),'settings':cfg,'platform':sys.platform,'server_mode':NAS,'amane':AMANE_STATUS,'token':TOKEN,'review':REVIEW.snapshot(videos,movies)}
    if parse_qs(urlparse(self.path).query).get('compact')==['1']:
     from library_transport import pack_library
     payload=pack_library(payload)
    return self.send(payload)
   if path.startswith('/thumb/'):
    name=path.split('/')[-1]
    if not re.fullmatch(r'\d+(?:-(?:[1-5]|poster|cover|still-(?:[1-9]|1[0-2])))?\.jpg',name):return self.send({},status=404)
    file=DATA/name
    if not file.exists() and '-cover' in name:file=DATA/(name.split('-')[0]+'-poster.jpg')
    if not file.exists() and ('-poster' in name or '-cover' in name):file=DATA/(name.split('-')[0]+'.jpg')
    if not file.exists():return self.send({},status=404)
    payload=file.read_bytes();kind='image/png' if payload.startswith(b'\x89PNG') else 'image/webp' if payload.startswith(b'RIFF') else 'image/jpeg'
    return self.send(payload,kind)
   if path.startswith('/media/'):
    with LEASES.hold('playback',resources=video_resources(db,[path.split('/')[-1]])):
     row=self.video(path.split('/')[-1]);p=Path(row['path']);size=p.stat().st_size
     start,end=0,size-1;header=self.headers.get('Range')
     if header:
      m=re.fullmatch(r'bytes=(\d*)-(\d*)',header)
      if not m or not any(m.groups()):return self.send({},status=416)
      if not m[1]:start=max(0,size-int(m[2]))
      else:start=int(m[1]);end=min(end,int(m[2])) if m[2] else end
      if start>end or start>=size:
       self.send_response(416);self.send_header('Content-Range',f'bytes */{size}');self.end_headers();return
     self.send_response(206 if header else 200);self.send_header('Content-Type',mimetypes.guess_type(p.name)[0] or 'application/octet-stream');self.send_header('Accept-Ranges','bytes');self.send_header('Content-Length',str(end-start+1))
     if header:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
     self.end_headers()
     with p.open('rb') as f:
      f.seek(start);remaining=end-start+1
      while remaining:
       chunk=f.read(min(262144,remaining))
       if not chunk:break
       self.wfile.write(chunk);remaining-=len(chunk)
     return
   return self.send({},status=404)
  except (BrokenPipeError,ConnectionResetError):pass
  except (ResourceBusy,InventoryQueryChanged,LibraryQueryChanged) as error:self.send({'error':str(error)},status=409)
  except Exception as e:self.send({'error':str(e)},status=400)
 def do_POST(self):
  global ACTIVE_POSTS
  if not self.authorize():return
  if self.headers.get('X-Velvet-Token')!=TOKEN:return self.send({},status=403)
  with POST_LEASE:ACTIVE_POSTS+=1
  try:
   path=urlparse(self.path).path
   if path.startswith('/api/organization/') or path in ('/api/runtime/stop','/api/runtime/resume','/api/task-cancel'):
    return self._post()
   dispatch=path in ('/api/scan','/api/library-scan','/api/storage/scan','/api/file-review/check','/api/file-review/resume','/api/artwork-retry')
   with LEASES.hold('restore' if path=='/api/restore' else 'backup' if path=='/api/backup' else 'http-change','write' if path in ('/api/restore','/api/backup') else 'read',resources=['metadata'] if dispatch else None):
    return self._post()
  except ResourceBusy as error:return self.send({'error':str(error)},status=409)
  finally:
   with POST_LEASE:ACTIVE_POSTS-=1
 def _post(self):
  global DB_REVISION
  if not self.authorize():return
  if self.headers.get('X-Velvet-Token')!=TOKEN:return self.send({},status=403)
  try:
   length=int(self.headers.get('Content-Length',0))
   selection_request=urlparse(self.path).path in ('/api/storage/selection','/api/storage/query-plan','/api/organization/prepare')
   if length>(2000000 if selection_request else 100000):raise ValueError('请求过大')
   a=json.loads(self.rfile.read(length) or '{}');path=urlparse(self.path).path
   DB_REVISION+=1
   if RESTORING.is_set():raise ValueError('恢复中，请稍后操作')
   if path=='/api/runtime/resume':
    SHUTTING_DOWN.clear();ORGANIZATION.start();REVIEW.cancel.clear();REVIEW.resume()
    for engine in BUILTIN_ENGINES.values():engine.paused.clear();engine.start()
    return self.send({'ok':True})
   if path=='/api/runtime/stop':
    SHUTTING_DOWN.set();ORGANIZATION.stop();SCAN_CANCEL.set();SYNC_CANCEL.set();DUP_CANCEL.set();REVIEW.cancel.set()
    for engine in BUILTIN_ENGINES.values():engine.paused.set()
    busy=ACTIVE_POSTS>1 or bool(ORGANIZATION.thread and ORGANIZATION.thread.is_alive()) or any(row['kind']!='organize-reconcile' for row in LEASES.active()) or RESTORING.is_set() or POLL_LOCK.locked() or ARTWORK.lock.locked() or LOCK.locked() or AMANE_LOCK.locked() or DUP_LOCK.locked() or SUBMIT_LOCK.locked() or REVIEW.lock.locked() or PEOPLE.state.get('running') or any(engine.worker_lock.locked() for engine in BUILTIN_ENGINES.values())
    if busy:return self.send({'ok':False,'waiting':True})
    self.send({'ok':True,'waiting':False});threading.Thread(target=self.server.shutdown,daemon=True).start();return
   if SHUTTING_DOWN.is_set() and path!='/api/task-cancel':raise ValueError('服务正在安全停止，请稍后操作')
   if path.startswith('/api/storage/') and RESTORING.is_set():raise ValueError('恢复中，请稍后操作')
   if path=='/api/organization/prepare':
    selected=ORGANIZATION.selected(a.get('id'),a.get('selected'))
    draft=ORGANIZATION.prepare(a.get('id'),a.get('digest'),selected,a.get('operation','copy'))
    return self.send({'ok':True,'id':draft['id']})
   elif path=='/api/organization/approve':
    if a.get('confirmed') is not True:raise ValueError('请核对并确认本次固定执行计划')
    job=ORGANIZATION.approve(a.get('id'),a.get('digest'));ORGANIZATION.start()
    return self.send({'ok':True,'id':job})
   elif path=='/api/organization/control':
    job=ORGANIZATION.control(a.get('id'),a.get('action'));ORGANIZATION.start()
    return self.send({'ok':True,'state':job['state']})
   elif path=='/api/organization/archive-preview':
    return self.send({'ok':True,'id':ORGANIZATION.archive.prepare(a.get('id'))['id']})
   elif path=='/api/organization/archive-approve':
    if a.get('confirmed') is not True:raise ValueError('请核对并单独确认源文件归档计划，目标副本将保留')
    job=ORGANIZATION.archive.approve(a.get('id'),a.get('draft'),a.get('digest'));ORGANIZATION.start()
    return self.send({'ok':True,'id':job})
   elif path=='/api/organization/undo-preview':
    return self.send({'ok':True,'id':ORGANIZATION.prepare_inverse(a.get('id'))['id']})
   elif path=='/api/organization/undo-approve':
    if a.get('confirmed') is not True:raise ValueError('请核对并确认本次撤销计划')
    job=ORGANIZATION.approve_inverse(a.get('id'),a.get('digest'));ORGANIZATION.start()
    return self.send({'ok':True,'id':job})
   elif path=='/api/organization/cleanup-preview':return self.send(ORGANIZATION.cleanup.preview(a.get('id')))
   elif path=='/api/organization/cleanup':
    if a.get('confirmed') is not True:raise ValueError('请确认本任务暂存清理范围')
    return self.send(ORGANIZATION.cleanup.clean(a.get('id'),a.get('digest')))
   elif path=='/api/organization/prefer':
    with LEASES.hold('location-choice'):ORGANIZATION.locations.prefer(a.get('id'),a.get('location'))
    return self.send({'ok':True})
   elif path=='/api/library/selection':return self.send(BROWSE.selection(a.get('snapshot',''),a.get('keys'),a.get('offset',0),a.get('limit',100)))
   elif path=='/api/storage/register':return self.send({'ok':True,'id':INVENTORY.register(a.get('path'),a.get('alias'))})
   elif path=='/api/storage/annotate':
    INVENTORY.annotate(a.get('id'),a.get('relative'),a.get('annotation'));return self.send({'ok':True})
   elif path=='/api/storage/remap':return self.send({'ok':True,'device':INVENTORY.remap(a.get('id'),a.get('path'),a.get('identity'))})
   elif path=='/api/storage/scan':
    INVENTORY.device(a.get('id'))
    if type(a.get('hashing',False)) is not bool:raise ValueError('哈希开关无效')
    if not LOCK.acquire(False):raise ValueError('扫描资源占用，请等待当前扫描结束')
    try:
     task_key=JOURNAL.start('inventory');SCAN_CANCEL.clear()
     threading.Thread(target=run_inventory,args=(task_key,a.get('id'),a.get('hashing',False)),daemon=True).start()
    except BaseException:LOCK.release();raise
    return self.send({'ok':True,'task_id':task_key})
   elif path=='/api/storage/prefer':
    INVENTORY.prefer(a.get('id'),a.get('relative'),a.get('preferred'),a.get('note',''));return self.send({'ok':True})
   elif path=='/api/storage/revise':return self.send({'ok':True,'plan':INVENTORY.revise_plan(a.get('id'),a.get('digest'),a.get('selected'),a.get('edits'))})
   elif path=='/api/storage/selection':
    return self.send(ORGANIZATION.planner.selection(a.get('snapshot'),a.get('selected')))
   elif path=='/api/storage/query-plan':
    plan=ORGANIZATION.planner.plan(a.get('snapshot'),a.get('selected'),a.get('target',''),a.get('layout','title'),a.get('operation','copy'))
    return self.send({'ok':True,'plan':{key:plan[key] for key in ('id','digest','bytes','source_roots','selection','space')}|{'total':len(plan['entries'])}})
   elif path=='/api/storage/plan':
    plan=INVENTORY.plan(a.get('id'),a.get('target'),a.get('layout','title'),a.get('operation','copy'))
    JOURNAL.observe('plan',plan['id'],'done',{'processed':len(plan['entries']),'total':len(plan['entries']),'phase':'preview_only'})
    return self.send({'ok':True,'plan':plan})
   elif path=='/api/directory-preview':return self.send(DIRECTORY_POLICY.preview(a.get('path')))
   elif path=='/api/collection-rule':
    if RESTORING.is_set():raise ValueError('恢复中，请稍后操作')
    with DB_MUTEX:preview=DIRECTORY_POLICY.set_rule(a.get('path'),a.get('mode'),a.get('digest'))
    return self.send({'ok':True,'preview':preview})
   elif path=='/api/exclusion-preview':
    target=Path(self.video(a['id'])['path']).parent if 'id' in a else a.get('path')
    return self.send(LIBRARIES.exclusion_preview(str(target) if target else ''))
   elif path=='/api/excluded-directories':
    target=Path(self.video(a['id'])['path']).parent if 'id' in a else a.get('path')
    with DB_MUTEX:
     if a.get('enabled',True) and LIBRARIES.exclusion_preview(str(target) if target else '')['digest']!=a.get('digest'):raise ValueError('排除范围已变化，请重新预览确认')
     LIBRARIES.exclude(str(target) if target else '',a.get('enabled',True))
    return self.send({'ok':True,'message':'已更新可见性；恢复后的新增文件需要重扫'})
   elif path=='/api/file-review/automatic':
    REVIEW.set_auto(a.get('enabled'));return self.send({'ok':True})
   elif path=='/api/file-review/automatic-mark':
    REVIEW.review_automatic(a.get('ids'),a.get('code'),a.get('decision'));return self.send({'ok':True})
   elif path=='/api/file-review/mark':
    REVIEW.mark(a.get('ids'),a.get('flag'),a.get('enabled',True),a.get('decision'),a.get('note'));return self.send({'ok':True})
   elif path=='/api/file-review/check':
    if RESTORING.is_set():raise ValueError('恢复中，请稍后检查')
    REVIEW.enqueue(a.get('ids'),a.get('mode','quick'));return self.send({'ok':True})
   elif path=='/api/file-review/target':
    REVIEW.target(a.get('height'));return self.send({'ok':True})
   elif path=='/api/file-review/cancel':
    REVIEW.set_auto(False);REVIEW.stop();return self.send({'ok':True})
   elif path=='/api/file-review/resume':
    REVIEW.resume();return self.send({'ok':True})
   elif path=='/api/scrape-sites':
    source=a.get('source')
    if source not in NATIVE_SOURCES:raise ValueError('未知内置来源')
    timeout=a.get('timeout',20);retries=a.get('retries',1);base=str(a.get('base_url','')).strip().rstrip('/');cookie=a.get('cookie','')
    if type(timeout) is not int or not 5<=timeout<=60 or type(retries) is not int or not 0<=retries<=2:raise ValueError('超时应为 5–60 秒，重试为 0–2 次')
    if base:
     parsed=urlparse(base)
     if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:raise ValueError('来源地址必须为无凭据的 HTTPS 地址')
    if not isinstance(cookie,str) or len(cookie)>16000 or '\n' in cookie or '\r' in cookie or not cookie.isascii():raise ValueError('Cookie 格式无效')
    cfg=config();options=cfg.setdefault('scrape_site_config',{}).setdefault(source,{})
    options.update(base_url=base,timeout=timeout,retries=retries)
    if cookie:options['cookie']=cookie
    if a.get('clear_cookie'):options.pop('cookie',None)
    (DATA/'settings.json').write_text(json.dumps(cfg,ensure_ascii=False,indent=2),'utf-8');return self.send({'ok':True})
   elif path=='/api/artwork-retry':
    row=self.video(a['id'])
    try:snapshot=json.loads(row['amane_snapshot'] or '{}')
    except ValueError:snapshot={}
    ARTWORK.queue(row['id'],snapshot,snapshot.get('_velvet_engine',config().get('scrape_engine','external')));ARTWORK.retry(row['id'])
    if not AMANE_LOCK.locked():SYNC_CANCEL.clear()
    threading.Thread(target=ARTWORK.process,args=(SYNC_CANCEL,),kwargs={'video_id':row['id']},daemon=True).start();return self.send({'ok':True})
   elif path=='/api/actors-sync':
    names=a.get('names');known={p['name'] for p in actor_library()}
    if not isinstance(names,list) or any(not isinstance(n,str) or n not in known for n in names):raise ValueError('只能同步当前演员库中的演员')
    return self.send({'ok':True,'started':PEOPLE.start(names,bool(a.get('force')))})
   elif path=='/api/category-favorite':
    CATEGORIES.favorite(a.get('kind'),a.get('name'),a.get('favorite'));return self.send({'ok':True})
   elif path=='/api/network/check':return self.send({'ok':True,'started':NETWORK.start(a.get('source_ids'))})
   elif path=='/api/libraries':
    library_path=LIBRARIES.save(a)
    if a.get('scan'):threading.Thread(target=scan,kwargs={'folder_path':library_path},daemon=True).start()
    return self.send({'ok':True})
   elif path=='/api/library-scan':
    library_path=str(Path(a['path']).resolve())
    if library_path not in [p['path'] for p in LIBRARIES.policies()]:raise ValueError('媒体库不存在')
    if LOCK.locked():return self.send({'ok':True,'busy':True})
    threading.Thread(target=scan,kwargs={'folder_path':library_path},daemon=True).start()
    return self.send({'ok':True})
   elif path=='/api/scraping-options':
    sources=a.get('sources');priority=a.get('field_priority',{})
    if not isinstance(sources,list) or not sources or len(sources)>len(NATIVE_SOURCES) or any(not isinstance(s,str) or s not in NATIVE_SOURCES for s in sources) or len(set(sources))!=len(sources):raise ValueError('请选择至少一个有效内置来源')
    allowed={'title','actors','tags','studio','release','plot','poster_urls','thumb_urls','runtime','series','directors','publisher'}
    if not isinstance(priority,dict) or any(k not in allowed or not isinstance(v,list) or len(v)>len(NATIVE_SOURCES) or any(s not in sources for s in v) for k,v in priority.items()):raise ValueError('字段优先级应为字段到已启用来源列表的对象')
    cfg=config();cfg.update(scrape_sources=sources,scrape_field_priority=priority)
    (DATA/'settings.json').write_text(json.dumps(cfg,ensure_ascii=False,indent=2),'utf-8')
    return self.send({'ok':True})
   elif path=='/api/pick-folder':
    return self.send({'path':choose_folder()})
   elif path=='/api/folder':
    p=Path(a['path']).expanduser().resolve()
    if not p.is_dir():raise ValueError('目录不存在')
    if str(p) not in [policy['path'] for policy in LIBRARIES.policies()]:LIBRARIES.save({'path':str(p)})
    threading.Thread(target=scan,daemon=True).start()
   elif path=='/api/remove-folder':
    LIBRARIES.policies()
    with db() as c:
     c.execute('DELETE FROM folders WHERE path=?',(a['path'],))
     c.execute('DELETE FROM library_settings WHERE path=?',(a['path'],))
   elif path=='/api/scan':
    if LOCK.locked():return self.send({'ok':True,'busy':True})
    threading.Thread(target=scan,args=(bool(a.get('force')),),daemon=True).start()
   elif path=='/api/settings':
    cfg=config();cfg.update({k:a[k] for k in DEFAULT if k in a and k in {'interval','online','provider'}})
    if a.get('api_key'):cfg['api_key']=a['api_key']
    cfg['interval']=max(30,min(86400,int(cfg['interval'])))
    if cfg['provider'] and (not cfg['provider'].startswith('https://') or '{code}' not in cfg['provider']):raise ValueError('接口需为包含 {code} 的 HTTPS 地址')
    (DATA/'settings.json').write_text(json.dumps(cfg,ensure_ascii=False,indent=2),'utf-8')
   elif path=='/api/movie-flag':
    field=a.get('field');value=a.get('value')
    if field not in ('favorite','watched') or value not in (0,1):raise ValueError('状态参数无效')
    movie=next((m for m in CORE.hierarchy() if any(p['id']==int(a['id']) for v in m['_versions'] for p in v['_parts'])),None)
    if not movie:raise ValueError('影片不存在')
    versions=movie['_versions'] if field=='favorite' else [v for v in movie['_versions'] if any(p['id']==int(a['id']) for p in v['_parts'])]
    ids=[p['id'] for v in versions for p in v['_parts']]
    with db() as c:
     for vid in ids:
      c.execute('UPDATE videos SET '+field+'=?'+(',position=0' if field=='watched' and value==0 else '')+' WHERE id=?',(int(value),vid))
   elif path=='/api/relationships':
    CORE.relation(a.get('changes'));return self.send({'ok':True})
   elif path=='/api/relationships-reset':CORE.reset(a['ids'])
   elif path=='/api/relationships-undo':CORE.undo()
   elif path=='/api/preferred-version':CORE.prefer(a['id'])
   elif path=='/api/task-cancel':
    kind=a.get('kind')
    if kind=='scan':SCAN_CANCEL.set()
    elif kind in ('sync','images'):SYNC_CANCEL.set()
    elif kind=='duplicates':DUP_CANCEL.set()
    elif kind=='scrape':
     with db() as c:job=c.execute('SELECT * FROM scrape_jobs WHERE id=?',(int(a['id']),)).fetchone()
     if not job:raise ValueError('任务不存在')
     if job['engine']!=config().get('scrape_engine','external'):raise ValueError('任务来源未确认或已切换，不能向当前引擎发送取消')
     if job['remote_id'] is None:raise ValueError('正在提交，尚未获得远程任务编号，请稍后取消')
     amane_request('tasks/batch',{'action':'cancel','task_ids':[job['remote_id']]})
     with db() as c:c.execute("UPDATE scrape_jobs SET status='cancelling' WHERE id=?",(job['id'],))
     poll_scrapes()
    else:raise ValueError('未知任务类型')
   elif path=='/api/backup':return self.send({'ok':True,'name':CORE.backup()})
   elif path=='/api/backup-validate':
    CORE.validate_backup(a['name']);return self.send({'ok':True})
   elif path=='/api/restore':
    if ARTWORK.status['running']:raise ValueError('请等待剧照下载完成后再恢复备份')
    if PEOPLE.state['running']:raise ValueError('请等待演员资料与头像同步完成后再恢复备份')
    if any(engine.worker_lock.locked() for engine in BUILTIN_ENGINES.values()):raise ValueError('请等待内置刮削任务停止后再恢复备份')
    if a.get('confirm')!=a.get('name'):raise ValueError('请明确确认恢复的备份名称')
    locks=[LOCK,AMANE_LOCK,DUP_LOCK,SUBMIT_LOCK,POLL_LOCK,ARTWORK.lock,REVIEW.lock];acquired=[]
    organization_was_running=bool(ORGANIZATION.thread and ORGANIZATION.thread.is_alive())
    with POST_LEASE:
     if ACTIVE_POSTS>1:raise ValueError('其它修改请求运行中，请稍后恢复')
     RESTORING.set()
    try:
     ORGANIZATION.stop()
     if ORGANIZATION.thread:ORGANIZATION.thread.join(3)
     if ORGANIZATION.thread and ORGANIZATION.thread.is_alive():raise ValueError('整理服务正在停止，请稍后重试')
     ORGANIZATION.before_restore()
     for lock in locks:
      if not lock.acquire(False):raise ValueError('后台任务运行中，请先暂停自动刮削并取消扫描/同步后重试')
      acquired.append(lock)
     with db() as c:
      if c.execute("SELECT COUNT(*) FROM scrape_jobs WHERE status IN ('queued','running','submitting','cancelling')").fetchone()[0]:raise ValueError('仍有刮削任务，请先在任务中心取消并等待结束后恢复')
     with DB_MUTEX:
      rollback=CORE.restore(a['name']);REVIEW.ensure(recover=True);ARTWORK.ensure(recover=True);DIRECTORY_POLICY.ensure();JOURNAL.ensure();JOURNAL.recover();INVENTORY.ensure();LEASES.ensure();LEASES.recover();ORGANIZATION.__init__(db,INVENTORY);INVENTORY_QUERY.__init__(INVENTORY);BROWSE.__init__(db,CORE,LIBRARIES,DIRECTORY_POLICY,config);ORGANIZATION.locations.migrate();ORGANIZATION.jobs.recover()
      with db() as c:
       if 'engine' not in {r[1] for r in c.execute('PRAGMA table_info(scrape_jobs)')}:c.execute("ALTER TABLE scrape_jobs ADD COLUMN engine TEXT DEFAULT ''")
    finally:
     for lock in reversed(acquired):lock.release()
     RESTORING.clear()
     if organization_was_running and not SHUTTING_DOWN.is_set():ORGANIZATION.start()
    return self.send({'ok':True,'rollback':rollback})
   elif path=='/api/field-apply':
    row=self.video(a['id']);snapshot=json.loads(row['amane_snapshot'] or '{}');field=a['field'];values={'title':snapshot.get('title'),'code':snapshot.get('number'),'actors':', '.join(snapshot.get('actors') or []),'studio':snapshot.get('studio'),'date':snapshot.get('release'),'plot':snapshot.get('plot')}
    if field not in values or not values[field]:raise ValueError('来源未返回此字段')
    locks=set(json.loads(row['locked_fields'] or '[]'));locks.discard(field)
    with db() as c:c.execute('UPDATE videos SET '+field+'=?,locked_fields=?,manual=0 WHERE id=?',(values[field],json.dumps(sorted(locks)),row['id']))
   elif path=='/api/field-lock':
    row=self.video(a['id']);field=a['field']
    if field not in ('title','code','actors','studio','date','plot'):raise ValueError('字段无效')
    locks=set(json.loads(row['locked_fields'] or '[]'))
    if a.get('locked'):locks.add(field)
    else:locks.discard(field)
    with db() as c:c.execute('UPDATE videos SET locked_fields=?,manual=0 WHERE id=?',(json.dumps(sorted(locks)),row['id']))
   elif path=='/api/actor-edit':
    name=str(a.get('name','')).strip()
    if not name or len(name)>200:raise ValueError('演员名称无效')
    with db() as c:
     old=c.execute('SELECT * FROM actor_preferences WHERE name=?',(name,)).fetchone()
     rating=a.get('rating',old['rating'] if old else 0);favorite=a.get('favorite',old['favorite'] if old else 0)
     if type(rating) is not int or not 0<=rating<=5:raise ValueError('评分范围为 0–5 星')
     if favorite not in (0,1,False,True):raise ValueError('收藏状态无效')
     c.execute('INSERT INTO actor_preferences VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET rating=excluded.rating,favorite=excluded.favorite',(name,rating,int(favorite)))
   elif path=='/api/auto-scrape':
    cfg=config();cfg['auto_scrape']=bool(a['enabled']);(DATA/'settings.json').write_text(json.dumps(cfg,ensure_ascii=False,indent=2),'utf-8');AUTO_STATUS['enabled']=cfg['auto_scrape']
   elif path=='/api/duplicates-scan':
    threading.Thread(target=detect_duplicates,daemon=True).start()
   elif path=='/api/duplicates-choice':
    group=next((g for g in duplicate_groups() if g['key']==a['key']),None)
    if not group:raise ValueError('重复分组已变化，请刷新')
    preferred=a.get('preferred_id')
    if preferred is not None and preferred not in [v['id'] for v in group['videos']]:raise ValueError('保留项必须属于该组')
    with db() as c:
     if preferred is None:c.execute('DELETE FROM duplicate_choices WHERE group_key=?',(a['key'],))
     else:
      before={}
      for video in group['videos']:
       link=c.execute('SELECT movie_key FROM media_links WHERE video_id=?',(video['id'],)).fetchone()
       if link:
        pref=c.execute('SELECT * FROM movie_preferences WHERE movie_key=?',(link[0],)).fetchone();before[link[0]]=dict(pref) if pref else None
      c.execute('INSERT OR IGNORE INTO duplicate_preference_history VALUES(?,?)',(a['key'],json.dumps(before)))
      c.execute('INSERT INTO duplicate_choices VALUES(?,?) ON CONFLICT(group_key) DO UPDATE SET preferred_id=excluded.preferred_id',(a['key'],preferred))
    if preferred is not None:CORE.prefer(preferred)
    else:
     with db() as c:
      saved=c.execute('SELECT payload FROM duplicate_preference_history WHERE group_key=?',(a['key'],)).fetchone()
      if saved:
       for movie_key,pref in json.loads(saved[0]).items():
        c.execute('DELETE FROM movie_preferences WHERE movie_key=?',(movie_key,))
        if pref:c.execute('INSERT INTO movie_preferences(movie_key,version_key,preferred_id) VALUES(?,?,?)',(pref['movie_key'],pref['version_key'],pref.get('preferred_id')))
       c.execute('DELETE FROM duplicate_preference_history WHERE group_key=?',(a['key'],))
      else:
       for video in group['videos']:
        link=c.execute('SELECT movie_key FROM media_links WHERE video_id=?',(video['id'],)).fetchone()
        if link:c.execute('DELETE FROM movie_preferences WHERE movie_key=?',(link[0],))
   elif path=='/api/amane-submit':
    return self.send(submit_scrapes(a.get('ids')))
   elif path=='/api/amane-poll':
    poll_scrapes();return self.send({'ok':True})
   elif path=='/api/unlock':
    with db() as c:c.execute('UPDATE videos SET locked_fields=?,manual=0 WHERE id=?',('[]',int(a['id'])))
   elif path=='/api/amane-settings':
    cfg=config();engine=a.get('engine',cfg.get('scrape_engine','external'))
    if engine not in ('builtin','external'):raise ValueError('未知刮削引擎')
    if engine!=cfg.get('scrape_engine','external'):
     if AMANE_LOCK.locked() or ARTWORK.status['running']:raise ValueError('请等待资料与图片同步完成，或在任务中心取消同步后再切换引擎')
     with db() as c:
      if c.execute("SELECT 1 FROM scrape_jobs WHERE status IN ('queued','running','submitting','cancelling')").fetchone():raise ValueError('请先完成或取消当前刮削任务，再切换引擎')
     if engine=='builtin':
      from builtin_scraper import source_types
      source_types()
    proxy=str(a.get('proxy',cfg.get('scrape_proxy',''))).strip()
    if proxy:
     proxy_url=urlparse(proxy)
     if proxy_url.scheme not in ('http','https') or not proxy_url.hostname or proxy_url.username:raise ValueError('请填写无用户名密码的 HTTP 代理地址')
    cfg['scrape_engine']=engine;cfg['scrape_proxy']=proxy;cfg['amane_url']=str(a.get('url',cfg['amane_url'])).rstrip('/')
    parsed=urlparse(cfg['amane_url'])
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username:raise ValueError('服务地址无效')
    if parsed.scheme=='http' and parsed.hostname not in ('127.0.0.1','localhost','::1'):raise ValueError('远程连接需要 HTTPS')
    if a.get('token'):cfg['amane_token']=str(a['token'])
    aliases=a.get('aliases',{})
    if not isinstance(aliases,dict) or not all(isinstance(k,str) and isinstance(v,str) for k,v in aliases.items()):raise ValueError('别名应为 JSON 对象')
    cfg['tag_aliases']=aliases
    (DATA/'settings.json').write_text(json.dumps(cfg,ensure_ascii=False,indent=2),'utf-8');retag_all()
   elif path=='/api/amane-sync':
    if AMANE_LOCK.locked():return self.send({'ok':True,'busy':True,'count':AMANE_STATUS['synced']})
    return self.send({'ok':True,'count':sync_amane()})
   elif path=='/api/retag':
    return self.send({'ok':True,'count':retag_all()})
   elif path=='/api/tag-rules':
    rules=a.get('rules',[])
    if not isinstance(rules,list) or len(rules)>100:raise ValueError('最多 100 条规则')
    for rule in rules:
     if not isinstance(rule,dict) or not isinstance(rule.get('tag'),str) or not isinstance(rule.get('keywords'),list) or not all(isinstance(k,str) for k in rule['keywords']):raise ValueError('规则格式应为 tag 与 keywords 数组')
    (DATA/'tag_rules.json').write_text(json.dumps(rules,ensure_ascii=False,indent=2),'utf-8')
    return self.send({'ok':True,'count':retag_all()})
   elif path=='/api/edit':
    allowed={'title','tags','actors','studio','code','plot','date','favorite','watched','rating','censor_override'};updates={k:v for k,v in a.items() if k in allowed}
    if 'censor_override' in updates and updates['censor_override'] not in ('auto','censored','uncensored','unknown'):raise ValueError('有码/无码标记无效')
    with db() as c:old=c.execute('SELECT * FROM videos WHERE id=?',(int(a['id']),)).fetchone()
    if not old:raise ValueError('视频不存在')
    locked=set(json.loads(old['locked_fields'] or '[]'))
    if old['manual'] and not locked:locked.update(['title','code','actors','studio','date','plot'])
    locked.update(k for k,v in updates.items() if k in {'title','code','actors','studio','date','plot','tags'} and str(v)!=str(old[k]))
    updates['locked_fields']=json.dumps(sorted(locked));updates['manual']=0
    if 'code' in updates and updates['code']!=old['code']:
     updates.update(amane_id=0,source_tags='',amane_snapshot='{}',source='filename',poster_origin='',cover_origin='')
     (DATA/f"{old['id']}-poster.jpg").unlink(missing_ok=True)
     (DATA/f"{old['id']}-cover.jpg").unlink(missing_ok=True)
    tags,reasons=classify(dict(old)|updates);updates.update(auto_tags=tags,tag_reasons=reasons)
    with db() as c:c.execute('UPDATE videos SET '+','.join(k+'=?' for k in updates)+' WHERE id=?',[*updates.values(),int(a['id'])])
   elif path=='/api/progress':
    with db() as c:c.execute('UPDATE videos SET position=?,watched=CASE WHEN ? THEN 1 ELSE watched END WHERE id=?',(max(0,float(a['position'])),bool(a.get('ended')),int(a['id'])))
   elif path=='/api/play':
    if NAS:raise ValueError('NAS 模式请使用浏览器播放，或通过 SMB 在电脑播放器打开原文件')
    row=self.video(a['id'])
    if not Path(row['path']).is_file():raise ValueError('文件不存在或磁盘未连接')
    launch(row['path'])
   elif path=='/api/reveal':
    if NAS:raise ValueError('NAS 模式下请通过 SMB 访问媒体目录')
    launch(str(Path(self.video(a['id'])['path']).parent))
   elif path=='/api/forget':
    with db() as c:c.execute('DELETE FROM videos WHERE id=?',(int(a['id']),))
   else:return self.send({},status=404)
   self.send({'ok':True})
  except (ResourceBusy,InventoryQueryChanged,LibraryQueryChanged) as e:self.send({'error':str(e)},status=409)
  except Exception as e:self.send({'error':str(e)},status=400)
def task_resources():
 cfg=config()
 with db() as c:
  queued_images=c.execute("SELECT COUNT(*) FROM artwork_jobs WHERE active=1 AND status='queued'").fetchone()[0]
  queued_reviews=c.execute("SELECT COUNT(*) FROM file_reviews WHERE status='queued'").fetchone()[0]
 reasons=[]
 if SHUTTING_DOWN.is_set():reasons.append('service_draining')
 if LOCK.locked():reasons.append('scan_resource_busy')
 if any(row['mode']=='write' for row in LEASES.active()):reasons.append('media_resource_busy')
 if any(not Path(policy['path']).is_dir() for policy in LIBRARIES.policies()):reasons.append('library_offline')
 if not cfg.get('auto_scrape'):reasons.append('automatic_scrape_paused')
 if not binary('ffprobe'):reasons.append('ffprobe_missing')
 if not REVIEW.auto_settings().get('available'):reasons.append('ocr_dependency_missing')
 return {'reasons':reasons,'images_queued':queued_images,'reviews_queued':queued_reviews,
         'limits':{'metadata_workers':1,'image_batch':24,'image_batch_seconds':45,'review_workers':1,'automatic_review_queue':1},
         'engines':[engine.resource_summary() for engine in BUILTIN_ENGINES.values()]}


def video_included(vid):
 with db() as c:row=c.execute('SELECT * FROM videos WHERE id=?',(vid,)).fetchone()
 return bool(row and LIBRARIES.filter_rows([dict(row)]))

def inventory_metadata(path):
 try:
  shared=path.parent/'movie.nfo'
  use_shared=shared.is_file() and not path.with_suffix('.nfo').is_file() and sum(p.suffix.lower() in {'.mp4','.mkv','.avi','.mov','.wmv','.m4v','.ts','.webm','.mpeg','.mpg','.flv'} for p in path.parent.iterdir())==1
  local=read_metadata(path,shared if use_shared else None)
 except Exception as error:local={'title':fallback_title(path),'code':identify(path.stem) or identify(path.parent.name),'source':'filename','metadata_warning':type(error).__name__}
 candidates={'filename':path.stem,'directory':path.parent.name,'nfo':local.get('title') if local.get('source')=='nfo' else None}
 with db() as c:row=c.execute('SELECT * FROM videos WHERE path=?',(str(path),)).fetchone()
 if row:
  metadata={key:row[key] for key in ('title','code','width','height','duration','codec','source','locked_fields')}
  candidates['existing_metadata']=row['title'];metadata['candidates']=candidates
  metadata['issues']=REVIEW.snapshot([dict(row)])['files'].get(row['id'],{}).get('issues',[])
  return metadata
 metadata=dict(local,candidates=candidates);probe=binary('ffprobe')
 if probe:
  result=run([probe,'-v','error','-show_format','-show_streams','-of','json',str(path)],45)
  if result.returncode:metadata['issues']=[{'code':'probe_error','label':'无法解析，需复核'}]
  else:
   info=json.loads(result.stdout);stream=next((s for s in info.get('streams',[]) if s.get('codec_type')=='video'),{})
   metadata.update(width=stream.get('width',0),height=stream.get('height',0),codec=stream.get('codec_name',''),duration=float(info.get('format',{}).get('duration') or 0))
 return metadata

def run_inventory(task_key,device_id,hashing):
 try:
  with LEASES.hold('inventory'):
   return run_inventory_leased(task_key,device_id,hashing)
 except ResourceBusy:JOURNAL.observe('inventory',task_key,'waiting',{'reason':'media_resource_busy'})
 finally:LOCK.release()

def run_inventory_leased(task_key,device_id,hashing):
 try:
  progress=lambda **counts:JOURNAL.observe('inventory',task_key,'running',{**counts,'phase':'hashing' if hashing else 'metadata','cancellable':True})
  result=INVENTORY.scan(device_id,SCAN_CANCEL.is_set,progress,hashing)
  JOURNAL.observe('inventory',task_key,result['state'],{'processed':result['processed'],'failed':result['failed'],'bytes_processed':result['bytes_processed']})
 except Exception as error:JOURNAL.observe('inventory',task_key,'failed',{'reason':type(error).__name__})

class LocalServer(ThreadingHTTPServer):
 allow_reuse_address=False
 def server_bind(self):
  if os.name=='nt':self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
  super().server_bind()

if __name__=='__main__':
 if NAS and len(PASSWORD)<12:raise SystemExit('NAS 模式必须设置至少 12 位 VELVET_PASSWORD')
 try:server=LocalServer((BIND,RUNTIME.port),Handler)
 except OSError as error:raise SystemExit('服务端口不可用，请检查已有实例或使用 --port；系统错误码 '+str(error.errno)) from None
 ORGANIZATION.locations.migrate();ORGANIZATION.jobs.recover()
 print('正在检查片库查询索引，首次升级大库可能需要一些时间…',flush=True)
 BROWSE.refresh()
 if not any((DATA/'backups').glob('auto-'+time.strftime('%Y%m%d')+'*.zip')):
  with LEASES.hold('backup','write'):CORE.backup('auto-'+time.strftime('%Y%m%d'))
 JOURNAL.recover()
 JOURNAL.reconcile_sources(uncertain_remote=True)
 REVIEW.resume()
 ORGANIZATION.start()
 threading.Thread(target=watcher,daemon=True).start()
 threading.Thread(target=amane_watcher,daemon=True).start()
 (DATA/'runtime-instance.json').write_text(json.dumps({'url':RUNTIME.local_url,'token':TOKEN,'pid':os.getpid()}),'utf-8')
 print('Velvet: '+RUNTIME.local_url+'  Ctrl+C 退出',flush=True)
 if not NAS and not RUNTIME.no_browser:webbrowser.open(RUNTIME.local_url)
 try:server.serve_forever()
 except KeyboardInterrupt:pass
 finally:
  ORGANIZATION.stop()
  if ORGANIZATION.thread:ORGANIZATION.thread.join(10)
  server.server_close()
  try:
   descriptor=DATA/'runtime-instance.json'
   if json.loads(descriptor.read_text('utf-8')).get('token')==TOKEN:descriptor.unlink()
  except (OSError,ValueError):pass
