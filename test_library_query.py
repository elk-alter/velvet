"""SQL browse behavior compared with authoritative movie/version relationships."""
import json
import unittest
import threading
from http.client import HTTPConnection
from unittest.mock import patch
from library_query import LibraryQuery,LibraryQueryChanged
import test_features as fixtures


class LibraryQueryTests(unittest.TestCase):
    setUp=fixtures.FeatureTests.setUp
    tearDown=fixtures.FeatureTests.tearDown
    add=fixtures.FeatureTests.add

    def query_service(self):
        return LibraryQuery(self.app.db,self.app.CORE,self.app.LIBRARIES,self.app.DIRECTORY_POLICY)

    def video(self,name,code='',**fields):
        path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True)
        ident=self.add(name,b'synthetic',code)
        if fields:
            with self.app.db() as c:c.execute('UPDATE videos SET '+','.join(key+'=?' for key in fields)+' WHERE id=?',(*fields.values(),ident))
        return ident

    def test_server_page_is_bounded_and_does_not_build_whole_hierarchy(self):
        for i in range(12):self.video(f'{i}.mp4',f'DEMO-{i}',height=1080,duration=1200)
        query=self.query_service()
        with patch.object(self.app.CORE,'hierarchy',side_effect=AssertionError('list must use SQL')):
            first=query.query({'limit':5,'collections':'off'})
            second=query.query({'limit':5,'offset':5,'collections':'off','snapshot':first['snapshot']})
        self.assertEqual((first['total'],first['movies'],len(first['items'])),(12,12,5))
        self.assertFalse({v['id'] for v in first['items']} & {v['id'] for v in second['items']})
        for item in first['items']:
            for key in ('plot','locked_fields','amane_snapshot','_versions','_parts'):self.assertNotIn(key,item)

    def test_parts_default_version_resume_and_matching_alternative(self):
        a=self.video('A/DEMO-1 part1.mp4','DEMO-1',height=2160,duration=500)
        b=self.video('A/DEMO-1 part2.mp4','DEMO-1',height=2160,duration=600,position=40)
        lower=self.video('B/DEMO-1.mp4','DEMO-1',height=720,duration=800,tags='Archive')
        query=self.query_service();default=query.query({'collections':'off'})['items'][0]
        expected=self.app.CORE.hierarchy()[0]
        self.assertEqual((default['id'],default['duration'],default['part_count']),(b,1100,2))
        self.assertEqual(default['id'],expected['id']);self.assertEqual(default['version_count'],2)
        alternative=query.query({'tags':'["Archive"]','collections':'off'})['items'][0]
        self.assertEqual(alternative['id'],lower)
        self.app.CORE.prefer(lower)
        self.assertEqual(query.query({'collections':'off'})['items'][0]['id'],lower)
        with self.app.db() as c:c.execute('UPDATE videos SET missing=1 WHERE id=?',(lower,))
        self.assertEqual(query.query({'collections':'off'})['items'][0]['id'],b)
        self.assertTrue(self.app.CORE.hierarchy()[0]['_versions'])

    def test_collection_counts_are_full_membership_and_filtered_hits(self):
        a=self.video('Example Collection/episode1.mp4',title='Alpha',height=1080)
        self.video('Example Collection/episode2.mp4',title='Beta',height=1080)
        query=self.query_service();group=query.query({})['items'][0]
        self.assertEqual((group['kind'],group['hit_count'],group['total_count']),('collection',2,2))
        filtered=query.query({'q':'Alpha'})['items'][0]
        self.assertEqual((filtered['kind'],filtered['hit_count'],filtered['total_count']),('collection',1,2))
        full=query.query({'collection':group['id']});self.assertEqual(full['total'],2)
        hit=query.query({'collection':group['id'],'q':'Alpha'});self.assertEqual(hit['total'],1)
        self.assertEqual(hit['items'][0]['id'],a)
        self.assertEqual(query.query({'collections':'off'})['total'],2)

    def test_explicit_rules_locks_and_exclusions_invalidate_scope(self):
        a=self.video('Shows/a.mp4','DEMO-1');self.video('Shows/b.mp4','DEMO-2')
        query=self.query_service();first=query.query({});self.assertEqual(first['total'],2)
        with self.app.db() as c:
            c.execute('INSERT INTO folders VALUES(?)',(str(self.root),))
            c.execute('INSERT INTO collection_rules VALUES(?,?)',(str(self.root/'Shows'),'group'))
        with self.assertRaises(LibraryQueryChanged):query.query({'snapshot':first['snapshot']})
        self.assertEqual(query.query({})['items'][0]['kind'],'collection')
        self.app.LIBRARIES.exclude(str(self.root/'Shows'))
        self.assertEqual(query.query({})['total'],0)
        with self.assertRaises(ValueError):query.movie(a)

    def test_filters_and_quality_respect_manual_decision_and_file_generation(self):
        a=self.video('a.mp4','DEMO-1',height=720,actors='Alex; Emma',tags='Drama,Travel',studio='Example',duration=500)
        b=self.video('b.mp4','DEMO-2',height=2160,actors='Alex',tags='Art',duration=1500)
        query=self.query_service()
        cases=[({'actor':'Emma'},[a]),({'tags':'["Drama","Travel"]'},[a]),({'tags':'["Drama","Art"]','tag_logic':'any'},[a,b]),({'exclude_tag':'Art'},[a]),({'height':'2160'},[b]),({'length':'short'},[a]),({'quality':'needs_update'},[a])]
        for criteria,expected in cases:
            result=query.query({'collections':'off',**criteria});self.assertEqual({row['id'] for row in result['items']},set(expected),criteria)
        self.app.REVIEW.mark([a],decision='accepted')
        self.assertEqual(query.query({'quality':'needs_update'})['total'],0)
        self.assertEqual(query.query({'quality':'accepted'})['total'],1)
        self.app.REVIEW.mark([b],flag='watermark')
        self.assertEqual(query.query({'quality':'watermark'})['items'][0]['id'],b)
        with self.app.db() as c:c.execute('UPDATE videos SET size=size+1 WHERE id=?',(b,))
        self.assertEqual(query.query({'quality':'watermark'})['total'],0)

    def test_lazy_detail_preserves_locks_and_other_movies_are_not_presented(self):
        ident=self.video('one.mp4','DEMO-1',plot='Full description',locked_fields='["title"]')
        self.video('two.mp4','DEMO-2');query=self.query_service();query.refresh()
        calls=[];original=self.app.CORE.present
        def present(row):calls.append(row['id']);return original(row)
        with patch.object(self.app.CORE,'present',side_effect=present):detail=query.movie(ident)
        self.assertEqual(calls,[ident]);self.assertEqual(detail['_parts'][0]['plot'],'Full description')
        self.assertEqual(detail['_parts'][0]['locked_fields'],'["title"]')

    def test_unchanged_warm_page_does_not_represent_all_files(self):
        for i in range(10):self.video(f'{i}.mp4',f'DEMO-{i}')
        query=self.query_service();first=query.query({})
        with patch.object(self.app.CORE,'present',side_effect=AssertionError('projection is current')):
            self.assertEqual(query.query({'snapshot':first['snapshot']})['total'],10)
        with self.app.db() as c:c.execute('UPDATE videos SET favorite=1 WHERE id=1')
        with self.assertRaises(LibraryQueryChanged):query.query({'snapshot':first['snapshot']})
        self.assertEqual(query.query({'mode':'favorite'})['total'],1)

    def test_root_relocation_preserves_collection_semantics(self):
        a=self.video('Example Collection/a.mp4',title='Alpha');self.video('Example Collection/b.mp4',title='Beta')
        query=self.query_service();before=query.query({})['items'][0]
        self.app.ORGANIZATION.locations.migrate([a])
        with self.app.db() as c:c.execute('UPDATE videos SET path=? WHERE id=?',(str(self.root/'New location/a.mp4'),a))
        after=query.query({})['items'][0]
        self.assertEqual(after['kind'],'collection');self.assertEqual(after['id'],before['id'])
        self.assertEqual(after['total_count'],2)

    def test_repeat_migration_does_not_drop_projection_or_manual_relations(self):
        a=self.video('one.mp4','DEMO-1');query=self.query_service();first=query.query({})
        self.app.CORE.relation([{'id':a,'movie_key':'manual','version_key':'cut','part_no':2}])
        query=self.query_service();self.assertEqual(query.query({})['items'][0]['movie_key'],'manual')
        self.app.CORE.undo();self.assertEqual(query.query({})['items'][0]['movie_key'],'code:DEMO-1')

    def test_quality_in_other_version_marks_movie_without_switching_default(self):
        low=self.video('Low/clip.mp4','DEMO-1',height=1080)
        high=self.video('High/clip.mp4','DEMO-1',height=2160)
        query=self.query_service();result=query.query({'quality':'needs_update'})
        self.assertEqual(result['items'][0]['id'],high);self.assertTrue(result['items'][0]['needs_update'])
        self.app.REVIEW.mark([low],decision='accepted')
        self.assertEqual(query.query({'quality':'needs_update'})['total'],0)
        self.app.REVIEW.mark([low],flag='watermark',decision='open')
        self.assertEqual(query.query({'quality':'watermark'})['items'][0]['id'],high)

    def test_concurrent_context_change_does_not_publish_mixed_projection(self):
        ident=self.video('sample.mp4','DEMO-1');query=self.query_service();query.query({})
        with self.app.db() as c:c.execute('UPDATE videos SET title=? WHERE id=?',('First title',ident))
        policies=self.app.LIBRARIES.policies
        def changed():
            result=policies()
            with self.app.db() as c:c.execute('UPDATE videos SET favorite=1 WHERE id=?',(ident,))
            return result
        with patch.object(self.app.LIBRARIES,'policies',side_effect=changed):
            with self.assertRaises(LibraryQueryChanged):query.query({})
        result=query.query({'mode':'favorite'});self.assertEqual(result['total'],1)
        self.assertEqual(result['items'][0]['title'],'First title')

    def test_fixed_selection_expands_matching_collection_members_and_rejects_foreign_keys(self):
        a=self.video('Example Collection/a.mp4',title='Alpha');b=self.video('Example Collection/b.mp4',title='Beta')
        query=self.query_service();data=query.query({});key=data['items'][0]['key']
        selected=query.selection(data['snapshot'],[key],limit=1)
        self.assertEqual((selected['total'],selected['files'],len(selected['items'])),(2,2,1))
        self.assertEqual(selected['eligible'],0)
        next_page=query.selection(data['snapshot'],[key],offset=1,limit=1)
        self.assertNotEqual(selected['items'][0]['id'],next_page['items'][0]['id'])
        hit=query.query({'q':'Alpha'});self.assertEqual(query.selection(hit['snapshot'],[hit['items'][0]['key']])['total'],1)
        self.assertEqual(query.random(hit['snapshot'])['id'],a)
        with self.assertRaises(ValueError):query.selection(hit['snapshot'],['movie:foreign'])
        with self.app.db() as c:c.execute('UPDATE videos SET favorite=1 WHERE id=?',(b,))
        with self.assertRaises(LibraryQueryChanged):query.selection(data['snapshot'],[key])

    def test_cover_filter_tracks_cache_changes_and_facets_are_bounded(self):
        a=self.video('a.mp4','DEMO-1',actors='Alex; Emma',tags='Drama,Travel',studio='Example')
        b=self.video('b.mp4','DEMO-2',actors='Alex',tags='Drama')
        query=self.query_service();before=query.query({'quality':'no_cover'});self.assertEqual(before['total'],2)
        (self.app.DATA/(str(a)+'-cover.jpg')).write_bytes(b'synthetic cover')
        with self.assertRaises(LibraryQueryChanged):query.query({'quality':'no_cover','snapshot':before['snapshot']})
        with self.assertRaises(LibraryQueryChanged):query.selection(before['snapshot'],[before['items'][0]['key']])
        self.assertEqual(query.query({'quality':'no_cover'})['items'][0]['id'],b)
        facets=query.facets('actors',limit=1);self.assertEqual(len(facets['items']),1);self.assertEqual(facets['items'][0],{'name':'Alex','movies':2})
        self.assertEqual(query.facets('tags','travel')['items'][0]['name'],'Travel')
        self.assertEqual(query.query({'facet_kind':'studio','facet_name':'Example'})['items'][0]['id'],a)

    def test_unchanged_fresh_requests_reuse_scope_but_edits_and_filter_changes_do_not(self):
        ident=self.video('one.mp4','DEMO-1');query=self.query_service();first=query.query({})
        self.assertEqual(query.query({'limit':1})['snapshot'],first['snapshot'])
        self.assertNotEqual(query.query({'sort':'title'})['snapshot'],first['snapshot'])
        with self.app.db() as c:c.execute('UPDATE videos SET title=? WHERE id=?',('Edited title',ident))
        self.assertNotEqual(query.query({})['snapshot'],first['snapshot'])

    def test_revision_does_not_confuse_database_journals_with_cover_changes(self):
        ident=self.video('one.mp4','DEMO-1');query=self.query_service();query.query({})
        before=query.revision();cover_before=query.revision('no_cover')
        (self.app.DATA/'synthetic-journal.tmp').write_bytes(b'not a cover')
        self.assertEqual(query.revision(),before);self.assertEqual(query.revision('no_cover'),cover_before)
        (self.app.DATA/(str(ident)+'-cover.jpg')).write_bytes(b'synthetic image')
        self.assertEqual(query.revision(),before);self.assertNotEqual(query.revision('no_cover'),cover_before)

    def test_global_lookup_and_relation_suggestions_do_not_depend_on_loaded_page(self):
        ident=self.video('sample.mp4','DEMO-12',title='Synthetic title')
        for i in range(120):self.video(f'other-{i}.mp4',f'OTHER-{i}')
        query=self.query_service();self.assertEqual(query.lookup('demo-12')['id'],ident)
        options=query.relationship_options('Synthetic title');self.assertEqual(len(options['movies']),1)
        self.assertEqual(options['movies'][0]['label'],'DEMO-12 · Synthetic title')
        self.assertLessEqual(len(query.relationship_options()['movies']),100)
        with self.assertRaises(ValueError):query.lookup('unknown-code')

    def test_invalid_inputs_and_empty_library(self):
        query=self.query_service();self.assertEqual(query.query({})['items'],[])
        for criteria in ({'sort':'invalid'},{'tags':'{}'},{'height':'-1'}):
            with self.assertRaises(ValueError):query.query(criteria)


