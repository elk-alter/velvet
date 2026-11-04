import json
import subprocess
import sys
import threading
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch
import test_features as fixtures
from builtin_scraper import Engine
from resource_leases import ResourceLeases,ResourceBusy
from organization_jobs import OrganizationJobs


class LeaseTests(unittest.TestCase):
    setUp=fixtures.FeatureTests.setUp
    tearDown=fixtures.FeatureTests.tearDown
    add=fixtures.FeatureTests.add

    def test_shared_readers_and_exclusive_writer_across_repository_instances(self):
        first=self.app.LEASES;second=ResourceLeases(self.app.db)
        a=first.try_acquire('scan');b=second.try_acquire('review')
        self.assertIsNotNone(a);self.assertIsNotNone(b)
        self.assertIsNone(first.try_acquire('organize','write'))
        first.release(a);second.release(b)
        owner=second.try_acquire('organize','write')
        self.assertIsNone(first.try_acquire('scan'))
        self.assertIsNone(first.try_acquire('restore','write'))
        second.release(owner);self.assertEqual(first.active(),[])

    def test_read_lease_blocks_separate_process_writer(self):
        owner=self.app.LEASES.try_acquire('review')
        code="""import sqlite3,sys
from contextlib import contextmanager
from resource_leases import ResourceLeases
@contextmanager
def db():
 c=sqlite3.connect(sys.argv[1]);c.row_factory=sqlite3.Row
 try:
  with c:yield c
 finally:c.close()
r=ResourceLeases(db);owner=r.try_acquire('organize','write');print(bool(owner));r.release(owner)
"""
        args=[sys.executable,'-c',code,str(self.app.DB)]
        self.assertEqual(subprocess.check_output(args,text=True).strip(),'False')
        self.app.LEASES.release(owner)
        self.assertEqual(subprocess.check_output(args,text=True).strip(),'True')

    def test_organization_claim_is_atomic_with_global_reader_gate(self):
        (self.root/'source').mkdir();vid=self.add('source/clip.mp4',b'content','')
        jobs=OrganizationJobs(self.app.db,lambda path:'volume')
        source=(self.root/'source'/'clip.mp4').resolve();target=(self.root/'target').resolve()
        draft=jobs.prepare([{'video_id':vid,'source':str(source),'destination':str(target/'clip.mp4'),'operation':'copy'}],source.parent,target)
        job=jobs.approve(draft['id'],draft['digest'])
        with self.app.LEASES.hold('review'):
            with self.assertRaises(ResourceBusy):jobs.claim(job)
        with self.app.db() as c:self.assertFalse(c.execute('SELECT 1 FROM organization_leases').fetchone())
        jobs.claim(job)
        self.assertIsNone(self.app.LEASES.try_acquire('scan'))
        jobs.finish(job,'interrupted');self.assertEqual(self.app.LEASES.active(),[])

    def test_scan_sync_duplicates_and_http_mutations_respect_writer(self):
        owner=self.app.LEASES.try_acquire('organize','write')
        try:
            with patch.object(self.app,'run',side_effect=AssertionError('media read during organization')):
                self.app.scan();self.app.sync_amane();self.app.detect_duplicates()
            self.assertFalse(self.app.LOCK.locked());self.assertFalse(self.app.DUP_LOCK.locked())
            server=self.app.LocalServer(('127.0.0.1',0),self.app.Handler)
            worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
            try:
                conn=HTTPConnection('127.0.0.1',server.server_address[1],timeout=3)
                conn.request('POST','/api/backup','{}',{'X-Velvet-Token':self.app.TOKEN,'Content-Type':'application/json'})
                response=conn.getresponse();response.read();self.assertEqual(response.status,409)
                conn.request('GET','/media/999999',headers={'Host':'127.0.0.1:'+str(self.app.RUNTIME.port)})
                response=conn.getresponse();response.read();self.assertEqual(response.status,409);conn.close()
            finally:server.shutdown();server.server_close();worker.join(3)
        finally:self.app.LEASES.release(owner)
        self.assertEqual(self.app.ACTIVE_POSTS,0)

    def test_review_queue_waits_then_holds_reader_for_actual_worker(self):
        vid=self.add('review.mp4',b'content','')
        owner=self.app.LEASES.try_acquire('organize','write')
        entered=threading.Event();release=threading.Event()
        def inspect(*args):entered.set();release.wait(3);return 'passed','synthetic result'
        self.app.REVIEW.binary=lambda name:name;self.app.REVIEW.inspect=inspect
        self.app.REVIEW.enqueue([vid],'quick')
        self.assertFalse(entered.is_set());self.assertFalse(self.app.REVIEW.lock.locked())
        self.app.LEASES.release(owner);self.app.REVIEW.resume()
        try:
            self.assertTrue(entered.wait(3));self.assertIsNone(self.app.LEASES.try_acquire('organize','write'))
        finally:release.set()
        deadline=time.monotonic()+3
        while self.app.REVIEW.lock.locked() and time.monotonic()<deadline:time.sleep(.01)
        self.assertFalse(self.app.REVIEW.lock.locked());self.assertEqual(self.app.LEASES.active(),[])

    def test_builtin_queue_waits_without_failing_and_wakes_after_release(self):
        entered=threading.Event();release=threading.Event()
        def fetch(code):entered.set();release.wait(3);return {'title':'Synthetic'},[]
        engine=Engine(self.app.DATA,lambda:{},fetch);engine.leases=self.app.LEASES
        owner=self.app.LEASES.try_acquire('organize','write')
        engine.request('tasks',{'number':'ABC-123','type':'scrape'})
        self.assertFalse(entered.is_set());self.assertFalse(engine.worker_lock.locked())
        self.app.LEASES.release(owner);engine.start()
        try:
            self.assertTrue(entered.wait(3));self.assertIsNone(self.app.LEASES.try_acquire('organize','write'))
        finally:release.set()
        deadline=time.monotonic()+3
        while engine.worker_lock.locked() and time.monotonic()<deadline:time.sleep(.01)
        self.assertFalse(engine.worker_lock.locked());self.assertEqual(self.app.LEASES.active(),[])

    def test_worker_dispatch_failure_releases_resource_and_worker_locks(self):
        with patch('file_review.threading.Thread.start',side_effect=RuntimeError('synthetic dispatch failure')):
            with self.assertRaises(RuntimeError):self.app.REVIEW.resume()
        self.assertFalse(self.app.REVIEW.lock.locked());self.assertEqual(self.app.LEASES.active(),[])
        engine=Engine(self.app.DATA,lambda:{},lambda code:({'title':'Synthetic'},[]));engine.leases=self.app.LEASES
        with patch('builtin_scraper.threading.Thread.start',side_effect=RuntimeError('synthetic dispatch failure')):
            with self.assertRaises(RuntimeError):engine.start()
        self.assertFalse(engine.worker_lock.locked());self.assertEqual(self.app.LEASES.active(),[])
