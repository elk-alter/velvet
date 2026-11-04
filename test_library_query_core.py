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
