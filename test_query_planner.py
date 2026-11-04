"""Freeze physical query selections into one multi-root organization plan."""
import unittest
import os
from pathlib import Path
import test_organization_service as fixtures
from inventory_query import InventoryQuery,InventoryQueryChanged


class QueryPlannerTests(unittest.TestCase):
    tearDown=fixtures.ServiceTests.tearDown
    shutdown=fixtures.ServiceTests.shutdown

    def setUp(self):
        fixtures.ServiceTests.setUp(self)
        self.other=self.root/'other';self.other.mkdir();(self.other/'second.mp4').write_bytes(b'second synthetic media')
        self.other_device=self.inventory.register(str(self.other),'Other synthetic disk')
        self.inventory.scan(self.other_device,hashing=True)
        self.query=InventoryQuery(self.inventory)

    def test_members_selection_uses_query_ordinals_and_rejects_unknown_rows(self):
        page=self.query.query({'limit':1})
        ordinal=page['items'][0]['query_ordinal']
        members=self.query.members(page['snapshot'],[ordinal])
        self.assertEqual(len(members['items']),1)
        self.assertEqual(members['items'][0]['relative_path'],page['items'][0]['relative_path'])
        self.assertEqual(self.query.members(page['snapshot'],'all')['total'],2)
        with self.assertRaises(ValueError):self.query.members(page['snapshot'],[999])
        with self.assertRaises(ValueError):self.query.members(page['snapshot'],[ordinal,ordinal])
        without=self.query.members(page['snapshot'],{'exclude':[ordinal]})
        self.assertEqual(without['total'],1);self.assertNotEqual(without['items'][0]['ordinal'],ordinal)
        with self.assertRaises(ValueError):self.query.members(page['snapshot'],{'exclude':[999]})

    def plan(self,operation='copy',selected='all',snapshot=None):
        page=self.query.query({}) if snapshot is None else {'snapshot':snapshot}
        return self.service.planner.plan(page['snapshot'],selected,str(self.target),'title',operation)

    def run_plan(self,plan,operation='copy'):
        draft=self.service.prepare(plan['id'],plan['digest'],list(range(len(plan['entries']))),operation)
        job=self.service.approve(draft['id'],draft['digest'])
        self.assertEqual(self.service.run_once(job)['state'],'done')
        return job,draft

    def test_all_matching_uses_pinned_files_after_a_later_scan(self):
        page=self.query.query({})
        (self.other/'later.mp4').write_bytes(b'later synthetic file');self.inventory.scan(self.other_device,hashing=True)
        plan=self.plan(snapshot=page['snapshot'])
        self.assertEqual(len(plan['entries']),2)
        self.assertFalse(any(e['relative']=='later.mp4' for e in plan['entries']))
        self.assertEqual(plan['selection']['total'],2)
        job,draft=self.run_plan(plan)
        self.assertEqual(len(draft['payload']['source_roots']),2)
        with self.app.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM videos').fetchone()[0],2)

    def test_keep_and_move_inverse_handle_each_original_root(self):
        self.run_plan(self.plan('keep'),'keep')
        plan=self.plan('move');job,_=self.run_plan(plan,'move')
        self.assertFalse(self.clip.exists());self.assertFalse((self.other/'second.mp4').exists())
        inverse=self.service.prepare_inverse(job);undo=self.service.approve_inverse(job,inverse['digest'])
        self.assertEqual(self.service.run_once(undo)['state'],'done')
        self.assertTrue(self.clip.exists());self.assertTrue((self.other/'second.mp4').exists())
        with self.app.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM videos').fetchone()[0],2)

    @unittest.skipUnless(os.name=='nt','Windows NTFS archive integration')
    def test_multi_source_copy_archive_and_restore(self):
        job,_=self.run_plan(self.plan())
        archive=self.service.archive.prepare(job);archived=self.service.archive.approve(job,archive['id'],archive['digest'])
        self.assertEqual(self.service.run_once(archived)['state'],'done')
        with self.app.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM organization_archive_roots').fetchone()[0],2)
        inverse=self.service.prepare_inverse(archived);undo=self.service.approve_inverse(archived,inverse['digest'])
        self.assertEqual(self.service.run_once(undo)['state'],'done')
        self.assertTrue(self.clip.exists());self.assertTrue((self.other/'second.mp4').exists())

    def test_changed_query_context_is_not_reinterpreted_and_offline_is_not_skipped(self):
        page=self.query.query({})
        self.other.rename(self.root/'offline-other')
        with self.assertRaises(InventoryQueryChanged):self.plan(snapshot=page['snapshot'])
        current=self.query.query({});plan=self.plan(snapshot=current['snapshot'])
        self.assertEqual(len(plan['entries']),2)
        self.assertTrue(any('source_unavailable' in e['conflicts'] for e in plan['entries']))
        with self.assertRaises(ValueError):self.service.prepare(plan['id'],plan['digest'],[0,1])

    def test_target_collision_across_roots_is_visible_and_revisable(self):
        (self.other/'second.mp4').rename(self.other/'part1.mp4');self.inventory.scan(self.other_device,hashing=True)
        plan=self.plan()
        self.assertTrue(any('planned_collision' in e['conflicts'] for e in plan['entries']))
        revised=self.inventory.revise_plan(plan['id'],plan['digest'],[0,1],{'1':'Other/part1.mp4'})
        self.assertFalse(any(e['conflicts'] for e in revised['entries']))
        self.run_plan(revised)

    def test_selected_range_is_not_expanded_to_other_page(self):
        page=self.query.query({'limit':1});ordinal=page['items'][0]['query_ordinal']
        summary=self.service.planner.selection(page['snapshot'],[ordinal])
        self.assertEqual(summary['total'],1);self.assertEqual(len(summary['devices']),1)
        plan=self.plan(snapshot=page['snapshot'],selected=[ordinal]);self.assertEqual(len(plan['entries']),1)
        self.run_plan(plan)


