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

    def test_verified_copy_survives_native_root_rename_and_undo_uses_reconnected_source(self):
        from organization_jobs import root_proof

        if not root_proof(self.source):
            self.skipTest('Requires native stable volume and directory proof')
        self.target.mkdir()
        draft=self.prepare()
        job=self.approve(draft)

        def interrupt(phase,*args):
            if phase=='verified':raise SystemExit('synthetic verified checkpoint')

        self.service.copy.checkpoint=interrupt
        with self.assertRaises(SystemExit):self.service.run_once(job)
        self.service.jobs.recover()
        original=self.service.jobs.get(job)
        proof=original['steps'][0]['details']['file_identity']
        before_reconnect=self.app.CORE.backup('before-reconnect')
        source=self.root/'source-reconnected'
        target=self.root/'target-reconnected'
        self.source.rename(source)
        self.target.rename(target)
        wrong=self.root/'wrong-directory'
        wrong.mkdir()
        (wrong/self.clip.name).write_bytes((source/self.clip.name).read_bytes())
        paths={str(self.source):str(source),str(self.target):str(target)}
        with self.assertRaisesRegex(ValueError,'目录身份'):
            self.service.jobs.recovery_map_preview(job,{**paths,str(self.source):str(wrong)})
        preview=self.service.jobs.recovery_map_preview(job,paths)
        self.service.jobs.confirm_recovery_map(job,paths,preview['digest'])
        self.service.control(job,'resume')
        self.service.copy.checkpoint=lambda *args:None
        bind=self.service.copy.binder

        def interrupt_after_binding(*args):
            bind(*args)
            raise SystemExit('synthetic interruption after DB binding')

        self.service.copy.binder=interrupt_after_binding
        with self.assertRaises(SystemExit):self.service.run_once(job)
        self.service.jobs.recover()
        self.service.control(job,'resume')
        self.service.copy.binder=bind
        result=self.service.run_once(job)
        self.assertEqual(result['state'],'done')
        self.assertEqual(result['payload'],original['payload'])
        self.assertEqual(result['approval_digest'],original['approval_digest'])
        actual=self.service.jobs.effective_job(job)['payload']['entries'][0]
        copied=Path(actual['destination'])
        self.assertEqual([copied.stat().st_dev,copied.stat().st_ino],proof)
        self.assertEqual(copied.read_bytes(),(source/self.clip.name).read_bytes())
        self.assertFalse(self.target.exists())
        with self.app.db() as c:
            locations=c.execute('SELECT path FROM media_locations ORDER BY path').fetchall()
        self.assertCountEqual([row[0] for row in locations],
                              [str(self.clip),str(source/self.clip.name),str(copied)])

        with self.assertRaisesRegex(ValueError,'重连记录不同'):
            self.app.CORE.preview_restore(before_reconnect)
        saved=self.app.CORE.backup('reconnected-copy')
        audit=self.app.CORE.preview_restore(saved)
        self.assertTrue(audit['ready'],audit['problems'])
        self.app.CORE.restore(saved,{key:audit[key] for key in ('id','digest')})
        self.assertEqual([copied.stat().st_dev,copied.stat().st_ino],proof)
        reverse=self.service.prepare_inverse(job)
        undo=self.service.approve_inverse(job,reverse['digest'])
        self.assertEqual(self.service.run_once(undo)['state'],'done')
        with self.app.db() as c:
            paths=[row[0] for row in c.execute('SELECT path FROM videos')]
        self.assertEqual(paths,[str(source/self.clip.name)])
        self.assertTrue(copied.is_file())

    def test_completed_copy_target_reconnect_preserves_binding_and_undo(self):
        from organization_jobs import root_proof

        if not root_proof(self.source):
            self.skipTest('Requires native stable volume and directory proof')
        self.target.mkdir()
        job=self.approve(self.prepare())
        original=self.service.run_once(job)
        self.assertEqual(original['state'],'done')
        binding_id=original['steps'][0]['details']['binding_id']
        old=Path(original['payload']['entries'][0]['destination'])
        target=self.root/'renamed-completed-target'
        self.target.rename(target)
        new=target/old.relative_to(self.target)
        replacements={str(self.target):str(target)}
        preview=self.service.jobs.recovery_map_preview(job,replacements)
        self.assertEqual(preview['binding_count'],1)
        with self.app.db() as c:
            self.assertEqual(c.execute('SELECT path FROM videos').fetchone()[0],str(old))
        from resource_leases import ResourceBusy
        asset=original['payload']['entries'][0]['asset_id']
        owner=self.service.jobs.leases.try_acquire('synthetic-active-reader',resources=['asset:'+asset])
        self.assertIsNotNone(owner)
        try:
            with self.assertRaises(ResourceBusy):
                self.service.jobs.confirm_recovery_map(job,replacements,preview['digest'])
        finally:
            self.service.jobs.leases.release(owner)
        self.service.jobs.confirm_recovery_map(job,replacements,preview['digest'])
        updated=self.service.jobs.effective_job(job)
        self.assertEqual(updated['payload']['entries'][0]['destination'],str(new))
        self.assertEqual(updated['steps'][0]['details']['binding_id'],binding_id)
        self.assertEqual(self.service.jobs.get(job)['payload'],original['payload'])
        with self.app.db() as c:
            self.assertEqual(c.execute('SELECT path FROM videos').fetchone()[0],str(new))
        latest=self.root/'second-completed-target'
        target.rename(latest)
        replacements[str(self.target)]=str(latest)
        preview=self.service.jobs.recovery_map_preview(job,replacements)
        self.service.jobs.confirm_recovery_map(job,replacements,preview['digest'])
        new=latest/new.relative_to(target)
        with self.app.db() as c:
            self.assertEqual(c.execute('SELECT path FROM videos').fetchone()[0],str(new))
            self.assertEqual(c.execute('SELECT path FROM media_locations WHERE id=?',(binding_id,)).fetchone()[0],str(new))
        audit=self.app.CORE.preview_restore(self.app.CORE.backup('completed-target-reconnect'))
        self.assertTrue(audit['ready'],audit['problems'])
        inverse=self.service.prepare_inverse(job)
        undo=self.service.approve_inverse(job,inverse['digest'])
        self.assertEqual(self.service.run_once(undo)['state'],'done')
        with self.app.db() as c:
            self.assertEqual(c.execute('SELECT path FROM videos').fetchone()[0],str(self.clip))
        self.assertEqual(new.read_bytes(),self.clip.read_bytes())

    def test_completed_move_target_reconnect_retains_native_file_identity(self):
        from organization_jobs import root_proof

        if not root_proof(self.source):
            self.skipTest('Requires native stable volume and directory proof')
        self.target.mkdir()
        job=self.approve(self.prepare('move'))
        original=self.service.run_once(job)
        self.assertEqual(original['state'],'done')
        old=Path(original['payload']['entries'][0]['destination'])
        target=self.root/'relocated-completed-move'
        self.target.rename(target)
        new=target/old.relative_to(self.target)
        identity=new.stat().st_ino
        displaced=new.with_suffix('.held')
        new.rename(displaced)
        new.write_bytes(displaced.read_bytes())
        replacements={str(self.target):str(target)}
        with self.assertRaisesRegex(ValueError,'文件身份'):
            self.service.jobs.recovery_map_preview(job,replacements)
        new.unlink()
        displaced.rename(new)
        preview=self.service.jobs.recovery_map_preview(job,replacements)
        self.service.jobs.confirm_recovery_map(job,replacements,preview['digest'])
        with self.app.db() as c:
            self.assertEqual(c.execute('SELECT path FROM videos').fetchone()[0],str(new))
        inverse=self.service.prepare_inverse(job)
        undo=self.service.approve_inverse(job,inverse['digest'])
        self.assertEqual(self.service.run_once(undo)['state'],'done')
        self.assertEqual(self.clip.stat().st_ino,identity)
        self.assertFalse(new.exists())

    def test_partial_copy_remaps_bound_file_and_reuses_verified_checkpoint(self):
        from organization_jobs import root_proof

        if not root_proof(self.source):
            self.skipTest('Requires native stable volume and directory proof')
        self.target.mkdir()
        second=self.source/'part2.mp4'
        second.write_bytes(b'second synthetic original')
        self.inventory.scan(self.device,hashing=True)
        plan=self.inventory.plan(self.device,str(self.target),layout='preserve')
        job=self.approve(self.service.prepare(plan['id'],plan['digest'],[0,1],'copy'))

        def interrupt(phase,ident,ordinal):
            if phase=='verified' and ordinal==1:
                raise SystemExit('synthetic interruption with mixed checkpoints')

        self.service.copy.checkpoint=interrupt
        with self.assertRaises(SystemExit):self.service.run_once(job)
        self.service.jobs.recover()
        original=self.service.jobs.get(job)
        self.assertEqual([step['state'] for step in original['steps']],['done','verified'])
        binding_id=original['steps'][0]['details']['binding_id']
        fragment=original['steps'][1]['details']['file_identity']
        target=self.root/'partial-copy-reconnected'
        self.target.rename(target)
        replacements={str(self.target):str(target)}
        preview=self.service.jobs.recovery_map_preview(job,replacements)
        self.assertEqual(preview['binding_count'],1)
        self.service.jobs.confirm_recovery_map(job,replacements,preview['digest'])
        with self.app.db() as c:
            self.assertEqual(c.execute('SELECT path FROM media_locations WHERE id=?',(binding_id,)).fetchone()[0],
                             str(target/self.clip.name))
        self.assertFalse((target/second.name).exists())

        self.service.copy.checkpoint=lambda *args:None
        self.service.control(job,'resume')
        done=self.service.run_once(job)
        self.assertEqual(done['state'],'done')
        self.assertEqual(done['payload'],original['payload'])
        self.assertEqual(done['steps'][0]['details']['binding_id'],binding_id)
        copied=(target/second.name).stat()
        self.assertEqual([copied.st_dev,copied.st_ino],fragment)
        for source in (self.clip,second):
            self.assertEqual((target/source.name).read_bytes(),source.read_bytes())

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

    def test_move_inside_original_root_renames_without_copy_and_can_undo(self):
        plan=self.inventory.plan(self.device,str(self.source),operation='move')
        self.assertEqual(plan['space']['required'],0)
        self.assertEqual(plan['entries'][0]['conflicts'],[])
        draft=self.service.prepare(plan['id'],plan['digest'],[0],'move')
        target=Path(draft['payload']['entries'][0]['destination'])
        job=self.approve(draft);self.assertEqual(self.service.run_once(job)['state'],'done')
        self.assertFalse(self.clip.exists());self.assertEqual(target.read_bytes(),b'synthetic raw media')
        inverse=self.service.prepare_inverse(job);undo=self.service.approve_inverse(job,inverse['digest'])
        self.assertEqual(self.service.run_once(undo)['state'],'done')
        self.assertEqual(self.clip.read_bytes(),b'synthetic raw media');self.assertFalse(target.exists())

    def test_case_only_move_revision_uses_selected_source_ownership(self):
        plan=self.inventory.plan(self.device,str(self.source),'preserve',operation='move')
        revised=self.inventory.revise_plan(plan['id'],plan['digest'],[0],{'0':'PART1.mp4'})
        self.assertEqual(revised['entries'][0]['conflicts'],[])
        draft=self.service.prepare(revised['id'],revised['digest'],[0],'move');job=self.approve(draft)
        self.assertEqual(self.service.run_once(job)['state'],'done')
        self.assertIn('PART1.mp4',[p.name for p in self.source.iterdir()])

    def test_move_revision_cannot_claim_an_unselected_existing_source(self):
        other=self.source/'other.mp4';other.write_bytes(b'unselected synthetic media')
        self.inventory.scan(self.device,hashing=True)
        plan=self.inventory.plan(self.device,str(self.source),operation='move')
        selected=next(i for i,entry in enumerate(plan['entries']) if entry['relative']=='part1.mp4')
        revised=self.inventory.revise_plan(plan['id'],plan['digest'],[selected],{str(selected):'other.mp4'})
        self.assertIn('target_exists',revised['entries'][0]['conflicts'])
        self.assertEqual(other.read_bytes(),b'unselected synthetic media')
        self.assertEqual(self.clip.read_bytes(),b'synthetic raw media')

    def test_device_change_during_full_validation_does_not_adopt_new_volume(self):
        plan=self.inventory.plan(self.device,str(self.target))
        original=self.service.jobs.device_identity
        def changed(path):return 'synthetic-replaced-volume' if Path(path)==self.clip else original(path)
        self.service.jobs.device_identity=changed;self.service.locations.device_identity=changed
        with self.assertRaises(ValueError):
            self.service.prepare(plan['id'],plan['digest'],[0],'copy')
        with self.app.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM media_assets').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM organization_drafts').fetchone()[0],0)
        self.assertFalse(self.target.exists());self.assertEqual(self.clip.read_bytes(),b'synthetic raw media')
