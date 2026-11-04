import json
import unittest
import threading
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch
import test_features as fixtures
from inventory_query import InventoryQueryChanged
from storage_inventory import volume_identity


class InventoryQueryTests(unittest.TestCase):
    tearDown=fixtures.FeatureTests.tearDown
    def setUp(self):
        fixtures.FeatureTests.setUp(self);self.root=self.root.resolve()
        self.a=self.root/'A';self.b=self.root/'B';self.a.mkdir();self.b.mkdir()
        self.inventory=self.app.INVENTORY;self.query=self.app.INVENTORY_QUERY
        self.da=self.inventory.register(str(self.a),'Disk A');self.db=self.app.db
        self.dbid=self.inventory.register(str(self.b),'Disk B')
        self.snapshot(self.da,'a',1,[('Title/clip1.mp4',10,'Alpha','DEMO-1','x'*64,[]),('Title/clip2.mp4',20,'Beta','','', [{'code':'watermark'}])])
        self.snapshot(self.dbid,'b',2,[('Else/second.mp4',10,'Gamma','DEMO-1','x'*64,[])])

    def snapshot(self,device,ident,created,entries):
        with self.db() as c:
            c.execute("INSERT INTO inventory_snapshots(id,device_id,state,created,processed,failed) VALUES(?,?,'done',?,?,0)",(ident,device,created,len(entries)))
            for path,size,title,code,digest,issues in entries:
                c.execute('INSERT INTO inventory_files VALUES(?,?,?,?,?,?,?,?)',(device,path,size,1,digest,json.dumps({'title':title,'code':code,'source':'filename','issues':issues}), '[]',ident))

    def test_server_pagination_and_counts_have_one_stable_scope(self):
        first=self.query.query({'limit':1,'sort':'size'})
        self.assertEqual((first['total'],first['bytes'],len(first['items'])),(3,40,1))
        self.assertEqual(first['items'][0]['display_title'],'Beta')
        second=self.query.query({'limit':1,'offset':1,'sort':'size','snapshot':first['snapshot']})
        self.assertEqual(second['total'],3)
        self.assertNotEqual(second['items'][0]['physical_path'],first['items'][0]['physical_path'])
        with self.assertRaises(InventoryQueryChanged):self.query.query({'sort':'title','snapshot':first['snapshot']})

    def test_new_scan_does_not_silently_change_existing_query_snapshot(self):
        first=self.query.query({})
        self.snapshot(self.da,'new',5,[('New.mp4',99,'New','','',[])])
        pinned=self.query.query({'snapshot':first['snapshot']})
        self.assertEqual(pinned['total'],3);self.assertEqual(pinned['bytes'],40)
        current=self.query.query({})
        self.assertEqual(current['total'],2);self.assertEqual(current['bytes'],109)

    def test_search_quality_identification_and_historical_duplicate_filters(self):
        self.assertEqual(self.query.query({'search':'ALPHA'})['total'],1)
        self.assertEqual(self.query.query({'status':'unknown'})['total'],1)
        self.assertEqual(self.query.query({'status':'identified'})['total'],2)
        self.assertEqual(self.query.query({'quality':'issue'})['items'][0]['display_title'],'Beta')
        duplicates=self.query.query({'device':self.da,'duplicate':'historical'})
        self.assertEqual(duplicates['total'],1);self.assertEqual(duplicates['items'][0]['historical_copies'],2)
        self.assertFalse(duplicates['items'][0]['current_content_verified'])

    def test_annotation_invalidates_old_snapshot_without_overwriting_metadata(self):
        first=self.query.query({})
        self.inventory.annotate(self.da,'Title/clip2.mp4',{'title':'Manual title'})
        with self.assertRaises(InventoryQueryChanged):self.query.query({'snapshot':first['snapshot']})
        current=self.query.query({'search':'manual title','status':'identified'})
        self.assertEqual(current['total'],1);self.assertEqual(current['items'][0]['metadata']['title'],'Beta')
        self.assertEqual(current['items'][0]['display_title'],'Manual title')

    def test_offline_device_remains_searchable_and_changes_invalidate_snapshot(self):
        first=self.query.query({});self.b.rmdir()
        with self.assertRaises(InventoryQueryChanged):self.query.query({'snapshot':first['snapshot']})
        offline=self.query.query({'online':'offline'})
        self.assertEqual(offline['total'],1);self.assertFalse(offline['items'][0]['online'])
        self.assertEqual(self.query.query({'online':'online'})['total'],2)

    def test_overlapping_roots_do_not_fabricate_duplicates_or_drop_parent_scope(self):
        child=self.a/'Title';child.mkdir();device=self.inventory.register(str(child),'Nested root')
        self.snapshot(device,'nested',3,[('clip1.mp4',10,'Alpha','DEMO-1','x'*64,[])])
        result=self.query.query({});self.assertEqual(result['total'],3)
        self.assertEqual(self.query.query({'device':self.da})['total'],2)
        duplicate=self.query.query({'duplicate':'historical'})
        self.assertEqual(duplicate['total'],2);self.assertEqual(duplicate['items'][0]['historical_copies'],2)

    def test_exclusions_directory_scope_and_invalid_inputs(self):
        with self.db() as c:c.execute('INSERT INTO folders VALUES(?)',(str(self.a),))
        self.app.LIBRARIES.exclude(str(self.a/'Title'))
        self.assertEqual(self.query.query({})['total'],1)
        self.assertEqual(self.query.query({'duplicate':'historical'})['total'],0)
        with self.assertRaises(ValueError):self.query.query({'directory':'../escape'})
        with self.assertRaises(ValueError):self.query.query({'sort':'DROP TABLE videos'})
        with self.assertRaises(ValueError):self.query.query({'device':'missing'})

    def test_snapshot_expiration_eviction_and_reinitialization(self):
        first=self.query.query({})
        self.query.__init__(self.inventory)
        self.assertEqual(self.query.query({'snapshot':first['snapshot']})['total'],3)
        for i in range(17):self.query.query({'search':str(i)})
        with self.assertRaises(InventoryQueryChanged):self.query.query({'snapshot':first['snapshot']})
        with self.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM inventory_query_snapshots').fetchone()[0],16)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM inventory_query_items WHERE query_id NOT IN (SELECT id FROM inventory_query_snapshots)').fetchone()[0],0)
        current=self.query.query({})
        with self.db() as c:c.execute('UPDATE inventory_query_snapshots SET created=0 WHERE id=?',(current['snapshot'],))
        with self.assertRaises(InventoryQueryChanged):self.query.query({'snapshot':current['snapshot']})

    def test_fallback_identity_is_consistent_within_one_filesystem(self):
        with patch('storage_inventory.os.name','posix'):
            self.assertEqual(volume_identity(self.a)[0],volume_identity(self.b)[0])
            self.assertFalse(volume_identity(self.a)[1])


