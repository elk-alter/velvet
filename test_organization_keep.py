"""In-place organization verifies/binds existing files without changing them."""
import unittest
from pathlib import Path
from unittest.mock import patch
import test_organization_service as fixtures


class KeepTests(unittest.TestCase):
    setUp=fixtures.ServiceTests.setUp
    tearDown=fixtures.ServiceTests.tearDown
    shutdown=fixtures.ServiceTests.shutdown

    def prepare(self):
        plan=self.inventory.plan(self.device,'',operation='keep')
        return self.service.prepare(plan['id'],plan['digest'],[0],'keep')

    def test_in_place_plan_has_no_destination_or_space_requirement(self):
        before=self.clip.stat()
        with patch('storage_inventory.target_space',side_effect=AssertionError('keep must not require disk space')):
            plan=self.inventory.plan(self.device,'',operation='keep')
        self.assertEqual(plan['entries'][0]['destination'],str(self.clip))
        self.assertEqual(plan['space']['required'],0);self.assertFalse(plan['entries'][0]['conflicts'])
        draft=self.service.prepare(plan['id'],plan['digest'],[0],'keep')
        job=self.service.approve(draft['id'],draft['digest'])
        self.assertEqual(self.service.run_once(job)['state'],'done')
        self.assertEqual((self.clip.stat().st_size,self.clip.stat().st_mtime_ns),(before.st_size,before.st_mtime_ns))
        self.assertFalse(self.target.exists())
        with self.app.db() as c:
            row=c.execute('SELECT path,title FROM videos').fetchone()
            self.assertEqual(tuple(row),(str(self.clip),'Synthetic title'))
        self.assertEqual(self.service.run_once(job)['state'],'done')

    def test_locked_metadata_and_default_copy_stay_unchanged(self):
        # Index/keep the source first, then make another location the default.
        draft=self.prepare();job=self.service.approve(draft['id'],draft['digest']);self.service.run_once(job)
        with self.app.db() as c:
            vid=c.execute('SELECT id FROM videos').fetchone()[0]
            c.execute('UPDATE videos SET title=?,favorite=1,position=12,locked_fields=? WHERE id=?',('Manual','["title"]',vid))
        plan=self.inventory.plan(self.device,str(self.target));copied=self.service.prepare(plan['id'],plan['digest'],[0])
        copy=self.service.approve(copied['id'],copied['digest']);self.assertEqual(self.service.run_once(copy)['state'],'done')
        draft=self.prepare();keep=self.service.approve(draft['id'],draft['digest']);self.assertEqual(self.service.run_once(keep)['state'],'done')
        with self.app.db() as c:
            row=c.execute('SELECT path,title,favorite,position,locked_fields FROM videos WHERE id=?',(vid,)).fetchone()
            self.assertEqual(tuple(row),(copied['payload']['entries'][0]['destination'],'Manual',1,12,'["title"]'))
            self.assertEqual(c.execute('SELECT COUNT(*) FROM videos').fetchone()[0],1)

    def test_companions_are_associated_at_original_paths(self):
        subtitle=self.clip.with_suffix('.srt');subtitle.write_bytes(b'synthetic subtitle')
        self.inventory.scan(self.device,hashing=True)
        draft=self.prepare();self.assertEqual(len(draft['payload']['entries']),2)
        job=self.service.approve(draft['id'],draft['digest']);self.assertEqual(self.service.run_once(job)['state'],'done')
        with self.app.db() as c:self.assertEqual(c.execute('SELECT path FROM media_resources').fetchone()[0],str(subtitle))
        self.assertEqual(subtitle.read_bytes(),b'synthetic subtitle')

    def test_keep_does_not_accept_an_unreviewed_relocation_preview(self):
        plan=self.inventory.plan(self.device,str(self.target))
        with self.assertRaises(ValueError):self.service.prepare(plan['id'],plan['digest'],[0],'keep')

    def test_source_change_after_approval_is_not_bound(self):
        draft=self.prepare();job=self.service.approve(draft['id'],draft['digest'])
        self.clip.write_bytes(b'changed synthetic file')
        self.assertEqual(self.service.run_once(job)['state'],'failed')
        self.assertIn('变化',self.service.detail(job)['items'][0]['step']['details'].get('error',''))
        with self.app.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM videos').fetchone()[0],0)

    def test_pause_cancel_and_reader_wait_have_no_file_writes(self):
        from organization_service import OrganizationService
        draft=self.prepare();job=self.service.approve(draft['id'],draft['digest'])
        self.service.control(job,'pause');self.assertEqual(self.service.run_once(job)['state'],'waiting')
        self.service.control(job,'cancel');self.assertEqual(self.service.run_once(job)['state'],'cancelled')
        self.service=OrganizationService(self.app.db,self.inventory);self.service.jobs.recover();self.service.control(job,'resume')
        owner=self.app.LEASES.try_acquire('scan')
        try:self.assertEqual(self.service.run_once(job)['state'],'waiting')
        finally:self.app.LEASES.release(owner)
        with patch('organization_executor.publish_no_replace',side_effect=AssertionError('keep cannot publish files')):
            self.assertEqual(self.service.run_once(job)['state'],'done')
        self.assertFalse(self.target.exists())

    def test_crash_after_binding_retries_without_a_duplicate_card(self):
        from organization_service import OrganizationService
        class Crash(BaseException):pass
        draft=self.prepare();job=self.service.approve(draft['id'],draft['digest'])
        def crash(stage,*args):
            if stage=='bound':raise Crash()
        self.service.keep.checkpoint=crash
        with self.assertRaises(Crash):self.service.run_once(job)
        with self.app.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM videos').fetchone()[0],1)
        self.service=OrganizationService(self.app.db,self.inventory);self.service.jobs.recover();self.service.control(job,'resume')
        self.assertEqual(self.service.run_once(job)['state'],'done')
        with self.app.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM videos').fetchone()[0],1)

    def test_keep_plan_cannot_be_revised_into_a_move_or_prepared_as_copy(self):
        plan=self.inventory.plan(self.device,'',operation='keep')
        with self.assertRaises(ValueError):self.inventory.revise_plan(plan['id'],plan['digest'],[0],{'0':'renamed.mp4'})
        with self.assertRaises(ValueError):self.service.prepare(plan['id'],plan['digest'],[0],'copy')
        with self.assertRaises(ValueError):self.service.jobs.prepare([{'source':str(self.clip),'destination':str(self.source/'other.mp4'),'operation':'keep'}],self.source,self.source)


