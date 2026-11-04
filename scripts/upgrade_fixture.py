"""Synthetic upgrade fixtures; never called against a user's data directory."""
import base64
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

# Queue rows that intentionally recover are checked separately from user decisions.
TABLES = {
    'videos':'', 'media_links':'', 'movie_preferences':'', 'actor_preferences':'',
    'actor_profiles':'', 'category_preferences':'', 'excluded_directories':'',
    'collection_rules':'', 'library_settings':'', 'folders':'',
    'duplicate_choices':'', 'duplicate_preference_history':'', 'relationship_undo':'',
    'app_state':'', 'review_settings':'', 'file_reviews':'WHERE video_id=1',
    'artwork_jobs':"WHERE status='done'", 'scrape_jobs':"WHERE id=1",
}
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aK1cAAAAASUVORK5CYII=')


def seed(app,source):
    source=Path(source);data=app.DATA
    media=source/'Example.mp4';second=source/'Second.mp4';second.write_bytes(b'synthetic pending review')
    (source/'Example.en.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\nSynthetic subtitle\n',encoding='utf-8')
    (source/'Example.nfo').write_text('<movie><title>Untrusted automatic title</title></movie>',encoding='utf-8')
    (source/'sentinel.txt').write_bytes(b'never organize this file')
    app.LIBRARIES.exclusions()
    with app.db() as c:
        for vid,path,title in ((1,media,'Manually locked title'),(2,second,'Pending inspection')):
            c.execute('INSERT INTO videos(id,path,title,size,mtime,manual,locked_fields,favorite,position,tags,code,rating,watched,cover_origin,poster_origin,amane_snapshot,censor_override) VALUES(?,?,?,?,?,1,?,1,42,?,?,?,?,?,?,?,?)',
                      (vid,str(path),title,path.stat().st_size,path.stat().st_mtime,'["title","actors"]','Example','EXAMPLE-00'+str(vid),4,1,'manual','manual',json.dumps({'title':'Remote title','actors':['Synthetic Person']}),'censored'))
            c.execute('INSERT INTO media_links VALUES(?,?,?,?,1)',(vid,'manual-movie','manual-version',vid))
        c.execute("UPDATE videos SET actors='Synthetic Person',studio='Synthetic Studio',date='2020-01-01',plot='Synthetic plot',source='amane',amane_id=117,source_tags='Drama',auto_tags='Synthetic',tag_reasons=?,width=3840,height=2160,duration=7200,codec='h265'",(json.dumps({'Synthetic':['filename rule']}),))
        c.execute("INSERT INTO movie_preferences VALUES('manual-movie','manual-version',1)")
        c.execute("INSERT INTO actor_preferences VALUES('Synthetic Person',5,1)")
        c.execute("INSERT INTO actor_profiles VALUES('Synthetic Person',?,'synthetic',1,'')",(json.dumps({'name':'Synthetic Person','birth_date':'2000-01-01'}),))
        c.execute("INSERT INTO category_preferences VALUES('tag','Example',1)")
        c.execute('INSERT INTO folders VALUES(?)',(str(source),))
        c.execute('INSERT INTO library_settings(path,name,automation,exclude_patterns,min_size,settle_seconds) VALUES(?,?,?, ?,0,0)',(str(source),'Synthetic library','scan','["skip/**"]'))
        c.execute('INSERT INTO excluded_directories VALUES(?)',(str(source/'Offline'),))
        c.execute('INSERT INTO collection_rules VALUES(?,?)',(str(source),'group'))
        c.execute("INSERT INTO duplicate_choices VALUES('synthetic-duplicate',1)")
        c.execute("INSERT INTO duplicate_preference_history VALUES('synthetic-duplicate',?)",(json.dumps({'preferred_id':1}),))
        c.execute('INSERT INTO relationship_undo VALUES(1,1,?)',(json.dumps([{'video_id':1,'movie_key':'old','version_key':'old','part_no':1,'manual':1}]),))
        c.execute("INSERT INTO app_state VALUES('synthetic-choice','retained')")
        c.execute("INSERT INTO review_settings VALUES('auto_enabled','false')")
        findings=[{'code':'suspected_watermark','label':'Synthetic corner mark','confidence':.8,'samples':[{'frame':1,'at':1,'text':'SYNTHETIC'}]}]
        c.execute('INSERT INTO file_reviews(video_id,size,mtime,flags,note,decision,status,mode,result,auto_findings,auto_overrides,auto_status,auto_generation,auto_attempts) VALUES(1,?,?,?,?,?,?,?,?,?,?,?,?,2)',
                  (media.stat().st_size,media.stat().st_mtime,'["replace"]','Synthetic manual note','accepted','done','auto','passed',json.dumps(findings),'{"suspected_watermark":"dismissed"}','done','a'*32))
        c.execute("INSERT INTO file_reviews(video_id,size,mtime,status,detail) VALUES(2,?,?,'running','synthetic interrupted inspection')",(second.stat().st_size,second.stat().st_mtime))
        for ordinal,state in enumerate(('done','running','queued','failed','cancelled'),1):
            c.execute('INSERT INTO artwork_jobs(video_id,ordinal,url,engine,status,active) VALUES(1,?,?,?,?,?)',(ordinal,'https://example.invalid/still/'+str(ordinal),'builtin',state,0 if state=='cancelled' else 1))
        for ident,state,engine in ((1,'done','builtin'),(2,'submitting',''),(3,'queued','external')):
            c.execute('INSERT INTO scrape_jobs(id,code,remote_id,status,created,updated,engine,video_id) VALUES(?,?,?,?,1,1,?,1)',(ident,'EXAMPLE-00'+str(ident),17 if ident==1 else None,state,engine))
    app.JOURNAL.observe('scan','synthetic-crash','running',{'processed':1})
    for filename in ('1-cover.jpg','1-poster.jpg','1.jpg','1-still-1.jpg','actors/'+app.actor_key('Synthetic Person')+'.jpg','analysis/1/'+'a'*32+'/frame-1.jpg'):
        path=data/filename;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(PNG)
    cfg=app.config();cfg.update(auto_scrape=False,scrape_engine='builtin',online=False)
    (data/'settings.json').write_text(json.dumps(cfg),encoding='utf-8')
    (data/'tag_rules.json').write_text(json.dumps([{'tag':'Synthetic','keywords':['Example']}]),encoding='utf-8')
    engine=app.builtin_engine()
    with engine.connect() as c:
        for ident,state in enumerate(('done','queued','running','failed','cancelled'),1):
            c.execute('INSERT INTO tasks(id,code,status,error,result,created) VALUES(?,?,?,\'\',\'{}\',1)',(ident,'CACHE-00'+str(ident),state))
        c.execute("INSERT INTO metadata VALUES(1,'CACHE-001',?)",(json.dumps({'title':'Completed cache'}),))
        c.execute("INSERT INTO source_cooldowns VALUES('synthetic',4102444800,'rate_limited')")
        c.execute("INSERT INTO source_cache VALUES('CACHE-001','synthetic','fixture',?,1)",(json.dumps({'title':'Cached source'}),))


