import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from directory_policy import DirectoryPolicy


class DirectoryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve(); self.media = self.root / 'media'; self.media.mkdir()
        self.library = type('Libraries', (), {'policies': lambda _: [{'path': str(self.media)}]})()
        with self.db() as c:
            c.execute('CREATE TABLE videos(id INTEGER PRIMARY KEY,path TEXT,size INTEGER,mtime REAL)')
            c.execute('INSERT INTO videos VALUES(1,?,?,?)', (str(self.media / 'Set' / '1.mp4'), 20, 1))
            c.execute('INSERT INTO videos VALUES(2,?,?,?)', (str(self.media / 'Set2' / '2.mp4'), 30, 1))
        self.policy = DirectoryPolicy(self.db, self.library)

    @contextlib.contextmanager
    def db(self):
        c = sqlite3.connect(self.root / 'fixture.db'); c.row_factory = sqlite3.Row
        try:
            with c:yield c
        finally:c.close()

    def test_preview_boundaries_and_no_source_writes(self):
        preview = self.policy.preview(str(self.media / 'Set'))
        self.assertEqual(preview['files'], 1)
        self.assertFalse(preview['source_files_modified'])
        self.assertFalse((self.media / 'Set').exists())
        with self.assertRaises(ValueError):self.policy.preview(str(self.root / 'outside'))

    def test_stale_confirmation_rejected(self):
        preview = self.policy.preview(str(self.media))
        with self.db() as c:c.execute('UPDATE videos SET size=21 WHERE id=1')
        with self.assertRaises(ValueError):self.policy.set_rule(str(self.media), 'group', preview['digest'])
        self.assertEqual(self.policy.rules(), [])

    def test_persistence_precedence_and_restore_auto(self):
        for path, mode in [(self.media, 'group'), (self.media / 'Set', 'ungroup')]:
            preview = self.policy.preview(str(path)); self.policy.set_rule(str(path), mode, preview['digest'])
        restored = DirectoryPolicy(self.db, self.library)
        self.assertEqual(restored.collection_root(self.media/'Set'/'1.mp4', restored.rules()), '')
        self.assertEqual(restored.collection_root(self.media/'Set2'/'2.mp4', restored.rules()), str(self.media))
        preview = restored.preview(str(self.media/'Set'))
        restored.set_rule(str(self.media/'Set'), 'inherit', preview['digest'])
        self.assertEqual(restored.collection_root(self.media/'Set'/'1.mp4', restored.rules()), str(self.media))
        with self.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM videos').fetchone()[0], 2)


if __name__ == '__main__':unittest.main()
