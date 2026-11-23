"""SQL browse behavior compared with authoritative movie/version relationships."""
import json
import unittest
import threading
from http.client import HTTPConnection
from unittest.mock import patch
from urllib.parse import urlencode
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

    def test_library_membership_hides_detached_records_and_preserves_organized_imports(self):
        member = self.video('library/member.mp4', 'DEMO-1')
        detached = self.video('outside.mp4', 'DEMO-2')
        organized = self.video('organized.mp4', 'DEMO-3')
        query = self.query_service()
        self.assertEqual(query.query({'collections':'off'})['total'], 3)
        root = self.app.LIBRARIES.save({'path': str(self.root/'library')})
        self.assertEqual(query.query({'collections':'off'})['total'], 1)
        with self.app.db() as c:
            c.execute("INSERT INTO organization_jobs(id) VALUES('copy-job')")
            c.execute("INSERT INTO media_assets(id,video_id,created) VALUES('copied',?,0)", (organized,))
            c.execute("""INSERT INTO media_locations(id,asset_id,path,device_token,preferred,origin_task)
                         SELECT 'copy-location','copied',path,'local',1,'copy-job' FROM videos WHERE id=?""",
                      (organized,))
        self.assertEqual(query.query({'collections':'off'})['total'], 2)

        self.app.LIBRARIES.remove(root)
        self.assertEqual(query.query({'collections':'off'})['items'][0]['code'], 'DEMO-3')
        with self.app.db() as c:
            rows = [dict(row) for row in c.execute('SELECT id,path FROM videos ORDER BY id')]
        self.assertEqual([r['id'] for r in rows], [member, detached, organized])
        self.assertEqual([r['id'] for r in self.app.LIBRARIES.filter_rows(rows)], [organized])
        self.assertTrue((self.root/'library/member.mp4').is_file())
        self.app.LIBRARIES.save({'path': root})
        self.assertEqual(query.query({'collections':'off'})['total'], 2)


    def test_missing_index_explains_scan_state_and_recovers_after_rescan(self):
        ident=self.video('library/Example.mp4','DEMO-5',missing=1)
        root=self.app.LIBRARIES.save({'path':str(self.root/'library'),'automation':'off'})
        self.app.LIBRARIES.finish(root,'Synthetic warning')
        before=self.app.LIBRARIES.files(root,status='missing')
        self.assertEqual(before['items'][0]['availability'],'indexed_missing')
        self.assertTrue(before['library']['online'])
        self.assertGreater(before['library']['last_scan'],0)
        self.assertEqual(before['library']['last_error'],'Synthetic warning')
        with patch.object(self.app,'binary',return_value=None):
            self.app.scan()
        after=self.app.LIBRARIES.files(root,status='missing')
        self.assertEqual(after['total'],0)
        with self.app.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM videos').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT missing FROM videos WHERE id=?',(ident,)).fetchone()[0],0)

    def test_snapshot_caches_only_requested_rows_and_resolves_uncached_selection(self):
        for i in range(12):self.video(f'{i}.mp4',f'DEMO-{i}',title=f'Film {i:02}')
        query=self.query_service()
        first=query.query({'sort':'title','limit':2})
        ident=first['snapshot']
        with self.app.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM browse_results WHERE query_id=?',(ident,)).fetchone()[0],2)
        self.assertEqual(query.selection(ident,['movie:code:DEMO-11'])['items'][0]['code'],'DEMO-11')
        later=query.query({'sort':'title','snapshot':ident,'offset':10,'limit':5})
        self.assertEqual([item['code'] for item in later['items']],['DEMO-10','DEMO-11'])
        with patch.object(query,'_matches',side_effect=AssertionError('cached page must be reused')):
            self.assertEqual(query.query({'sort':'title','snapshot':ident,'offset':10,'limit':5}),later)

    def test_old_projected_index_and_restored_scope_are_rebuilt(self):
        ident=self.video('Example.mp4','DEMO-12',domestic=1,source_tags='无码')
        query=self.query_service()
        before=query.query({})['items'][0]
        with self.app.db() as c:
            c.execute('DROP TABLE library_scope')
            c.execute("UPDATE browse_projection SET marks='{}'")
            c.execute('UPDATE browse_state SET projection_schema=9')
        query=self.query_service()
        after=query.query({})['items'][0]
        self.assertEqual((after['id'],after['domestic'],after['censorship']),(ident,True,'uncensored'))
        self.assertEqual(before['version_key'],after['version_key'])
        with self.app.db() as c:
            c.execute("UPDATE videos SET source_tags='有码' WHERE id=?",(ident,))
        changed=query.query({})['items'][0]
        self.assertEqual(changed['censorship'],'censored')
        self.assertNotEqual(changed['version_key'],after['version_key'])

    def test_snapshot_rejects_changed_facet_aliases_even_without_video_edits(self):
        self.video('demo.mp4','DEMO-1',source_tags='Action')
        aliases={'Action':'Adventure'}
        query=LibraryQuery(self.app.db,self.app.CORE,self.app.LIBRARIES,self.app.DIRECTORY_POLICY,
                           config=lambda:{'tag_aliases':aliases})
        page=query.query({'facet_kind':'tags','facet_name':'Adventure'})
        self.assertEqual(page['total'],1)
        aliases['Action']='Different'
        with self.assertRaises(LibraryQueryChanged):
            query.selection(page['snapshot'],[page['items'][0]['key']])
        self.assertEqual(query.query({'facet_kind':'tags','facet_name':'Adventure'})['total'],0)

    def test_concurrent_metadata_change_cannot_cache_old_projection_as_new_revision(self):
        ident=self.video('Sample.mp4','DEMO-1')
        query=self.query_service()
        original=query.refresh
        def refresh_then_change():
            original()
            with self.app.db() as c:
                c.execute("UPDATE videos SET title='Updated after projection' WHERE id=?",(ident,))
        with patch.object(query,'refresh',side_effect=refresh_then_change):
            with self.assertRaises(LibraryQueryChanged):query.query({})
        self.assertEqual(query.query({})['items'][0]['title'],'Updated after projection')

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

    def test_metadata_on_nonrepresentative_part_is_searchable_without_cross_version_leak(self):
        a=self.video('A/DEMO-1 part1.mp4','DEMO-1',height=2160,title='First',tags='Common')
        b=self.video('A/DEMO-1 part2.mp4','DEMO-1',height=2160,title='Unique needle',actors='Synthetic Actor',tags='Rare',studio='Synthetic Studio')
        lower=self.video('B/DEMO-1.mp4','DEMO-1',height=720,tags='Archive')
        query=self.query_service()
        for criteria in ({'actor':'Synthetic Actor'},{'studio':'Synthetic Studio'},{'q':'Unique needle'},{'tags':'["Rare"]'},{'tags':'["Rare","Common"]'}):
            result=query.query({'collections':'off',**criteria})
            self.assertEqual(result['total'],1,criteria)
            self.assertEqual(result['items'][0]['id'],a,criteria)
        self.assertEqual(query.query({'actor':'Synthetic'})['total'],0)
        self.assertEqual(query.query({'tags':'["Rare","Archive"]'})['total'],0)
        self.assertEqual(query.query({'exclude_tag':'Rare'})['items'][0]['id'],lower)
        with self.app.db() as c:
            c.execute('INSERT INTO excluded_directories VALUES(?)',(str(self.root/'A'),))
        for criteria in ({'actor':'Synthetic Actor'},{'studio':'Synthetic Studio'},{'q':'Unique needle'},{'tags':'["Rare"]'}):
            self.assertEqual(query.query(criteria)['total'],0,criteria)

    def test_old_matching_snapshot_is_not_reused_after_query_semantics_change(self):
        self.video('one.mp4','DEMO-1');query=self.query_service();first=query.query({})
        old=dict(first['criteria']);old.pop('_match_schema',None)
        with self.app.db() as c:c.execute('UPDATE browse_queries SET criteria=? WHERE id=?',(json.dumps(old),first['snapshot']))
        with self.assertRaises(LibraryQueryChanged):query.query({'snapshot':first['snapshot']})
        with self.assertRaises(LibraryQueryChanged):query.selection(first['snapshot'],[first['items'][0]['key']])
        with self.assertRaises(LibraryQueryChanged):query.random(first['snapshot'])
        self.assertNotEqual(query.query({})['snapshot'],first['snapshot'])

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
    def test_listener_accepts_browser_startup_burst(self):
        from contextlib import ExitStack
        from socket import create_connection

        with ExitStack() as stack:
            pending=stack.enter_context(self.app.LocalServer(('127.0.0.1',0),self.app.Handler))
            for _ in range(12):
                stack.enter_context(create_connection(pending.server_address,timeout=2))

    def test_directory_browsing_and_registration_preview(self):
        child=self.root/'External archive';child.mkdir()
        route='/api/storage/directories'
        body={'path':str(child),'alias':'Archive'}
        with patch.object(self.app,'directory_roots',return_value=[self.root]):
            self.assertEqual(self.request(route)[1]['items'][0]['path'],str(self.root))
            status,data=self.request(route+'?'+urlencode({'path':str(self.root)}))
            self.assertEqual(status,200)
            self.assertIn(child.name,[item['name'] for item in data['items']])
            self.assertNotIn('Sample.mp4',[item['name'] for item in data['items']])
            self.assertEqual(self.request(route+'?'+urlencode({'path':str(child/'..')}))[0],400)
            self.assertEqual(self.request('/api/storage/register-preview',data=body,token=False)[0],403)
            self.assertEqual(self.request('/api/storage/register-preview',data=body)[1]['state'],'new')
            device=self.request('/api/storage/register',data=body)[1]['id']
            status,preview=self.request('/api/storage/register-preview',data=body)
            self.assertEqual((status,preview['state'],preview['id'],preview['alias']),(200,'registered',device,'Archive'))
            with self.app.db() as connection:
                connection.execute('UPDATE storage_root_proofs SET file_id=? WHERE device_id=?',('other',device))
            self.assertEqual(self.request('/api/storage/register-preview',data=body)[1]['state'],'changed')

    def test_playback_reports_missing_and_serves_only_adjacent_subtitle(self):
        subtitle=self.root/'Sample.srt'
        subtitle.write_text('1\n00:00:00,000 --> 00:00:01,000\nSynthetic example\n',encoding='utf-8')
        (self.root/'Other.vtt').write_text('WEBVTT\n',encoding='utf-8')
        args='?'+urlencode({'id':self.ident})
        status,data=self.request('/api/playback-info'+args)
        self.assertEqual((status,data['state'],data['tracks']),(200,'ready',[{'id':'srt','label':'Sample.srt'}]))
        self.assertEqual(self.request('/api/playback-subtitle'+args+'&track=vtt')[0],404)
        connection=HTTPConnection('127.0.0.1',self.server.server_port,timeout=10)
        try:
            connection.request('GET','/api/playback-subtitle'+args+'&track=srt',headers={'Host':'127.0.0.1:'+str(self.app.RUNTIME.port)})
            response=connection.getresponse()
            self.assertEqual(response.status,200)
            self.assertIn(b'00:00:00.000 --> 00:00:01.000',response.read())
        finally:
            connection.close()
        (self.root/'Sample.mp4').unlink()
        self.assertEqual(self.request('/api/playback-info'+args)[1]['state'],'offline')

    def test_retry_preview_limits_submission_to_current_reviewed_scope(self):
        engine=self.app.config()['scrape_engine']
        with self.app.db() as connection:
            ready=connection.execute("INSERT INTO scrape_jobs(code,status,remote_id,engine,retry_kind,error) VALUES('DEMO-1','failed',17,?,'retryable','timeout')",(engine,)).lastrowid
        body={'job_ids':[ready,ready+1]}
        preview='/api/amane-retry-preview'
        self.assertEqual(self.request(preview,data=body,token=False)[0],403)
        self.assertEqual(self.request(preview,data=body),(200,{'ready':[ready],'manual':1,'active':0}))
        with patch.object(self.app,'submit_scrape_codes',return_value={'submitted':1,'skipped':0,'failures':[]}) as submit:
            self.assertEqual(self.request('/api/amane-retry',data={**body,'reviewed':True})[0],400)
            submit.assert_not_called()
            self.assertEqual(self.request('/api/amane-retry',data={'job_ids':[ready],'reviewed':True})[0],200)
            submit.assert_called_once_with(['DEMO-1'])
        with self.app.db() as connection:
            connection.execute("INSERT INTO scrape_jobs(code,status,engine) VALUES('DEMO-1','running',?)",(engine,))
        self.assertEqual(self.request(preview,data={'job_ids':[ready]})[1],{'ready':[],'manual':0,'active':1})

    def test_http_list_lazy_detail_and_snapshot_invalidations(self):
        status,data=self.request('/api/library/query?limit=1');self.assertEqual(status,200)
        self.assertEqual(data['total'],1);self.assertNotIn('plot',data['items'][0])
        status,detail=self.request('/api/library/movie?id='+str(self.ident));self.assertEqual(status,200)
        self.assertEqual(detail['movie']['plot'],'Synthetic full description')
        with self.app.db() as c:c.execute('UPDATE videos SET favorite=1 WHERE id=?',(self.ident,))
        self.assertEqual(self.request('/api/library/query?snapshot='+data['snapshot'])[0],409)
        self.assertEqual(self.request('/api/library/query?sort=invalid')[0],400)

    def test_edited_categories_drive_both_filters_and_survive_scraping(self):
        with self.app.db() as c:
            c.execute("UPDATE videos SET source_tags='Original',tags='Existing' WHERE id=?", (self.ident,))
        query = '/api/library/query?collections=off&'
        self.assertEqual(self.request(query+'tags=%5B%22Original%22%5D')[1]['total'], 1)
        status, _ = self.request('/api/edit', data={
            'id':self.ident, 'category_tags':'Existing, Replacement',
            'expected':{'category_tags':'Original, Existing'},
        })
        self.assertEqual(status, 200)
        with self.app.db() as c:
            c.execute("UPDATE videos SET source_tags='UpdatedSource' WHERE id=?", (self.ident,))
        self.assertEqual(self.request(query+'tags=%5B%22Original%22%5D')[1]['total'], 0)
        self.assertEqual(self.request(query+'tags=%5B%22Replacement%22%5D')[1]['total'], 1)
        self.assertEqual(self.request(query+'facet_kind=tags&facet_name=Replacement')[1]['total'], 1)
        self.assertEqual(self.request(query+'facet_kind=tags&facet_name=UpdatedSource')[1]['total'], 0)
        self.assertEqual(self.request('/api/library/movie?id=99999')[0],400)
    def test_progress_is_readable_while_projection_runs_and_resets_on_failure(self):
        from concurrent.futures import ThreadPoolExecutor

        started=threading.Event()
        release=threading.Event()

        def interrupted_projection(*args):
            started.set()
            if not release.wait(5):
                raise AssertionError('progress request did not finish')
            raise ValueError('synthetic indexing failure')

        progress='/api/library/query-progress'
        with ThreadPoolExecutor(max_workers=1) as executor:
            with patch.object(self.app.BROWSE,'project',side_effect=interrupted_projection):
                request=executor.submit(self.request,'/api/library/query')
                try:
                    self.assertTrue(started.wait(5))
                    self.assertEqual(self.request(progress),(200,{'stage':'indexing','processed':0,'total':1}))
                finally:
                    release.set()
                self.assertEqual(request.result(timeout=5)[0],400)
        self.assertEqual(self.request(progress),(200,{'stage':'idle'}))
        self.assertEqual(self.request('/api/library/query')[1]['total'],1)
        self.assertEqual(self.request(progress),(200,{'stage':'idle'}))

    def test_workbench_reports_only_latest_visible_failure(self):
        with self.app.db() as c:
            c.execute("INSERT INTO scrape_jobs(code,status,error,retry_kind) VALUES('DEMO-1','failed','synthetic failure','not_found')")
            c.execute("INSERT INTO scrape_jobs(code,status,error,retry_kind) VALUES('EXCLUDED-2','failed','unrelated','retryable')")
        status,data=self.request('/api/library?review=1')
        self.assertEqual(status,200)
        self.assertEqual(list(data['scrape_failures']),['DEMO-1'])
        self.assertEqual(data['scrape_failures']['DEMO-1']['kind'],'not_found')
        with self.app.db() as c:
            c.execute("INSERT INTO scrape_jobs(code,status) VALUES(' demo-1 ','done')")
        self.assertEqual(self.request('/api/library?review=1')[1]['scrape_failures'],{})

    def test_cover_recovery_is_scoped_and_preserves_cached_image_and_manual_fields(self):
        cover=self.app.DATA/f'{self.ident}-cover.jpg'
        cover.write_bytes(b'synthetic existing image')
        snapshot=json.dumps({'poster_url':'https://example.invalid/sample.jpg','_velvet_engine':'builtin'})
        with self.app.db() as c:
            c.execute("UPDATE videos SET amane_snapshot=?,locked_fields=? WHERE id=?",(snapshot,'["title"]',self.ident))
        route='/api/cover-retry'
        expected={'id':self.ident}
        with patch.object(self.app,'cache_artwork',return_value=False) as cache:
            self.assertEqual(self.request(route,data=expected,token=False)[0],403)
            self.assertEqual(self.request(route,data=expected),(200,{'fetched':[],'failed':['poster']}))
            cache.assert_called_once_with('https://example.invalid/sample.jpg',self.ident,'poster',engine='builtin')
        self.assertEqual(cover.read_bytes(),b'synthetic existing image')
        with self.app.db() as c:
            self.assertEqual(tuple(c.execute('SELECT amane_snapshot,locked_fields FROM videos WHERE id=?',(self.ident,)).fetchone()),(snapshot,'["title"]'))

    def test_manual_card_flags_are_independent_and_persist_in_browse(self):
        edit='/api/edit'
        page='/api/library/query'
        def flags():
            item=self.request(page)[1]['items'][0]
            return item['domestic'],item['amateur']
        self.assertEqual(flags(),(False,False))
        self.assertEqual(self.request(edit,data={'id':self.ident,'domestic':1,'amateur':1})[0],200)
        self.assertEqual(flags(),(True,True))
        self.assertEqual(self.request(edit,data={'id':self.ident,'domestic':0})[0],200)
        self.assertEqual(flags(),(False,True))
        self.assertEqual(self.request(edit,data={'id':self.ident,'amateur':'false'})[0],400)
        self.assertEqual(flags(),(False,True))

    def test_automatic_badges_use_explicit_signals_and_respect_overrides(self):
        ident=self.video('国产/Example.mp4','DEMO-2',source_tags='素人, 无码')
        self.video('FC2-123.mp4','FC2-123',amane_snapshot='{"content_type":"fc2"}')
        self.video('中文短片.mp4','DEMO-3')
        self.video('作品-中文字幕.mp4','DEMO-4')
        self.video('作品の記録.mp4','DEMO-5')
        self.video('中文译名.mp4','DEMO-6',amane_snapshot='{"country":"Japan"}')
        self.video('Tokyo-Hot n1153.mp4','n1153')
        self.video('StudioSample.mp4','n1154',studio='東京熱')
        self.video('n1155.mp4','n1155')
        self.video('Tokyo-Hot n1156.mp4','n1156',source_tags='有码')
        def card(code):
            return self.request('/api/library/query?q='+code)[1]['items'][0]
        auto=card('DEMO-2')
        self.assertEqual((auto['domestic'],auto['amateur'],auto['censorship']),(True,True,'uncensored'))
        other=card('FC2-123')
        self.assertEqual((other['domestic'],other['amateur'],other['censorship']),(False,False,'unknown'))
        self.assertTrue(card('DEMO-3')['domestic'])
        self.assertEqual(card('n1153')['censorship'],'uncensored')
        self.assertEqual(card('n1154')['censorship'],'uncensored')
        self.assertEqual(card('n1155')['censorship'],'unknown')
        self.assertEqual(card('n1156')['censorship'],'censored')
        for code in ('DEMO-4','DEMO-5','DEMO-6'):
            self.assertFalse(card(code)['domestic'],code)
        self.assertEqual(self.request('/api/edit',data={
            'id':ident,'domestic':-1,'censor_override':'censored',
            'expected':{'domestic':'0','censor_override':'auto'},
        })[0],200)
        current=card('DEMO-2')
        self.assertEqual((current['domestic'],current['amateur'],current['censorship']),(False,True,'censored'))
        detail=self.request('/api/library/detail?id='+str(ident))[1]['detail']['selected']
        self.assertEqual((detail['domestic'],detail['domestic_override']),(False,-1))
        self.assertEqual(self.request('/api/edit',data={'id':ident,'domestic':0,'censor_override':'auto'})[0],200)
        self.assertTrue(card('DEMO-2')['domestic'])
        self.assertEqual(card('DEMO-2')['censorship'],'uncensored')

    def test_recognition_filters_share_manual_and_source_conflict_decisions(self):
        ident=self.video('国产/Conflict.mp4','DEMO-9',source_tags='素人',
                         amane_snapshot=json.dumps({'raw':{'first':{'content_type':'censored'},
                                                          'second':{'content_type':'uncensored'}}}))
        page='/api/library/query?domestic=yes&amateur=yes'
        result=self.request(page)[1]
        self.assertEqual(result['total'],1)
        self.assertTrue(result['items'][0]['recognition_conflict'])
        detail=self.request('/api/library/detail?id='+str(ident))[1]['detail']['selected']
        self.assertEqual(detail['recognition']['censorship']['value'],'unknown')
        self.assertEqual(len(detail['recognition']['censorship']['evidence']),2)
        self.assertEqual(self.request('/api/edit',data={'id':ident,'domestic':-1})[0],200)
        self.assertEqual(self.request(page)[1]['total'],0)
        self.assertEqual(self.request('/api/library/query?domestic=no&amateur=yes')[1]['total'],1)
        self.assertEqual(self.request('/api/edit',data={'id':ident,'domestic':0})[0],200)
        self.assertEqual(self.request(page)[1]['total'],1)
        self.assertEqual(self.request('/api/library/query?domestic=invalid')[0],400)

    def test_restricted_source_history_is_classified_before_paging(self):
        with self.app.db() as c:
            c.execute("""INSERT INTO scrape_jobs(code,status,details,retry_kind)
                         VALUES('DEMO-8','failed','javdb: login_required','needs_review')""")
        status,data=self.request('/api/amane-jobs?status=failed&reason=restricted&limit=1')
        self.assertEqual(status,200)
        self.assertEqual(data['total'],1)
        self.assertEqual(data['jobs'][0]['failure']['kind'],'restricted')

    def test_unidentified_files_have_a_separate_review_filter(self):
        ident=self.video('VIDEO.mp4','')
        status,result=self.request('/api/library/query?source=unidentified&collections=off')
        self.assertEqual(status,200)
        self.assertEqual([item['id'] for item in result['items']],[ident])

    def test_manually_grouped_parts_show_mixed_content_in_card_and_header(self):
        other=self.video('OtherPart.mp4','DEMO-1',source_tags='无码')
        with self.app.db() as c:
            c.execute("UPDATE videos SET source_tags='有码' WHERE id=?",(self.ident,))
            c.executemany('INSERT OR REPLACE INTO media_links VALUES(?,?,?,?,1)',
                          [(self.ident,'mixed-work','mixed-version',1),(other,'mixed-work','mixed-version',2)])
        status,page=self.request('/api/library/query?censorship=mixed')
        self.assertEqual(status,200)
        self.assertEqual(page['total'],1)
        self.assertEqual(page['items'][0]['censorship'],'mixed')
        status,detail=self.request('/api/library/detail?id='+str(self.ident))
        self.assertEqual(status,200)
        self.assertIn('分段标记不一致',detail['detail']['active_version']['version_label'])
        self.assertEqual(detail['detail']['selected']['censorship'],'censored')

    def test_identification_lists_candidates_without_replacing_manual_code(self):
        ident=self.video('DIR-202/FILE-101.mp4','LOCK-303',locked_fields='["code"]')
        source=self.root/'DIR-202/FILE-101.nfo'
        source.write_text('<movie><num>NFO-404</num></movie>',encoding='utf-8')
        status,data=self.request(f'/api/identification/{ident}')
        self.assertEqual(status,200)
        self.assertEqual(data['code'],'LOCK-303')
        self.assertTrue(data['locked'])
        self.assertEqual(data['candidates'],{'filename':'FILE-101','directory':'DIR-202','nfo':'NFO-404'})
        source.write_text('<broken>',encoding='utf-8')
        self.assertTrue(self.request(f'/api/identification/{ident}')[1]['warning'])

    def test_fc2_number_detection_agrees_between_metadata_and_http_evidence(self):
        for name in ('FC2PPV-7654321-C','FC2-PPV-7654321-C','FC2 PPV 7654321','FC2_7654321'):
            with self.subTest(name=name):
                ident=self.video(f'{name}/VIDEO__0123.mp4','LOCK-303',locked_fields='["code"]')
                path=self.root/name/'VIDEO__0123.mp4'
                self.assertEqual(self.app.read_metadata(path)['code'],'FC2-7654321')
                status,data=self.request(f'/api/identification/{ident}')
                self.assertEqual(status,200)
                self.assertEqual(data['candidates'],{'filename':'','directory':'FC2-7654321','nfo':None})
                self.assertEqual(data['code'],'LOCK-303')

        self.assertEqual(self.app.identify('FC2-PPV-7654321 sample'),'FC2-7654321')
        self.assertEqual(self.app.identify('ABC-001 part2'),'ABC-001')
        self.assertEqual(self.app.identify('prefixFC2PPV-7654321'),'')
        self.assertEqual(self.app.identify('FC2PPV-7654321x'),'')
        self.assertEqual(self.app.identify('VIDEO-0123'),'')

    def test_scrape_jobs_report_image_failures_only_on_latest_completed_job(self):
        with self.app.db() as c:
            c.executemany("INSERT INTO scrape_jobs(code,status) VALUES('DEMO-1',?)",[('done',),('done',)])
            c.executemany("INSERT INTO artwork_jobs(video_id,ordinal,status) VALUES(?,?,?)",
                          [(self.ident,1,'failed'),(self.ident,2,'done')])
        status,data=self.request('/api/amane-jobs')
        self.assertEqual(status,200)
        newest,older=data['jobs']
        self.assertEqual(newest['artwork_failures'],[{'id':self.ident,'count':1}])
        self.assertEqual(older['image_failures'],0)

    def test_scrape_jobs_filter_before_pagination_and_check_latest_across_all_pages(self):
        with self.app.db() as c:
            c.execute("INSERT INTO scrape_jobs(code,status,engine,remote_id,retry_kind) VALUES('DEMO-1','failed','external',1,'retryable')")
            c.executemany("INSERT INTO scrape_jobs(code,status,engine,remote_id,retry_kind) VALUES(?,'failed','external',?,'not_found')",
                          ((f'OTHER-{n}',n+2) for n in range(125)))
            c.execute("INSERT INTO scrape_jobs(code,status) VALUES('DEMO-1','done')")
            c.execute("INSERT INTO artwork_jobs(video_id,ordinal,status) VALUES(?,1,'failed')",(self.ident,))
        status,data=self.request('/api/amane-jobs?status=failed&reason=retryable&limit=10')
        self.assertEqual(status,200)
        self.assertEqual((data['total'],len(data['jobs'])),(1,1))
        self.assertFalse(data['jobs'][0]['can_retry'])
        status,data=self.request('/api/amane-jobs?status=failed&limit=10&offset=120')
        self.assertEqual(status,200)
        self.assertEqual((data['total'],data['offset'],len(data['jobs'])),(126,120,6))
        self.assertEqual(data['jobs'][-1]['code'],'DEMO-1')
        status,data=self.request('/api/amane-jobs?status=images&limit=1')
        self.assertEqual(status,200)
        self.assertEqual((data['total'],data['jobs'][0]['image_failures']),(1,1))

    def test_metadata_edit_rejects_conflicting_field_without_overwriting_draft_scope(self):
        with self.app.db() as c:
            c.execute("UPDATE videos SET title='New title',plot='New plot' WHERE id=?",(self.ident,))
        status,data=self.request('/api/edit',data={
            'id':self.ident,'title':'Draft title','expected':{'title':'Old title'}})
        self.assertEqual(status,409)
        self.assertIn('草稿保留',data['error'])
        self.assertEqual(self.request('/api/edit',data={
            'id':self.ident,'rating':4,'expected':{'rating':'0'}})[0],200)
        with self.app.db() as c:
            row=c.execute('SELECT title,plot,rating FROM videos WHERE id=?',(self.ident,)).fetchone()
        self.assertEqual(tuple(row),('New title','New plot',4))

    def test_browse_routes_do_not_bypass_host_or_nas_auth(self):
        for path in ('/api/library/query','/api/library/query-progress','/api/library/movie?id='+str(self.ident)):
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
