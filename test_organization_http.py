import json
import threading
import time
import unittest
from unittest.mock import patch
from http.client import HTTPConnection
from pathlib import Path
import test_organization_service as fixtures


class OrganizationHTTPTests(unittest.TestCase):
    shutdown=fixtures.ServiceTests.shutdown
    def setUp(self):
        fixtures.ServiceTests.setUp(self)
        (self.source/'part1.en.srt').write_bytes(b'synthetic subtitle')
        self.inventory.scan(self.device,hashing=True)
        self.server=self.app.LocalServer(('127.0.0.1',0),self.app.Handler)
        self.worker=threading.Thread(target=self.server.serve_forever,daemon=True);self.worker.start()

    def tearDown(self):
        try:
            self.app.ORGANIZATION.stop()
            if self.app.ORGANIZATION.thread:self.app.ORGANIZATION.thread.join(5)
            self.server.shutdown();self.server.server_close();self.worker.join(5)
        finally:fixtures.ServiceTests.tearDown(self)

    def request(self,path,data=None,token=True):
        conn=HTTPConnection('127.0.0.1',self.server.server_address[1],timeout=20)
        headers={'Host':'127.0.0.1:'+str(self.app.RUNTIME.port)}
        if data is not None:
            headers['Content-Type']='application/json'
            if token:headers['X-Velvet-Token']=self.app.TOKEN
        conn.request('POST' if data is not None else 'GET',path,json.dumps(data) if data is not None else None,headers)
        response=conn.getresponse();body=response.read();status=response.status;conn.close()
        return status,json.loads(body)

    def draft(self,operation='copy'):
        status,result=self.request('/api/storage/plan',{'id':self.device,'target':str(self.target),'layout':'preserve'})
        self.assertEqual(status,200);plan=result['plan']
        status,result=self.request('/api/organization/prepare',{'id':plan['id'],'digest':plan['digest'],'selected':'all','operation':operation})
        self.assertEqual(status,200,result)
        status,draft=self.request('/api/organization/draft?id='+result['id']+'&limit=1')
        self.assertEqual(status,200);self.assertEqual(len(draft['items']),1);self.assertEqual(draft['total'],2)
        return draft

    def wait_job(self,job_id):
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            status,job=self.request('/api/organization/job?id='+job_id)
            self.assertEqual(status,200)
            if job['state'] in ('done','failed','partial'):return job
            time.sleep(.03)
        self.fail('Synthetic organization job did not finish')

    def test_completed_target_reconnect_updates_video_and_subtitle_via_http(self):
        from organization_jobs import root_proof

        if not root_proof(self.source):
            self.skipTest('Requires native stable volume and directory proof')
        self.target.mkdir()
        draft=self.draft()
        service=self.app.ORGANIZATION
        job=service.approve(draft['id'],draft['digest'])
        self.assertEqual(service.run_once(job)['state'],'done')
        moved=self.target.with_name('http-reconnected-target')
        self.target.rename(moved)
        request={'id':job,'replacements':{str(self.target):str(moved)}}
        status,preview=self.request('/api/organization/recovery-map-preview',request)
        self.assertEqual((status,preview.get('binding_count')),(200,2),preview)
        payload=dict(request,digest=preview['digest'])
        self.assertEqual(self.request('/api/organization/recovery-map',payload)[0],400)
        self.assertEqual(self.request('/api/organization/recovery-map',dict(payload,confirmed=True))[0],200)
        status,detail=self.request('/api/organization/job?id='+job)
        self.assertEqual(status,200)
        self.assertEqual(detail['recovery']['mapping'],request['replacements'])
        with self.app.db() as c:
            video=c.execute('SELECT path FROM videos').fetchone()[0]
            subtitle=c.execute('SELECT path FROM media_resources').fetchone()[0]
        self.assertTrue(Path(video).is_relative_to(moved))
        self.assertTrue(Path(subtitle).is_relative_to(moved))
        self.assertEqual(Path(subtitle).read_bytes(),b'synthetic subtitle')

    def test_recovery_roots_requires_token_and_preserves_approved_job(self):
        route='/api/organization/recovery-roots'
        def proof(root):
            path=Path(root)
            return {'volume':'synthetic-volume','directory':str(path.stat().st_ino)} if path.is_dir() else None
        with patch('organization_jobs.root_proof',side_effect=proof):
            draft=self.draft()
            job=self.app.ORGANIZATION.approve(draft['id'],draft['digest'])
            original=self.app.ORGANIZATION.jobs.get(job)
            moved=self.source.with_name('reconnected')
            self.source.rename(moved)
            request={'id':job,'replacements':{str(self.source):str(moved)}}
            self.assertEqual(self.request(route,request,token=False)[0],403)
            self.assertEqual(self.request(route,request),(200,{'roots':request['replacements']}))
            self.assertEqual(self.app.ORGANIZATION.jobs.get(job),original)
            self.assertFalse(self.target.exists())

    def test_recovery_map_requires_stopped_job_current_checkpoints_and_confirmation(self):
        preview_route='/api/organization/recovery-map-preview'
        confirm_route='/api/organization/recovery-map'
        def proof(root):
            path=Path(root)
            return {'volume':'synthetic-volume','directory':str(path.stat().st_ino)} if path.is_dir() else None
        with patch('organization_jobs.root_proof',side_effect=proof):
            draft=self.draft()
            job=self.app.ORGANIZATION.approve(draft['id'],draft['digest'])
            moved=self.source.with_name('reconnected')
            self.source.rename(moved)
            request={'id':job,'replacements':{str(self.source):str(moved)}}
            self.assertEqual(self.request(preview_route,request)[0],400)
            self.app.ORGANIZATION.control(job,'pause')
            original=self.app.ORGANIZATION.jobs.get(job)
            status,preview=self.request(preview_route,request)
            self.assertEqual(status,200)
            self.assertEqual(self.request(confirm_route,dict(request,digest=preview['digest']))[0],400)
            with self.app.db() as c:
                c.execute("UPDATE organization_steps SET details=? WHERE job_id=? AND ordinal=0",(json.dumps({'error':'Synthetic change'}),job))
            self.assertEqual(self.request(confirm_route,dict(request,digest=preview['digest'],confirmed=True))[0],400)
            status,preview=self.request(preview_route,request)
            self.assertEqual(status,200)
            confirmed=dict(request,digest=preview['digest'],confirmed=True)
            self.assertEqual(self.request(confirm_route,confirmed,token=False)[0],403)
            self.assertEqual(self.request(confirm_route,confirmed)[0],200)
            self.assertEqual(self.app.ORGANIZATION.jobs.get(job)['payload'],original['payload'])
            with self.app.db() as c:
                row=c.execute('SELECT * FROM organization_recovery_maps WHERE job_id=?',(job,)).fetchone()
                self.assertEqual(json.loads(row['roots']),request['replacements'])
                self.assertEqual(row['checkpoint_digest'],preview['checkpoint_digest'])
            self.assertFalse(self.target.exists())

    def test_skip_group_requires_token_confirmation_and_current_digest(self):
        draft=self.draft();job=self.app.ORGANIZATION.approve(draft['id'],draft['digest'])
        self.app.ORGANIZATION.control(job,'pause')
        request={'id':job,'ordinal':0}
        self.assertEqual(self.request('/api/organization/skip-preview',request,token=False)[0],403)
        status,preview=self.request('/api/organization/skip-preview',request);self.assertEqual(status,200)
        self.assertEqual(len(preview['items']),2)
        request.update(digest=preview['digest'],confirmed=False)
        self.assertEqual(self.request('/api/organization/skip',request)[0],400)
        request.update(confirmed=True)
        self.assertEqual(self.request('/api/organization/skip',request,token=False)[0],403)
        self.assertEqual(self.request('/api/organization/skip',dict(request,digest='stale'))[0],400)
        status,result=self.request('/api/organization/skip',request)
        self.assertEqual((status,result['skipped'],result['state']),(200,2,'cancelled'))
        _,current=self.request('/api/organization/job?id='+job)
        self.assertEqual((current['progress']['processed'],current['progress']['skipped']),(0,2))
        self.assertEqual(self.request('/api/organization/control',{'id':job,'action':'resume'})[0],400)
        self.assertTrue(self.clip.exists());self.assertFalse(self.target.exists())

    def test_restore_preview_is_bounded_authenticated_and_revalidated(self):
        status,result=self.request('/api/backup',{});self.assertEqual(status,200)
        backup=result['name'];draft=self.draft()
        status,result=self.request('/api/organization/approve',{'id':draft['id'],'digest':draft['digest'],'confirmed':True})
        self.assertEqual(status,200);self.assertEqual(self.wait_job(result['id'])['state'],'done')
        status,_=self.request('/api/restore-preview',{'name':backup},False);self.assertEqual(status,403)
        status,report=self.request('/api/restore-preview',{'name':backup})
        self.assertEqual(status,200,report);self.assertTrue(report['ready'])
        self.assertEqual(report['retained_count'],2);self.assertEqual(report['verified_count'],2)
        self.assertNotIn('journals',report);self.assertNotIn('fingerprint',report['retained_files'][0])
        status,_=self.request('/api/restore',{'name':backup,'confirm':backup,'reconciliation':True})
        self.assertEqual(status,400)
        with self.app.db() as c:c.execute('UPDATE videos SET favorite=1')
        status,_=self.request('/api/restore',{'name':backup,'confirm':backup,'reconciliation':report})
        self.assertEqual(status,400)
        status,report=self.request('/api/restore-preview',{'name':backup});self.assertEqual(status,200)
        status,_=self.request('/api/restore',{'name':backup,'confirm':'wrong','reconciliation':report})
        self.assertEqual(status,400)
        status,result=self.request('/api/restore',{'name':backup,'confirm':backup,'reconciliation':report})
        self.assertEqual(status,200,result)
        self.assertTrue((self.target/'part1.mp4').exists());self.assertTrue((self.target/'part1.en.srt').exists())
        with self.app.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM videos').fetchone()[0],0)

    def test_copy_requires_confirmation_and_token_then_binds_resources(self):
        draft=self.draft()
        status,_=self.request('/api/organization/approve',{'id':draft['id'],'digest':draft['digest'],'confirmed':True},False)
        self.assertEqual(status,403);self.assertFalse(self.target.exists())
        status,_=self.request('/api/organization/approve',{'id':draft['id'],'digest':draft['digest'],'confirmed':False})
        self.assertEqual(status,400);self.assertFalse(self.target.exists())
        status,result=self.request('/api/organization/approve',{'id':draft['id'],'digest':draft['digest'],'confirmed':True})
        self.assertEqual(status,200);job=self.wait_job(result['id']);self.assertEqual(job['state'],'done')
        self.assertTrue(self.clip.exists());self.assertEqual((self.target/'part1.en.srt').read_bytes(),b'synthetic subtitle')
        with self.app.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM videos').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM media_resources').fetchone()[0],1)
        status,again=self.request('/api/organization/approve',{'id':draft['id'],'digest':draft['digest'],'confirmed':True})
        self.assertEqual(status,200);self.assertEqual(again['id'],result['id'])

    def test_move_inverse_is_previewed_and_separately_confirmed(self):
        draft=self.draft('move')
        status,result=self.request('/api/organization/approve',{'id':draft['id'],'digest':draft['digest'],'confirmed':True})
        self.assertEqual(status,200);job_id=result['id'];self.assertEqual(self.wait_job(job_id)['state'],'done')
        self.assertFalse(self.clip.exists())
        status,result=self.request('/api/organization/undo-preview',{'id':job_id});self.assertEqual(status,200)
        status,inverse=self.request('/api/organization/draft?id='+result['id']);self.assertEqual(status,200)
        self.assertEqual(inverse['inverse_of'],job_id);self.assertFalse(self.clip.exists())
        status,result=self.request('/api/organization/undo-approve',{'id':job_id,'digest':inverse['digest'],'confirmed':True})
        self.assertEqual(status,200);self.assertEqual(self.wait_job(result['id'])['state'],'done')
        self.assertEqual(self.clip.read_bytes(),b'synthetic raw media')
        self.assertEqual((self.source/'part1.en.srt').read_bytes(),b'synthetic subtitle')
        status,original=self.request('/api/organization/job?id='+job_id)
        self.assertEqual(original['inverse']['state'],'done')

    def test_changed_preview_digest_and_out_of_scope_selection_are_rejected(self):
        status,result=self.request('/api/storage/plan',{'id':self.device,'target':str(self.target),'layout':'preserve'});plan=result['plan']
        status,_=self.request('/api/organization/prepare',{'id':plan['id'],'digest':'wrong','selected':'all'})
        self.assertEqual(status,400)
        status,_=self.request('/api/organization/prepare',{'id':plan['id'],'digest':plan['digest'],'selected':[999]})
        self.assertEqual(status,400);self.assertFalse(self.target.exists())

    def test_resource_wait_and_user_pause_remain_distinct_until_explicit_resume(self):
        draft=self.draft()
        owner=self.app.LEASES.try_acquire('synthetic-reader')
        try:
            status,result=self.request('/api/organization/approve',{'id':draft['id'],'digest':draft['digest'],'confirmed':True})
            self.assertEqual(status,200);job_id=result['id']
            status,_=self.request('/api/organization/control',{'id':job_id,'action':'pause'})
            self.assertEqual(status,200)
            status,job=self.request('/api/organization/job?id='+job_id)
            self.assertEqual(status,200);self.assertEqual(job['state'],'waiting');self.assertTrue(job['paused'])
            self.assertFalse(self.target.exists())
        finally:self.app.LEASES.release(owner)
        status,_=self.request('/api/organization/control',{'id':job_id,'action':'resume'})
        self.assertEqual(status,200);job=self.wait_job(job_id)
        self.assertEqual(job['state'],'done');self.assertFalse(job['paused'])
