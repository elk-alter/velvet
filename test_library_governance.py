"""Exercise catalog changes through Velvet's HTTP routes."""
import json
import unittest
from urllib.parse import urlencode
import test_library_query as fixtures


class CatalogGovernanceTests(unittest.TestCase):
    setUp=fixtures.LibraryQueryHTTPTests.setUp
    tearDown=fixtures.LibraryQueryHTTPTests.tearDown
    def request(self,path,data=None,token=True):
        return fixtures.LibraryQueryHTTPTests.request(self,path,data=data,token=token)
    video=fixtures.LibraryQueryHTTPTests.video
    add=fixtures.LibraryQueryHTTPTests.add

    def filtered(self,**criteria):
        return self.request('/api/library/query?'+urlencode(criteria))[1]

    def test_tag_preview_apply_and_undo_preserve_source_and_manual_tags(self):
        with self.app.db() as c:
            c.execute("UPDATE videos SET title='Garden Walk',source_tags='Outdoors',tags='My Tag' WHERE id=?",(self.ident,))
        values={'aliases':{'Outdoors':'Nature'},'rules':[{'tag':'Walking','keywords':['walk']}]}
        self.assertEqual(self.request('/api/tag-rules')[0],200)
        path='/api/catalog-tags/'
        self.assertEqual(self.request(path+'preview',values,token=False)[0],403)
        status,preview=self.request(path+'preview',values)
        self.assertEqual((status,preview['changed']),(200,1))
        self.assertFalse(self.filtered(tags=json.dumps(['Nature']))['total'])
        self.assertEqual(self.request(path+'apply',values|{'digest':'stale'})[0],400)
        self.assertEqual(self.request(path+'apply',values|{'digest':preview['digest']})[0],200)
        self.assertEqual(self.filtered(tags=json.dumps(['Nature','My Tag','Walking']))['total'],1)
        self.assertEqual(self.filtered(q='Nature')['total'],1)
        _,detail=self.request('/api/library/detail?id='+str(self.ident))
        self.assertEqual(detail['detail']['selected']['catalog_tags'],['Nature','My Tag'])
        undo=self.request(path+'undo-preview',{})[1]
        self.assertEqual(self.request(path+'undo',{'digest':undo['digest']})[0],200)
        self.assertEqual(self.filtered(tags=json.dumps(['Outdoors','My Tag']))['total'],1)
        self.assertEqual(self.filtered(tags=json.dumps(['Walking']))['total'],0)
        with self.app.db() as c:
            self.assertEqual(tuple(c.execute('SELECT source_tags,tags FROM videos').fetchone()),('Outdoors','My Tag'))

    def test_actor_merge_alias_lookup_and_same_name_split_are_reversible(self):
        other=self.video('Other.mp4','DEMO-2',actors='Harbor')
        self.video('Third.mp4','DEMO-3',actors='Harbor')
        with self.app.db() as c:
            c.execute("UPDATE videos SET actors='Harbor Alias' WHERE id=?",(self.ident,))
            c.execute("INSERT INTO actor_preferences VALUES('Harbor Alias',4,1)")
        self.assertEqual(self.request('/api/actor-governance')[0],200)
        path='/api/actor-governance/'
        merge={'mode':'merge','source':'Harbor Alias','target':'Harbor'}
        preview=self.request(path+'preview',merge)[1]
        self.assertEqual(self.request(path+'apply',merge|{'digest':preview['digest'],'rating':4,'favorite':True})[0],200)
        self.assertEqual(self.filtered(actor='Harbor Alias')['total'],3)
        _,detail=self.request('/api/library/detail?id='+str(self.ident))
        self.assertEqual(detail['detail']['selected']['catalog_actors'],['Harbor'])
        self.assertEqual(detail['detail']['selected']['actors'],'Harbor Alias')
        self.assertEqual(self.request(path+'undo',{})[0],200)
        self.assertEqual(self.filtered(actor='Harbor')['total'],2)
        split={'mode':'split','source':'Harbor','target':'Harbor (second)','ids':[other]}
        preview=self.request(path+'preview',split)[1]
        self.assertEqual(self.request(path+'apply',split|{'digest':preview['digest']})[0],200)
        self.assertEqual(self.filtered(actor='Harbor (second)')['total'],1)
        self.assertEqual(self.filtered(q='Harbor (second)')['total'],1)
        self.assertEqual(self.filtered(actor='Harbor')['total'],1)
        self.assertEqual(self.request(path+'undo',{})[0],200)
        self.assertEqual(self.filtered(actor='Harbor')['total'],2)

    def test_collection_cover_is_persistent_and_rejects_nonmembers(self):
        self.video('Series collection/Example A.mp4','DEMO-2')
        chosen=self.video('Series collection/Example B.mp4','DEMO-3')
        root=self.root/'Series collection'
        with self.app.db() as c:c.execute("INSERT INTO collection_rules(path,mode) VALUES(?,'group')",(str(root),))
        group=next(item for item in self.filtered()['items'] if item['kind']=='collection')
        path='/api/library/collection-cover'
        body={'collection':group['id'],'video_id':chosen}
        self.assertEqual(self.request(path,body|{'video_id':self.ident})[0],400)
        self.assertEqual(self.request(path,body)[0],200)
        self.assertEqual(self.filtered(collection=group['id'])['collection_info']['custom_cover'],chosen)
        self.assertEqual(next(item for item in self.filtered()['items'] if item['kind']=='collection')['custom_cover'],chosen)
        self.assertEqual(self.request(path,body|{'video_id':None})[0],200)
        self.assertIsNone(self.filtered(collection=group['id'])['collection_info']['custom_cover'])
