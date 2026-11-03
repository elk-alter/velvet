"""Local actor portraits and library facets; never writes to a media directory."""
import hashlib
import json
import os
import re
import threading
import time
import unicodedata
from pathlib import Path
from urllib.parse import quote, urlparse
from scrapers.compat import fetch_bytes


def actor_key(name):
    return hashlib.sha256(unicodedata.normalize('NFC', name).encode()).hexdigest()


def image_payload(payload):
    return len(payload)<=12_000_000 and (payload.startswith(b'\xff\xd8') or payload.startswith(b'\x89PNG') or payload.startswith(b'RIFF'))


class PeopleCache:
    def __init__(self, db, data, config, request, image_loader):
        self.db,self.data,self.config,self.request,self.image_loader=db,Path(data),config,request,image_loader
        self.lock=threading.Lock()
        self.state={'running':False,'processed':0,'matched':0,'failed':0,'message':'头像按需自动补全'}
        self.data.joinpath('actors').mkdir(exist_ok=True)
        self.ensure()

    def ensure(self):
        with self.db() as c:
            c.execute("CREATE TABLE IF NOT EXISTS actor_profiles(name TEXT PRIMARY KEY,profile TEXT DEFAULT '{}',origin TEXT DEFAULT '',updated REAL DEFAULT 0,error TEXT DEFAULT '')")

    def profiles(self):
        self.ensure()
        with self.db() as c:return {r['name']:dict(r) for r in c.execute('SELECT * FROM actor_profiles')}

    def start(self, names, force=False):
        if not isinstance(names,list) or len(names)>36 or not all(isinstance(n,str) and 0<len(n)<=200 for n in names):
            raise ValueError('每批最多补全 36 位演员')
        if not self.lock.acquire(False):return False
        self.state.update(running=True,processed=0,matched=0,failed=0,message='正在获取演员头像')
        threading.Thread(target=self.run,args=(list(dict.fromkeys(names)),force),daemon=True).start()
        return True

    def index(self):
        candidates=[self.data/'gfriends-index.json']
        if os.environ.get('LOCALAPPDATA'):candidates.append(Path(os.environ['LOCALAPPDATA'])/'Amane'/'gfriends.json')
        for path in candidates:
            if path.is_file():
                try:
                    result=json.loads(path.read_text(encoding='utf-8'))
                    if isinstance(result,dict):return result
                except (OSError,ValueError):pass
        body,_=fetch_bytes('https://raw.githubusercontent.com/gfriends/gfriends/master/Filetree.json',self.config().get('scrape_proxy',''),limit=12_000_000)
        content=json.loads(body).get('Content',{})
        result={}
        # Same filename-first lookup semantics as Amane's GFriends adapter.
        for folder in sorted(content):
            if not isinstance(content[folder],dict):continue
            for filename,value in content[folder].items():
                target=str(value or filename).split('?')[0]
                result.setdefault(filename,{'url':'https://raw.githubusercontent.com/gfriends/gfriends/master/Content/'+quote(folder,safe='')+'/'+quote(target,safe='')})
        self.data.joinpath('gfriends-index.json').write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
        return result

    def run(self,names,force):
        try:
            profiles=self.profiles();index=None;remote_available=True
            for name in names:
                target=self.data/'actors'/(actor_key(name)+'.jpg')
                old=profiles.get(name,{})
                if not force and (target.exists() or time.time()-old.get('updated',0)<86400):
                    self.state['processed']+=1;self.state['matched']+=int(target.exists());continue
                try:profile=json.loads(old.get('profile') or '{}')
                except ValueError:profile={}
                origin=old.get('origin','');payload=None
                if remote_available:
                    try:
                        rows=self.request('actors?limit=20&search='+quote(name,safe='')).get('items',[])
                        exact=[row for row in rows if unicodedata.normalize('NFC',row.get('name','')).casefold()==unicodedata.normalize('NFC',name).casefold()]
                        if len(exact)==1:
                            row=exact[0]
                            detail=self.request('actors/'+str(row['id']))
                            profile.update({key:detail[key] for key in ('overview','birthday','height','aliases','gender') if detail.get(key)})
                            urls=detail.get('image_urls') or row.get('image_urls') or []
                            if urls:
                                payload=self.image_loader(urls[0]);origin='Amane'
                    except Exception:remote_available=False
                try:
                    if payload is None:
                        if index is None:index=self.index()
                        hit=next((index.get(name+ext) for ext in ('.jpg','.png','.JPG','.PNG') if index.get(name+ext)),None)
                        url=hit.get('url','') if isinstance(hit,dict) else hit if isinstance(hit,str) else ''
                        if url and urlparse(url).hostname in ('raw.githubusercontent.com','cdn.jsdelivr.net'):
                            payload,kind=fetch_bytes(url,self.config().get('scrape_proxy',''),limit=12_000_000)
                            if not kind.startswith('image/'):payload=None
                            origin='GFriends'
                    if payload is not None and image_payload(payload):
                        temp=target.with_suffix('.part');temp.write_bytes(payload);temp.replace(target)
                        error='';self.state['matched']+=1
                    else:error='未找到精确匹配的头像';self.state['failed']+=1
                except Exception:
                    error='头像下载失败，可稍后重试';self.state['failed']+=1
                with self.db() as c:
                    c.execute('INSERT INTO actor_profiles(name,profile,origin,updated,error) VALUES(?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET profile=excluded.profile,origin=excluded.origin,updated=excluded.updated,error=excluded.error',(name,json.dumps(profile,ensure_ascii=False),origin,time.time(),error))
                self.state['processed']+=1
            self.state['message']=f"头像 {self.state['matched']}/{self.state['processed']} · 未完成 {self.state['failed']}"
        finally:self.state['running']=False;self.lock.release()


