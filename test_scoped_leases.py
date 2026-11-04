"""Path scopes and durable writer fairness across independent connections."""
import unittest
import threading
import time
from http.client import HTTPConnection
import test_features as fixtures
from resource_leases import ResourceLeases


class ScopedLeaseTests(unittest.TestCase):
    setUp=fixtures.FeatureTests.setUp
    tearDown=fixtures.FeatureTests.tearDown

    def test_unrelated_file_reader_can_run_while_writer_owns_another_file(self):
        leases=self.app.LEASES
        writer=leases.try_acquire('organize','write',resources=['file:/synthetic/a'])
        self.assertIsNotNone(writer)
        self.assertIsNotNone(reader:=leases.try_acquire('playback',resources=['file:/synthetic/b']))
        self.assertIsNone(leases.try_acquire('review',resources=['file:/synthetic/a']))
        leases.release(reader);leases.release(writer)

    def test_waiting_writer_blocks_new_conflicting_readers_and_later_writers(self):
        leases=self.app.LEASES;other=ResourceLeases(self.app.db)
        reader=leases.try_acquire('review',resources=['file:/synthetic/a'])
        self.assertIsNone(leases.try_acquire('organize','write','writer-one',resources=['file:/synthetic/a'],queue=True))
        self.assertIsNone(other.try_acquire('review',resources=['file:/synthetic/a']))
        self.assertIsNotNone(unrelated:=other.try_acquire('review',resources=['file:/synthetic/b']))
        leases.release(reader)
        self.assertIsNone(other.try_acquire('organize','write','writer-two',resources=['file:/synthetic/a'],queue=True))
        self.assertEqual(leases.try_acquire('organize','write','writer-one',resources=['file:/synthetic/a'],queue=True),'writer-one')
        leases.release('writer-one')
        self.assertEqual(other.try_acquire('organize','write','writer-two',resources=['file:/synthetic/a'],queue=True),'writer-two')
        leases.release('writer-two');other.release(unrelated)

    def test_directory_ancestors_overlap_but_siblings_do_not(self):
        leases=self.app.LEASES
        writer=leases.try_acquire('organize','write',resources=['file:/synthetic/root/a.mp4'])
        self.assertIsNone(leases.try_acquire('scan',resources=['tree:/synthetic/root']))
        self.assertIsNotNone(other:=leases.try_acquire('scan',resources=['tree:/synthetic/other']))
        leases.release(writer);leases.release(other)

    def test_cancelled_waiter_and_startup_recovery_do_not_leave_a_gate(self):
        leases=self.app.LEASES;reader=leases.try_acquire('review')
        self.assertIsNone(leases.try_acquire('organize','write','waiting',resources=['file:/synthetic/a'],queue=True))
        leases.release('waiting');leases.release(reader)
        self.assertIsNotNone(ready:=leases.try_acquire('review',resources=['file:/synthetic/a']))
        leases.recover();self.assertEqual(leases.active(),[])
        self.assertEqual(leases.waiting(),[])

    def test_old_direct_claim_without_scopes_remains_global(self):
        with self.app.db() as c:c.execute("INSERT INTO resource_claims VALUES('legacy','organize','write',0)")
        self.assertIsNone(self.app.LEASES.try_acquire('playback',resources=['file:/synthetic/unrelated']))
        self.app.LEASES.release('legacy')

    def test_dotted_sibling_does_not_hide_a_directory_descendant(self):
        from resource_leases import intersects
        self.assertTrue(intersects(['tree:/synthetic/root'],['file:/synthetic/root.txt','file:/synthetic/root/a']))
        self.assertFalse(intersects(['tree:/synthetic/root'],['file:/synthetic/root.txt','file:/synthetic/root2/a']))


