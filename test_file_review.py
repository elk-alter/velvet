import json
import sys
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from file_review import FileReview


class FileReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.mutex = threading.RLock()
        self.path = self.root / 'fixture.mp4'
        self.path.write_bytes(b'synthetic fixture')
        self.original = self.path.read_bytes()
        with self.db() as c:
            c.execute('CREATE TABLE videos(id INTEGER PRIMARY KEY,path TEXT,size INTEGER,mtime REAL,height INTEGER,duration REAL)')
            c.execute('INSERT INTO videos VALUES(1,?,?,?,?,?)', (str(self.path), self.path.stat().st_size, self.path.stat().st_mtime, 720, 60))
        self.review = FileReview(self.db, lambda name: name, self.root)

    @contextmanager
    def db(self):
        with self.mutex:
            c = sqlite3.connect(self.root / 'test.db')
            c.row_factory = sqlite3.Row
            try:
                with c:
                    yield c
            finally:
                c.close()

    def snapshot(self):
        with self.db() as c:
            videos = [dict(r) for r in c.execute('SELECT * FROM videos')]
        return self.review.snapshot(videos)

    def test_each_automatic_finding_has_an_independent_decision(self):
        self.review.mark([1],note='synthetic')
        findings=[
            {'code':'suspected_watermark','label':'Watermark','text':'A','samples':[{'at':2}]},
            {'code':'suspected_watermark','label':'Watermark','text':'B','samples':[{'at':8}]},
        ]
        with self.db() as c:
            c.execute("UPDATE file_reviews SET auto_findings=?,auto_generation='first' WHERE video_id=1",(json.dumps(findings),))
        first,second=self.snapshot()['files'][1]['automatic_findings']
        self.assertNotEqual(first['id'],second['id'])
        self.review.review_automatic([1],first['code'],'dismissed',finding=first['id'],generation='first')
        self.assertEqual([f['review'] for f in self.snapshot()['files'][1]['automatic_findings']],['dismissed','pending'])
        with self.db() as c:
            c.execute("UPDATE file_reviews SET auto_generation='second' WHERE video_id=1")
        with self.assertRaises(ValueError):
            self.review.review_automatic([1],second['code'],'confirmed',finding=second['id'],generation='first')
        self.assertEqual([f['review'] for f in self.snapshot()['files'][1]['automatic_findings']],['dismissed','pending'])

    def wait(self):
        deadline = time.monotonic() + 3
        while self.review.lock.locked() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.review.lock.locked())

    def test_manual_flags_decisions_and_file_revision(self):
        self.review.mark([1], 'watermark', note='Corner logo')
        self.assertTrue(self.snapshot()['files'][1]['needs_update'])
        self.review.mark([1], decision='accepted')
        self.assertFalse(self.snapshot()['files'][1]['needs_update'])
        self.review.mark([1], decision='open')
        self.review.mark([1], 'watermark', enabled=False)
        self.assertFalse(any(i['code'] == 'watermark' for i in self.snapshot()['files'][1]['issues']))
        with self.db() as c:
            c.execute('UPDATE videos SET mtime=mtime+5')
        record = self.snapshot()['files'][1]
        self.assertEqual(record['status'], 'stale')
        self.assertEqual(record['decision'], 'open')
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_target_and_validation(self):
        self.assertEqual(self.snapshot()['target'], 1080)
        self.review.target(0)
        self.assertFalse(self.snapshot()['files'][1]['needs_update'])
        for args in [([], 'watermark'), ([99], 'watermark'), ([True], 'watermark'), ([1], 'arbitrary')]:
            with self.assertRaises(ValueError):
                self.review.mark(*args)
        with self.assertRaises(ValueError):
            self.review.target(True)

    def test_sample_decode_failure_is_separate_from_probe_error(self):
        self.review.command = lambda args, timeout: (0, json.dumps({'streams': [{'codec_type': 'video'}], 'format': {'duration': '60'}}), '') if 'ffprobe' in args[0] else (1, '', 'Invalid data at packet')
        self.review.enqueue([1], 'sample'); self.wait()
        record = self.snapshot()['files'][1]
        self.assertEqual(record['result'], 'decode_error')
        self.assertIn('0.0', record['detail'])
        self.assertEqual(self.path.read_bytes(), self.original)
        self.review.command = lambda args, timeout: (1, '', 'Invalid container')
        self.review.enqueue([1], 'quick'); self.wait()
        record = self.snapshot()['files'][1]
        self.assertEqual(record['result'], 'probe_error')
        self.assertFalse(any(i['code'] == 'decode_error' for i in record['issues']))

    def test_scope_missing_tools_and_changes_during_check(self):
        self.review.command = lambda args, timeout: (0, '{"streams":[{"codec_type":"video"}]}', '')
        self.review.enqueue([1], 'quick'); self.wait()
        self.assertIn('不代表全片正常', self.snapshot()['files'][1]['detail'])
        self.path.write_bytes(b'changed')
        self.review.enqueue([1], 'full'); self.wait()
        self.assertEqual(self.snapshot()['files'][1]['result'], 'changed')
        self.review.binary = lambda name: None
        with self.assertRaises(ValueError):
            self.review.enqueue([1], 'sample')

    def test_recovery_and_cancel_queue(self):
        with self.db() as c:
            self.review.touch(c, self.review.rows([1])[0])
            c.execute("UPDATE file_reviews SET status='running'")
        restored = FileReview(self.db, lambda name: name)
        self.assertEqual(restored.snapshot(self.review.rows([1]))['files'][1]['status'], 'queued')
        restored.stop()
        self.assertEqual(restored.snapshot(self.review.rows([1]))['files'][1]['status'], 'cancelled')

    def test_cancel_terminates_owned_decoder(self):
        self.review.cancel.set()
        code, _, _ = self.review.command([sys.executable, '-c', 'import time; time.sleep(10)'], 20)
        self.assertEqual(code, 'cancelled')

    def test_higher_local_version_is_only_a_candidate(self):
        movie = {'_versions': [{'height': 720, '_parts': [{'id': 1}]}, {'height': 2160, '_parts': [{'id': 2}]}]}
        record = self.review.snapshot(self.review.rows([1]), [movie])['files'][1]
        self.assertTrue(any(i['code'] == 'higher_local' and '待对比' in i['label'] for i in record['issues']))


if __name__ == '__main__':
    unittest.main()
