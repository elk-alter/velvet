"""Numeric media identity must never inherit another file's caches or decisions."""
import sqlite3
import json
import tempfile
import unittest
from unittest.mock import patch
from contextlib import contextmanager,closing
from pathlib import Path
import test_features as fixtures


class VideoIdentityIntegrationTests(unittest.TestCase):
    setUp=fixtures.FeatureTests.setUp
    tearDown=fixtures.FeatureTests.tearDown
    add=fixtures.FeatureTests.add

    def test_forget_highest_id_does_not_reuse_cover_or_quality(self):
        old=self.add('old.mp4',b'synthetic','DEMO-1')
        (self.app.DATA/f'{old}-cover.jpg').write_bytes(b'synthetic old cover')
        self.app.REVIEW.mark([old],flag='watermark')
        with self.app.db() as c:c.execute('DELETE FROM videos WHERE id=?',(old,))
        new=self.add('new.mp4',b'synthetic','DEMO-2')
        self.assertGreater(new,old)
        with self.app.db() as c:row=dict(c.execute('SELECT * FROM videos WHERE id=?',(new,)).fetchone())
        result=self.app.REVIEW.snapshot([row])['files'][new]
        self.assertFalse(result['has_cover']);self.assertFalse(any(item['code']=='watermark' for item in result['issues']))

    def test_main_restore_keeps_current_high_watermark_even_without_cached_files(self):
        old=self.add('old.mp4',b'synthetic','DEMO-1');backup=self.app.CORE.backup()
        later=self.add('later.mp4',b'synthetic','DEMO-2')
        with self.app.db() as c:c.execute('DELETE FROM videos WHERE id=?',(later,))
        self.app.CORE.restore(backup)
        self.assertGreater(self.add('after.mp4',b'synthetic','DEMO-3'),later)
        with self.app.db() as c:self.assertEqual(c.execute('SELECT path FROM videos WHERE id=?',(old,)).fetchone()[0],str(self.root/'old.mp4'))

    def test_main_restore_migration_failure_leaves_current_library_intact(self):
        self.add('old.mp4',b'synthetic','DEMO-1');backup=self.app.CORE.backup()
        later=self.add('later.mp4',b'synthetic','DEMO-2')
        with patch('library_core.ensure_video_ids',side_effect=RuntimeError('synthetic migration failure')):
            with self.assertRaises(RuntimeError):self.app.CORE.restore(backup)
        with self.app.db() as c:self.assertIsNotNone(c.execute('SELECT * FROM videos WHERE id=?',(later,)).fetchone())


class VideoIdMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.data=self.root/'data';self.data.mkdir();self.path=self.data/'library.db';self.deny=False
        with self.db() as c:
            c.execute('CREATE TABLE videos(id INTEGER PRIMARY KEY,path TEXT UNIQUE,title TEXT,extra BLOB)')
            c.execute("INSERT INTO videos VALUES(1,'synthetic/one.mp4','Preserved',X'010203')")
            c.execute('CREATE INDEX custom_title ON videos(title)')
            c.execute('CREATE TABLE audit(id INTEGER)')
            c.execute('CREATE TRIGGER custom_insert AFTER INSERT ON videos BEGIN INSERT INTO audit VALUES(NEW.id); END')
            c.execute('CREATE VIEW video_view AS SELECT id,title,extra FROM videos')
            c.execute('CREATE TABLE linked(video_id INTEGER REFERENCES videos(id))');c.execute('INSERT INTO linked VALUES(1)')
            c.execute('CREATE TABLE media_assets(id TEXT PRIMARY KEY,video_id INTEGER UNIQUE REFERENCES videos(id))');c.execute("INSERT INTO media_assets VALUES('stable-fixture-uuid',1)")
            c.execute('CREATE TRIGGER detach_fixture AFTER DELETE ON videos BEGIN UPDATE media_assets SET video_id=NULL WHERE video_id=OLD.id; END')

    @contextmanager
    def db(self):
        c=sqlite3.connect(self.path);c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON')
        if self.deny:c.set_authorizer(lambda action,a,b,*_:sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_CREATE_INDEX and a=='custom_title' else sqlite3.SQLITE_OK)
        try:
            with c:yield c
        finally:c.close()

    def test_upgrade_preserves_unknown_columns_views_indexes_triggers_and_references(self):
        from video_ids import ensure_video_ids
        with self.db() as c:
            c.execute('CREATE TABLE file_reviews(video_id INTEGER)');c.execute('INSERT INTO file_reviews VALUES(400)')
        (self.data/'300-cover.jpg').write_bytes(b'synthetic')
        (self.data/'analysis'/'350').mkdir(parents=True)
        self.assertEqual(ensure_video_ids(self.db,self.data),400)
        with self.db() as c:
            self.assertIn('AUTOINCREMENT',c.execute("SELECT sql FROM sqlite_master WHERE name='videos'").fetchone()[0])
            self.assertEqual(tuple(c.execute('SELECT * FROM video_view').fetchone()),(1,'Preserved',b'\x01\x02\x03'))
            self.assertEqual(c.execute('SELECT COUNT(*) FROM audit').fetchone()[0],0)
            ident=c.execute("INSERT INTO videos(path,title) VALUES('synthetic/new.mp4','New')").lastrowid
            self.assertEqual(ident,401);self.assertEqual(c.execute('SELECT id FROM audit').fetchone()[0],401)
            self.assertEqual(c.execute('PRAGMA foreign_keys').fetchone()[0],1)
            self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual(tuple(c.execute('SELECT * FROM media_assets').fetchone()),('stable-fixture-uuid',1))
            self.assertEqual((self.data/'300-cover.jpg').read_bytes(),b'synthetic')
            self.assertIsNotNone(c.execute("SELECT 1 FROM sqlite_master WHERE name='custom_title'").fetchone())

    def test_repeat_migration_and_all_rows_deleted_keep_sequence(self):
        from video_ids import ensure_video_ids
        ensure_video_ids(self.db,self.data)
        with self.db() as c:c.execute("INSERT INTO videos(id,path) VALUES(900,'synthetic/high.mp4')");c.execute('DELETE FROM linked');c.execute('DELETE FROM videos')
        self.assertEqual(ensure_video_ids(self.db,self.data),900)
        with self.db() as c:self.assertEqual(c.execute("INSERT INTO videos(path) VALUES('synthetic/after.mp4')").lastrowid,901)

    def test_legacy_history_only_references_are_not_reused(self):
        from video_ids import ensure_video_ids
        with self.db() as c:
            for table,payload in (
                ('relationship_undo',[{'video_id':700}]),
                ('duplicate_preference_history',{'work:demo':{'preferred_id':710},'work:empty':None}),
                ('organization_drafts',{'entries':[{'video_id':720,'metadata':{'video_id':900000}}]}),
                ('organization_jobs',{'entries':[{'video_id':730},{'video_id':None}]})):
                c.execute('CREATE TABLE '+table+'(payload TEXT)')
                c.execute('INSERT INTO '+table+' VALUES(?)',(json.dumps(payload),))
        self.assertEqual(ensure_video_ids(self.db,self.data),730)
        with self.db() as c:self.assertEqual(c.execute("INSERT INTO videos(path) VALUES('synthetic/history.mp4')").lastrowid,731)

    def test_unreadable_history_rolls_back_before_id_allocation(self):
        from video_ids import ensure_video_ids
        with self.db() as c:
            c.execute('CREATE TABLE relationship_undo(payload TEXT)')
            c.execute("INSERT INTO relationship_undo VALUES('invalid json')")
        with self.assertRaises(json.JSONDecodeError):ensure_video_ids(self.db,self.data)
        with self.db() as c:self.assertNotIn('AUTOINCREMENT',c.execute("SELECT sql FROM sqlite_master WHERE name='videos'").fetchone()[0])

    def test_failed_schema_rebuild_is_atomic(self):
        from video_ids import ensure_video_ids
        self.deny=True
        with self.assertRaises(sqlite3.DatabaseError):ensure_video_ids(self.db,self.data)
        self.deny=False
        with self.db() as c:
            self.assertNotIn('AUTOINCREMENT',c.execute("SELECT sql FROM sqlite_master WHERE name='videos'").fetchone()[0])
            self.assertEqual(c.execute('SELECT COUNT(*) FROM video_view').fetchone()[0],1)
            self.assertIsNotNone(c.execute("SELECT 1 FROM sqlite_master WHERE name='custom_title'").fetchone())
            self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(),[])
        ensure_video_ids(self.db,self.data)

    def test_full_restore_carries_high_watermark_and_preserves_rollback(self):
        from video_ids import ensure_video_ids
        from full_backup import create,restore
        ensure_video_ids(self.db,self.data);archive=self.root/'snapshot.zip';create(self.data,archive)
        with self.db() as c:c.execute("INSERT INTO videos(id,path) VALUES(800,'synthetic/later.mp4')");c.execute('DELETE FROM videos WHERE id=800')
        result=restore(self.data,archive)
        self.assertTrue(Path(result['rollback']).is_dir())
        with self.db() as c:self.assertEqual(c.execute("INSERT INTO videos(path) VALUES('synthetic/restored.mp4')").lastrowid,801)

    def test_quoted_legacy_table_and_cache_only_watermark(self):
        from video_ids import ensure_video_ids
        alternate=self.root/'quoted.db'
        @contextmanager
        def db():
            with closing(sqlite3.connect(alternate)) as c:
                with c:yield c
        with db() as c:c.execute('CREATE TABLE "videos"("id" INTEGER PRIMARY KEY,"path" TEXT UNIQUE)')
        (self.data/'510-still-2.jpg').write_bytes(b'synthetic')
        (self.data/'analysis'/'600').mkdir(parents=True)
        (self.data/'999999999999999999999-cover.jpg').write_bytes(b'not a valid numeric SQLite ID')
        self.assertEqual(ensure_video_ids(db,self.data),600)
        with db() as c:self.assertEqual(c.execute("INSERT INTO videos(path) VALUES('synthetic/a.mp4')").lastrowid,601)

    def test_unreadable_cache_cannot_partially_migrate_database(self):
        from video_ids import ensure_video_ids
        with patch('video_ids.os.scandir',side_effect=PermissionError('synthetic cache permission')):
            with self.assertRaises(PermissionError):ensure_video_ids(self.db,self.data)
        with self.db() as c:self.assertNotIn('AUTOINCREMENT',c.execute("SELECT sql FROM sqlite_master WHERE name='videos'").fetchone()[0])

    def test_offline_restore_migration_failure_does_not_swap_live_data(self):
        from full_backup import create,restore
        archive=self.root/'before.zip';create(self.data,archive)
        with self.db() as c:c.execute("UPDATE videos SET title='Current title' WHERE id=1")
        with patch('full_backup.ensure_video_ids',side_effect=RuntimeError('synthetic failure')):
            with self.assertRaises(RuntimeError):restore(self.data,archive)
        with self.db() as c:self.assertEqual(c.execute('SELECT title FROM videos').fetchone()[0],'Current title')
