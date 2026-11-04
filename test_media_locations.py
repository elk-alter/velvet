import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from media_locations import MediaLocations


class LocationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.source=self.root/'source.mp4';self.source.write_bytes(b'synthetic media')
        with self.db() as c:
            c.execute('CREATE TABLE videos(id INTEGER PRIMARY KEY,path TEXT UNIQUE,size INTEGER,favorite INTEGER,position REAL,locked_fields TEXT)')
            c.execute('INSERT INTO videos VALUES(1,?,15,1,42,?)',(str(self.source),'["title"]'))
            c.execute('CREATE TABLE media_links(video_id INTEGER PRIMARY KEY,movie_key TEXT,manual INTEGER)')
            c.execute("INSERT INTO media_links VALUES(1,'manual-movie',1)")
        self.repository=MediaLocations(self.db,lambda path:"volume");self.repository.migrate()

    @contextlib.contextmanager
    def db(self):
        c=sqlite3.connect(self.root/'fixture.db');c.row_factory=sqlite3.Row
        try:
            with c:yield c
        finally:c.close()

    def test_migration_is_stable_and_does_not_require_online_files(self):
        identity=self.repository.asset(1)['id'];self.source.unlink()
        self.repository.migrate();self.repository.ensure();self.repository.migrate()
        self.assertEqual(self.repository.asset(1)['id'],identity)
        self.assertEqual(len(self.repository.locations(1)),1)
        self.assertFalse(self.repository.locations(1)[0]['verified'])

    def test_copy_binding_is_idempotent_preserves_metadata_and_source(self):
        target=self.root/'copy.mp4';target.write_bytes(self.source.read_bytes())
        first=self.repository.bind_verified_copy(1,target,'volume','task')
        self.assertEqual(first,self.repository.bind_verified_copy(1,target,'volume','task'))
        self.repository.prefer(1,first)
        self.assertEqual(self.source.read_bytes(),b'synthetic media')
        with self.db() as c:
            row=c.execute('SELECT * FROM videos').fetchone()
            self.assertEqual((row['id'],row['favorite'],row['position'],row['locked_fields']),(1,1,42,'["title"]'))
            self.assertEqual(row['path'],str(target))
            self.assertEqual(c.execute('SELECT COUNT(*) FROM videos').fetchone()[0],1)
            self.assertEqual(tuple(c.execute('SELECT * FROM media_links').fetchone()),(1,'manual-movie',1))
            self.assertEqual(c.execute("SELECT COUNT(*) FROM media_location_events WHERE kind='bound'").fetchone()[0],1)

    def test_mismatched_or_changed_copy_cannot_become_default(self):
        target=self.root/'copy.mp4';target.write_bytes(b'different')
        with self.assertRaises(ValueError):self.repository.bind_verified_copy(1,target,'volume','task')
        target.write_bytes(self.source.read_bytes());location=self.repository.bind_verified_copy(1,target,'volume','task')
        target.write_bytes(b'changed')
        with self.assertRaises(ValueError):self.repository.prefer(1,location)
        self.assertEqual(self.repository.locations(1)[0]['path'],str(self.source))

    def test_source_changes_do_not_silently_reuse_identity(self):
        target=self.root/'copy.mp4';target.write_bytes(self.source.read_bytes())
        self.repository.bind_verified_copy(1,target,'volume','task')
        self.source.write_bytes(b'new source');target.write_bytes(b'new source')
        with self.assertRaises(ValueError):self.repository.bind_verified_copy(1,target,'volume','task')

    def test_replaced_device_cannot_be_selected(self):
        target=self.root/'copy.mp4';target.write_bytes(self.source.read_bytes())
        location=self.repository.bind_verified_copy(1,target,'volume','task')
        self.repository.device_identity=lambda path:'other-volume'
        with self.assertRaises(ValueError):self.repository.prefer(1,location)
        with self.assertRaises(ValueError):self.repository.bind_verified_copy(1,target,'volume','task')

    def test_verified_original_can_be_selected_after_copy_location(self):
        target=self.root/'copy.mp4';target.write_bytes(self.source.read_bytes())
        location=self.repository.bind_verified_copy(1,target,'volume','task')
        original=next(r for r in self.repository.locations(1) if r['path']==str(self.source))
        self.assertTrue(original['verified']);self.assertEqual(original['device_token'],'volume')
        self.repository.prefer(1,location);self.repository.prefer(1,original['id'])
        with self.db() as c:self.assertEqual(c.execute('SELECT path FROM videos WHERE id=1').fetchone()[0],str(self.source))
