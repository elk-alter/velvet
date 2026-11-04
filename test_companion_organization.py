import unittest
from pathlib import Path
from unittest.mock import patch
import test_organization_service as fixtures
from organization_moves import MoveExecutor


class CompanionTests(unittest.TestCase):
    setUp=fixtures.ServiceTests.setUp
    tearDown=fixtures.ServiceTests.tearDown
    shutdown=fixtures.ServiceTests.shutdown
    approve=fixtures.ServiceTests.approve

    def resources(self):
        self.originals={}
        for name in ['part1.nfo','part1.en.srt','part1.zh-CN.forced.ass','part1-poster.jpg','poster.jpg','movie.nfo']:
            path=self.source/name;path.write_bytes(('synthetic '+name).encode());self.originals[name]=path.read_bytes()
        self.inventory.scan(self.device,hashing=True)

    def prepare(self,operation='copy',rename=True):
        plan=self.inventory.plan(self.device,str(self.target))
        if rename:plan=self.inventory.revise_plan(plan['id'],plan['digest'],[0],{'0':'Renamed/New title.mp4'})
        return self.service.prepare(plan['id'],plan['digest'],[0],operation)

    def test_copy_maps_named_resources_and_keeps_shared_resources(self):
        self.resources();draft=self.prepare();entries=draft['payload']['entries']
        self.assertEqual(len(entries),5);self.assertEqual(len(draft['payload']['retained']),2)
        job=self.approve(draft);self.assertEqual(self.service.run_once(job)['state'],'done')
        for name in self.originals:self.assertEqual((self.source/name).read_bytes(),self.originals[name])
        for suffix in ['.nfo','.en.srt','.zh-CN.forced.ass','-poster.jpg']:
            self.assertTrue((self.target/'Renamed'/('New title'+suffix)).exists())
        self.assertFalse((self.target/'Renamed'/'poster.jpg').exists())
        with self.app.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM videos').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM media_resources').fetchone()[0],4)

    def test_move_and_undo_keep_resource_names_and_associations(self):
        self.resources();draft=self.prepare('move');job=self.approve(draft)
        self.assertEqual(self.service.run_once(job)['state'],'done')
        self.assertFalse((self.source/'part1.nfo').exists());self.assertTrue((self.source/'poster.jpg').exists())
        reverse=self.service.move.prepare_undo(job);undo=self.service.move.approve_undo(job,reverse['digest'])
        self.assertEqual(self.service.run_once(undo)['state'],'done')
        for name in self.originals:self.assertEqual((self.source/name).read_bytes(),self.originals[name])
        with self.app.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM videos').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM media_resources').fetchone()[0],4)
            self.assertTrue(all(Path(r[0]).parent==self.source for r in c.execute('SELECT path FROM media_resources')))

    def test_resource_change_since_scan_prevents_execution_plan(self):
        self.resources();(self.source/'part1.en.srt').write_bytes(b'changed')
        with self.assertRaises(ValueError):self.prepare()
        self.assertFalse(self.target.exists());self.assertTrue(self.clip.exists())

    def test_ambiguous_same_stem_versions_keep_subtitle_shared(self):
        (self.source/'part1.mkv').write_bytes(b'alternative version')
        (self.source/'part1.srt').write_bytes(b'shared subtitle')
        self.inventory.scan(self.device,hashing=True)
        plan=self.inventory.plan(self.device,str(self.target))
        draft=self.service.prepare(plan['id'],plan['digest'],[0,1],'move')
        self.assertEqual(len(draft['payload']['entries']),2);self.assertEqual(len(draft['payload']['retained']),1)
        self.assertEqual(self.service.run_once(self.approve(draft))['state'],'done')
        self.assertEqual((self.source/'part1.srt').read_bytes(),b'shared subtitle')

    def test_resource_binding_failure_rolls_back_media_binding_and_retries(self):
        self.resources();draft=self.prepare('move');job=self.approve(draft)
        with patch.object(self.service.resources,'apply_moves',side_effect=RuntimeError('synthetic resource DB failure')):
            result=self.service.run_once(job)
        self.assertEqual(result['state'],'partial')
        with self.app.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM videos').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM media_resources').fetchone()[0],0)
        self.service.control(job,'resume')
        self.assertEqual(self.service.run_once(job)['state'],'done')
        with self.app.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM media_resources').fetchone()[0],4)

    def test_cancel_between_video_and_resource_staging_can_undo_all(self):
        self.resources();draft=self.prepare('move');job=self.approve(draft)
        def stop(stage,task,ordinal):
            if stage=='staged' and ordinal==0:self.service.jobs.request_cancel(task)
        self.service.move=MoveExecutor(self.service.jobs,self.service.bind_moves,stop)
        self.assertEqual(self.service.run_once(job)['state'],'cancelled')
        reverse=self.service.move.prepare_undo(job);undo=self.service.move.approve_undo(job,reverse['digest'])
        self.service.move=MoveExecutor(self.service.jobs,self.service.bind_moves)
        self.assertEqual(self.service.run_once(undo)['state'],'done')
        self.assertTrue(self.clip.exists())
        for name in self.originals:self.assertEqual((self.source/name).read_bytes(),self.originals[name])

    def test_paused_move_quarantines_scans_until_recovered_or_reversed(self):
        self.resources();draft=self.prepare('move');job=self.approve(draft)
        def stop(stage,task,ordinal):
            if stage=='staged':self.service.jobs.request_cancel(task)
        self.service.move=MoveExecutor(self.service.jobs,self.service.bind_moves,stop)
        self.assertEqual(self.service.run_once(job)['state'],'cancelled')
        self.assertIsNone(self.app.LEASES.try_acquire('scan'))
        with self.app.LEASES.hold('backup','write'):pass
        self.app.LEASES.recover();self.service.jobs.recover()
        self.assertIsNone(self.app.LEASES.try_acquire('review'))
        reverse=self.service.move.prepare_undo(job);undo=self.service.move.approve_undo(job,reverse['digest'])
        self.service.move=MoveExecutor(self.service.jobs,self.service.bind_moves)
        self.assertEqual(self.service.run_once(undo)['state'],'done')
        self.service.jobs.recover()
        self.assertFalse(self.app.LEASES.active())

    def test_scanning_both_copy_positions_preserves_single_card(self):
        self.resources();draft=self.prepare();job=self.approve(draft)
        self.assertEqual(self.service.run_once(job)['state'],'done')
        with self.app.db() as c:
            c.execute('INSERT INTO folders VALUES(?)',(str(self.source),));c.execute('INSERT INTO folders VALUES(?)',(str(self.target),))
        with patch.object(self.app,'binary',return_value=None):self.app.scan()
        with self.app.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM videos').fetchone()[0],1)
        (self.source/'part1.mp4').write_bytes(b'changed old position')
        with patch.object(self.app,'binary',return_value=None):self.app.scan()
        with self.app.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM videos').fetchone()[0],1)
            source_location=c.execute('SELECT verified FROM media_locations WHERE path=?',(str(self.clip),)).fetchone()
            self.assertEqual(source_location[0],0)

    def test_scan_after_raw_preview_reuses_provisional_identity(self):
        self.resources();draft=self.prepare();asset=draft['payload']['entries'][0]['asset_id']
        with self.app.db() as c:c.execute('INSERT INTO folders VALUES(?)',(str(self.source),))
        with patch.object(self.app,'binary',return_value=None):self.app.scan()
        with self.app.db() as c:
            video_id=c.execute('SELECT id FROM videos').fetchone()[0]
            self.assertEqual(c.execute('SELECT COUNT(*) FROM videos').fetchone()[0],1)
        self.assertEqual(self.service.locations.asset(video_id)['id'],asset)
        self.assertEqual(self.service.run_once(self.approve(draft))['state'],'done')
        with self.app.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM videos').fetchone()[0],1)
