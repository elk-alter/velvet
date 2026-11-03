import hashlib
import json
import unittest
import test_file_review as fixtures
from storage_inventory import StorageInventory, safe_component
from task_journal import TaskJournal


class StorageTests(unittest.TestCase):
    setUp = fixtures.FileReviewTests.setUp
    db = fixtures.FileReviewTests.db

    def inventory(self):
        return StorageInventory(self.db, lambda p: {'title': p.parent.name, 'source': 'filename'}, lambda: [])

    def test_scan_hash_preview_and_original_integrity(self):
        source = self.root/'Media'; source.mkdir()
        clip = source/'Title'/'001.mp4'; clip.parent.mkdir(); clip.write_bytes(b'synthetic-video')
        subtitle = clip.with_suffix('.srt'); subtitle.write_text('synthetic subtitle')
        digest = hashlib.sha256(clip.read_bytes()).hexdigest()
        inventory = self.inventory(); device = inventory.register(str(source), 'Demo disk')
        result = inventory.scan(device, hashing=True)
        self.assertEqual(result['state'], 'done')
        rows = inventory.files(device)
        self.assertEqual(rows[0]['digest'], digest)
        self.assertFalse(rows[0]['companions'][0]['shared'])
        target = self.root/'Target'
        plan = inventory.plan(device, str(target))
        self.assertFalse(plan['executable'])
        self.assertEqual(len(plan['entries']), 1)
        self.assertFalse(target.exists())
        self.assertEqual(hashlib.sha256(clip.read_bytes()).hexdigest(), digest)
        self.assertEqual(inventory.plans(device)[0]['id'], plan['id'])
        inventory.scan(device, hashing=True)
        self.assertEqual(len(inventory.files(device)), 1)

    def test_exclusion_cancel_and_offline_snapshot(self):
        source = self.root/'Media'; source.mkdir()
        (source/'one.mp4').write_bytes(b'fixture')
        excluded = source/'Excluded'; excluded.mkdir(); (excluded/'hidden.mp4').write_bytes(b'hidden')
        inventory = self.inventory(); inventory.excluded = lambda: [str(excluded)]
        device = inventory.register(str(source), 'Demo')
        self.assertEqual(inventory.scan(device)['processed'], 1)
        cancelled = inventory.scan(device, cancel=lambda: True)
        self.assertEqual(cancelled['state'], 'cancelled')
        self.assertEqual(len(inventory.files(device)),1)
        offline = self.root/'Offline'; source.rename(offline)
        self.assertFalse(inventory.devices()[0]['online'])
        self.assertEqual(len(inventory.files(device)), 1)
        with self.assertRaises(ValueError):inventory.scan(device)

    def test_unsafe_targets_and_names(self):
        inventory = self.inventory(); source = self.root/'Media'; source.mkdir()
        device = inventory.register(str(source), 'Demo')
        for target in (source, source/'inside', self.root):
            with self.assertRaises(ValueError):inventory.plan(device, str(target))
        self.assertEqual(safe_component('CON'), '_CON')
        self.assertNotIn('/', safe_component('../escape'))

    def test_plan_revision_conflicts_space_and_source_changes(self):
        from unittest.mock import patch
        source=self.root/'Media';source.mkdir();clip=source/'Part01.mp4';clip.write_bytes(b'fixture')
        inv=self.inventory();device=inv.register(str(source),'Example');inv.scan(device,hashing=True)
        inv.annotate(device,'Part01.mp4',{'title':'Chosen','kind':'part','number':1})
        for layout in ('preserve','title','version','episode'):
            plan=inv.plan(device,str(self.root/'Target'),layout)
            self.assertEqual(len(plan['entries']),1)
            self.assertFalse((self.root/'Target').exists())
        with patch('storage_inventory.shutil.disk_usage',return_value=type('Space',(),{'free':0})()),patch('storage_inventory.volume_details',return_value={'filesystem':'FAT32','read_only':True}):
            insufficient=inv.plan(device,str(self.root/'Target'))
        self.assertFalse(insufficient['space']['sufficient'])
        self.assertIn('target_read_only',insufficient['entries'][0]['conflicts'])
        revised=inv.revise_plan(plan['id'],plan['digest'],[0],{'0':'CON/illegal?.mp4'})
        self.assertIn('invalid_component',revised['entries'][0]['conflicts'])
        for invalid in ('../outside.mp4',str(self.root/'Outside.mp4')):
            with self.assertRaises(ValueError):inv.revise_plan(plan['id'],plan['digest'],[0],{'0':invalid})
        with self.assertRaises(ValueError):inv.revise_plan(plan['id'],'stale',[0])
        clip.write_bytes(b'changed-source-content')
        changed=inv.plan(device,str(self.root/'Target'))
        self.assertFalse(changed['source_unchanged'])
        self.assertIn('source_changed',changed['entries'][0]['conflicts'])
        self.assertEqual(inv.files(device)[0]['metadata']['organization_annotation']['title'],'Chosen')

    def test_full_hash_rechecks_same_size_same_timestamp_and_failed_files(self):
        import os
        from unittest.mock import patch
        source=self.root/'Media';source.mkdir();clip=source/'one.mp4';clip.write_bytes(b'aaaa')
        inv=self.inventory();device=inv.register(str(source),'Example');inv.scan(device,hashing=True)
        previous=inv.files(device)[0];stamp=clip.stat();clip.write_bytes(b'bbbb');os.utime(clip,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
        inv.scan(device,hashing=True)
        self.assertNotEqual(inv.files(device)[0]['digest'],previous['digest'])
        with patch.object(inv,'metadata',side_effect=PermissionError('synthetic failure')):result=inv.scan(device)
        self.assertEqual(result['state'],'partial');self.assertEqual(result['failed'],1)
        self.assertEqual(inv.errors(device)[0]['kind'],'PermissionError')
        self.assertEqual(clip.read_bytes(),b'bbbb')

    def test_remap_requires_identity_confirmation_and_preserves_snapshot(self):
        from unittest.mock import patch
        source=self.root/'Media';source.mkdir();(source/'one.mp4').write_bytes(b'fixture')
        inv=self.inventory();device=inv.register(str(source),'Example');inv.scan(device,hashing=True)
        old=inv.device(device);new=self.root/'NewMount';source.rename(new)
        with self.assertRaises(ValueError):inv.remap(device,str(new),'not-confirmed')
        if old['stable']:
            with patch('storage_inventory.volume_identity',return_value=('different-volume',True)):
                with self.assertRaises(ValueError):inv.remap(device,str(new),old['identity'])
        inv.remap(device,str(new),old['identity'])
        self.assertTrue(inv.devices()[0]['online'])
        self.assertEqual(len(inv.files(device)),1)
        self.assertTrue(inv.plan(device,str(self.root/'Target'))['source_unchanged'])

    def test_legacy_inventory_key_migrates_idempotently(self):
        with self.db() as c:
            c.execute('CREATE TABLE inventory_files(device_id TEXT,relative_path TEXT,size INTEGER,mtime_ns INTEGER,digest TEXT,metadata TEXT,companions TEXT,snapshot TEXT,PRIMARY KEY(device_id,relative_path))')
            c.execute("INSERT INTO inventory_files VALUES('device','one.mp4',1,1,'digest','{}','[]','snapshot')")
        inv=self.inventory();inv.ensure()
        with self.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM inventory_files').fetchone()[0],1)
            self.assertEqual([r[1] for r in c.execute('PRAGMA table_info(inventory_files)') if r[5]],['device_id','relative_path','snapshot'])


class TaskJournalTests(unittest.TestCase):
    setUp = fixtures.FileReviewTests.setUp
    db = fixtures.FileReviewTests.db

    def test_identity_idempotence_recovery_and_privacy(self):
        journal = TaskJournal(self.db)
        first = journal.observe('scan', 'fixture', 'running', {'processed': 1, 'path': '/private', 'token': 'secret'})
        self.assertEqual(first, journal.observe('scan', 'fixture', 'running', {'processed': 1}))
        with self.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM task_events').fetchone()[0], 1)
        journal.recover()
        row = journal.list()[0]
        self.assertEqual(row['id'], first)
        self.assertEqual(row['state'], 'interrupted')
        self.assertNotIn('private', json.dumps(journal.list()))
        self.assertNotIn('secret', json.dumps(journal.list()))
        with self.assertRaises(ValueError):journal.observe('arbitrary', 'fixture', 'running')

    def test_executor_reconciliation_retains_stable_identity(self):
        journal = TaskJournal(self.db)
        with self.db() as c:
            c.execute('CREATE TABLE scrape_jobs(id INTEGER PRIMARY KEY,status TEXT)')
            c.execute("INSERT INTO scrape_jobs VALUES(1,'queued')")
        journal.reconcile_sources(); first = journal.list()[0]['id']
        with self.db() as c:c.execute("UPDATE scrape_jobs SET status='done'")
        journal.reconcile_sources()
        self.assertEqual(journal.list()[0]['id'], first)
        self.assertEqual(journal.list()[0]['state'], 'done')