class ScopedWorkflowTests(unittest.TestCase):
    import test_organization_service as fixtures
    setUp=fixtures.ServiceTests.setUp
    tearDown=fixtures.ServiceTests.tearDown
    shutdown=fixtures.ServiceTests.shutdown
    prepare=fixtures.ServiceTests.prepare
    approve=fixtures.ServiceTests.approve

    def indexed(self):
        with self.app.db() as c:return c.execute('INSERT INTO videos(path,title,size,mtime) VALUES(?,?,?,?)',(str(self.clip),'Synthetic',self.clip.stat().st_size,self.clip.stat().st_mtime)).lastrowid

    def test_streaming_an_unrelated_file_is_not_blocked_by_organization(self):
        vid=self.indexed();other=self.root/'other.mp4';other.write_bytes(b'synthetic other media')
        with self.app.db() as c:other_id=c.execute('INSERT INTO videos(path,title,size,mtime) VALUES(?,?,?,?)',(str(other),'Other',other.stat().st_size,other.stat().st_mtime)).lastrowid
        job=self.approve(self.prepare());self.service.jobs.claim(job)
        server=self.app.LocalServer(('127.0.0.1',0),self.app.Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            for ident,expected in ((vid,409),(other_id,200)):
                connection=HTTPConnection('127.0.0.1',server.server_address[1],timeout=5)
                connection.request('GET','/media/'+str(ident),headers={'Host':'127.0.0.1:'+str(self.app.RUNTIME.port)})
                response=connection.getresponse();content=response.read();self.assertEqual(response.status,expected)
                if expected==200:self.assertEqual(content,b'synthetic other media')
                connection.close()
        finally:server.shutdown();server.server_close();thread.join(5);self.service.jobs.finish(job,'interrupted')

    def test_review_skips_a_blocked_file_and_does_not_spin_or_fail_it(self):
        a=self.indexed();other=self.root/'other.mp4';other.write_bytes(b'synthetic second media')
        with self.app.db() as c:b=c.execute('INSERT INTO videos(path,title,size,mtime) VALUES(?,?,?,?)',(str(other),'Other',other.stat().st_size,other.stat().st_mtime)).lastrowid
        job=self.approve(self.prepare());self.service.jobs.claim(job)
        seen=[];finished=threading.Event()
        def inspect(row,mode):seen.append(row['id']);finished.set();return 'passed','synthetic check'
        self.app.REVIEW.binary=lambda name:name;self.app.REVIEW.inspect=inspect
        self.app.REVIEW.enqueue([a,b],'quick');self.assertTrue(finished.wait(3))
        deadline=time.monotonic()+3
        while self.app.REVIEW.lock.locked() and time.monotonic()<deadline:time.sleep(.01)
        self.assertFalse(self.app.REVIEW.lock.locked());self.assertEqual(seen,[b])
        with self.app.db() as c:self.assertEqual(c.execute('SELECT status FROM file_reviews WHERE video_id=?',(a,)).fetchone()[0],'queued')
        self.service.jobs.finish(job,'interrupted');self.app.REVIEW.resume()
        deadline=time.monotonic()+3
        while self.app.REVIEW.lock.locked() and time.monotonic()<deadline:time.sleep(.01)
        self.assertEqual(seen,[b,a])

    def test_pausing_waiting_writer_removes_its_admission_gate(self):
        from resource_leases import path_resource
        job=self.approve(self.prepare());reader=self.app.LEASES.try_acquire('review',resources=[path_resource(self.clip)])
        try:
            self.assertEqual(self.service.run_once(job)['state'],'waiting')
            self.assertEqual(len(self.app.LEASES.waiting()),1)
            self.assertIsNone(self.app.LEASES.try_acquire('playback',resources=[path_resource(self.clip)]))
            self.service.control(job,'pause');self.assertEqual(self.app.LEASES.waiting(),[])
            next_reader=self.app.LEASES.try_acquire('playback',resources=[path_resource(self.clip)])
            self.assertIsNotNone(next_reader);self.app.LEASES.release(next_reader)
        finally:self.app.LEASES.release(reader)

    def test_waiting_raw_asset_scope_survives_materialization(self):
        from resource_leases import path_resource,video_resources
        draft=self.prepare();asset=draft['payload']['entries'][0]['asset_id']
        job=self.approve(draft);before=self.service.jobs.resources(self.service.jobs.get(job))
        reader=self.app.LEASES.try_acquire('review',resources=[path_resource(self.clip)])
        try:
            self.assertEqual(self.service.run_once(job)['state'],'waiting')
            vid=self.service.locations.materialize(asset)
            self.assertEqual(self.service.jobs.resources(self.service.jobs.get(job)),before)
            self.assertIn('asset:'+asset,video_resources(self.app.db,[vid]))
            self.assertIsNone(self.app.LEASES.try_acquire('review',resources=video_resources(self.app.db,[vid])))
        finally:self.app.LEASES.release(reader)
        self.assertEqual(self.service.run_once(job)['state'],'done')
        self.assertEqual(self.app.LEASES.waiting(),[])

    def test_unrelated_device_job_bypasses_a_blocked_head_job(self):
        from organization_service import OrganizationService
        from organization_executor import CopyExecutor
        from resource_leases import path_resource
        b=self.root/'device-b';b.mkdir();clip=b/'other.mp4';clip.write_bytes(b'synthetic second device')
        target_a=self.root/'target-a';target_b=self.root/'target-b'
        def device(path):return 'synthetic-a' if str(path).startswith(str(self.source)) or str(path).startswith(str(target_a)) else 'synthetic-b'
        self.service=OrganizationService(self.app.db,self.inventory,device)
        self.service.copy=CopyExecutor(self.service.jobs,lambda *args:'synthetic-binding')
        jobs=[]
        for source,target in ((self.clip,target_a),(clip,target_b)):
            draft=self.service.jobs.prepare([{'source':str(source),'destination':str(target/source.name),'operation':'copy'}],source.parent,target)
            jobs.append(self.service.jobs.approve(draft['id'],draft['digest']))
        reader=self.app.LEASES.try_acquire('review',resources=[path_resource(self.clip)])
        try:
            self.service.start();deadline=time.monotonic()+5
            while self.service.jobs.get(jobs[1])['state']!='done' and time.monotonic()<deadline:time.sleep(.02)
            self.assertEqual(self.service.jobs.get(jobs[1])['state'],'done')
            self.assertEqual(self.service.jobs.get(jobs[0])['state'],'waiting')
        finally:self.app.LEASES.release(reader)
        deadline=time.monotonic()+5
        while self.service.jobs.get(jobs[0])['state']!='done' and time.monotonic()<deadline:time.sleep(.02)
        self.assertEqual(self.service.jobs.get(jobs[0])['state'],'done')