def snapshot(data):
    with closing(sqlite3.connect(Path(data)/'library.db')) as c:
        return {table:[list(row) for row in c.execute('SELECT * FROM '+table+' '+scope+' ORDER BY 1')] for table,scope in TABLES.items()}


def files(data):
    return {p.relative_to(data).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(data).rglob('*') if p.is_file() and p.suffix.lower() in ('.jpg','.png','.webp')}


def cache_snapshot(data):
    with closing(sqlite3.connect(Path(data)/'builtin-scraper.db')) as c:
        return {name:[list(row) for row in c.execute('SELECT * FROM '+name+' ORDER BY 1')] for name in ('tasks','metadata','source_cooldowns','source_cache')}


def start_without_workers(app):
    """Exercise startup reconciliation, with outbound operations forbidden."""
    def forbidden(*args,**kwargs):raise AssertionError('Unexpected outbound operation in synthetic upgrade rehearsal')
    app.amane_request=forbidden;app.ARTWORK.fetcher=forbidden;app.REVIEW.command=forbidden
    app.JOURNAL.recover();app.JOURNAL.reconcile_sources(uncertain_remote=True)
    app._poll_scrapes()
    engine=app.builtin_engine()
    with engine.connect() as c:
        states=dict(c.execute('SELECT id,status FROM tasks'))
        assert states=={1:'done',2:'queued',3:'queued',4:'failed',5:'cancelled'},'Builtin queue recovery mismatch'
        assert c.execute('SELECT COUNT(*) FROM metadata').fetchone()[0]==1
        assert c.execute('SELECT reason FROM source_cooldowns').fetchone()[0]=='rate_limited'
        assert json.loads(c.execute('SELECT data FROM source_cache').fetchone()[0])=={'title':'Cached source'}
    with app.db() as c:
        assert dict(c.execute('SELECT ordinal,status FROM artwork_jobs'))=={1:'done',2:'queued',3:'queued',4:'failed',5:'cancelled'}
        assert c.execute('SELECT status FROM file_reviews WHERE video_id=2').fetchone()[0]=='queued'
        assert all(tuple(row)==('failed','needs_review') for row in c.execute('SELECT status,retry_kind FROM scrape_jobs WHERE id IN (2,3)'))
        assert c.execute("SELECT state FROM task_journal WHERE external_key='scan:synthetic-crash'").fetchone()[0]=='interrupted'


