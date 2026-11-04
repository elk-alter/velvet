"""Source archival is separate from copying and never deletes the retained copy."""
import unittest
import os
import json
from pathlib import Path
from unittest.mock import patch
import test_organization_service as fixtures


@unittest.skipUnless(os.name=='nt','Source archival is currently enabled only for verified Windows NTFS/ReFS')
class ArchiveTests(unittest.TestCase):
    setUp=fixtures.ServiceTests.setUp
    tearDown=fixtures.ServiceTests.tearDown
    shutdown=fixtures.ServiceTests.shutdown
    prepare=fixtures.ServiceTests.prepare
    approve=fixtures.ServiceTests.approve

    def copied(self):
        draft=self.prepare();job=self.approve(draft)
        self.assertEqual(self.service.run_once(job)['state'],'done')
        return job,Path(draft['payload']['entries'][0]['destination'])

    def test_archive_is_explicit_recoverable_and_keeps_default_copy(self):
        job,target=self.copied();original=self.clip.read_bytes()
        draft=self.service.archive.prepare(job)
        destination=Path(draft['payload']['entries'][0]['destination'])
        self.assertTrue(self.clip.exists());self.assertFalse(destination.parent.exists())
        archived=self.service.archive.approve(job,draft['id'],draft['digest'])
        self.assertEqual(self.service.run_once(archived)['state'],'done')
        self.assertFalse(self.clip.exists());self.assertEqual(destination.read_bytes(),original)
        self.assertEqual(target.read_bytes(),original)
        with self.app.db() as c:self.assertEqual(c.execute('SELECT path FROM videos').fetchone()[0],str(target))
        reverse=self.service.prepare_inverse(archived)
        inverse=self.service.approve_inverse(archived,reverse['digest'])
        self.assertEqual(self.service.run_once(inverse)['state'],'done')
        self.assertEqual(self.clip.read_bytes(),original);self.assertTrue(target.exists())
        repeated=self.service.archive.prepare(job)
        self.assertNotEqual(repeated['digest'],draft['digest'])
        again=self.service.archive.approve(job,repeated['id'],repeated['digest'])
        self.assertEqual(self.service.run_once(again)['state'],'done')

    def test_incomplete_copy_and_plain_approval_cannot_archive(self):
        draft=self.prepare();job=self.approve(draft)
        with self.assertRaises(ValueError):self.service.archive.prepare(job)
        self.assertEqual(self.service.run_once(job)['state'],'done')
        archive=self.service.archive.prepare(job)
        with self.assertRaises(ValueError):self.service.approve(archive['id'],archive['digest'])
        self.assertTrue(self.clip.exists())

    def test_target_changes_invalidate_preview_and_approved_execution(self):
        job,target=self.copied();draft=self.service.archive.prepare(job)
        target.write_bytes(b'changed synthetic copy')
        with self.assertRaises(ValueError):self.service.archive.approve(job,draft['id'],draft['digest'])
        self.assertTrue(self.clip.exists())

    def test_source_changed_since_copy_cannot_be_archived_as_if_backed_up(self):
        job,target=self.copied();self.clip.write_bytes(b'new unbacked source contents')
        with self.assertRaisesRegex(ValueError,'复制后发生变化'):self.service.archive.prepare(job)
        self.assertEqual(self.clip.read_bytes(),b'new unbacked source contents');self.assertTrue(target.exists())

    def test_same_byte_external_replacement_after_approval_is_not_adopted(self):
        job,target=self.copied();draft=self.service.archive.prepare(job)
        archived=self.service.archive.approve(job,draft['id'],draft['digest'])
        target.rename(target.with_suffix('.owned'));target.write_bytes(self.clip.read_bytes())
        self.assertEqual(self.service.run_once(archived)['state'],'failed')
        self.assertTrue(self.clip.exists())

    def test_change_after_staging_retains_source_and_can_be_recovered(self):
        job,target=self.copied();original=self.clip.read_bytes();draft=self.service.archive.prepare(job)
        archived=self.service.archive.approve(job,draft['id'],draft['digest'])
        def change(stage,*args):
            if stage=='staged':target.write_bytes(b'external changed target')
        self.service.move.checkpoint=change
        self.assertEqual(self.service.run_once(archived)['state'],'partial')
        self.assertTrue(any(r['kind']=='organize-reconcile' for r in self.app.LEASES.active()))
        self.service.move.checkpoint=lambda *args:None
        reverse=self.service.prepare_inverse(archived);inverse=self.service.approve_inverse(archived,reverse['digest'])
        self.assertEqual(self.service.run_once(inverse)['state'],'done')
        self.assertEqual(self.clip.read_bytes(),original)
        self.assertEqual(target.read_bytes(),b'external changed target')

    def test_archive_root_is_mandatory_exclusion_and_not_playable_default(self):
        job,target=self.copied();draft=self.service.archive.prepare(job)
        archived=self.service.archive.approve(job,draft['id'],draft['digest'])
        root=self.source/'.velvet-archive'
        self.assertIn(str(root),self.app.LIBRARIES.exclusions())
        with self.assertRaises(ValueError):self.app.LIBRARIES.exclude(str(root),False)
        self.assertEqual(self.service.run_once(archived)['state'],'done')
        with self.assertRaises(ValueError):self.app.LIBRARIES.save({'path':str(root)})
        with self.assertRaises(ValueError):self.inventory.register(str(root),'Synthetic archive')
        self.assertEqual(self.inventory.scan(self.device,hashing=True)['processed'],0)
        with self.app.db() as c:vid=c.execute('SELECT id FROM videos').fetchone()[0]
        location=next(row for row in self.service.locations.locations(vid) if row['archived'])
        with self.assertRaises(ValueError):self.service.locations.prefer(vid,location['id'])
        with self.assertRaises(ValueError):self.service.prepare_inverse(job)
        self.assertTrue(target.exists())

    def test_default_source_and_unsupported_or_unowned_archive_are_rejected(self):
        job,target=self.copied()
        with patch('organization_archive.volume_details',return_value={'filesystem':'exFAT','read_only':False}):
            with self.assertRaisesRegex(ValueError,'未确认支持'):self.service.archive.prepare(job)
            detail=self.service.detail(job)
            self.assertFalse(detail['archive_capability']['supported'])
            self.assertIn('未确认支持',detail['archive_capability']['reason'])
        with patch('organization_archive.volume_details',return_value={'filesystem':'NTFS','read_only':True}):
            with self.assertRaises(ValueError):self.service.archive.prepare(job)
        with self.app.db() as c:
            vid=c.execute('SELECT id FROM videos').fetchone()[0]
            source=c.execute('SELECT id FROM media_locations WHERE path=?',(str(self.clip),)).fetchone()[0]
        self.service.locations.prefer(vid,source)
        with self.assertRaisesRegex(ValueError,'默认播放'):self.service.archive.prepare(job)
        (self.source/'.velvet-archive').mkdir()
        with self.assertRaisesRegex(ValueError,'无应用归属'):self.service.archive.prepare(job)

    def test_pause_cancel_restart_and_restore_collision(self):
        from organization_service import OrganizationService
        job,target=self.copied();draft=self.service.archive.prepare(job)
        archived=self.service.archive.approve(job,draft['id'],draft['digest'])
        self.assertEqual(archived,self.service.archive.approve(job,draft['id'],draft['digest']))
        self.service.control(archived,'pause');self.assertEqual(self.service.run_once(archived)['state'],'waiting')
        self.service.control(archived,'cancel');self.assertTrue(self.clip.exists())
        self.service=OrganizationService(self.app.db,self.inventory);self.service.jobs.recover();self.service.control(archived,'resume')
        self.assertEqual(self.service.run_once(archived)['state'],'done')
        self.clip.write_bytes(b'external new source')
        with self.assertRaises(ValueError):self.service.prepare_inverse(archived)
        self.assertEqual(self.clip.read_bytes(),b'external new source');self.assertTrue(target.exists())

    def test_restart_after_archive_publication_finishes_binding_once(self):
        from organization_service import OrganizationService
        class Crash(BaseException):pass
        job,target=self.copied();draft=self.service.archive.prepare(job)
        archived=self.service.archive.approve(job,draft['id'],draft['digest'])
        def crash(stage,*args):
            if stage=='published':raise Crash()
        self.service.move.checkpoint=crash
        with self.assertRaises(Crash):self.service.run_once(archived)
        self.service=OrganizationService(self.app.db,self.inventory);self.service.jobs.recover();self.service.control(archived,'resume')
        self.assertEqual(self.service.run_once(archived)['state'],'done')
        with self.app.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM videos').fetchone()[0],1)
        self.assertEqual(target.read_bytes(),Path(draft['payload']['entries'][0]['destination']).read_bytes())

    def test_legacy_single_root_archive_draft_can_still_approve_and_restore(self):
        from organization_jobs import digest
        job,target=self.copied();draft=self.service.archive.prepare(job)
        payload=draft['payload'];payload.pop('source_roots');payload.pop('target_roots')
        for entry in payload['entries']:entry.pop('source_root');entry.pop('target_root')
        stamp=digest(payload)
        with self.app.db() as c:c.execute('UPDATE organization_drafts SET digest=?,payload=? WHERE id=?',(stamp,json.dumps(payload),draft['id']))
        archived=self.service.archive.approve(job,draft['id'],stamp)
        self.assertEqual(self.service.run_once(archived)['state'],'done')
        inverse=self.service.prepare_inverse(archived);undo=self.service.approve_inverse(archived,inverse['digest'])
        self.assertEqual(self.service.run_once(undo)['state'],'done');self.assertTrue(self.clip.exists());self.assertTrue(target.exists())


