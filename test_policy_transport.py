import contextlib
import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path
from media_libraries import MediaLibraries
from library_transport import pack_library


class PolicyTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.media=self.root/'Media';self.media.mkdir()
        with self.db() as c:
            c.execute('CREATE TABLE folders(path TEXT PRIMARY KEY)')
            c.execute('INSERT INTO folders VALUES(?)',(str(self.media),))
            c.execute('CREATE TABLE videos(id INTEGER PRIMARY KEY,path TEXT,size INTEGER,mtime REAL)')
            for i,name,size in [(1,'Set/a.mp4',100),(2,'Set2/b.mp4',100),(3,'skip.mp4',100),(4,'small.mp4',1)]:
                c.execute('INSERT INTO videos VALUES(?,?,?,1)',(i,str(self.media/name),size))
        self.libraries=MediaLibraries(self.db)

    @contextlib.contextmanager
    def db(self):
        c=sqlite3.connect(self.root/'fixture.db');c.row_factory=sqlite3.Row
        try:
            with c:yield c
        finally:c.close()

    def rows(self):
        with self.db() as c:return [dict(r) for r in c.execute('SELECT * FROM videos')]

    def test_preview_boundaries_restore_and_offline_rules(self):
        preview=self.libraries.exclusion_preview(str(self.media/'Set'))
        self.assertEqual(preview['files'],1)
        self.assertFalse(preview['source_files_modified'])
        self.libraries.exclude(preview['path'])
        self.libraries.exclude(preview['path'])
        self.assertEqual([r['id'] for r in self.libraries.filter_rows(self.rows())],[2,3,4])
        self.assertNotEqual(preview['digest'],self.libraries.exclusion_preview(preview['path'])['digest'])
        self.libraries.exclude(preview['path'],False)
        self.assertEqual(len(self.libraries.filter_rows(self.rows())),4)
        self.assertFalse((self.media/'Set').exists())
        with self.assertRaises(ValueError):self.libraries.exclusion_preview(str(self.root/'Other'))

    def test_pattern_and_minimum_size_share_visibility_contract(self):
        self.libraries.save({'path':str(self.media),'exclude_patterns':['skip.*'],'min_size':10})
        self.assertEqual([r['id'] for r in self.libraries.filter_rows(self.rows())],[1,2])
        self.libraries.exclude(str(self.media/'Set'))
        self.assertEqual([r['id'] for r in self.libraries.filter_rows(self.rows())],[2])
        self.libraries.exclude(str(self.media/'Set'),False)
        self.assertEqual([r['id'] for r in self.libraries.filter_rows(self.rows())],[1,2])

    def test_wire_references_are_lossless_and_leave_source_untouched(self):
        video={'id':1,'path':'Example/a.mp4','title':'Example','height':1080,'duration':10}
        version=dict(video,duration=20,_parts=[video],resume_id=1)
        movie=dict(version,_versions=[version],favorite=True)
        original={'videos':[video],'movies':[movie],'token':'fixture'}
        snapshot=copy.deepcopy(original);packed=pack_library(original)
        def decode(value):
            if isinstance(value,list):return [decode(v) for v in value]
            if not isinstance(value,dict):return value
            if '$video' in value:return dict(video,**decode(value['$delta']))
            return {k:decode(v) for k,v in value.items()}
        self.assertEqual(decode(packed['movies']),original['movies'])
        self.assertEqual(snapshot,original)
        self.assertEqual(packed['transport'],'video-references-v1')

    def test_data_guard_rejects_concurrent_restore_and_releases(self):
        from data_guard import DataGuard
        with DataGuard(self.root/'data'):
            with self.assertRaises(ValueError):DataGuard(self.root/'data')
        with DataGuard(self.root/'data'):pass
