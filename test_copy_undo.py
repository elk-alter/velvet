"""Copy binding rollback retains files, user data and durable approval semantics."""
import json
import unittest
from pathlib import Path
import test_organization_service as fixtures
from organization_service import OrganizationService
from organization_executor import CopyExecutor
from resource_leases import ResourceBusy


class CopyUndoTests(unittest.TestCase):
    setUp=fixtures.ServiceTests.setUp
    tearDown=fixtures.ServiceTests.tearDown
    shutdown=fixtures.ServiceTests.shutdown
    prepare=fixtures.ServiceTests.prepare
    approve=fixtures.ServiceTests.approve

    def copied(self):
        draft=self.prepare();job=self.approve(draft)
        self.assertEqual(self.service.run_once(job)['state'],'done')
        return job,Path(draft['payload']['entries'][0]['destination'])

    def inverse(self,job):
        draft=self.service.prepare_inverse(job)
        inverse=self.service.approve_inverse(job,draft['digest'])
        self.assertEqual(inverse,self.service.approve_inverse(job,draft['digest']))
        return inverse

    def test_unbind_keeps_identity_manual_data_quality_and_both_files(self):
        with self.app.db() as c:
            vid=c.execute('INSERT INTO videos(path,title,size,mtime,favorite,position,locked_fields) VALUES(?,?,?,?,?,?,?)',(str(self.clip),'Manual',self.clip.stat().st_size,self.clip.stat().st_mtime,1,42,'["title"]')).lastrowid
        self.app.REVIEW.mark([vid],flag='watermark')
        job,target=self.copied();asset=self.service.locations.asset(vid)['id']
        inverse=self.inverse(job);result=self.service.run_once(inverse)
        self.assertEqual(result['state'],'done')
        with self.app.db() as c:
            row=dict(c.execute('SELECT * FROM videos WHERE id=?',(vid,)).fetchone())
            self.assertEqual((row['path'],row['title'],row['favorite'],row['position'],row['locked_fields']),(str(self.clip),'Manual',1,42,'["title"]'))
            self.assertEqual(c.execute('SELECT COUNT(*) FROM videos').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM media_locations WHERE path=?',(str(target),)).fetchone()[0],0)
        self.assertEqual(self.service.locations.asset(vid)['id'],asset)
        self.assertTrue(any(i['code']=='watermark' for i in self.app.REVIEW.snapshot([row])['files'][vid]['issues']))
        self.assertEqual(target.read_bytes(),self.clip.read_bytes())
        self.assertEqual(self.service.run_once(inverse)['state'],'done')
        with self.assertRaises(ValueError):self.service.control(job,'resume')

    def test_raw_card_survives_and_companion_files_are_retained(self):
        subtitle=self.source/'part1.en.srt';subtitle.write_bytes(b'synthetic subtitle')
        self.inventory.scan(self.device,hashing=True)
        job,target=self.copied();inverse=self.inverse(job)
        self.assertEqual(self.service.run_once(inverse)['state'],'done')
        with self.app.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM media_resources').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT path FROM videos').fetchone()[0],str(self.clip))
            self.assertEqual(c.execute("SELECT COUNT(*) FROM media_resource_events WHERE kind='copy_unbound'").fetchone()[0],1)
        self.assertEqual((target.parent/subtitle.name).read_bytes(),subtitle.read_bytes())

    def test_secondary_copy_undo_preserves_later_default(self):
        first,target=self.copied()
        with self.app.db() as c:vid=c.execute('SELECT id FROM videos').fetchone()[0]
        self.target=self.root/'other-target'
        later,other=self.copied()
        inverse=self.inverse(first);self.assertEqual(self.service.run_once(inverse)['state'],'done')
        with self.app.db() as c:self.assertEqual(c.execute('SELECT path FROM videos WHERE id=?',(vid,)).fetchone()[0],str(other))
        self.assertTrue(target.exists());self.assertTrue(other.exists())

    def test_changed_source_blocks_approval_and_can_be_replanned(self):
        job,target=self.copied();draft=self.service.prepare_inverse(job)
        data=self.clip.read_bytes();self.clip.write_bytes(b'changed synthetic source')
        with self.assertRaises(ValueError):self.service.approve_inverse(job,draft['digest'])
        self.assertTrue(target.exists())
        self.clip.write_bytes(data)
        fresh=self.service.prepare_inverse(job)
        self.assertNotEqual(draft['digest'],fresh['digest'])
        with self.assertRaises(ValueError):self.service.approve_inverse(job,draft['digest'])
        inverse=self.service.approve_inverse(job,fresh['digest']);self.assertEqual(self.service.run_once(inverse)['state'],'done')

    def test_same_bytes_target_replacement_is_not_adopted(self):
        job,target=self.copied();target.rename(target.with_suffix('.owned'));target.write_bytes(self.clip.read_bytes())
        with self.assertRaises(ValueError):self.service.prepare_inverse(job)
        with self.app.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM media_locations').fetchone()[0],2)

    def test_current_preference_change_invalidates_old_approval(self):
        job,target=self.copied();draft=self.service.prepare_inverse(job)
        with self.app.db() as c:
            vid=c.execute('SELECT id FROM videos').fetchone()[0]
            location=c.execute('SELECT id FROM media_locations WHERE path=?',(str(self.clip),)).fetchone()[0]
        self.service.locations.prefer(vid,location)
        with self.assertRaises(ValueError):self.service.approve_inverse(job,draft['digest'])
        fresh=self.service.prepare_inverse(job);inverse=self.service.approve_inverse(job,fresh['digest'])
        self.assertEqual(self.service.run_once(inverse)['state'],'done')

    def test_reader_blocks_preview_and_execution_waits_without_mutation(self):
        job,target=self.copied();owner=self.app.LEASES.try_acquire('scan')
        try:
            with self.assertRaises(ResourceBusy):self.service.prepare_inverse(job)
        finally:self.app.LEASES.release(owner)
        inverse=self.inverse(job);owner=self.app.LEASES.try_acquire('scan')
        try:self.assertEqual(self.service.run_once(inverse)['state'],'waiting')
        finally:self.app.LEASES.release(owner)
        with self.app.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM media_locations').fetchone()[0],2)
        self.assertEqual(self.service.run_once(inverse)['state'],'done');self.assertTrue(target.exists())

    def test_own_reconciliation_does_not_override_another_jobs_quarantine(self):
        job,target=self.copied();inverse=self.inverse(job)
        with self.app.db() as c:
            for owner in ('reconcile:'+job,'reconcile:another-synthetic-job'):
                c.execute("INSERT INTO resource_claims VALUES(?,'organize-reconcile','write',0)",(owner,))
        self.assertEqual(self.service.run_once(inverse)['state'],'waiting')
        with self.app.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM media_locations').fetchone()[0],2)
            c.execute("DELETE FROM resource_claims WHERE owner='reconcile:another-synthetic-job'")
        self.assertEqual(self.service.run_once(inverse)['state'],'done')

    def test_pause_cancel_restart_and_resume_keep_fixed_approval(self):
        job,target=self.copied();inverse=self.inverse(job)
        self.service.control(inverse,'pause');self.assertEqual(self.service.run_once(inverse)['state'],'waiting')
        self.service.control(inverse,'cancel');self.assertEqual(self.service.run_once(inverse)['state'],'cancelled')
        self.service=OrganizationService(self.app.db,self.inventory);self.service.jobs.recover()
        self.service.control(inverse,'resume');self.assertEqual(self.service.run_once(inverse)['state'],'done')
        self.assertTrue(target.exists());self.assertTrue(self.clip.exists())

    def test_crash_before_atomic_commit_preserves_bindings_and_recovers(self):
        class Crash(BaseException):pass
        job,target=self.copied();inverse=self.inverse(job)
        def crash(stage,*args):
            if stage=='before_commit':raise Crash()
        self.service.copy_undo.checkpoint=crash
        with self.assertRaises(Crash):self.service.run_once(inverse)
        with self.app.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM media_locations').fetchone()[0],2)
            self.assertEqual(c.execute('SELECT path FROM videos').fetchone()[0],str(target))
        self.service=OrganizationService(self.app.db,self.inventory);self.service.jobs.recover()
        self.service.control(inverse,'resume');self.assertEqual(self.service.run_once(inverse)['state'],'done')

    def test_crash_after_commit_is_terminal_and_not_repeated(self):
        class Crash(BaseException):pass
        job,target=self.copied();inverse=self.inverse(job)
        def crash(stage,*args):
            if stage=='committed':raise Crash()
        self.service.copy_undo.checkpoint=crash
        with self.assertRaises(Crash):self.service.run_once(inverse)
        self.service=OrganizationService(self.app.db,self.inventory);self.service.jobs.recover()
        self.assertEqual(self.service.run_once(inverse)['state'],'done')
        with self.app.db() as c:self.assertEqual(c.execute("SELECT COUNT(*) FROM media_location_events WHERE kind='copy_unbound'").fetchone()[0],1)

    def test_ordinary_approval_cannot_bypass_inverse_registration(self):
        job,target=self.copied();draft=self.service.prepare_inverse(job)
        with self.assertRaises(ValueError):self.service.approve(draft['id'],draft['digest'])
        with self.assertRaises(ValueError):self.service.approve_inverse(job,'wrong')
        self.assertTrue(target.exists())

    def test_failed_approved_plan_can_be_replaced_and_old_task_stays_disabled(self):
        job,target=self.copied();inverse=self.inverse(job)
        with self.app.db() as c:
            vid=c.execute('SELECT id FROM videos').fetchone()[0]
            original=c.execute('SELECT id FROM media_locations WHERE path=?',(str(self.clip),)).fetchone()[0]
        self.service.locations.prefer(vid,original)
        self.assertEqual(self.service.run_once(inverse)['state'],'failed')
        fresh=self.service.prepare_inverse(job)
        self.assertTrue(self.service.detail(inverse)['superseded'])
        with self.assertRaises(ValueError):self.service.control(inverse,'resume')
        replacement=self.service.approve_inverse(job,fresh['digest'])
        self.assertNotEqual(replacement,inverse)
        self.service=OrganizationService(self.app.db,self.inventory);self.service.jobs.recover()
        with self.assertRaises(ValueError):self.service.control(inverse,'resume')
        self.assertEqual(self.service.run_once(replacement)['state'],'done')
        self.assertTrue(target.exists())

    def test_active_undo_cannot_be_replanned_until_stopped(self):
        job,target=self.copied();inverse=self.inverse(job)
        with self.assertRaisesRegex(ValueError,'暂停或取消'):self.service.prepare_inverse(job)
        self.service.control(inverse,'pause')
        fresh=self.service.prepare_inverse(job)
        self.assertTrue(fresh['id']);self.assertTrue(self.service.detail(inverse)['superseded'])
        repeated=self.service.prepare_inverse(job)
        self.assertEqual(fresh['digest'],repeated['digest'])
        replacement=self.service.approve_inverse(job,fresh['digest'])
        self.assertNotEqual(replacement,inverse)
        self.assertEqual(self.service.run_once(replacement)['state'],'done')

    def test_unbound_committed_target_is_retained_without_false_undo(self):
        draft=self.prepare();job=self.approve(draft)
        self.service.copy=CopyExecutor(self.service.jobs,lambda *args:None)
        self.assertEqual(self.service.run_once(job)['state'],'partial')
        with self.assertRaises(ValueError):self.service.prepare_inverse(job)
        self.assertTrue(Path(draft['payload']['entries'][0]['destination']).exists())

    def test_changed_target_after_approval_fails_without_partial_unbinding(self):
        job,target=self.copied();inverse=self.inverse(job)
        target.write_bytes(b'changed synthetic target')
        self.assertEqual(self.service.run_once(inverse)['state'],'failed')
        with self.app.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM media_locations').fetchone()[0],2)
            self.assertEqual(c.execute('SELECT path FROM videos').fetchone()[0],str(target))
        self.assertIn('变化',self.service.detail(inverse)['items'][0]['step']['details']['error'])

    def test_missing_original_cannot_choose_another_target_also_being_unbound(self):
        from media_locations import fingerprint
        import uuid
        with self.app.db() as c:
            vid=c.execute('INSERT INTO videos(path,title,size,mtime) VALUES(?,?,?,?)',(str(self.clip),'Synthetic',self.clip.stat().st_size,self.clip.stat().st_mtime)).lastrowid
        self.service.locations.migrate([vid]);asset=self.service.locations.asset(vid)['id']
        alias=self.source/'alias.mp4';alias.write_bytes(self.clip.read_bytes());stamp=fingerprint(alias)
        with self.app.db() as c:c.execute('INSERT INTO media_locations VALUES(?,?,?,?,?,?,?,0,?)',(uuid.uuid4().hex,asset,self.service.jobs.device_identity(alias),str(alias),stamp['size'],stamp['mtime_ns'],1,'synthetic'))
        self.inventory.scan(self.device,hashing=True)
        plan=self.inventory.plan(self.device,str(self.target));draft=self.service.prepare(plan['id'],plan['digest'],[0,1])
        job=self.approve(draft);self.assertEqual(self.service.run_once(job)['state'],'done')
        self.clip.rename(self.clip.with_suffix('.offline'));alias.rename(alias.with_suffix('.offline'))
        with self.assertRaisesRegex(ValueError,'连接原盘'):self.service.prepare_inverse(job)


