import hashlib
import unittest
from pathlib import Path
from unittest.mock import patch
import test_media_locations as fixtures
from organization_jobs import OrganizationJobs
from organization_executor import CopyExecutor


class ExecutorTests(unittest.TestCase):
    db=fixtures.LocationTests.db
    def setUp(self):
        fixtures.LocationTests.setUp(self)
        self.scope=self.root/'source';self.scope.mkdir()
        self.source.rename(self.scope/'one.mp4');self.source=self.scope/'one.mp4'
        with self.db() as c:
            c.execute('UPDATE videos SET path=? WHERE id=1',(str(self.source),))
            c.execute('UPDATE media_locations SET path=?',(str(self.source),))
        self.target_root=self.root/'target';self.target=self.target_root/'one.mp4'
        self.jobs=OrganizationJobs(self.db,lambda path:'volume')
        draft=self.jobs.prepare([{'video_id':1,'source':str(self.source),'destination':str(self.target),'operation':'copy'}],self.scope,self.target_root)
        self.job=self.jobs.approve(draft['id'],draft['digest'])
        self.original=hashlib.sha256(self.source.read_bytes()).hexdigest()

    def bind(self,entry,job):
        return self.repository.bind_verified_copy(entry['video_id'],entry['destination'],entry['target_device'],job)

    def assert_source(self):self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(),self.original)

    def test_real_copy_verification_binding_and_idempotent_run(self):
        executor=CopyExecutor(self.jobs,self.bind)
        result=executor.run(self.job)
        self.assertEqual(result['state'],'done');self.assertEqual(self.target.read_bytes(),self.source.read_bytes())
        self.assertEqual(len(self.repository.locations(1)),2);self.assert_source()
        self.assertEqual(self.target.stat().st_mtime_ns,self.source.stat().st_mtime_ns)
        identity=self.target.stat().st_ino
        executor.run(self.job);self.assertEqual(identity,self.target.stat().st_ino)
        self.assertFalse(list(self.target_root.glob('*.partial')))

    def test_committed_target_binding_failure_is_retried_without_copy(self):
        result=CopyExecutor(self.jobs,lambda *args:None).run(self.job)
        self.assertEqual(result['state'],'partial');self.assertEqual(result['steps'][0]['state'],'needs_binding')
        identity=self.target.stat().st_ino
        result=CopyExecutor(self.jobs,self.bind).run(self.job)
        self.assertEqual(result['state'],'done');self.assertEqual(identity,self.target.stat().st_ino);self.assert_source()

    def test_crash_after_commit_reconciles_file_identity_before_binding(self):
        def crash(stage,*args):
            if stage=='committed':raise SystemExit('synthetic crash')
        with self.assertRaises(SystemExit):CopyExecutor(self.jobs,self.bind,crash).run(self.job)
        self.jobs.recover();identity=self.target.stat().st_ino
        result=CopyExecutor(self.jobs,self.bind).run(self.job)
        self.assertEqual(result['state'],'done');self.assertEqual(identity,self.target.stat().st_ino);self.assert_source()

    def test_crash_before_commit_rejects_foreign_existing_file(self):
        def crash(stage,*args):
            if stage=='committing':raise SystemExit('synthetic crash')
        with self.assertRaises(SystemExit):CopyExecutor(self.jobs,self.bind,crash).run(self.job)
        self.jobs.recover();self.target.write_bytes(b'foreign file')
        result=CopyExecutor(self.jobs,self.bind).run(self.job)
        self.assertNotEqual(result['state'],'done');self.assertEqual(self.target.read_bytes(),b'foreign file');self.assert_source()

    def test_cancel_during_copy_retains_checkpoint_and_explicit_resume(self):
        def stop(stage,*args):
            if stage=='copying':self.jobs.request_cancel(self.job)
        result=CopyExecutor(self.jobs,self.bind,stop).run(self.job)
        self.assertEqual(result['state'],'cancelled');self.assertFalse(self.target.exists())
        partial=Path(result['steps'][0]['details']['temporary']);self.assertTrue(partial.exists())
        self.jobs.resume(self.job)
        result=CopyExecutor(self.jobs,self.bind).run(self.job)
        self.assertEqual(result['state'],'done');self.assertTrue(partial.exists());self.assert_source()

    def test_reconnected_source_resumes_verified_copy_under_original_approval(self):
        def proof(root):
            path=Path(root)
            return {'volume':'volume','directory':str(path.stat().st_ino)} if path.is_dir() else None

        def interrupt(stage,*args):
            if stage=='verified':raise SystemExit('synthetic interruption')

        def register(entry,stamp,device):
            return self.repository.register_verified(entry['source'],stamp,device,entry['video_id'])

        def bind_committed(entry,job):
            return self.repository.bind_committed_copy(entry['asset_id'],entry['destination'],entry['fingerprint'],
                                                        entry['target_device'],job,expected_identity=entry['verified_identity'])[1]

        with patch('organization_jobs.root_proof',side_effect=proof):
            draft=self.jobs.prepare([{'video_id':1,'source':str(self.source),'destination':str(self.target),
                                      'operation':'copy'}],self.scope,self.target_root,register=register)
            job=self.jobs.approve(draft['id'],draft['digest'])
            with self.assertRaises(SystemExit):
                CopyExecutor(self.jobs,self.bind,interrupt).run(job)
            self.jobs.recover()
            original=self.jobs.get(job)
            fragment=Path(original['steps'][0]['details']['temporary'])
            identity=fragment.stat().st_ino
            moved=self.root/'reconnected-source'
            self.scope.rename(moved)
            paths={str(self.scope):str(moved)}
            preview=self.jobs.recovery_map_preview(job,paths)
            self.jobs.confirm_recovery_map(job,paths,preview['digest'])
            jobs=OrganizationJobs(self.db,lambda path:'volume')
            jobs.resume(job)
            def interrupt_committed(stage,*args):
                if stage=='committed':raise SystemExit('synthetic interruption after commit')
            with self.assertRaises(SystemExit):
                CopyExecutor(jobs,bind_committed,interrupt_committed).run(job)
            jobs=OrganizationJobs(self.db,lambda path:'volume')
            self.assertEqual(jobs.recover(),[job])
            jobs.resume(job)
            result=CopyExecutor(jobs,bind_committed).run(job)
            self.assertEqual(result['state'],'done')
            self.assertEqual(result['approval_digest'],original['approval_digest'])
            self.assertEqual(result['payload'],original['payload'])
            self.assertEqual(self.target.read_bytes(),(moved/'one.mp4').read_bytes())
            self.assertEqual(self.target.stat().st_ino,identity)
            self.assertFalse(fragment.exists())

    def test_target_reconnection_reuses_verified_fragment_and_binds_new_path(self):
        from organization_copy_undo import CopyUndo
        self.jobs.finish(self.job,'cancelled')
        from organization_moves import MoveExecutor
        from resource_leases import path_resource
        self.target_root.mkdir()
        def proof(root):
            path=Path(root)
            return {'volume':'volume','directory':str(path.stat().st_ino)} if path.is_dir() else None
        def register(entry,stamp,device):
            return self.repository.register_verified(entry['source'],stamp,device,entry['video_id'])
        def binder(entry,job):
            return self.repository.bind_committed_copy(entry['asset_id'],entry['destination'],entry['fingerprint'],
                                                        entry['target_device'],job,expected_identity=entry['verified_identity'])[1]
        def interrupt(stage,*args):
            if stage=='verified':raise SystemExit('synthetic interruption')

        with patch('organization_jobs.root_proof',side_effect=proof),patch('organization_executor.root_proof',side_effect=proof):
            draft=self.jobs.prepare([{'video_id':1,'source':str(self.source),'destination':str(self.target),
                                      'operation':'copy'}],self.scope,self.target_root,register=register)
            job=self.jobs.approve(draft['id'],draft['digest'])
            with self.assertRaises(SystemExit):CopyExecutor(self.jobs,binder,interrupt).run(job)
            self.jobs.recover()
            original=self.jobs.get(job)
            fragment=Path(original['steps'][0]['details']['temporary'])
            identity=fragment.stat().st_ino
            moved=self.root/'reconnected-target'
            self.target_root.rename(moved)
            paths={str(self.target_root):str(moved)}
            preview=self.jobs.recovery_map_preview(job,paths)
            self.jobs.confirm_recovery_map(job,paths,preview['digest'])
            self.jobs.resume(job)
            def after_commit(stage,*args):
                if stage=='committed':raise SystemExit('synthetic interruption')
            with self.assertRaises(SystemExit):CopyExecutor(self.jobs,binder,after_commit).run(job)
            self.jobs.recover()
            target=moved/self.target.name
            with self.db() as c:
                scopes={r[0] for r in c.execute('SELECT resource FROM resource_scopes WHERE owner=?',('reconcile:'+job,))}
            self.assertIn(path_resource(target),scopes)
            self.jobs.resume(job)
            result=CopyExecutor(self.jobs,binder).run(job)
            self.assertEqual(result['state'],'done')
            self.assertEqual(target.read_bytes(),self.source.read_bytes())
            self.assertEqual(target.stat().st_ino,identity)
            self.assertEqual(result['payload'],original['payload'])
            self.assertFalse(self.target_root.exists())
            MoveExecutor(self.jobs,None)
            inverse=CopyUndo(self.jobs,self.repository).prepare(job)
            self.assertEqual(inverse['payload']['entries'][0]['source'],str(target))
            from restore_reconciliation import RestoreAudit,approve_report
            from contextlib import closing
            import sqlite3
            with self.db() as c,patch('restore_reconciliation.volume_identity',return_value=('volume',True)):
                report=RestoreAudit(c,c).inspect()
                self.assertTrue(report['ready'],report['problems'])
                self.assertEqual([file['path'] for file in report['files']],[str(target)])
                approve_report(c,c,report).validate(c,'current')
                with closing(sqlite3.connect(':memory:')) as previous:
                    c.backup(previous)
                    previous.execute('DELETE FROM organization_recovery_maps')
                    with self.assertRaisesRegex(ValueError,'重连记录不同'):
                        RestoreAudit(c,previous).inspect()
            moved.rename(self.root/'old-location')
            moved.mkdir()
            with self.db() as c,self.assertRaisesRegex(ValueError,'目录身份'):
                RestoreAudit(c,c).inspect()

    def test_source_changed_or_space_exhausted_never_commits(self):
        self.source.write_bytes(b'changed source')
        result=CopyExecutor(self.jobs,self.bind).run(self.job)
        self.assertEqual(result['state'],'failed');self.assertFalse(self.target.exists())
        self.source.write_bytes(b'synthetic media')
        # Re-approval is necessary after source metadata changes; use a fresh plan.
        draft=self.jobs.prepare([{'video_id':1,'source':str(self.source),'destination':str(self.target),'operation':'copy'}],self.scope,self.target_root)
        job=self.jobs.approve(draft['id'],draft['digest'])
        with patch('organization_executor.shutil.disk_usage',return_value=type('Space',(),{'free':0})()):
            result=CopyExecutor(self.jobs,self.bind).run(job)
        self.assertEqual(result['state'],'failed');self.assertFalse(self.target_root.exists())

    def test_target_created_during_commit_is_not_adopted_even_with_same_bytes(self):
        def occupy(stage,*args):
            if stage=='committing':self.target.write_bytes(self.source.read_bytes())
        result=CopyExecutor(self.jobs,self.bind,occupy).run(self.job)
        self.assertNotEqual(result['state'],'done')
        self.assertEqual(len(self.repository.locations(1)),1)
        self.assertEqual(self.target.read_bytes(),self.source.read_bytes());self.assert_source()

    def test_cancel_before_start_does_not_create_target_directory(self):
        self.jobs.request_cancel(self.job)
        result=CopyExecutor(self.jobs,self.bind).run(self.job)
        self.assertEqual(result['state'],'cancelled');self.assertFalse(self.target_root.exists());self.assert_source()

    def test_pause_is_persistent_distinct_from_cancel_and_resumes(self):
        def pause(stage,*args):
            if stage=='copying':self.jobs.request_pause(self.job)
        result=CopyExecutor(self.jobs,self.bind,pause).run(self.job)
        self.assertEqual(result['state'],'waiting');self.assertFalse(self.target.exists())
        self.assertFalse(self.jobs.cancel_requested(self.job));self.assertTrue(self.jobs.pause_requested(self.job))
        restarted=OrganizationJobs(self.db,lambda path:'volume')
        self.assertTrue(restarted.pause_requested(self.job))
        self.assertEqual(restarted.journal.list()[0]['summary']['reason'],'user_paused')
        restarted.resume(self.job)
        self.assertEqual(CopyExecutor(restarted,self.bind).run(self.job)['state'],'done')
        self.assert_source()

    def test_batch_progress_counts_committed_files_and_never_double_counts_retry(self):
        second=self.scope/'two.mp4';second.write_bytes(b'more data')
        with self.db() as c:c.execute('INSERT INTO videos VALUES(2,?,9,0,0,?)',(str(second),'[]'))
        self.repository.migrate()
        entries=[{'video_id':vid,'source':str(source),'destination':str(target),'operation':'copy'} for vid,source,target in [(1,self.source,self.target),(2,second,self.target_root/'two.mp4')]]
        draft=self.jobs.prepare(entries,self.scope,self.target_root);job=self.jobs.approve(draft['id'],draft['digest'])
        def crash(stage,*args):
            if stage=='done':raise SystemExit('synthetic crash between files')
        with self.assertRaises(SystemExit):CopyExecutor(self.jobs,self.bind,crash).run(job)
        progress=self.jobs.progress(job)
        self.assertEqual((progress['processed'],progress['bytes_processed'],progress['bytes_total']),(1,15,24))
        self.jobs.recover()
        self.assertEqual(CopyExecutor(self.jobs,self.bind).run(job)['state'],'done')
        progress=self.jobs.progress(job)
        self.assertEqual((progress['processed'],progress['bytes_processed'],progress['bytes_verified'],progress['bytes_committed']),(2,24,24,24))
