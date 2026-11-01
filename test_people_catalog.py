import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from people_catalog import PeopleCache,FacetCatalog,actor_key


class PeopleCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        @contextmanager
        def db():
            connection=sqlite3.connect(self.root/'test.db');connection.row_factory=sqlite3.Row
            try:
                with connection:yield connection
            finally:connection.close()
        self.db=db
        with db() as c:
            c.executescript("CREATE TABLE videos(id INTEGER PRIMARY KEY,path TEXT,code TEXT,missing INTEGER DEFAULT 0,source_tags TEXT DEFAULT '',tags TEXT DEFAULT '',auto_tags TEXT DEFAULT '',studio TEXT DEFAULT '',amane_snapshot TEXT DEFAULT '{}');CREATE TABLE media_links(video_id INTEGER,movie_key TEXT);")

    def tearDown(self):self.tmp.cleanup()

    def run_cache(self,cache,names,force=False):
        self.assertTrue(cache.lock.acquire(False));cache.run(names,force)

    def test_exact_portrait_match_and_cache_preservation(self):
        def request(path):
            if path.startswith('actors?'):return {'items':[{'id':1,'name':'Actor'}]}
            return {'image_urls':['/api/resources/1234567890abcdef'],'aliases':['Alias'],'overview':'Bio'}
        cache=PeopleCache(self.db,self.root,lambda:{},request,lambda url:b'\xff\xd8synthetic')
        self.run_cache(cache,['Actor'])
        portrait=self.root/'actors'/(actor_key('Actor')+'.jpg')
        self.assertTrue(portrait.is_file());self.assertEqual(cache.state['matched'],1)
        self.assertEqual(json.loads(cache.profiles()['Actor']['profile'])['aliases'],['Alias'])
        cache.image_loader=lambda url:b'<html>bad image</html>'
        self.run_cache(cache,['Actor'],True)
        self.assertEqual(portrait.read_bytes(),b'\xff\xd8synthetic')
        self.assertEqual(cache.state['failed'],1)

    def test_ambiguous_name_is_not_assigned(self):
        def request(path):return {'items':[{'id':1,'name':'Actor'},{'id':2,'name':'Actor'}]}
        cache=PeopleCache(self.db,self.root,lambda:{},request,lambda url:self.fail('ambiguous actor must not download'))
        cache.index=lambda:{}
        self.run_cache(cache,['Actor'])
        self.assertFalse((self.root/'actors'/(actor_key('Actor')+'.jpg')).exists())
        with self.assertRaises(ValueError):cache.start(['x']*37)

    def test_categories_count_movies_not_versions_and_keep_preferences(self):
        with self.db() as c:
            for i in (1,2):
                c.execute('INSERT INTO videos(id,path,code,source_tags,tags,auto_tags,studio,amane_snapshot) VALUES(?,?,?,?,?,?,?,?)',(i,'file'+str(i),'ABC-1','AliasTag','MyTag','画质/4K','Studio',json.dumps({'series':'Series','directors':['Director']})))
                c.execute('INSERT INTO media_links VALUES(?,?)',(i,'one-movie'))
            c.execute("INSERT INTO videos(id,path,code,missing,source_tags) VALUES(3,'offline','ABC-2',1,'Other')")
        catalog=FacetCatalog(self.db,lambda:{'tag_aliases':{'AliasTag':'Canonical'}})
        tags=[c for c in catalog.list() if c['kind']=='tags']
        self.assertEqual(len(tags),1);self.assertEqual(tags[0]['name'],'Canonical')
        self.assertEqual((tags[0]['movie_count'],tags[0]['file_count']),(1,2))
        catalog.favorite('tags','Canonical',1)
        self.assertTrue(next(c for c in catalog.list() if c['kind']=='tags')['favorite'])
        self.assertEqual({c['kind'] for c in catalog.list()},{'tags','user_tags','technical','studio','series','director'})
        with self.assertRaises(ValueError):catalog.favorite('invalid','Name',1)


if __name__=='__main__':unittest.main()