class FacetCatalog:
    def __init__(self,db,config):
        self.db,self.config=db,config
        self.ensure()

    def ensure(self):
        with self.db() as c:c.execute('CREATE TABLE IF NOT EXISTS category_preferences(kind TEXT,name TEXT,favorite INTEGER DEFAULT 0,PRIMARY KEY(kind,name))')

    def list(self):
        self.ensure()
        with self.db() as c:
            rows=[dict(r) for r in c.execute('SELECT * FROM videos WHERE missing=0')]
            favorites={(r['kind'],r['name']):r['favorite'] for r in c.execute('SELECT * FROM category_preferences')}
            links={r['video_id']:r['movie_key'] for r in c.execute('SELECT video_id,movie_key FROM media_links')}
        rows=getattr(self,'filter_rows',lambda rows:rows)(rows)
        aliases=self.config().get('tag_aliases',{});groups={}
        def split(value):return [s.strip() for s in re.split(r'[,，;；\n]',value or '') if s.strip()]
        for row in rows:
            try:snapshot=json.loads(row.get('amane_snapshot') or '{}')
            except ValueError:snapshot={}
            values={'tags':[aliases.get(tag,tag) for tag in split(row.get('source_tags'))], 'user_tags':split(row.get('tags')), 'technical':[tag for tag in split(row.get('auto_tags')) if not tag.startswith('题材/')], 'studio':[row['studio']] if row['studio'] else [], 'series':[snapshot['series']] if isinstance(snapshot.get('series'),str) and snapshot['series'] else [], 'director':snapshot.get('directors') or []}
            for kind,names in values.items():
                if not isinstance(names,list):names=[]
                for name in set(n for n in names if isinstance(n,str) and n):
                    item=groups.setdefault((kind,name),{'kind':kind,'name':name,'ids':set(),'movies':set(),'favorite':favorites.get((kind,name),0)})
                    item['ids'].add(row['id']);item['movies'].add(links.get(row['id']) or row['code'].upper() or 'file:'+str(row['id']))
        return [{**{k:v for k,v in item.items() if k not in ('ids','movies')},'ids':sorted(item['ids']),'movie_count':len(item['movies']),'file_count':len(item['ids']),'cover_id':min(item['ids'])} for item in groups.values()]

    def favorite(self,kind,name,value):
        if kind not in ('tags','user_tags','technical','studio','series','director') or not isinstance(name,str) or not 0<len(name)<=200 or value not in (0,1,True,False):raise ValueError('分类收藏参数无效')
        self.ensure()
        with self.db() as c:c.execute('INSERT INTO category_preferences VALUES(?,?,?) ON CONFLICT(kind,name) DO UPDATE SET favorite=excluded.favorite',(kind,name,int(value)))
