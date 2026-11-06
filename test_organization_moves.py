import unittest
from unittest.mock import patch
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

    def test_reconnected_move_resumes_staged_and_committed_files_then_can_undo(self):
        scope=self.root/'mounted'
        scope.mkdir()
        source=scope/self.source.name
        self.source.rename(source)
        with self.db() as c:
            c.execute('UPDATE videos SET path=? WHERE id=1',(str(source),))
            c.execute('UPDATE media_locations SET path=? WHERE asset_id=?',(str(source),self.repository.asset(1)['id']))
        target=scope/'organized'/'renamed.mp4'
        def proof(root):
            path=Path(root)
            return {'volume':'volume','directory':str(path.stat().st_ino)} if path.is_dir() else None
        def crash_at(expected):
            def crash(stage,*args):
                if stage==expected:raise SystemExit('synthetic interruption')
            return crash

        with patch('organization_jobs.root_proof',side_effect=proof),patch('organization_moves.root_proof',side_effect=proof):
            draft=self.jobs.prepare([{'video_id':1,'source':str(source),'destination':str(target),'operation':'move'}],scope,scope)
            job=self.jobs.approve(draft['id'],draft['digest'])
            with self.assertRaises(SystemExit):
                MoveExecutor(self.jobs,self.repository.bind_moves,crash_at('staged')).run(job)
            self.jobs.recover()
            moved=self.root/'remounted'
            scope.rename(moved)
            paths={str(scope):str(moved)}
            preview=self.jobs.recovery_map_preview(job,paths)
            self.jobs.confirm_recovery_map(job,paths,preview['digest'])
            self.jobs.resume(job)
            with self.assertRaises(SystemExit):
                MoveExecutor(self.jobs,self.repository.bind_moves,crash_at('committed')).run(job)
            self.jobs.recover()
            self.jobs.resume(job)
            result=self.executor.run(job)
            self.assertEqual(result['state'],'done')
            self.assertEqual(result['payload'],draft['payload'])
            new_target=moved/'organized'/'renamed.mp4'
            self.assertEqual(new_target.read_bytes(),b'synthetic media')
            self.assertFalse(scope.exists())
            with self.db() as c:
                self.assertEqual(tuple(c.execute('SELECT path,favorite,position FROM videos WHERE id=1').fetchone()),(str(new_target),1,42))
            from restore_reconciliation import RestoreAudit
            with self.db() as c,patch('restore_reconciliation.volume_identity',return_value=('volume',True)):
                report=RestoreAudit(c,c).inspect()
                self.assertTrue(report['ready'],report['problems'])
                self.assertEqual([proof['path'] for proof in report['files']],[str(new_target)])
            reverse=self.executor.prepare_undo(job)
            undo=self.executor.approve_undo(job,reverse['digest'])
            with self.assertRaises(SystemExit):
                MoveExecutor(self.jobs,self.repository.bind_moves,crash_at('staged')).run(undo)
            self.jobs.recover()
            restored=self.root/'remounted-again'
            moved.rename(restored)
            reverse_paths={str(moved):str(restored)}
            preview=self.jobs.recovery_map_preview(undo,reverse_paths)
            self.jobs.confirm_recovery_map(undo,reverse_paths,preview['digest'])
            self.jobs.resume(undo)
            self.assertEqual(self.executor.run(undo)['state'],'done')
            self.assertEqual((restored/source.name).read_bytes(),b'synthetic media')
            self.assertFalse((restored/'organized'/'renamed.mp4').exists())

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

    def test_target_occupied_before_staging_leaves_source_untouched(self):
        target=self.root/'renamed.mp4';job=self.approve([(1,self.source,target)])
        target.write_bytes(b'external')
        result=self.executor.run(job)
        self.assertEqual(result['state'],'failed');self.assertEqual(result['steps'][0]['state'],'pending')
        self.assertEqual(self.source.read_bytes(),b'synthetic media');self.assertEqual(target.read_bytes(),b'external')
        self.assertIn('target_exists',result['steps'][0]['details']['preflight'])
        self.assertEqual(list(self.root.glob('.velvet-move-*')),[])

    def test_target_occupied_after_staging_keeps_undo_available(self):
        target=self.root/'renamed.mp4';job=self.approve([(1,self.source,target)])
        def occupy(stage,*args):
            if stage=='staged':target.write_bytes(b'external')
        result=MoveExecutor(self.jobs,self.repository.bind_moves,occupy).run(job)
        self.assertEqual(result['state'],'partial');self.assertEqual(target.read_bytes(),b'external')
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
