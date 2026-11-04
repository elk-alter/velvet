import unittest
from pathlib import Path
import test_media_locations as fixtures
from organization_jobs import OrganizationJobs
from organization_moves import MoveExecutor


class MoveTests(unittest.TestCase):
    db=fixtures.LocationTests.db
    def setUp(self):
        fixtures.LocationTests.setUp(self)
        self.other=self.root/'other.mp4';self.other.write_bytes(b'second media')
        with self.db() as c:c.execute('INSERT INTO videos VALUES(2,?,12,0,9,?)',(str(self.other),'[]'))
        self.repository.migrate()
        self.jobs=OrganizationJobs(self.db,lambda path:'volume')
        self.executor=MoveExecutor(self.jobs,self.repository.bind_moves)

    def approve(self,pairs):
        entries=[{'video_id':vid,'operation':'move','source':str(source),'destination':str(target)} for vid,source,target in pairs]
        draft=self.jobs.prepare(entries,self.root,self.root)
        return self.jobs.approve(draft['id'],draft['digest'])

    def test_move_and_explicit_undo_preserve_identity_and_preferences(self):
        target=self.root/'Organized'/'renamed.mp4'
        job=self.approve([(1,self.source,target)])
        self.assertEqual(self.executor.run(job)['state'],'done')
        self.assertFalse(self.source.exists());self.assertEqual(target.read_bytes(),b'synthetic media')
        with self.db() as c:
            self.assertEqual(tuple(c.execute('SELECT favorite,position,locked_fields FROM videos WHERE id=1').fetchone()),(1,42,'["title"]'))
        draft=self.executor.prepare_undo(job);undo=self.executor.approve_undo(job,draft['digest'])
        self.assertEqual(self.executor.run(undo)['state'],'done')
        self.assertEqual(self.source.read_bytes(),b'synthetic media');self.assertFalse(target.exists())
        with self.db() as c:self.assertEqual(c.execute('SELECT path FROM videos WHERE id=1').fetchone()[0],str(self.source))

    def test_path_swap_is_staged_and_bound_atomically(self):
        job=self.approve([(1,self.source,self.other),(2,self.other,self.source)])
        self.assertEqual(self.executor.run(job)['state'],'done')
        self.assertEqual(self.source.read_bytes(),b'second media');self.assertEqual(self.other.read_bytes(),b'synthetic media')
        draft=self.executor.prepare_undo(job);undo=self.executor.approve_undo(job,draft['digest'])
        self.assertEqual(self.executor.run(undo)['state'],'done')
        self.assertEqual(self.source.read_bytes(),b'synthetic media');self.assertEqual(self.other.read_bytes(),b'second media')

    def test_crash_in_staging_can_resume_without_losing_file(self):
        target=self.root/'renamed.mp4';job=self.approve([(1,self.source,target)])
        def crash(stage,*args):
            if stage=='staged':raise SystemExit('synthetic interruption')
        with self.assertRaises(SystemExit):MoveExecutor(self.jobs,self.repository.bind_moves,crash).run(job)
        self.jobs.recover()
        self.assertEqual(self.executor.run(job)['state'],'done');self.assertEqual(target.read_bytes(),b'synthetic media')

    def test_rollback_conflict_does_not_overwrite_external_file(self):
        target=self.root/'renamed.mp4';job=self.approve([(1,self.source,target)])
        self.executor.run(job);self.source.write_bytes(b'external')
        with self.assertRaises(ValueError):self.executor.prepare_undo(job)
        self.assertEqual(self.source.read_bytes(),b'external');self.assertEqual(target.read_bytes(),b'synthetic media')

    def test_cancelled_staging_can_be_undone(self):
        target=self.root/'renamed.mp4';job=self.approve([(1,self.source,target)])
        def cancel(stage,*args):
            if stage=='staged':self.jobs.request_cancel(job)
        self.assertEqual(MoveExecutor(self.jobs,self.repository.bind_moves,cancel).run(job)['state'],'cancelled')
        draft=self.executor.prepare_undo(job);undo=self.executor.approve_undo(job,draft['digest'])
        self.assertEqual(self.executor.run(undo)['state'],'done');self.assertTrue(self.source.exists());self.assertFalse(target.exists())

    def test_case_only_rename(self):
        target=self.root/'SOURCE.mp4';job=self.approve([(1,self.source,target)])
        self.assertEqual(self.executor.run(job)['state'],'done')
        self.assertIn('SOURCE.mp4',[p.name for p in self.root.iterdir()])

    def test_crash_after_publish_before_journal_can_rollback(self):
        target=self.root/'renamed.mp4';job=self.approve([(1,self.source,target)])
        def crash(stage,*args):
            if stage=='published':raise SystemExit('synthetic crash after rename')
        with self.assertRaises(SystemExit):MoveExecutor(self.jobs,self.repository.bind_moves,crash).run(job)
        self.assertEqual(self.jobs.get(job)['steps'][0]['state'],'committing')
        self.jobs.recover()
        draft=self.executor.prepare_undo(job);undo=self.executor.approve_undo(job,draft['digest'])
        self.assertEqual(self.executor.run(undo)['state'],'done')
        self.assertEqual(self.source.read_bytes(),b'synthetic media');self.assertFalse(target.exists())
        with self.assertRaises(ValueError):self.jobs.claim(job)
        self.assertTrue(self.repository.locations(1)[0]['verified'])

    def test_binding_failure_retries_without_moving_again(self):
        target=self.root/'renamed.mp4';job=self.approve([(1,self.source,target)])
        result=MoveExecutor(self.jobs,lambda *args:None).run(job)
        self.assertEqual(result['state'],'partial');self.assertEqual(result['steps'][0]['state'],'needs_binding')
        inode=target.stat().st_ino
        self.assertEqual(self.executor.run(job)['state'],'done');self.assertEqual(target.stat().st_ino,inode)

    def test_crash_after_database_binding_is_idempotently_reconciled(self):
        target=self.root/'renamed.mp4';job=self.approve([(1,self.source,target)])
        def crash(stage,*args):
            if stage=='bound':raise SystemExit('synthetic crash after binding')
        with self.assertRaises(SystemExit):MoveExecutor(self.jobs,self.repository.bind_moves,crash).run(job)
        self.jobs.recover()
        self.assertEqual(self.executor.run(job)['state'],'done')
        self.assertEqual(len(self.repository.locations(1)),1)
        self.assertEqual(self.repository.locations(1)[0]['path'],str(target))

    def test_target_occupied_after_approval_is_never_overwritten(self):
        target=self.root/'renamed.mp4';job=self.approve([(1,self.source,target)])
        target.write_bytes(b'external')
        self.assertEqual(self.executor.run(job)['state'],'partial')
        self.assertEqual(target.read_bytes(),b'external')
        draft=self.executor.prepare_undo(job);undo=self.executor.approve_undo(job,draft['digest'])
        self.assertEqual(self.executor.run(undo)['state'],'done')
        self.assertEqual(self.source.read_bytes(),b'synthetic media');self.assertEqual(target.read_bytes(),b'external')

    def test_pause_after_staging_keeps_recoverable_location_and_resumes(self):
        target=self.root/'renamed.mp4';job=self.approve([(1,self.source,target)])
        def pause(stage,*args):
            if stage=='staged':self.jobs.request_pause(job)
        result=MoveExecutor(self.jobs,self.repository.bind_moves,pause).run(job)
        self.assertEqual(result['state'],'waiting')
        self.assertFalse(self.source.exists());self.assertFalse(target.exists())
        self.assertTrue(Path(result['steps'][0]['details']['temporary']).exists())
        self.jobs.resume(job)
        self.assertEqual(self.executor.run(job)['state'],'done')
        self.assertEqual(self.jobs.progress(job)['bytes_processed'],0)
        self.assertEqual(self.jobs.progress(job)['bytes_committed'],15)
