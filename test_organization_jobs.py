from pathlib import Path
import unittest
from unittest.mock import patch
import test_media_locations as fixtures
from organization_jobs import OrganizationJobs


class OrganizationJobTests(unittest.TestCase):
    db=fixtures.LocationTests.db
    def setUp(self):
        fixtures.LocationTests.setUp(self)
        self.scope=self.root/'source';self.scope.mkdir()
        self.source.rename(self.scope/'one.mp4');self.source=self.scope/'one.mp4'
        self.target=self.root/'target'
        self.jobs=OrganizationJobs(self.db,lambda path:'fixture-device')

    def draft(self,name='one.mp4'):
        return self.jobs.prepare([{'source':str(self.source),'destination':str(self.target/name),'video_id':1,'operation':'copy'}],self.scope,self.target)

    def approved(self,name='one.mp4'):
        draft=self.draft(name)
        return self.jobs.approve(draft['id'],draft['digest'])

    def test_approval_is_immutable_and_idempotent(self):
        draft=self.draft();job=self.jobs.approve(draft['id'],draft['digest'])
        self.assertEqual(job,self.jobs.approve(draft['id'],draft['digest']))
        self.assertFalse(self.target.exists())
        self.assertEqual(self.jobs.get(job)['steps'][0]['state'],'pending')
        with self.assertRaises(ValueError):self.jobs.approve(draft['id'],'different')
        self.assertEqual(len([r for r in self.jobs.journal.list() if r['kind']=='organize']),1)

    def test_changed_source_or_device_invalidates_approval(self):
        draft=self.draft();self.source.write_bytes(b'changed')
        with self.assertRaises(ValueError):self.jobs.approve(draft['id'],draft['digest'])
        draft=self.draft();self.jobs.device_identity=lambda path:'changed-device'
        with self.assertRaises(ValueError):self.jobs.approve(draft['id'],draft['digest'])

    def test_recovery_root_requires_the_original_directory_and_volume(self):
        with patch('organization_jobs.volume_identity',return_value=('synthetic-volume',True)):
            job=self.approved()
            approval=self.jobs.get(job)
            source_bytes=self.source.read_bytes()
            original=str(self.scope)
            moved=self.root/'same-directory-new-path'
            self.scope.rename(moved)
            self.scope.mkdir()
            self.assertEqual(self.jobs.verify_recovery_roots(job,{original:str(moved)}),{original:str(moved)})
            with self.assertRaisesRegex(ValueError,'目录身份'):
                self.jobs.verify_recovery_roots(job,{original:original})
            with self.assertRaisesRegex(ValueError,'缺少原目录证明'):
                self.jobs.verify_recovery_roots(job,{str(self.target):str(moved)})
            self.assertEqual(self.jobs.get(job),approval)
            self.assertEqual((moved/'one.mp4').read_bytes(),source_bytes)
        with patch('organization_jobs.volume_identity',return_value=('different-volume',True)):
            with self.assertRaisesRegex(ValueError,'目录身份'):
                self.jobs.verify_recovery_roots(job,{original:str(moved)})

    def test_approval_rejects_replaced_target_directory_on_same_volume(self):
        self.target.mkdir()
        with patch('organization_jobs.volume_identity',return_value=('synthetic-volume',True)):
            draft=self.draft()
            self.target.rename(self.root/'original-target')
            self.target.mkdir()
            with self.assertRaisesRegex(ValueError,'目录身份'):
                self.jobs.approve(draft['id'],draft['digest'])

    def test_leases_are_exclusive_and_released(self):
        first=self.approved();second=self.approved('second.mp4')
        self.jobs.claim(first)
        with self.assertRaises(ValueError):self.jobs.claim(first)
        with self.assertRaises(ValueError):self.jobs.claim(second)
        self.assertEqual(self.jobs.get(second)['state'],'queued')
        self.jobs.finish(first,'interrupted');self.jobs.claim(second)
        self.assertEqual(self.jobs.get(second)['state'],'running')

    def test_transitions_compare_and_swap_and_completion_gate(self):
        job=self.approved();self.jobs.claim(job)
        self.jobs.transition(job,0,'pending','copying',{'bytes':0})
        with self.assertRaises(ValueError):self.jobs.transition(job,0,'pending','copying')
        with self.assertRaises(ValueError):self.jobs.finish(job,'done')
        for before,after in [('copying','verified'),('verified','committing'),('committing','committed'),('committed','binding'),('binding','done')]:self.jobs.transition(job,0,before,after)
        self.jobs.finish(job,'done')
        self.assertEqual(self.jobs.get(job)['state'],'done')
        with self.assertRaises(ValueError):self.jobs.claim(job)

    def test_restart_preserves_exact_checkpoint_and_reconciles_task(self):
        job=self.approved();self.jobs.claim(job);self.jobs.transition(job,0,'pending','copying',{'bytes':4})
        restarted=OrganizationJobs(self.db,lambda path:'fixture-device')
        self.assertEqual(restarted.recover(),[job]);self.assertEqual(restarted.recover(),[])
        step=restarted.get(job)['steps'][0]
        self.assertEqual((step['state'],step['details']['bytes']),('copying',4))
        self.assertEqual(restarted.journal.list()[0]['state'],'interrupted')
        restarted.claim(job)

    def test_paths_and_targets_cannot_escape_scope(self):
        with self.assertRaises(ValueError):self.jobs.prepare([{'source':str(self.source),'destination':str(self.root/'escape.mp4'),'operation':'copy'}],self.scope,self.target)
        with self.assertRaises(ValueError):self.jobs.prepare([{'source':str(self.source),'destination':str(self.scope/'nested'/'copy.mp4'),'operation':'copy'}],self.scope,self.scope/'nested')
        self.target.mkdir();(self.target/'one.mp4').write_bytes(b'foreign')
        with self.assertRaises(ValueError):self.draft()
        self.assertEqual((self.target/'one.mp4').read_bytes(),b'foreign')

    def test_resource_conflict_rolls_back_all_partial_leases(self):
        self.jobs.device_identity=lambda path:'A-source' if self.scope in Path(path).parents or Path(path)==self.scope else 'Z-target'
        job=self.approved()
        with self.db() as c:c.execute('INSERT INTO organization_leases VALUES(?,?)',('Z-target','other-owner'))
        with self.assertRaises(ValueError):self.jobs.claim(job)
        with self.db() as c:self.assertFalse(c.execute('SELECT 1 FROM organization_leases WHERE resource=?',('A-source',)).fetchone())
        self.assertEqual(self.jobs.get(job)['state'],'queued')