class KeepHTTPTests(unittest.TestCase):
    import test_organization_http as fixtures
    setUp=fixtures.OrganizationHTTPTests.setUp
    tearDown=fixtures.OrganizationHTTPTests.tearDown
    request=fixtures.OrganizationHTTPTests.request
    wait_job=fixtures.OrganizationHTTPTests.wait_job
    shutdown=KeepTests.shutdown

    def test_keep_preview_confirmation_and_queue_without_target_directory(self):
        status,result=self.request('/api/storage/plan',{'id':self.device,'operation':'keep'})
        self.assertEqual(status,200,result);plan=result['plan']
        self.assertEqual(plan['space']['required'],0)
        status,result=self.request('/api/organization/prepare',{'id':plan['id'],'digest':plan['digest'],'selected':'all','operation':'keep'})
        self.assertEqual(status,200,result)
        status,draft=self.request('/api/organization/draft?id='+result['id'])
        self.assertEqual(status,200);self.assertEqual(draft['operation'],'keep');self.assertEqual(draft['resource_count'],1)
        status,_=self.request('/api/organization/approve',{'id':draft['id'],'digest':draft['digest'],'confirmed':False})
        self.assertEqual(status,400)
        status,result=self.request('/api/organization/approve',{'id':draft['id'],'digest':draft['digest'],'confirmed':True})
        self.assertEqual(status,200,result);self.assertEqual(self.wait_job(result['id'])['state'],'done')
        with self.app.db() as c:
            self.assertEqual(c.execute('SELECT path FROM videos').fetchone()[0],str(self.clip))
            self.assertEqual(c.execute('SELECT path FROM media_resources').fetchone()[0],str(self.source/'part1.en.srt'))
        self.assertFalse(self.target.exists())