class CopyUndoHTTPTests(unittest.TestCase):
    # Real HTTP authorization and public preview/approve/queue, with isolated data.
    import test_organization_http as http_fixtures
    setUp=http_fixtures.OrganizationHTTPTests.setUp
    tearDown=http_fixtures.OrganizationHTTPTests.tearDown
    request=http_fixtures.OrganizationHTTPTests.request
    draft=http_fixtures.OrganizationHTTPTests.draft
    wait_job=http_fixtures.OrganizationHTTPTests.wait_job
    shutdown=fixtures.ServiceTests.shutdown

    def test_public_copy_undo_confirmation_and_paged_details(self):
        draft=self.draft()
        status,result=self.request('/api/organization/approve',{'id':draft['id'],'digest':draft['digest'],'confirmed':True})
        self.assertEqual(status,200);original=result['id'];self.assertEqual(self.wait_job(original)['state'],'done')
        status,result=self.request('/api/organization/undo-preview',{'id':original},False);self.assertEqual(status,403)
        status,result=self.request('/api/organization/undo-preview',{'id':original});self.assertEqual(status,200,result)
        status,reverse=self.request('/api/organization/draft?id='+result['id']+'&limit=1')
        self.assertEqual(status,200);self.assertEqual((reverse['operation'],reverse['inverse_of'],reverse['total']),('unbind_copy',original,2))
        status,_=self.request('/api/organization/undo-approve',{'id':original,'digest':reverse['digest'],'confirmed':False});self.assertEqual(status,400)
        status,result=self.request('/api/organization/undo-approve',{'id':original,'digest':reverse['digest'],'confirmed':True});self.assertEqual(status,200,result)
        self.assertEqual(self.wait_job(result['id'])['state'],'done')
        with self.app.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM media_resources').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT path FROM videos').fetchone()[0],str(self.clip))
        self.assertTrue((self.target/'part1.mp4').exists());self.assertTrue((self.target/'part1.en.srt').exists());self.assertTrue(self.clip.exists())
