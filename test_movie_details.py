"""Large single works remain paged without changing preferred playback or edit scope."""
import unittest
from unittest.mock import patch
from library_query import LibraryQueryChanged
import test_features as fixtures
import test_library_query as query_fixtures


class MovieDetailTests(unittest.TestCase):
    setUp=fixtures.FeatureTests.setUp
    tearDown=fixtures.FeatureTests.tearDown

    def seed(self,versions=24,parts=65):
        ids=[]
        with self.app.db() as c:
            for version in range(versions):
                for part in range(1,parts+1):
                    ident=c.execute('INSERT INTO videos(path,title,size,mtime,height,duration,codec,locked_fields,manual) VALUES(?,?,?,?,?,?,?,?,1)',(str(self.root/f'Cut-{version}'/f'Part-{part}.mp4'),'Synthetic movie',100,version,1080+version,10,'h265','["title"]')).lastrowid
                    c.execute('INSERT INTO media_links VALUES(?,?,?,?,1)',(ident,'synthetic-movie',f'cut-{version}',part))
                    ids.append(ident)
        return ids

    def test_detail_is_bounded_and_finds_preferred_resume_part(self):
        ids=self.seed();preferred=ids[13*65+59]
        with self.app.db() as c:
            c.execute('INSERT INTO movie_preferences VALUES(?,?,?)',('synthetic-movie','cut-13',preferred))
            c.execute('UPDATE videos SET position=7 WHERE id=?',(preferred,))
        self.app.BROWSE.refresh()
        with patch.object(self.app.CORE,'hierarchy',side_effect=AssertionError('Detail must not hydrate a full work')),patch.object(self.app.CORE,'present',wraps=self.app.CORE.present) as presented:
            result=self.app.MOVIE_DETAILS.query({'id':ids[0]})
            data=result['detail']
            self.assertLessEqual(presented.call_count,21)
        self.assertEqual(data['selected']['id'],preferred)
        self.assertEqual((data['total_files'],data['versions']['total'],data['parts']['total']),(24*65,24,65))
        self.assertEqual((len(data['versions']['items']),len(data['parts']['items'])),(10,20))
        self.assertIn(preferred,[p['id'] for p in data['parts']['items']])
        self.assertEqual(data['default_version_key'],'cut-13')

    def test_version_comparison_is_bounded_and_ignores_stale_checks(self):
        ids=self.seed(3,2)
        with self.app.db() as c:
            c.executemany(
                "INSERT INTO file_reviews(video_id,size,mtime,status,result) VALUES(?,?,?,'done',?)",
                [(ids[0],100,0,'passed'),(ids[1],100,0,'decode_error'),
                 (ids[2],99,1,'passed'),(ids[4],100,2,'probe_error')])
        first=self.app.MOVIE_DETAILS.query({'id':ids[0],'exact':'1'})['detail']
        with patch.object(self.app.CORE,'present',side_effect=AssertionError('comparison must not hydrate files')):
            result=self.app.MOVIE_DETAILS.query({'id':ids[0],'exact':'1','compare':'1','revision':first['revision']})
        versions={item['version_key']:item for item in result['versions']}
        self.assertEqual(versions['cut-0']['estimated_bitrate'],80)
        self.assertEqual(versions['cut-0']['codec'],'h265')
        self.assertEqual(versions['cut-0']['checks'],{'passed':1,'errors':1,'probe_errors':0})
        self.assertEqual(versions['cut-1']['checks']['passed'],0)
        self.assertEqual(versions['cut-2']['checks']['probe_errors'],1)

    def test_pages_use_same_revision_and_never_expand_selection(self):
        ids=self.seed(3,55);first=self.app.MOVIE_DETAILS.query({'id':ids[0],'exact':'1'})['detail']
        next_page=self.app.MOVIE_DETAILS.query({'id':ids[0],'exact':'1','part_offset':20,'revision':first['revision']})['detail']
        self.assertEqual([p['part_index'] for p in next_page['parts']['items']],list(range(21,41)))
        self.assertEqual(next_page['selected']['id'],ids[0])
        with self.app.db() as c:c.execute('UPDATE videos SET favorite=1 WHERE id=?',(ids[-1],))
        with self.assertRaises(LibraryQueryChanged):self.app.MOVIE_DETAILS.query({'id':ids[0],'revision':first['revision']})

    def test_exact_part_and_defaults_match_legacy_preference_and_quality(self):
        ids=self.seed(3,3)
        with self.app.db() as c:
            c.execute('UPDATE videos SET height=2160 WHERE id>?',(ids[5],))
            c.execute('UPDATE videos SET position=3 WHERE id=?',(ids[7],))
        legacy=self.app.BROWSE.movie(ids[0])
        default=self.app.MOVIE_DETAILS.query({'id':ids[0]})['detail']
        self.assertEqual(default['selected']['id'],legacy['id'])
        exact=self.app.MOVIE_DETAILS.query({'id':ids[1],'exact':'1'})
        self.assertEqual(exact['detail']['selected']['id'],ids[1])
        self.assertEqual(exact['detail']['selected']['locked_fields'],'["title"]')
        self.assertIn('higher_local',{i['code'] for i in exact['review']['files'][ids[1]]['issues']})

    def test_hidden_members_cannot_bypass_relation_hydration_limit(self):
        ids=self.seed(3,55)
        with self.app.db() as c:
            for cut in (1,2):c.execute('INSERT INTO excluded_directories VALUES(?)',(str(self.root/f'Cut-{cut}'),))
        data=self.app.MOVIE_DETAILS.query({'id':ids[0]})['detail']
        self.assertEqual((data['total_files'],data['edit_file_count']),(55,165))
        with patch.object(self.app.CORE,'hierarchy',side_effect=AssertionError('Limit must precede hydration')):
            with self.assertRaises(ValueError):self.app.MOVIE_DETAILS.for_edit(ids[0],100)
        with self.assertRaises(ValueError):self.app.MOVIE_DETAILS.query({'id':ids[-1],'exact':'1'})

    def test_invalid_pages_and_all_offline_resume_are_explicit(self):
        ids=self.seed(1,4)
        with self.app.db() as c:c.execute('UPDATE videos SET missing=1,position=CASE WHEN id=? THEN 4 ELSE 0 END',(ids[-1],))
        legacy=self.app.BROWSE.movie(ids[0])
        data=self.app.MOVIE_DETAILS.query({'id':ids[-1]})['detail']
        self.assertEqual(data['selected']['id'],legacy['id'])
        for extra in ({'part_offset':-1},{'part_offset':4},{'version_offset':1},{'part_limit':0},{'exact':'invalid'}):
            with self.subTest(extra=extra),self.assertRaises(ValueError):self.app.MOVIE_DETAILS.query({'id':ids[0],**extra})

    def test_continuation_looks_beyond_page_without_skipping_a_missing_part(self):
        ids=self.seed(1,45)
        data=self.app.MOVIE_DETAILS.query({'id':ids[19],'exact':'1'})['detail']
        self.assertNotIn(ids[20],[r['id'] for r in data['parts']['items']])
        self.assertEqual(data['next'],{'id':ids[20],'missing':0})
        with self.app.db() as c:c.execute('UPDATE videos SET missing=1 WHERE id=?',(ids[20],))
        data=self.app.MOVIE_DETAILS.query({'id':ids[19],'exact':'1'})['detail']
        self.assertEqual(data['next'],{'id':ids[20],'missing':1})

    def test_new_reader_generation_invalidates_equal_database_revision(self):
        ids=self.seed(1,2);before=self.app.MOVIE_DETAILS.query({'id':ids[0]})['detail']
        self.app.MOVIE_DETAILS.__init__(self.app.BROWSE,self.app.REVIEW)
        with self.assertRaises(LibraryQueryChanged):self.app.MOVIE_DETAILS.query({'id':ids[0],'revision':before['revision']})

    def test_nullable_legacy_attributes_keep_default_and_resume_semantics(self):
        ids=self.seed(2,3)
        with self.app.db() as c:
            c.execute('UPDATE videos SET height=1080')
            c.execute('UPDATE videos SET height=NULL,watched=NULL,missing=NULL,position=9,size=NULL,favorite=NULL WHERE id=?',(ids[0],))
            c.execute('UPDATE videos SET size=1000000,position=5 WHERE id IN (?,?)',(ids[1],ids[2]))
            c.execute('UPDATE videos SET duration=NULL WHERE id<=?',(ids[2],))
        legacy=self.app.BROWSE.movie(ids[0])
        result=self.app.MOVIE_DETAILS.query({'id':ids[0]})['detail']
        self.assertEqual(result['selected']['id'],legacy['id'])
        exact=self.app.MOVIE_DETAILS.query({'id':ids[1],'exact':'1'})['detail']
        version=next(v for v in legacy['_versions'] if v['version_key']=='cut-0')
        self.assertEqual(exact['active_version']['resume_id'],version['resume_id'])
        self.assertEqual(exact['active_version']['duration'],0)

    def test_schema_one_rebuilds_aggregate_without_representing_unchanged_files(self):
        ids=self.seed(2,4);self.app.BROWSE.refresh()
        with self.app.db() as c:
            c.execute('UPDATE browse_state SET projection_schema=1')
            c.execute("UPDATE browse_version_rows SET min_height=9999 WHERE version_key='cut-0'")
        with patch.object(self.app.CORE,'present',side_effect=AssertionError('Existing projection must be reused')):
            self.app.BROWSE.__init__(self.app.db,self.app.CORE,self.app.LIBRARIES,self.app.DIRECTORY_POLICY,self.app.config)
            self.app.BROWSE.refresh()
        with self.app.db() as c:
            self.assertEqual(c.execute('SELECT projection_schema FROM browse_state').fetchone()[0],5)
            self.assertEqual(c.execute("SELECT min_height FROM browse_version_rows WHERE version_key='cut-0'").fetchone()[0],1080)

    def test_version_ties_keep_legacy_latest_visible_member_order(self):
        ids=self.seed(4,3)
        with self.app.db() as c:
            c.execute('UPDATE videos SET height=1080,mtime=0')
            for ident in (ids[2],ids[3],ids[7]):c.execute('UPDATE videos SET mtime=9 WHERE id=?',(ident,))
        legacy=self.app.BROWSE.movie(ids[0])
        detail=self.app.MOVIE_DETAILS.query({'id':ids[0]})['detail']
        self.assertEqual([v['version_key'] for v in detail['versions']['items']],[v['version_key'] for v in legacy['_versions']])

        self.assertEqual(detail['selected']['id'],legacy['id'])
        with self.app.db() as c:
            c.execute('UPDATE videos SET path=? WHERE id=?',(str(self.root/'Hidden'/'part.mp4'),ids[2]))
            c.execute('INSERT INTO excluded_directories VALUES(?)',(str(self.root/'Hidden'),))
        legacy=self.app.BROWSE.movie(ids[0])
        detail=self.app.MOVIE_DETAILS.query({'id':ids[0]})['detail']
        self.assertEqual([v['version_key'] for v in detail['versions']['items']],[v['version_key'] for v in legacy['_versions']])

        # NULL timestamps have a deterministic tie order even though the legacy
        # whole-work max() cannot compare nullable timestamps.
        with self.app.db() as c:c.execute('UPDATE videos SET mtime=NULL')
        detail=self.app.MOVIE_DETAILS.query({'id':ids[0]})['detail']
        self.assertEqual(len(detail['versions']['items']),4)

    def test_schema_two_adds_detail_order_without_representing_existing_projection(self):
        ids=self.seed(3,3);before=self.app.MOVIE_DETAILS.query({'id':ids[0]})['detail']
        with self.app.db() as c:
            columns=[r[1] for r in c.execute('PRAGMA table_info(browse_version_rows)') if r[1] not in ('version_mtime','version_first_id')]
            c.execute('CREATE TABLE legacy_versions AS SELECT '+','.join(columns)+' FROM browse_version_rows')
            c.execute('DROP TABLE browse_version_rows');c.execute('ALTER TABLE legacy_versions RENAME TO browse_version_rows')
            c.execute('UPDATE browse_state SET projection_schema=2')
        with patch.object(self.app.CORE,'present',side_effect=AssertionError('Reuse valid per-file projection')):
            self.app.BROWSE.__init__(self.app.db,self.app.CORE,self.app.LIBRARIES,self.app.DIRECTORY_POLICY,self.app.config)
            self.app.BROWSE.refresh()
        with self.app.db() as c:
            self.assertEqual(c.execute('SELECT version_mtime,version_first_id FROM browse_version_rows WHERE version_key=?',('cut-1',)).fetchone()[:],(1,ids[3]))
        with self.assertRaises(LibraryQueryChanged):self.app.MOVIE_DETAILS.query({'id':ids[0],'revision':before['revision']})
        self.assertEqual(self.app.MOVIE_DETAILS.query({'id':ids[0]})['detail']['versions']['total'],3)


