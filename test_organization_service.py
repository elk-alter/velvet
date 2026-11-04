import json
import threading
import time
import unittest
from pathlib import Path
import test_features as fixtures
from storage_inventory import StorageInventory
from organization_service import OrganizationService
from organization_executor import CopyExecutor


class ServiceTests(unittest.TestCase):
    add=fixtures.FeatureTests.add

    def setUp(self):
        fixtures.FeatureTests.setUp(self);self.root=self.root.resolve()
        self.source=self.root/'source';self.source.mkdir();self.target=self.root/'target'
        self.clip=self.source/'part1.mp4';self.clip.write_bytes(b'synthetic raw media')
        self.inventory=StorageInventory(self.app.db,lambda path:{'title':'Synthetic title','source':'filename'},lambda:[])
        self.device=self.inventory.register(str(self.source),'Synthetic disk')
        self.inventory.scan(self.device,hashing=True)
        self.service=OrganizationService(self.app.db,self.inventory)

    def tearDown(self):
        try:self.shutdown()
        finally:fixtures.FeatureTests.tearDown(self)

    def shutdown(self):
        self.service.stop()
        if self.service.thread:self.service.thread.join(5)
        self.assertFalse(self.service.thread and self.service.thread.is_alive())

    def prepare(self,operation='copy'):
        plan=self.inventory.plan(self.device,str(self.target))
        return self.service.prepare(plan['id'],plan['digest'],[0],operation)

    def approve(self,draft):return self.service.approve(draft['id'],draft['digest'])

    def test_raw_preview_creates_no_card_and_copy_materializes_one_identity(self):
        draft=self.prepare();asset=draft['payload']['entries'][0]['asset_id']
        with self.app.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM videos').fetchone()[0],0)
        self.assertFalse(self.target.exists())
        job=self.approve(draft);self.assertEqual(job,self.approve(draft))
        self.assertEqual(self.service.run_once(job)['state'],'done')
        with self.app.db() as c:
            row=dict(c.execute('SELECT * FROM videos').fetchone())
            self.assertEqual((row['title'],row['missing']),('Synthetic title',0))
            self.assertEqual(c.execute('SELECT COUNT(*) FROM videos').fetchone()[0],1)
        self.assertEqual(self.service.locations.asset(row['id'])['id'],asset)
        self.assertEqual(len(self.service.locations.locations(row['id'])),2)
        self.assertEqual(self.clip.read_bytes(),b'synthetic raw media')
        self.service.run_once(job)
        self.assertEqual(len(self.service.listing()['items']),1)

    def test_raw_move_and_approved_inverse_keep_same_identity(self):
        draft=self.prepare('move');job=self.approve(draft)
        self.assertEqual(self.service.run_once(job)['state'],'done');self.assertFalse(self.clip.exists())
        asset=draft['payload']['entries'][0]['asset_id']
        reverse=self.service.move.prepare_undo(job);undo=self.service.move.approve_undo(job,reverse['digest'])
        self.assertEqual(self.service.run_once(undo)['state'],'done')
        self.assertEqual(self.clip.read_bytes(),b'synthetic raw media')
        with self.app.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM videos').fetchone()[0],1)
        self.assertEqual(self.service.locations.asset(self.service.locations.materialize(asset))['id'],asset)

    def test_committed_copy_can_bind_after_original_becomes_unavailable(self):
        draft=self.prepare();job=self.approve(draft)
        def lose_source(stage,*args):
            if stage=='committed':self.clip.rename(self.source/'offline.fixture')
        self.service.copy=CopyExecutor(self.service.jobs,self.service.bind_copy,lose_source)
        self.assertEqual(self.service.run_once(job)['state'],'done')
        with self.app.db() as c:row=dict(c.execute('SELECT * FROM videos').fetchone())
        self.assertEqual(Path(row['path']).read_bytes(),b'synthetic raw media');self.assertFalse(row['missing'])

    def test_indexed_file_keeps_locked_metadata_preferences_and_identity(self):
        with self.app.db() as c:
            vid=c.execute('INSERT INTO videos(path,title,size,mtime,favorite,position,locked_fields) VALUES(?,?,?,?,?,?,?)',(str(self.clip),'Manual',self.clip.stat().st_size,self.clip.stat().st_mtime,1,42,'["title"]')).lastrowid
        draft=self.prepare();job=self.approve(draft)
        self.assertEqual(self.service.run_once(job)['state'],'done')
        with self.app.db() as c:
            row=c.execute('SELECT * FROM videos WHERE id=?',(vid,)).fetchone()
            self.assertEqual((row['title'],row['favorite'],row['position'],row['locked_fields']),('Manual',1,42,'["title"]'))
            self.assertEqual(c.execute('SELECT COUNT(*) FROM videos').fetchone()[0],1)

    def test_worker_waits_for_reader_and_processes_persisted_approved_job(self):
        draft=self.prepare();job=self.approve(draft)
        owner=self.app.LEASES.try_acquire('scan')
        try:
            self.assertEqual(self.service.run_once(job)['state'],'waiting');self.assertFalse(self.target.exists())
            self.service.start()
        finally:self.app.LEASES.release(owner)
        deadline=time.monotonic()+5
        while self.service.jobs.get(job)['state']!='done' and time.monotonic()<deadline:time.sleep(.02)
        self.assertEqual(self.service.jobs.get(job)['state'],'done')
        self.assertEqual(self.service.detail(job,0,1)['total'],1)

    def test_queued_pause_and_cancel_have_no_file_side_effects(self):
        draft=self.prepare();job=self.approve(draft)
        self.assertEqual(self.service.control(job,'pause')['state'],'waiting')
        self.assertEqual(self.service.run_once(job)['state'],'waiting')
        self.assertFalse(self.target.exists())
        self.assertEqual(self.service.control(job,'cancel')['state'],'cancelled')
        self.service.control(job,'resume')
        self.assertEqual(self.service.run_once(job)['state'],'done')

    def test_stale_inventory_or_wrong_digest_cannot_be_approved_for_execution(self):
        plan=self.inventory.plan(self.device,str(self.target))
        with self.assertRaises(ValueError):self.service.prepare(plan['id'],'wrong',[0])
        self.clip.write_bytes(b'changed after inventory')
        with self.assertRaises(ValueError):self.service.prepare(plan['id'],plan['digest'],[0])
        self.assertFalse(self.target.exists())

    def test_plan_metadata_is_frozen_before_a_later_annotation(self):
        self.inventory.annotate(self.device,'part1.mp4',{'title':'First title'})
        first=self.prepare()
        self.inventory.annotate(self.device,'part1.mp4',{'title':'Later title'})
        second=self.prepare()
        self.assertNotEqual(first['digest'],second['digest'])
        job=self.approve(first);self.assertEqual(self.service.run_once(job)['state'],'done')
        with self.app.db() as c:row=c.execute('SELECT title,locked_fields FROM videos').fetchone()
        self.assertEqual((row['title'],row['locked_fields']),('First title','["title"]'))

    def test_secondary_location_move_and_undo_do_not_change_default_playback(self):
        first=self.prepare();job=self.approve(first)
        self.assertEqual(self.service.run_once(job)['state'],'done')
        with self.app.db() as c:default_path=c.execute('SELECT path FROM videos').fetchone()[0]
        self.target=self.root/'secondary-target'
        draft=self.prepare('move');move=self.approve(draft)
        self.assertEqual(self.service.run_once(move)['state'],'done')
        with self.app.db() as c:self.assertEqual(c.execute('SELECT path FROM videos').fetchone()[0],default_path)
        reverse=self.service.move.prepare_undo(move);undo=self.service.move.approve_undo(move,reverse['digest'])
        self.assertEqual(self.service.run_once(undo)['state'],'done')
        self.assertTrue(self.clip.exists())
        with self.app.db() as c:
            self.assertEqual(c.execute('SELECT path FROM videos').fetchone()[0],default_path)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM videos').fetchone()[0],1)

    def test_later_corrected_raw_plan_applies_its_own_frozen_metadata(self):
        self.prepare()
        self.inventory.annotate(self.device,'part1.mp4',{'title':'Corrected title'})
        corrected=self.prepare();job=self.approve(corrected)
        self.assertEqual(self.service.run_once(job)['state'],'done')
        with self.app.db() as c:self.assertEqual(c.execute('SELECT title FROM videos').fetchone()[0],'Corrected title')

    def test_drain_before_dispatch_preserves_approved_plan_without_moving_source(self):
        draft=self.prepare();job=self.approve(draft)
        self.service.stop()
        self.assertEqual(self.service.run_once(job)['state'],'waiting')
        self.assertFalse(self.target.exists());self.assertTrue(self.clip.exists())
        self.assertTrue(self.service.jobs.pause_requested(job))

    def test_failed_binding_has_no_premature_raw_card_and_can_retry(self):
        draft=self.prepare();job=self.approve(draft)
        self.service.copy=CopyExecutor(self.service.jobs,lambda *args:None)
        self.assertEqual(self.service.run_once(job)['state'],'partial')
        with self.app.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM videos').fetchone()[0],0)
        self.service.copy=CopyExecutor(self.service.jobs,self.service.bind_copy)
        self.service.control(job,'resume')
        self.assertEqual(self.service.run_once(job)['state'],'done')
        with self.app.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM videos').fetchone()[0],1)

    def test_same_content_external_replacement_is_not_adopted_during_binding(self):
        draft=self.prepare();job=self.approve(draft)
        def replace_then_bind(entry,task):
            path=Path(entry['destination']);path.rename(path.with_suffix('.owned'))
            path.write_bytes(b'synthetic raw media')
            return self.service.bind_copy(entry,task)
        self.service.copy=CopyExecutor(self.service.jobs,replace_then_bind)
        result=self.service.run_once(job)
        self.assertEqual(result['state'],'partial');self.assertEqual(result['steps'][0]['state'],'needs_binding')
        with self.app.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM videos').fetchone()[0],0)

    def test_stored_preview_tampering_cannot_reuse_old_digest(self):
        plan=self.inventory.plan(self.device,str(self.target))
        with self.app.db() as c:
            payload=json.loads(c.execute('SELECT payload FROM organization_plans WHERE id=?',(plan['id'],)).fetchone()[0])
            payload['entries'][0]['destination']=str(self.target/'changed.mp4')
            c.execute('UPDATE organization_plans SET payload=? WHERE id=?',(json.dumps(payload),plan['id']))
        with self.assertRaises(ValueError):self.service.prepare(plan['id'],plan['digest'],[0])
        self.assertFalse(self.target.exists())

    def test_direct_index_restore_cannot_bypass_file_history_guard(self):
        backup=self.app.CORE.backup()
        draft=self.prepare();job=self.approve(draft)
        self.assertEqual(self.service.run_once(job)['state'],'done')
        with self.assertRaises(ValueError):self.app.CORE.restore(backup)
        with self.app.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM videos').fetchone()[0],1)
        self.assertTrue(self.clip.exists())
