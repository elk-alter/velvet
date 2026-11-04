"""Physical journal reconciliation must precede destructive database rollback."""
import unittest
import json
import sqlite3
import os
from pathlib import Path
import test_organization_service as fixtures


class RestoreReconciliationTests(unittest.TestCase):
    setUp=fixtures.ServiceTests.setUp
    tearDown=fixtures.ServiceTests.tearDown
    shutdown=fixtures.ServiceTests.shutdown

    def execute(self,operation='copy'):
        plan=self.inventory.plan(self.device,str(self.target))
        draft=self.service.prepare(plan['id'],plan['digest'],[0],operation)
        job=self.service.approve(draft['id'],draft['digest'])
        self.assertEqual(self.service.run_once(job)['state'],'done')
        return job,Path(draft['payload']['entries'][0]['destination'])

    def test_copy_rollback_retains_files_and_private_journal_receipt(self):
        backup=self.app.CORE.backup('before-copy')
        job,target=self.execute()
        before={p:(p.read_bytes(),p.stat().st_mtime_ns) for p in (self.clip,target)}
        with self.assertRaises(ValueError):self.app.CORE.restore(backup)
        report=self.app.CORE.preview_restore(backup)
        self.assertTrue(report['ready'],report['problems'])
        self.assertEqual([p['path'] for p in report['retained_files']],[str(target)])
        rollback=self.app.CORE.restore(backup,report)
        self.assertTrue((self.app.DATA/'backups'/rollback).is_file())
        with self.app.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM videos').fetchone()[0],0)
        self.assertEqual(before,{p:(p.read_bytes(),p.stat().st_mtime_ns) for p in before})
        receipt=json.loads(next((self.app.DATA/'restore-audits').glob('*.json')).read_text('utf-8'))
        self.assertIn(job,receipt['journals']['current']['jobs'])

    def test_move_requires_physical_inverse_before_old_database(self):
        backup=self.app.CORE.backup('before-move')
        before=(self.clip.read_bytes(),self.clip.stat().st_mtime_ns)
        job,target=self.execute('move')
        blocked=self.app.CORE.preview_restore(backup)
        self.assertFalse(blocked['ready'])
        self.assertIn('file_position_differs_from_backup',{p['code'] for p in blocked['problems']})
        with self.assertRaises(ValueError):self.app.CORE.restore(backup,blocked)
        reverse=self.service.move.prepare_undo(job)
        undo=self.service.move.approve_undo(job,reverse['digest'])
        self.assertEqual(self.service.run_once(undo)['state'],'done')
        with self.assertRaises(ValueError):self.app.CORE.restore(backup,blocked)
        report=self.app.CORE.preview_restore(backup)
        self.assertTrue(report['ready'],report['problems'])
        self.app.CORE.restore(backup,report)
        self.assertFalse(target.exists())
        self.assertEqual(before,(self.clip.read_bytes(),self.clip.stat().st_mtime_ns))

    def test_post_move_snapshot_does_not_require_unnecessary_inverse(self):
        job,target=self.execute('move')
        backup=self.app.CORE.backup('after-move')
        report=self.app.CORE.preview_restore(backup)
        self.assertTrue(report['ready'],report['problems'])
        self.app.CORE.restore(backup,report)
        self.assertTrue(target.exists());self.assertFalse(self.clip.exists())
        with self.app.db() as c:self.assertEqual(c.execute('SELECT state FROM organization_jobs WHERE id=?',(job,)).fetchone()[0],'done')

    def test_changed_database_and_replaced_target_invalidate_approval(self):
        backup=self.app.CORE.backup('before-copy')
        job,target=self.execute()
        report=self.app.CORE.preview_restore(backup)
        with self.app.db() as c:c.execute('UPDATE videos SET favorite=1')
        with self.assertRaisesRegex(ValueError,'变化'):self.app.CORE.restore(backup,report)
        report=self.app.CORE.preview_restore(backup)
        displaced=target.with_suffix('.old');target.rename(displaced);target.write_bytes(displaced.read_bytes())
        with self.assertRaisesRegex(ValueError,'未解决'):self.app.CORE.restore(backup,report)
        self.assertEqual(target.read_bytes(),self.clip.read_bytes())
        self.assertFalse((self.app.DATA/'restore-audits').exists())

    def test_semantic_signature_handles_unknown_blob_without_rowid(self):
        from restore_reconciliation import semantic_signature
        with sqlite3.connect(':memory:') as c:
            c.execute('CREATE TABLE extra(id TEXT PRIMARY KEY, data BLOB) WITHOUT ROWID')
            c.execute('INSERT INTO extra VALUES(?,?)',('x',b'\x00\xff'))
            first=semantic_signature(c)
            c.execute('CREATE TABLE resource_claims(id INTEGER)')
            c.execute('INSERT INTO resource_claims VALUES(1)')
            self.assertEqual(first,semantic_signature(c))
            c.execute('UPDATE extra SET data=?',(b'new',))
            self.assertNotEqual(first,semantic_signature(c))

    def test_offline_full_restore_preserves_receipts_and_physical_copies(self):
        from full_backup import create,preview_restore,restore
        archive=self.root/'before.zip';create(self.app.DATA,archive)
        job,target=self.execute()
        original=target.read_bytes()
        with self.assertRaises(ValueError):restore(self.app.DATA,archive)
        report=preview_restore(self.app.DATA,archive)
        self.assertTrue(report['ready'],report['problems'])
        result=restore(self.app.DATA,archive,report)
        self.assertTrue((Path(result['rollback'])/'library.db').is_file())
        self.assertEqual(target.read_bytes(),original)
        receipt=next((self.app.DATA/'restore-audits').glob('*.json'))
        self.assertIn(job,json.loads(receipt.read_text())['journals']['current']['jobs'])
        second=self.root/'with-receipt.zip';create(self.app.DATA,second)
        import zipfile
        with zipfile.ZipFile(second) as z:self.assertIn('restore-audits/'+receipt.name,z.namelist())
        # Restoring an older snapshot still carries the local reconciliation trail.
        restore(self.app.DATA,archive)
        self.assertTrue(receipt.is_file())

    def test_full_restore_rejects_modified_archive_after_preview(self):
        from full_backup import create,preview_restore,restore
        archive=self.root/'before.zip';create(self.app.DATA,archive)
        self.execute()
        report=preview_restore(self.app.DATA,archive)
        # A valid replacement archive with different bytes is still a new review.
        other=self.root/'other.zip';create(self.app.DATA,other)
        archive.write_bytes(other.read_bytes())
        with self.assertRaises(ValueError):restore(self.app.DATA,archive,report)
        self.assertFalse(list(self.root.glob('data.before-*')))

    def test_move_checkpoint_histories_require_inverse_and_reconcile_without_media_write(self):
        for checkpoint in ('staging','staged','committing','published','committed','bound'):
            with self.subTest(checkpoint=checkpoint):
                backup=self.app.CORE.backup('before-checkpoint')
                original=(self.clip.read_bytes(),self.clip.stat().st_mtime_ns)
                plan=self.inventory.plan(self.device,str(self.target/checkpoint))
                draft=self.service.prepare(plan['id'],plan['digest'],[0],'move')
                job=self.service.approve(draft['id'],draft['digest'])
                def crash(stage,*args):
                    if stage==checkpoint:raise SystemExit('synthetic checkpoint')
                self.service.move.checkpoint=crash
                with self.assertRaises(SystemExit):self.service.run_once(job)
                self.service.move.checkpoint=lambda *args:None
                self.service.jobs.recover()
                report=self.app.CORE.preview_restore(backup)
                self.assertFalse(report['ready'])
                with self.assertRaises(ValueError):self.app.CORE.restore(backup,report)
                inverse=self.service.move.prepare_undo(job)
                undo=self.service.move.approve_undo(job,inverse['digest'])
                self.assertEqual(self.service.run_once(undo)['state'],'done')
                report=self.app.CORE.preview_restore(backup)
                self.assertTrue(report['ready'],report['problems'])
                self.assertEqual(original,(self.clip.read_bytes(),self.clip.stat().st_mtime_ns))
                # Audit itself is read-only; keep every journal to exercise chains.

    def test_keep_active_or_tampered_history_cannot_bypass_restore_guard(self):
        backup=self.app.CORE.backup('before-keep')
        plan=self.inventory.plan(self.device,'',operation='keep')
        draft=self.service.prepare(plan['id'],plan['digest'],[0],'keep')
        job=self.service.approve(draft['id'],draft['digest'])
        with self.assertRaises(ValueError):self.app.CORE.restore(backup)
        self.assertEqual(self.service.run_once(job)['state'],'done')
        with self.app.db() as c:c.execute("UPDATE organization_jobs SET state='future-state' WHERE id=?",(job,))
        with self.assertRaises(ValueError):self.app.CORE.restore(backup)
        with self.assertRaises(ValueError):self.app.CORE.preview_restore(backup)
        with self.app.db() as c:c.execute("UPDATE organization_jobs SET state='done' WHERE id=?",(job,))
        with self.app.db() as c:c.execute("UPDATE organization_jobs SET approval_digest='changed' WHERE id=?",(job,))
        with self.assertRaises(ValueError):self.app.CORE.restore(backup)
        with self.assertRaises(ValueError):self.app.CORE.preview_restore(backup)

    @unittest.skipUnless(os.name=='nt','Source archival requires verified Windows NTFS/ReFS')
    def test_archive_must_be_reversed_but_copy_can_remain(self):
        backup=self.app.CORE.backup('before-archive')
        copy,target=self.execute()
        draft=self.service.archive.prepare(copy)
        archive=self.service.archive.approve(copy,draft['id'],draft['digest'])
        self.assertEqual(self.service.run_once(archive)['state'],'done')
        self.assertFalse(self.app.CORE.preview_restore(backup)['ready'])
        reverse=self.service.prepare_inverse(archive)
        undo=self.service.approve_inverse(archive,reverse['digest'])
        self.assertEqual(self.service.run_once(undo)['state'],'done')
        report=self.app.CORE.preview_restore(backup)
        self.assertTrue(report['ready'],report['problems'])
        self.app.CORE.restore(backup,report)
        self.assertEqual(target.read_bytes(),self.clip.read_bytes())

    def test_completed_keep_can_restore_metadata_without_changing_media(self):
        backup=self.app.CORE.backup('synthetic-before-keep')
        before=(self.clip.read_bytes(),self.clip.stat().st_mtime_ns)
        plan=self.inventory.plan(self.device,'',operation='keep')
        draft=self.service.prepare(plan['id'],plan['digest'],[0],'keep')
        job=self.service.approve(draft['id'],draft['digest'])
        self.assertEqual(self.service.run_once(job)['state'],'done')
        self.app.CORE.restore(backup)
        with self.app.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM videos').fetchone()[0],0)
        self.assertEqual((self.clip.read_bytes(),self.clip.stat().st_mtime_ns),before)