class MovieDetailHTTPTests(unittest.TestCase):
    setUp=query_fixtures.LibraryQueryHTTPTests.setUp
    tearDown=query_fixtures.LibraryQueryHTTPTests.tearDown
    request=query_fixtures.LibraryQueryHTTPTests.request
    video=query_fixtures.LibraryQueryTests.video
    add=fixtures.FeatureTests.add

    def test_paged_detail_permissions_revision_and_legacy_edit_guard(self):
        path='/api/library/detail?id='+str(self.ident)
        self.assertEqual(self.request(path,host='untrusted.invalid')[0],403)
        with patch.object(self.app,'NAS',True):self.assertEqual(self.request(path)[0],401)
        status,result=self.request(path);self.assertEqual(status,200)
        self.assertEqual(result['detail']['selected']['plot'],'Synthetic full description')
        self.assertEqual(self.request('/api/library/movie?id='+str(self.ident)+'&max_files=100')[0],200)
        self.assertEqual(self.request('/api/library/movie?id='+str(self.ident)+'&max_files=101')[0],400)
        with self.app.db() as c:c.execute('UPDATE videos SET favorite=1 WHERE id=?',(self.ident,))
        self.assertEqual(self.request(path+'&revision='+str(result['detail']['revision']))[0],409)

    def test_http_restore_invalidates_open_detail_generation(self):
        path='/api/library/detail?id='+str(self.ident)
        _,before=self.request(path)
        status,backup=self.request('/api/backup',data={});self.assertEqual(status,200)
        status,result=self.request('/api/restore',data={'name':backup['name'],'confirm':backup['name']})
        self.assertEqual(status,200,result)
        self.assertEqual(self.request(path+'&revision='+before['detail']['revision'])[0],409)
        self.assertEqual(self.request(path)[0],200)
        with self.app.db() as c:c.execute('UPDATE videos SET title=? WHERE id=?',('Synthetic restored title',self.ident))
        with patch.object(self.app.CORE,'present',side_effect=AssertionError('Restore lost the list projection callback')):
            status,page=self.request('/api/library/query?collections=off')
        self.assertEqual(status,200,page)
        self.assertEqual(page['items'][0]['title'],'Synthetic restored title')
