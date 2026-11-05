"""Relationship cache and bounded-detail regressions for server-side browsing."""
import unittest
from unittest.mock import patch
from pathlib import Path
from media_libraries import canonical
import test_features as fixtures


class LibraryQueryCoreTests(unittest.TestCase):
    setUp=fixtures.FeatureTests.setUp
    tearDown=fixtures.FeatureTests.tearDown
    add=fixtures.FeatureTests.add

    def test_unreviewed_projection_reuses_only_equivalent_quality_states(self):
        import json
        from file_review import FileReview
        ids=[]
        for index,height in enumerate((0,720,1080,2160,480,4320,None,-1)):
            ident=self.add(f'quality-{index}.mp4',b'synthetic',f'DEMO-{index}');ids.append(ident)
            with self.app.db() as c:c.execute('UPDATE videos SET height=? WHERE id=?',(height,ident))
        describe=FileReview.describe
        with patch.object(FileReview,'describe',wraps=describe) as called:
            self.app.BROWSE.refresh()
        self.assertEqual(called.call_count,2,'Unreviewed files have only below-target and other quality summaries')
        def verify():
            with self.app.db() as c:
                rows=[dict(row) for row in c.execute('SELECT v.*,p.review FROM videos v JOIN browse_projection p ON p.video_id=v.id')]
                records={row['video_id']:dict(row) for row in c.execute('SELECT * FROM file_reviews')}
                setting=c.execute("SELECT value FROM review_settings WHERE key='target'").fetchone();target=int(setting[0]) if setting else 1080
            for row in rows:
                full=describe(row,records.get(row['id'],{}),target)
                expected={key:full[key] for key in ('needs_update','decision','status','result','auto_status')}
                expected['issues']=[{'code':item['code'],'source':item['source']} for item in full['issues']]
                expected['pending_automatic']=any(item['review']=='pending' for item in full['automatic_findings'])
                self.assertEqual(json.loads(row['review']),expected)
        verify()
        self.app.REVIEW.mark([ids[0]],flag='watermark')
        self.app.REVIEW.mark([ids[1]],decision='accepted')
        with self.app.db() as c:c.execute("INSERT OR REPLACE INTO review_settings VALUES('target','0')")
        self.app.BROWSE.refresh();verify()
        with self.app.db() as c:
            c.execute('UPDATE videos SET size=size+1 WHERE id=?',(ids[0],))
            c.execute("UPDATE review_settings SET value='2160' WHERE key='target'")
        self.app.BROWSE.refresh();verify()

    def test_global_summary_keeps_preferred_missing_and_part_semantics_under_filters(self):
        (self.root/'A').mkdir();(self.root/'B').mkdir()
        first=self.add('A/DEMO-1 part1.mp4',b'one','DEMO-1')
        second=self.add('A/DEMO-1 part2.mp4',b'two','DEMO-1')
        alternative=self.add('B/DEMO-1.mp4',b'lower','DEMO-1')
        missing=self.add('DEMO-2.mp4',b'missing','DEMO-2')
        with self.app.db() as c:
            c.execute('UPDATE videos SET height=2160,favorite=1 WHERE id IN (?,?)',(first,second))
            c.execute('UPDATE videos SET height=720,watched=1 WHERE id=?',(alternative,))
            c.execute('UPDATE videos SET missing=1 WHERE id=?',(missing,))
        self.app.CORE.prefer(alternative)
        def verify(criteria):
            movies=self.app.CORE.hierarchy()
            expected={'movies':len(movies),'favorites':sum(bool(v['favorite']) for v in movies),'bytes':sum(v['size'] for v in movies),'unwatched':sum(not v['watched'] and not v['missing'] for v in movies)}
            self.assertEqual(self.app.BROWSE.query(criteria)['summary'],expected)
        for criteria in ({},{'q':'no match'},{'mode':'missing'},{'height':'2160'}):verify(criteria)
        with self.app.db() as c:c.execute('UPDATE videos SET missing=1 WHERE id=?',(alternative,))
        verify({})
    def test_unchanged_relationships_do_not_rescan_or_reinfer(self):
        ident=self.add('DEMO-1.mp4',b'synthetic','DEMO-1');self.app.CORE.reconcile()
        with patch.object(self.app.CORE,'infer',side_effect=AssertionError('unchanged graph must not reinfer')):
            self.app.CORE.reconcile();self.app.CORE.hierarchy()
        with self.app.db() as c:c.execute('UPDATE videos SET code=? WHERE id=?',('DEMO-2',ident))
        self.assertEqual(self.app.CORE.hierarchy()[0]['movie_key'],'code:DEMO-2')

    def test_movie_detail_reads_only_one_movie_after_initial_reconciliation(self):
        for i in range(30):self.add(f'DEMO-{i}.mp4',b'synthetic',f'DEMO-{i}')
        self.app.CORE.reconcile();presented=[];original=self.app.CORE.present
        def present(row):presented.append(row['id']);return original(row)
        with patch.object(self.app.CORE,'present',side_effect=present):
            result=self.app.CORE.hierarchy(movie_key='code:DEMO-12')
        self.assertEqual(len(result),1);self.assertEqual(len(presented),1)
        self.assertEqual(result[0]['code'],'DEMO-12')
        self.assertEqual(self.app.CORE.hierarchy(movie_key='not-there'),[])

    def test_playback_updates_preserve_cache_but_relation_reset_invalidates(self):
        ident=self.add('DEMO-1.mp4',b'synthetic','DEMO-1');self.app.CORE.reconcile()
        with self.app.db() as c:c.execute('UPDATE videos SET position=12,favorite=1 WHERE id=?',(ident,))
        with patch.object(self.app.CORE,'infer',side_effect=AssertionError('playback does not change identity')):
            movie=self.app.CORE.hierarchy()[0]
        self.assertEqual(movie['position'],12);self.assertTrue(movie['favorite'])
        self.app.CORE.relation([{'id':ident,'movie_key':'manual','version_key':'version','part_no':1}])
        self.assertEqual(self.app.CORE.hierarchy()[0]['movie_key'],'manual')
        self.app.CORE.reset([ident]);self.assertEqual(self.app.CORE.hierarchy()[0]['movie_key'],'code:DEMO-1')


    @unittest.skipUnless(__import__('os').name=='nt','Windows short path semantics')
    def test_query_local_short_path_prefix_expands_once_without_resolving_files(self):
        expanded=self.root.resolve()/'Expanded folder';cache={}
        with patch('media_libraries.Path.resolve',autospec=True,return_value=expanded) as resolver:
            first=canonical(self.root/'FOLDER~1'/'offline-a.mp4',cache)
            second=canonical(self.root/'FOLDER~1'/'offline-b.mp4',cache)
        self.assertEqual(first,expanded/'offline-a.mp4');self.assertEqual(second,expanded/'offline-b.mp4')
        self.assertEqual(resolver.call_count,1)
        self.assertEqual(resolver.call_args.args[0].name,'FOLDER~1')

    def test_directory_cache_preserves_per_file_identity_and_rule_precedence(self):
        query=self.app.BROWSE;cache={}
        root=self.root.resolve()/'Synthetic folder'
        base={'path':str(root/'a.mp4'),'relation_path':'','relation_manual':0,'code':'','source':'filename'}
        self.assertEqual(query.folder(base,[],{},cache),str(root).replace('\\','/'))
        self.assertEqual(query.folder(dict(base,path=str(root/'b.mp4'),relation_manual=1),[],{},cache),'')
        self.assertEqual(len(cache),1)
        grouped=self.root.resolve()/'Synthetic Box Set'/'Season 1'
        explicit=dict(base,path=str(grouped/'a.mp4'),relation_manual=1)
        self.assertEqual(query.folder(explicit,[],{},{}),str(grouped.parent).replace('\\','/'))
        rules=[{'path':str(grouped.parent),'mode':'separate'}]
        self.assertEqual(query.folder(explicit,rules,{},{}),'')
        rules.append({'path':str(grouped),'mode':'group'})
        self.assertEqual(query.folder(explicit,rules,{},{}),str(grouped).replace('\\','/'))