class QueryPlannerHTTPTests(unittest.TestCase):
    import test_organization_http as fixtures
    setUp=fixtures.OrganizationHTTPTests.setUp
    tearDown=fixtures.OrganizationHTTPTests.tearDown
    request=fixtures.OrganizationHTTPTests.request
    wait_job=fixtures.OrganizationHTTPTests.wait_job
    shutdown=QueryPlannerTests.shutdown

    def test_public_cross_root_selection_plan_and_queue_is_paged(self):
        other=self.root/'other';other.mkdir();(other/'second.mp4').write_bytes(b'synthetic second video')
        device=self.inventory.register(str(other),'Other disk');self.inventory.scan(device,hashing=True)
        status,page=self.request('/api/storage/query?limit=1');self.assertEqual(status,200)
        status,_=self.request('/api/storage/selection',{'snapshot':page['snapshot'],'selected':'all'},False);self.assertEqual(status,403)
        status,summary=self.request('/api/storage/selection',{'snapshot':page['snapshot'],'selected':'all'})
        self.assertEqual(status,200);self.assertEqual(summary['total'],2);self.assertEqual(len(summary['devices']),2)
        status,result=self.request('/api/storage/query-plan',{'snapshot':page['snapshot'],'selected':'all','target':str(self.target)})
        self.assertEqual(status,200,result);plan=result['plan'];self.assertEqual(plan['total'],2);self.assertNotIn('entries',plan)
        status,review=self.request('/api/organization/preview?id='+plan['id']+'&limit=1')
        self.assertEqual(status,200);self.assertEqual(review['total'],2);self.assertEqual(len(review['items']),1)
        status,result=self.request('/api/organization/prepare',{'id':plan['id'],'digest':plan['digest'],'selected':'all','operation':'copy'})
        self.assertEqual(status,200,result)
        status,draft=self.request('/api/organization/draft?id='+result['id']);self.assertEqual(status,200);self.assertEqual(draft['media_count'],2)
        status,result=self.request('/api/organization/approve',{'id':draft['id'],'digest':draft['digest'],'confirmed':True})
        self.assertEqual(status,200,result);self.assertEqual(self.wait_job(result['id'])['state'],'done')
        with self.app.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM videos').fetchone()[0],2)

    def test_stale_selection_returns_409_instead_of_changing_members(self):
        status,page=self.request('/api/storage/query');self.assertEqual(status,200)
        self.inventory.annotate(self.device,'part1.mp4',{'title':'Changed title'})
        status,_=self.request('/api/storage/selection',{'snapshot':page['snapshot'],'selected':'all'});self.assertEqual(status,409)
        status,_=self.request('/api/storage/query-plan',{'snapshot':page['snapshot'],'selected':'all','target':str(self.target)});self.assertEqual(status,409)
        self.assertFalse(self.target.exists())