@unittest.skipUnless(os.name=='nt','Verified Windows archive capability required')
class ArchiveHTTPTests(unittest.TestCase):
    import test_organization_http as fixtures
    setUp=fixtures.OrganizationHTTPTests.setUp
    tearDown=fixtures.OrganizationHTTPTests.tearDown
    request=fixtures.OrganizationHTTPTests.request
    draft=fixtures.OrganizationHTTPTests.draft
    wait_job=fixtures.OrganizationHTTPTests.wait_job
    shutdown=ArchiveTests.shutdown

    def test_public_archive_and_reverse_include_subtitle_and_confirmation(self):
        draft=self.draft()
        status,result=self.request('/api/organization/approve',{'id':draft['id'],'digest':draft['digest'],'confirmed':True})
        self.assertEqual(status,200);original=result['id'];self.assertEqual(self.wait_job(original)['state'],'done')
        status,_=self.request('/api/organization/archive-preview',{'id':original},False);self.assertEqual(status,403)
        status,result=self.request('/api/organization/archive-preview',{'id':original});self.assertEqual(status,200,result)
        status,archive=self.request('/api/organization/draft?id='+result['id']+'&limit=1')
        self.assertEqual(status,200);self.assertEqual((archive['operation'],archive['archive_of'],archive['total']),('archive',original,2))
        status,_=self.request('/api/organization/archive-approve',{'id':original,'draft':archive['id'],'digest':archive['digest'],'confirmed':False});self.assertEqual(status,400)
        status,result=self.request('/api/organization/archive-approve',{'id':original,'draft':archive['id'],'digest':archive['digest'],'confirmed':True});self.assertEqual(status,200,result)
        archived=result['id'];self.assertEqual(self.wait_job(archived)['state'],'done')
        self.assertFalse(self.clip.exists());self.assertTrue((self.target/'part1.mp4').exists());self.assertTrue((self.target/'part1.en.srt').exists())
        status,result=self.request('/api/organization/undo-preview',{'id':archived});self.assertEqual(status,200,result)
        status,reverse=self.request('/api/organization/draft?id='+result['id']);self.assertEqual(status,200)
        status,_=self.request('/api/organization/approve',{'id':reverse['id'],'digest':reverse['digest'],'confirmed':True});self.assertEqual(status,400)
        status,result=self.request('/api/organization/undo-approve',{'id':archived,'digest':reverse['digest'],'confirmed':True});self.assertEqual(status,200,result)
        self.assertEqual(self.wait_job(result['id'])['state'],'done')
        self.assertTrue(self.clip.exists());self.assertTrue((self.source/'part1.en.srt').exists())
        with self.app.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM videos').fetchone()[0],1)
