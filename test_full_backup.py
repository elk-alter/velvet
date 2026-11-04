import json
from contextlib import closing
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from full_backup import create, restore


class FullBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.data=self.root/'data';self.data.mkdir()
        for name in ('library.db','builtin-scraper.db'):
            with closing(sqlite3.connect(self.data/name)) as c, c:
                c.execute('CREATE TABLE fixture(value TEXT)');c.execute("INSERT INTO fixture VALUES('before')")
        (self.data/'settings.json').write_text('{"fixture":"private-test-value"}')
        (self.data/'analysis').mkdir();(self.data/'analysis'/'fixture.jpg').write_bytes(b'synthetic image cache')

    def test_full_restore_preserves_both_databases_config_and_optional_cache(self):
        archive=self.root/'backup.zip';create(self.data,archive,True)
        with closing(sqlite3.connect(self.data/'library.db')) as c, c:c.execute("UPDATE fixture SET value='after'")
        result=restore(self.data,archive)
        self.assertTrue(Path(result['rollback']).is_dir())
        for name in ('library.db','builtin-scraper.db'):
            with closing(sqlite3.connect(self.data/name)) as c, c:self.assertEqual(c.execute('SELECT value FROM fixture').fetchone()[0],'before')
        self.assertEqual(json.loads((self.data/'settings.json').read_text())['fixture'],'private-test-value')
        self.assertTrue((self.data/'analysis'/'fixture.jpg').exists())

    def test_bad_archive_cannot_replace_live_data_or_escape(self):
        archive=self.root/'bad.zip'
        with zipfile.ZipFile(archive,'w') as z:
            z.writestr('manifest.json',json.dumps({'format':'velvet-full-1','files':{'../escape':{'size':1,'sha256':'bad'},'library.db':{'size':1,'sha256':'bad'}}}))
            z.writestr('../escape',b'x');z.writestr('library.db',b'x')
        with self.assertRaises(ValueError):restore(self.data,archive)
        self.assertFalse((self.root/'escape').exists())
        with closing(sqlite3.connect(self.data/'library.db')) as c, c:self.assertEqual(c.execute('SELECT value FROM fixture').fetchone()[0],'before')

    def test_default_excludes_cache_and_runtime_descriptor(self):
        archive=self.root/'backup.zip';create(self.data,archive)
        with zipfile.ZipFile(archive) as z:self.assertNotIn('analysis/fixture.jpg',z.namelist())
        with self.assertRaises(ValueError):create(self.data,archive)
        with self.assertRaises(ValueError):create(self.data,self.data/'recursive.zip')

    def test_restore_cannot_discard_physical_operation_history(self):
        archive=self.root/'before-operations.zip';create(self.data,archive)
        with closing(sqlite3.connect(self.data/'library.db')) as c, c:
            c.execute('CREATE TABLE organization_steps(state TEXT)')
            c.execute("INSERT INTO organization_steps VALUES('committed')")
        with self.assertRaises(ValueError):restore(self.data,archive)
        with closing(sqlite3.connect(self.data/'library.db')) as c:
            self.assertEqual(c.execute('SELECT state FROM organization_steps').fetchone()[0],'committed')
        advanced=self.root/'with-operations.zip';create(self.data,advanced)
        with self.assertRaises(ValueError):restore(self.root/'new-data',advanced)
        self.assertFalse((self.root/'new-data').exists())
