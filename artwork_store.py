"""Persistent still-image jobs and local screenshot fallback."""
import threading
import time
from pathlib import Path
from urllib.parse import urljoin


def still_urls(metadata):
    result=[]
    for key in ('extrafanart','extrafanart_urls'):
        value=metadata.get(key,[])
        values=[url for group in value.values() if isinstance(group,list) for url in group] if isinstance(value,dict) else value if isinstance(value,list) else []
        for url in values:
            if not isinstance(url,str):continue
            if url.startswith('//'):url='https:'+url
            elif url.startswith('/') and not url.startswith('/api/resources/') and str(metadata.get('source_url','')).startswith('https://'):url=urljoin(metadata['source_url'],url)
            if url.startswith(('https://','http://','/api/resources/')):result.append(url)
    return list(dict.fromkeys(result))[:12]


class ArtworkStore:
    def __init__(self,db,data,fetcher):
        self.db,self.data,self.fetcher=db,Path(data),fetcher
        self.lock=threading.Lock()
        self.priority=None
        self.status={'running':False,'processed':0,'failed':0}
        self.ensure(recover=True)

    def ensure(self,recover=False):
        with self.db() as c:
            c.execute("CREATE TABLE IF NOT EXISTS artwork_jobs(video_id INTEGER,ordinal INTEGER,url TEXT,engine TEXT,referer TEXT,status TEXT DEFAULT 'queued',error TEXT DEFAULT '',updated REAL DEFAULT 0,active INTEGER DEFAULT 1,PRIMARY KEY(video_id,ordinal))")
            if recover:c.execute("UPDATE artwork_jobs SET status='queued' WHERE status='running'")

    def queue(self,video_id,metadata,engine):
        if not getattr(self,'allowed',lambda vid:True)(video_id):return
        self.ensure()
        urls=still_urls(metadata)
        if not urls:return
        with self.db() as c:
            c.execute('UPDATE artwork_jobs SET active=0 WHERE video_id=?',(video_id,))
            for ordinal,url in enumerate(urls,1):
                c.execute("INSERT INTO artwork_jobs(video_id,ordinal,url,engine,referer) VALUES(?,?,?,?,?) ON CONFLICT(video_id,ordinal) DO UPDATE SET status=CASE WHEN url=excluded.url AND engine=excluded.engine THEN status ELSE 'queued' END,url=excluded.url,engine=excluded.engine,referer=excluded.referer,active=1",(video_id,ordinal,url,engine,metadata.get('source_url') or ''))

    def process(self,cancel,limit=24,video_id=None):
        if video_id is not None:self.priority=video_id
        if not self.lock.acquire(False):return
        leases=getattr(self,'leases',None)
        resource_owner=leases.try_acquire('images') if leases else None
        if leases and resource_owner is None:self.lock.release();return
        if self.priority is not None:video_id=self.priority;self.priority=None
        self.status.update(running=True,processed=0,failed=0)
        started=time.monotonic()
        try:
            self.ensure()
            with self.db() as c:
                for row in c.execute("SELECT video_id,ordinal FROM artwork_jobs WHERE status='done' AND active=1").fetchall():
                    if not self.target(row['video_id'],row['ordinal']).is_file():c.execute("UPDATE artwork_jobs SET status='queued' WHERE video_id=? AND ordinal=?",tuple(row))
                scope=' AND video_id=?' if video_id is not None else ''
                parameters=[time.time()-3600]+([video_id] if video_id is not None else [])+[limit]
                jobs=[dict(row) for row in c.execute("SELECT * FROM artwork_jobs WHERE active=1 AND (status='queued' OR (status='failed' AND updated<?))"+scope+" ORDER BY updated,video_id,ordinal LIMIT ?",parameters)]
            for row in jobs:
                if cancel.is_set() or time.monotonic()-started>45:break
                if not getattr(self,'allowed',lambda vid:True)(row['video_id']):
                    with self.db() as c:c.execute("UPDATE artwork_jobs SET status='cancelled',error='目录已排除' WHERE video_id=? AND ordinal=?",(row['video_id'],row['ordinal']))
                    continue
                with self.db() as c:c.execute("UPDATE artwork_jobs SET status='running' WHERE video_id=? AND ordinal=?",(row['video_id'],row['ordinal']))
                getattr(self,'observe',lambda *args:None)(row['video_id'],row['ordinal'],'running')
                try:ok=self.fetcher(row['url'],self.target(row['video_id'],row['ordinal']),row['engine'],row['referer'])
                except Exception:ok=False
                if not getattr(self,'allowed',lambda vid:True)(row['video_id']):
                    with self.db() as c:c.execute("UPDATE artwork_jobs SET status='cancelled',error='目录已排除，结果跳过' WHERE video_id=? AND ordinal=?",(row['video_id'],row['ordinal']))
                    continue
                with self.db() as c:c.execute('UPDATE artwork_jobs SET status=?,error=?,updated=? WHERE video_id=? AND ordinal=? AND url=? AND engine=?',('done' if ok else 'failed','' if ok else '剧照下载失败，可单独重试',time.time(),row['video_id'],row['ordinal'],row['url'],row['engine']))
                getattr(self,'observe',lambda *args:None)(row['video_id'],row['ordinal'],'done' if ok else 'failed')
                self.status['processed']+=1;self.status['failed']+=int(not ok)
        finally:
            self.status['running']=False
            if leases:leases.release(resource_owner)
            self.lock.release()

    def target(self,video_id,ordinal):return self.data/f'{video_id}-still-{ordinal}.jpg'

    def retry(self,video_id):
        self.ensure()
        with self.db() as c:c.execute("UPDATE artwork_jobs SET status='queued',error='' WHERE video_id=? AND active=1 AND status='failed'",(video_id,))

    def manifest(self,video_id):
        self.ensure()
        with self.db() as c:rows=[dict(r) for r in c.execute('SELECT ordinal,status FROM artwork_jobs WHERE video_id=? AND active=1 ORDER BY ordinal',(video_id,))]
        items=[{'url':f"/thumb/{video_id}-still-{row['ordinal']}.jpg",'kind':'still','label':'剧照 '+str(row['ordinal'])} for row in rows if row['status']=='done' and self.target(video_id,row['ordinal']).is_file()]
        items.extend({'url':f"/thumb/{video_id}{'-'+str(i) if i else ''}.jpg",'kind':'screenshot','label':'本地截图 '+str(i+1)} for i in range(6) if (self.data/f"{video_id}{'-'+str(i) if i else ''}.jpg").is_file())
        return {'items':items,'stills':sum(i['kind']=='still' for i in items),'screenshots':sum(i['kind']=='screenshot' for i in items),'pending':sum(r['status'] in ('queued','running') for r in rows),'failed':sum(r['status']=='failed' for r in rows)}