class LibraryQueryHTTPTests(unittest.TestCase):
    video=LibraryQueryTests.video
    add=fixtures.FeatureTests.add
    def setUp(self):
        fixtures.FeatureTests.setUp(self)
        self.ident=self.video('Sample.mp4','DEMO-1',plot='Synthetic full description')
        self.server=self.app.LocalServer(('127.0.0.1',0),self.app.Handler)
        self.worker=threading.Thread(target=self.server.serve_forever,daemon=True);self.worker.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.worker.join(5)
        fixtures.FeatureTests.tearDown(self)
    def request(self,path,host=None,data=None,token=True):
        connection=HTTPConnection('127.0.0.1',self.server.server_port,timeout=10)
        headers={'Host':host or '127.0.0.1:'+str(self.app.RUNTIME.port)}
        if data is not None:
            headers['Content-Type']='application/json'
            if token:headers['X-Velvet-Token']=self.app.TOKEN
        connection.request('POST' if data is not None else 'GET',path,json.dumps(data) if data is not None else None,headers)
        response=connection.getresponse();body=response.read();connection.close()
        return response.status,json.loads(body) if body else {}
    def test_http_list_lazy_detail_and_snapshot_invalidations(self):
        status,data=self.request('/api/library/query?limit=1');self.assertEqual(status,200)
        self.assertEqual(data['total'],1);self.assertNotIn('plot',data['items'][0])
        status,detail=self.request('/api/library/movie?id='+str(self.ident));self.assertEqual(status,200)
        self.assertEqual(detail['movie']['plot'],'Synthetic full description')
        with self.app.db() as c:c.execute('UPDATE videos SET favorite=1 WHERE id=?',(self.ident,))
        self.assertEqual(self.request('/api/library/query?snapshot='+data['snapshot'])[0],409)
        self.assertEqual(self.request('/api/library/query?sort=invalid')[0],400)
        self.assertEqual(self.request('/api/library/movie?id=99999')[0],400)
    def test_browse_routes_do_not_bypass_host_or_nas_auth(self):
        for path in ('/api/library/query','/api/library/movie?id='+str(self.ident)):
            self.assertEqual(self.request(path,host='untrusted.invalid')[0],403)
            with patch.object(self.app,'NAS',True):self.assertEqual(self.request(path)[0],401)

    def test_selection_http_requires_token_and_conflict_is_409(self):
        _,data=self.request('/api/library/query');payload={'snapshot':data['snapshot'],'keys':[data['items'][0]['key']]}
        self.assertEqual(self.request('/api/library/selection',data=payload,token=False)[0],403)
        status,selection=self.request('/api/library/selection',data=payload);self.assertEqual(status,200)
        self.assertEqual(selection['eligible'],1);self.assertEqual(selection['items'][0]['code'],'DEMO-1')
        with self.app.db() as c:c.execute('UPDATE videos SET favorite=1 WHERE id=?',(self.ident,))
        self.assertEqual(self.request('/api/library/selection',data=payload)[0],409)

    def test_cancelled_browser_connection_does_not_turn_into_second_error_response(self):
        for error in (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):
            handler=object.__new__(self.app.Handler)
            handler.send_response=lambda status:None;handler.send_header=lambda *args:None;handler.end_headers=lambda:None
            class Disconnected:
                def write(self,data):raise error('synthetic disconnect')
            handler.wfile=Disconnected();handler.close_connection=False
            self.app.Handler.send(handler,{'items':[]})
            self.assertTrue(handler.close_connection)