class InventoryQueryHTTPTests(unittest.TestCase):
    snapshot=InventoryQueryTests.snapshot
    def setUp(self):
        InventoryQueryTests.setUp(self)
        self.server=self.app.LocalServer(('127.0.0.1',0),self.app.Handler)
        self.worker=threading.Thread(target=self.server.serve_forever,daemon=True);self.worker.start()

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.worker.join(5)
        InventoryQueryTests.tearDown(self)

    def request(self,path,host=None):
        connection=HTTPConnection('127.0.0.1',self.server.server_port,timeout=10)
        connection.request('GET',path,headers={'Host':host or '127.0.0.1:'+str(self.app.RUNTIME.port)})
        response=connection.getresponse();body=response.read();connection.close()
        return response.status,json.loads(body) if body else {}

    def test_context_does_not_materialize_library_and_redacts_credentials(self):
        cfg=self.app.config();cfg.update(api_key='synthetic-secret',amane_token='synthetic-token',scrape_site_config={'example':{'token':'synthetic-site-secret'}})
        (self.app.DATA/'settings.json').write_text(json.dumps(cfg),encoding='utf-8')
        with patch.object(self.app.CORE,'hierarchy',side_effect=AssertionError('must not build movie graph')),patch.object(self.app.REVIEW,'snapshot',side_effect=AssertionError('must not build review graph')):
            status,data=self.request('/api/context')
        self.assertEqual(status,200);self.assertEqual(data['transport'],'context-v1')
        self.assertEqual(data['settings']['api_key'],'');self.assertTrue(data['settings']['has_key'])
        self.assertEqual(data['settings']['amane_token'],'');self.assertNotIn('scrape_site_config',data['settings'])
        for field in ('videos','movies','review'):self.assertNotIn(field,data)
        self.assertNotIn('synthetic-secret',json.dumps(data));self.assertEqual(data['token'],self.app.TOKEN)

    def test_http_pagination_and_changed_snapshot_status(self):
        status,first=self.request('/api/storage/query?limit=1&sort=size');self.assertEqual(status,200)
        self.assertEqual(first['total'],3);self.assertEqual(len(first['items']),1)
        status,page=self.request('/api/storage/query?limit=1&sort=size&offset=1&snapshot='+first['snapshot'])
        self.assertEqual(status,200);self.assertNotEqual(page['items'][0]['relative_path'],first['items'][0]['relative_path'])
        status,_=self.request('/api/storage/query?sort=title&snapshot='+first['snapshot']);self.assertEqual(status,409)
        status,_=self.request('/api/storage/query?sort=invalid');self.assertEqual(status,400)

    def test_new_get_routes_preserve_host_and_nas_authentication(self):
        for path in ('/api/context','/api/storage/query'):
            self.assertEqual(self.request(path,host='untrusted.invalid')[0],403)
        with patch.object(self.app,'NAS',True):
            for path in ('/api/context','/api/storage/query'):self.assertEqual(self.request(path)[0],401)
