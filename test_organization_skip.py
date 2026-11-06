"""Explicit group skip must never execute or falsely complete omitted files."""
import unittest
from pathlib import Path
from unittest.mock import patch
import test_organization_service as fixtures
from organization_service import OrganizationService


class SkipTests(unittest.TestCase):
    setUp=fixtures.ServiceTests.setUp
    tearDown=fixtures.ServiceTests.tearDown
    shutdown=fixtures.ServiceTests.shutdown

    def prepare_two(self,operation='copy'):
        (self.source/'other.mp4').write_bytes(b'independent synthetic video')
        (self.source/'part1.srt').write_text('synthetic subtitle',encoding='utf-8')
        self.inventory.scan(self.device,hashing=True)
        plan=self.inventory.plan(self.device,str(self.target),operation=operation)
        draft=self.service.prepare(plan['id'],plan['digest'],list(range(len(plan['entries']))),operation)
        job=self.service.approve(draft['id'],draft['digest'])
        self.service.control(job,'pause')
        entries=self.service.jobs.get(job)['payload']['entries']
        ordinal=next(i for i,e in enumerate(entries) if e['source']==str(self.clip))
        return job,ordinal,entries

    def test_skip_media_includes_companion_survives_restart_and_continues_other_copy(self):
        job,ordinal,entries=self.prepare_two();original=self.service.jobs.get(job)['approval_digest']
        preview=self.service.jobs.skip_preview(job,ordinal)
        self.assertEqual(len(preview['items']),2)
        with self.assertRaises(ValueError):self.service.jobs.skip(job,ordinal,'wrong')
        result=self.service.jobs.skip(job,ordinal,preview['digest']);self.assertEqual(result['skipped'],2)
        self.service=OrganizationService(self.app.db,self.inventory)
        self.service.jobs.recover();self.service.control(job,'resume');result=self.service.run_once(job)
        self.assertEqual(result['state'],'cancelled')
        self.assertEqual(self.service.jobs.get(job)['approval_digest'],original)
        for entry in entries:self.assertEqual(Path(entry['destination']).exists(),Path(entry['source']).name=='other.mp4')
        self.assertEqual((self.service.jobs.progress(job)['processed'],self.service.jobs.progress(job)['skipped']),(1,2))
        self.assertTrue(self.clip.exists());self.assertTrue((self.source/'part1.srt').exists())
        with self.assertRaises(ValueError):self.service.control(job,'resume')
        inverse=self.service.prepare_inverse(job)
        self.assertEqual(len(inverse['payload']['entries']),1)
        undo=self.service.approve_inverse(job,inverse['digest'])
        self.assertEqual(self.service.run_once(undo)['state'],'done')
        self.assertTrue(Path(next(e for e in entries if Path(e['source']).name=='other.mp4')['destination']).exists())

    def test_skip_before_move_does_not_move_or_bind_omitted_group_and_inverse_ignores_it(self):
        job,ordinal,entries=self.prepare_two('move');preview=self.service.jobs.skip_preview(job,ordinal)
        self.service.jobs.skip(job,ordinal,preview['digest']);self.service.control(job,'resume')
        self.assertEqual(self.service.run_once(job)['state'],'cancelled')
        self.assertTrue(self.clip.exists());self.assertTrue((self.source/'part1.srt').exists())
        inverse=self.service.prepare_inverse(job)
        self.assertEqual(len(inverse['payload']['entries']),1)
        undo=self.service.approve_inverse(job,inverse['digest'])
        self.assertEqual(self.service.run_once(undo)['state'],'done')
        self.assertTrue((self.source/'other.mp4').exists())

    def test_skip_cannot_hide_committed_binding_or_move_staging(self):
        for operation,state in (('copy','committed'),('copy','needs_binding'),('move','staged'),('keep','binding')):
            with self.subTest(operation=operation,state=state):
                job,ordinal,entries=self.prepare_two(operation)
                with self.app.db() as c:c.execute('UPDATE organization_steps SET state=? WHERE job_id=? AND ordinal=?',(state,job,ordinal))
                with self.assertRaises(ValueError):self.service.jobs.skip_preview(job,ordinal)

    def test_running_queued_stale_or_maintenance_cannot_be_skipped(self):
        job,ordinal,_=self.prepare_two();preview=self.service.jobs.skip_preview(job,ordinal)
        self.service.control(job,'resume')
        with self.assertRaises(ValueError):self.service.jobs.skip(job,ordinal,preview['digest'])
        self.service.control(job,'pause')
        with self.assertRaises(ValueError):self.service.jobs.skip(job,ordinal,preview['digest'])
        with self.app.db() as c:c.execute('INSERT INTO organization_maintenance VALUES(?)',(job,))
        with self.assertRaises(ValueError):self.service.jobs.skip_preview(job,ordinal)

    def test_keep_skip_does_not_create_a_card_or_modify_the_omitted_file(self):
        job,ordinal,_=self.prepare_two('keep');original=self.clip.read_bytes()
        preview=self.service.jobs.skip_preview(job,ordinal);self.service.jobs.skip(job,ordinal,preview['digest'])
        self.service.control(job,'resume');self.assertEqual(self.service.run_once(job)['state'],'cancelled')
        self.assertEqual(self.clip.read_bytes(),original)
        with self.app.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM videos').fetchone()[0],1)
            self.assertEqual(Path(c.execute('SELECT path FROM videos').fetchone()[0]).name,'other.mp4')

    def test_metadata_backup_restore_preserves_skip_decisions_without_reactivation(self):
        job,ordinal,_=self.prepare_two('keep')
        preview=self.service.jobs.skip_preview(job,ordinal);self.service.jobs.skip(job,ordinal,preview['digest'])
        self.service.control(job,'resume');self.service.run_once(job)
        backup=self.app.CORE.backup('synthetic-skip')
        with self.app.db() as c:
            c.execute("UPDATE organization_steps SET details='{}' WHERE job_id=? AND state='pending'",(job,))
        self.app.CORE.restore(backup)
        self.service=OrganizationService(self.app.db,self.inventory)
        self.assertEqual(self.service.jobs.progress(job)['skipped'],2)
        with self.assertRaises(ValueError):self.service.control(job,'resume')

    def test_skip_keeps_owned_copy_fragment_available_for_explicit_cleanup(self):
        self.skip_fragment('copying')

    def test_skipped_verified_copy_fragment_is_cleanable_without_reactivating_the_file(self):
        self.skip_fragment('verified')

    def skip_fragment(self,checkpoint):
        job,ordinal,entries=self.prepare_two();self.service.control(job,'resume')
        def interrupted(stage,*_):
            if stage==checkpoint:raise SystemExit('synthetic interruption')
        from organization_executor import CopyExecutor
        try:CopyExecutor(self.service.jobs,self.service.bind_copy,interrupted).run(job)
        except SystemExit:pass
        else:self.fail('interruption checkpoint missing')
        self.app.LEASES.recover();self.service.jobs.recover()
        step=next(s for s in self.service.jobs.get(job)['steps'] if s['state']==checkpoint)
        preview=self.service.jobs.skip_preview(job,step['ordinal']);self.service.jobs.skip(job,step['ordinal'],preview['digest'])
        plan=self.service.cleanup.preview(job)
        self.assertTrue(plan['entries'])
        self.service.cleanup.clean(job,plan['digest'])
        self.assertTrue(self.service.jobs.get(job)['steps'][step['ordinal']]['details'].get('skip'))

    def test_executor_rereads_a_skip_committed_between_initial_read_and_claim(self):
        job,ordinal,entries=self.prepare_two()
        with self.app.db() as c:
            c.execute("UPDATE organization_jobs SET state='failed' WHERE id=?",(job,))
            c.execute('DELETE FROM organization_control WHERE job_id=?',(job,))
        original=self.service.jobs.claim
        def claim(ident):
            preview=self.service.jobs.skip_preview(ident,ordinal)
            self.service.jobs.skip(ident,ordinal,preview['digest'])
            return original(ident)
        with patch.object(self.service.jobs,'claim',side_effect=claim):
            self.assertEqual(self.service.copy.run(job)['state'],'cancelled')
        for entry in entries:self.assertEqual(Path(entry['destination']).exists(),Path(entry['source']).name=='other.mp4')