def drain_builtin_with_mock(app):
    """Only the two eligible cache jobs execute; completed/failed/cancelled stay put."""
    calls=[];engine=app.builtin_engine()
    def fetch(code):
        calls.append(code)
        return {'title':'Synthetic result '+code},[]
    engine.fetcher=fetch;engine.start()
    deadline=time.monotonic()+10
    while engine.worker_lock.locked() and time.monotonic()<deadline:time.sleep(.01)
    assert not engine.worker_lock.locked(),'Synthetic cache worker did not terminate'
    assert calls==['CACHE-002','CACHE-003'],'Ineligible task was replayed or eligible task was lost'
    with engine.connect() as c:
        assert dict(c.execute('SELECT id,status FROM tasks'))=={1:'done',2:'done',3:'done',4:'failed',5:'cancelled'}
        assert json.loads(c.execute("SELECT data FROM metadata WHERE code='CACHE-001'").fetchone()[0])=={'title':'Completed cache'}


def assert_protected(app,expected,image_hashes):
    actual=snapshot(app.DATA)
    # Only physical playback location/proof fields may change during organization.
    with app.db() as c:columns=[r[1] for r in c.execute('PRAGMA table_info(videos)')]
    excluded={'path','size','mtime','missing','file_hash','hash_stamp'}
    for label in ('videos','file_reviews'):
        if label=='file_reviews':
            with app.db() as c:cols=[r[1] for r in c.execute('PRAGMA table_info(file_reviews)')]
            skip={'size','mtime'}
        else:cols=columns;skip=excluded
        indexes=[i for i,name in enumerate(cols) if name not in skip]
        actual[label]=[[row[i] for i in indexes] for row in actual[label]]
        expected=dict(expected);expected[label]=[[row[i] for i in indexes] for row in expected[label]]
    assert actual==expected,'Protected metadata changed during organization'
    assert files(app.DATA)==image_hashes,'Cached image/evidence bytes changed'
    with app.db() as c:
        video=dict(c.execute('SELECT * FROM videos WHERE id=1').fetchone())
        review=dict(c.execute('SELECT * FROM file_reviews WHERE video_id=1').fetchone())
    shown=app.REVIEW.describe(video,review,0,has_cover=True)
    assert shown['decision']=='accepted' and shown['automatic_findings'][0]['review']=='dismissed'
    assert 'suspected_watermark' not in {item['code'] for item in shown['issues']}
