"""Read-only details and indexed-file to inventory navigation contracts."""
import json
import unittest
from unittest.mock import patch
from urllib.parse import urlencode
import test_organization_http as http_fixtures
from inventory_query import InventoryQueryChanged


class StorageFileDetailsTests(unittest.TestCase):
    setUp=http_fixtures.OrganizationHTTPTests.setUp
    tearDown=http_fixtures.OrganizationHTTPTests.tearDown
    shutdown=http_fixtures.OrganizationHTTPTests.shutdown
    request=http_fixtures.OrganizationHTTPTests.request

    def page(self):return self.app.INVENTORY_QUERY.query({'device':self.device})
    def detail(self,page=None):
        page=page or self.page()
        return self.app.STORAGE_FILES.file(page['snapshot'],page['items'][0]['query_ordinal'])
    def indexed(self):
        with self.app.db() as c:
            return c.execute('INSERT INTO videos(path,title,size,mtime,code,locked_fields) VALUES(?,?,?,?,?,?)',(str(self.clip),'Manual title',self.clip.stat().st_size,self.clip.stat().st_mtime,'DEMO-1','["title"]')).lastrowid

    def test_detail_keeps_historical_proof_separate_from_current_stat(self):
        before=(self.clip.read_bytes(),self.clip.stat().st_mtime_ns)
        detail=self.detail()
        self.assertEqual(detail['status'],'current');self.assertFalse(detail['current_content_verified'])
        self.assertEqual(len(detail['historical_sha256']),64)
        self.assertEqual(detail['resources']['total'],1);self.assertIsNone(detail['binding'])
        self.assertEqual((self.clip.read_bytes(),self.clip.stat().st_mtime_ns),before)
        self.assertEqual(self.app.LEASES.active(),[])

    def test_changed_missing_and_linked_files_are_explicit(self):
        page=self.page();self.clip.write_bytes(b'changed synthetic content')
        self.assertEqual(self.detail(page)['status'],'changed')
        self.clip.unlink();self.assertEqual(self.detail(page)['status'],'missing')
        with patch('storage_file_details.no_links',side_effect=ValueError('synthetic linked path')):
            self.assertEqual(self.detail(page)['status'],'linked_or_unsafe')

    def test_permission_error_is_not_reported_as_missing(self):
        with patch('storage_file_details.no_links',side_effect=PermissionError('synthetic ACL')):
            self.assertEqual(self.detail()['status'],'permission_denied')

    def test_offline_snapshot_detail_needs_no_file_access(self):
        hidden=self.source.with_name('offline');self.source.rename(hidden)
        try:
            with patch('storage_file_details.no_links',side_effect=AssertionError('offline file must not be probed')):
                detail=self.detail()
            self.assertEqual(detail['status'],'offline');self.assertIsNone(detail['current'])
        finally:hidden.rename(self.source)

    def test_old_query_context_and_unknown_ordinal_are_rejected(self):
        page=self.page();self.inventory.annotate(self.device,'part1.mp4',{'title':'Changed suggestion'})
        with self.assertRaises(InventoryQueryChanged):self.detail(page)
        page=self.page()
        with self.assertRaises(InventoryQueryChanged):self.app.STORAGE_FILES.file(page['snapshot'],9999)
        with self.assertRaises(ValueError):self.app.STORAGE_FILES.file(page['snapshot'],-1)

    def test_locating_unmigrated_indexed_file_does_not_create_asset(self):
        vid=self.indexed();locations=self.app.STORAGE_FILES.locations(vid)
        self.assertEqual(locations['items'][0]['id'],'indexed')
        located=self.app.STORAGE_FILES.locate(vid,'indexed')
        detail=self.app.STORAGE_FILES.file(located['snapshot'],located['ordinal'])
        self.assertEqual(detail['binding']['video_id'],vid);self.assertEqual(detail['binding']['title'],'Manual title')
        with self.app.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM media_assets').fetchone()[0],0)

    def test_unregistered_indexed_file_is_explained_without_automatic_import(self):
        vid=self.indexed()
        with self.app.db() as c:c.execute('DELETE FROM storage_devices')
        locations=self.app.STORAGE_FILES.locations(vid)
        self.assertIsNone(locations['items'][0]['inventory'])
        recipe=self.app.STORAGE_FILES.locate(vid,'indexed');self.assertTrue(recipe['indexed'])
        detail=self.app.STORAGE_FILES.indexed_file(vid,recipe['location'])
        self.assertEqual(detail['status'],'identity_unverified');self.assertIsNone(detail['file_snapshot'])
        self.assertEqual(detail['metadata']['title'],'Manual title')

    def test_relationships_and_paged_locations_are_kept_separate(self):
        vid=self.indexed();self.app.CORE.reconcile()
        self.app.CORE.relation([{'id':vid,'movie_key':'manual-work','version_key':'manual-version','part_no':3}])
        self.app.ORGANIZATION.locations.migrate([vid])
        page=self.app.STORAGE_FILES.locations(vid,0,1)
        located=self.app.STORAGE_FILES.locate(vid,page['items'][0]['id'])
        detail=self.app.STORAGE_FILES.file(located['snapshot'],located['ordinal'])
        self.assertEqual((detail['binding']['movie_key'],detail['binding']['part_no']),('manual-work',3))
        self.assertEqual(self.app.STORAGE_FILES.locations(vid,1,1)['items'],[])
        with self.assertRaises(ValueError):self.app.STORAGE_FILES.locate(vid,'foreign-location')

    def test_resource_details_are_bounded_and_preserve_shared_flags(self):
        page=self.page()
        resources=[{'name':f'Synthetic {i}.srt','role':'subtitle','shared':bool(i%2)} for i in range(120)]
        with self.app.db() as c:c.execute('UPDATE inventory_files SET companions=? WHERE device_id=?',(json.dumps(resources),self.device))
        detail=self.detail(page);self.assertEqual((len(detail['resources']['items']),detail['resources']['total']),(50,120))
        tail=self.app.STORAGE_FILES.file(page['snapshot'],page['items'][0]['query_ordinal'],100)
        self.assertEqual(len(tail['resources']['items']),20);self.assertTrue(tail['resources']['items'][1]['shared'])

    def test_http_routes_use_record_ids_and_existing_auth(self):
        page=self.page();params=urlencode({'snapshot':page['snapshot'],'ordinal':page['items'][0]['query_ordinal']})
        status,body=self.request('/api/storage/file?'+params);self.assertEqual(status,200);self.assertEqual(body['status'],'current')
        vid=self.indexed();status,body=self.request('/api/storage/video?id='+str(vid));self.assertEqual(status,200)
        status,body=self.request('/api/storage/locate?'+urlencode({'id':vid,'location':'indexed'}));self.assertEqual(status,200)
        status,_=self.request('/api/storage/file?path=arbitrary');self.assertEqual(status,400)
        status,_=self.request('/api/storage/video?id=999999');self.assertEqual(status,400)
        self.app.NAS=True;self.app.USER='synthetic';self.app.PASSWORD='synthetic-secret'
        try:
            from http.client import HTTPConnection
            connection=HTTPConnection('127.0.0.1',self.server.server_address[1],timeout=5)
            connection.request('GET','/api/storage/file?'+params,headers={'Host':'127.0.0.1:'+str(self.app.RUNTIME.port)})
            response=connection.getresponse();response.read();self.assertEqual(response.status,401);connection.close()
        finally:self.app.NAS=False

    def test_unregistered_bound_copy_uses_verified_identity_without_inventory(self):
        vid=self.indexed()
        plan=self.inventory.plan(self.device,str(self.target))
        draft=self.app.ORGANIZATION.prepare(plan['id'],plan['digest'],[0])
        job=self.app.ORGANIZATION.approve(draft['id'],draft['digest'])
        self.assertEqual(self.app.ORGANIZATION.run_once(job)['state'],'done')
        locations=self.app.STORAGE_FILES.locations(vid)
        target=next(item for item in locations['items'] if item['preferred'])
        self.assertIsNone(target['inventory'])
        detail=self.app.STORAGE_FILES.indexed_file(vid,target['id'])
        self.assertEqual(detail['status'],'current');self.assertEqual(detail['source_type'],'index')
        self.assertEqual(len(detail['historical_sha256']),64);self.assertFalse(detail['current_content_verified'])
        self.assertEqual(detail['resources']['total'],1)
        with self.assertRaises(ValueError):self.app.STORAGE_FILES.indexed_file(vid,'foreign')

    def test_indexed_file_http_cannot_read_arbitrary_paths(self):
        vid=self.indexed()
        status,detail=self.request('/api/storage/indexed-file?'+urlencode({'id':vid,'location':'indexed'}))
        self.assertEqual(status,200);self.assertEqual(detail['status'],'identity_unverified')
        status,_=self.request('/api/storage/indexed-file?'+urlencode({'id':vid,'location':'arbitrary','path':str(self.clip)}))
        self.assertEqual(status,400)
