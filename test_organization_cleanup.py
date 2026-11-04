import unittest
from pathlib import Path
from unittest.mock import patch
import test_organization_executor as fixtures
from organization_executor import CopyExecutor
from organization_cleanup import CopyCleanup,remove_verified_fragment


class CleanupTests(unittest.TestCase):
    db=fixtures.ExecutorTests.db
    setUp=fixtures.ExecutorTests.setUp
    bind=fixtures.ExecutorTests.bind
    assert_source=fixtures.ExecutorTests.assert_source

    def stopped_copy(self):
        def stop(stage,*args):
            if stage=='copying':self.jobs.request_cancel(self.job)
        result=CopyExecutor(self.jobs,self.bind,stop).run(self.job)
        self.assertEqual(result['state'],'cancelled')
        return Path(result['steps'][0]['details']['temporary'])

    def test_confirmed_owned_fragment_removal_and_resume(self):
        temporary=self.stopped_copy();cleanup=CopyCleanup(self.jobs)
        plan=cleanup.preview(self.job);self.assertEqual(plan['bytes'],15)
        self.assertEqual(cleanup.clean(self.job,plan['digest'])['removed'],1)
        self.assertFalse(temporary.exists());self.assert_source()
        self.jobs.resume(self.job)
        self.assertEqual(CopyExecutor(self.jobs,self.bind).run(self.job)['state'],'done')

    def test_changed_or_replaced_fragment_is_never_removed(self):
        temporary=self.stopped_copy();cleanup=CopyCleanup(self.jobs);plan=cleanup.preview(self.job)
        temporary.unlink();temporary.write_bytes(b'external file')
        with self.assertRaises(ValueError):cleanup.clean(self.job,plan['digest'])
        self.assertEqual(temporary.read_bytes(),b'external file')
        self.assertEqual(cleanup.preview(self.job)['entries'][0]['status'],'review')
        self.assert_source()

    def test_final_handle_recheck_rejects_file_replaced_after_path_checks(self):
        temporary=self.stopped_copy();cleanup=CopyCleanup(self.jobs);plan=cleanup.preview(self.job)
        def replace_before_handle(path,record,stamp):
            path.unlink();path.write_bytes(b'external replacement')
            remove_verified_fragment(path,record,stamp)
        with patch('organization_cleanup.remove_verified_fragment',side_effect=replace_before_handle):
            with self.assertRaises(ValueError):cleanup.clean(self.job,plan['digest'])
        self.assertEqual(temporary.read_bytes(),b'external replacement')
        with self.db() as c:self.assertFalse(c.execute('SELECT 1 FROM organization_maintenance').fetchone())

    def test_previous_fragment_is_cleanable_after_success_without_removing_target(self):
        temporary=self.stopped_copy();self.jobs.resume(self.job)
        self.assertEqual(CopyExecutor(self.jobs,self.bind).run(self.job)['state'],'done')
        cleanup=CopyCleanup(self.jobs);plan=cleanup.preview(self.job)
        self.assertEqual(cleanup.clean(self.job,plan['digest'])['removed'],1)
        self.assertFalse(temporary.exists());self.assertEqual(self.target.read_bytes(),b'synthetic media')
        self.assert_source()

    def test_active_task_or_device_lease_blocks_cleanup(self):
        temporary=self.stopped_copy();cleanup=CopyCleanup(self.jobs);plan=cleanup.preview(self.job)
        with self.db() as c:c.execute('INSERT INTO organization_leases VALUES(?,?)',('volume','other-job'))
        with self.assertRaises(ValueError):cleanup.clean(self.job,plan['digest'])
        with self.db() as c:self.assertFalse(c.execute('SELECT 1 FROM organization_maintenance').fetchone())
        self.assertTrue(temporary.exists())
        with self.db() as c:c.execute('DELETE FROM organization_leases')
        self.jobs.resume(self.job)
        with self.assertRaises(ValueError):cleanup.preview(self.job)

    def test_move_staging_is_never_a_copy_cleanup_candidate(self):
        with self.db() as c:
            import json
            job=self.jobs.get(self.job);payload=job['payload'];payload['entries'][0]['operation']='move'
            c.execute('UPDATE organization_jobs SET payload=?,state=? WHERE id=?',(json.dumps(payload),'cancelled',self.job))
        self.assertEqual(CopyCleanup(self.jobs).preview(self.job)['entries'],[])

    def test_uncertain_previous_fragment_is_retained_in_log_when_current_is_cleaned(self):
        previous=self.stopped_copy();self.jobs.resume(self.job)
        current=self.stopped_copy();previous.write_bytes(b'changed outside the task')
        cleanup=CopyCleanup(self.jobs);plan=cleanup.preview(self.job)
        self.assertEqual(cleanup.clean(self.job,plan['digest']),{'removed':1,'review':1})
        self.assertFalse(current.exists());self.assertTrue(previous.exists())
        remaining=cleanup.preview(self.job)['entries']
        self.assertEqual(len(remaining),1);self.assertEqual(remaining[0]['status'],'review')
        self.assertEqual(remaining[0]['record']['path'],str(previous))
