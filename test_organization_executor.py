import hashlib
import unittest
from pathlib import Path
from unittest.mock import patch
import test_media_locations as fixtures
from organization_jobs import OrganizationJobs
from organization_executor import CopyExecutor


class ExecutorTests(unittest.TestCase):
    db=fixtures.LocationTests.db
    def setUp(self):
        fixtures.LocationTests.setUp(self)
        self.scope=self.root/'source';self.scope.mkdir()
        self.source.rename(self.scope/'one.mp4');self.source=self.scope/'one.mp4'
        with self.db() as c:
            c.execute('UPDATE videos SET path=? WHERE id=1',(str(self.source),))
            c.execute('UPDATE media_locations SET path=?',(str(self.source),))
        self.target_root=self.root/'target';self.target=self.target_root/'one.mp4'
        self.jobs=OrganizationJobs(self.db,lambda path:'volume')
        draft=self.jobs.prepare([{'video_id':1,'source':str(self.source),'destination':str(self.target),'operation':'copy'}],self.scope,self.target_root)
        self.job=self.jobs.approve(draft['id'],draft['digest'])
        self.original=hashlib.sha256(self.source.read_bytes()).hexdigest()

    def bind(self,entry,job):
        return self.repository.bind_verified_copy(entry['video_id'],entry['destination'],entry['target_device'],job)

    def assert_source(self):self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(),self.original)

    def test_real_copy_verification_binding_and_idempotent_run(self):
        executor=CopyExecutor(self.jobs,self.bind)
        result=executor.run(self.job)
        self.assertEqual(result['state'],'done');self.assertEqual(self.target.read_bytes(),self.source.read_bytes())
        self.assertEqual(len(self.repository.locations(1)),2);self.assert_source()
        self.assertEqual(self.target.stat().st_mtime_ns,self.source.stat().st_mtime_ns)
        identity=self.target.stat().st_ino
        executor.run(self.job);self.assertEqual(identity,self.target.stat().st_ino)
        self.assertFalse(list(self.target_root.glob('*.partial')))

    def test_committed_target_binding_failure_is_retried_without_copy(self):
        result=CopyExecutor(self.jobs,lambda *args:None).run(self.job)
        self.assertEqual(result['state'],'partial');self.assertEqual(result['steps'][0]['state'],'needs_binding')
        identity=self.target.stat().st_ino
        result=CopyExecutor(self.jobs,self.bind).run(self.job)
        self.assertEqual(result['state'],'done');self.assertEqual(identity,self.target.stat().st_ino);self.assert_source()

    def test_crash_after_commit_reconciles_file_identity_before_binding(self):
        def crash(stage,*args):
            if stage=='committed':raise SystemExit('synthetic crash')
        with self.assertRaises(SystemExit):CopyExecutor(self.jobs,self.bind,crash).run(self.job)
        self.jobs.recover();identity=self.target.stat().st_ino
        result=CopyExecutor(self.jobs,self.bind).run(self.job)
        self.assertEqual(result['state'],'done');self.assertEqual(identity,self.target.stat().st_ino);self.assert_source()

    def test_crash_before_commit_rejects_foreign_existing_file(self):
        def crash(stage,*args):
            if stage=='committing':raise SystemExit('synthetic crash')
        with self.assertRaises(SystemExit):CopyExecutor(self.jobs,self.bind,crash).run(self.job)
        self.jobs.recover();self.target.write_bytes(b'foreign file')
        result=CopyExecutor(self.jobs,self.bind).run(self.job)
        self.assertNotEqual(result['state'],'done');self.assertEqual(self.target.read_bytes(),b'foreign file');self.assert_source()

    def test_cancel_during_copy_retains_checkpoint_and_explicit_resume(self):
        def stop(stage,*args):
            if stage=='copying':self.jobs.request_cancel(self.job)
        result=CopyExecutor(self.jobs,self.bind,stop).run(self.job)
        self.assertEqual(result['state'],'cancelled');self.assertFalse(self.target.exists())
        partial=Path(result['steps'][0]['details']['temporary']);self.assertTrue(partial.exists())
        self.jobs.resume(self.job)
        result=CopyExecutor(self.jobs,self.bind).run(self.job)
        self.assertEqual(result['state'],'done');self.assertTrue(partial.exists());self.assert_source()

    def test_source_changed_or_space_exhausted_never_commits(self):
        self.source.write_bytes(b'changed source')
        result=CopyExecutor(self.jobs,self.bind).run(self.job)
        self.assertEqual(result['state'],'failed');self.assertFalse(self.target.exists())
        self.source.write_bytes(b'synthetic media')
        # Re-approval is necessary after source metadata changes; use a fresh plan.
        draft=self.jobs.prepare([{'video_id':1,'source':str(self.source),'destination':str(self.target),'operation':'copy'}],self.scope,self.target_root)
        job=self.jobs.approve(draft['id'],draft['digest'])
        with patch('organization_executor.shutil.disk_usage',return_value=type('Space',(),{'free':0})()):
            result=CopyExecutor(self.jobs,self.bind).run(job)
        self.assertEqual(result['state'],'failed');self.assertFalse(self.target_root.exists())

    def test_target_created_during_commit_is_not_adopted_even_with_same_bytes(self):
        def occupy(stage,*args):
            if stage=='committing':self.target.write_bytes(self.source.read_bytes())
        result=CopyExecutor(self.jobs,self.bind,occupy).run(self.job)
        self.assertNotEqual(result['state'],'done')
        self.assertEqual(len(self.repository.locations(1)),1)
        self.assertEqual(self.target.read_bytes(),self.source.read_bytes());self.assert_source()

    def test_cancel_before_start_does_not_create_target_directory(self):
        self.jobs.request_cancel(self.job)
        result=CopyExecutor(self.jobs,self.bind).run(self.job)
        self.assertEqual(result['state'],'cancelled');self.assertFalse(self.target_root.exists());self.assert_source()
