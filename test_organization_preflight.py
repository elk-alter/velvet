"""Preflight must reject unsafe plans before identity registration or file writes."""
import unittest
import os
import shutil
import subprocess
import json
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from organization_preflight import PreflightError,validate_manifest
from organization_executor import CopyExecutor
import test_organization_jobs as fixtures


class OrganizationPreflightTests(unittest.TestCase):
    db=fixtures.OrganizationJobTests.db
    setUp=fixtures.OrganizationJobTests.setUp
    draft=fixtures.OrganizationJobTests.draft

    def test_long_target_path_is_rejected_before_registration(self):
        target=self.target.joinpath(*(['SyntheticDirectory'+str(i)+'x'*25 for i in range(6)]),'one.mp4')
        registered=[]
        with self.assertRaises(ValueError):
            self.jobs.prepare([{'source':str(self.source),'destination':str(target),'operation':'copy'}],self.scope,self.target,register=lambda *args:registered.append(args))
        self.assertEqual(registered,[]);self.assertFalse(self.target.exists())

    def entry(self,target=None,source=None,operation='copy'):
        return {'source':str(source or self.source),'destination':str(target or self.target/'one.mp4'),'operation':operation,'video_id':1}

    def test_existing_case_and_unicode_aliases_are_not_overwritten(self):
        self.target.mkdir()
        for actual,requested in [('ONE.mp4','one.mp4'),('Cafe\u0301.mp4','Caf\u00e9.mp4')]:
            with self.subTest(requested=requested):
                foreign=self.target/actual;foreign.write_bytes(b'external synthetic media')
                with self.assertRaises(PreflightError) as caught:self.draft(requested)
                self.assertIn('target_exists',caught.exception.codes)
                self.assertEqual(foreign.read_bytes(),b'external synthetic media');foreign.unlink()

    def test_planned_unicode_aliases_are_rejected_as_one_collision(self):
        other=self.scope/'two.mp4';other.write_bytes(b'second media')
        entries=[self.entry(self.target/'Caf\u00e9.mp4'),self.entry(self.target/'Cafe\u0301.mp4',other)]
        with self.assertRaises(PreflightError) as caught:self.jobs.prepare(entries,self.scope,self.target)
        self.assertIn('planned_collision',caught.exception.codes);self.assertFalse(self.target.exists())

    def test_parent_directory_alias_and_non_directory_are_preflighted(self):
        self.target.mkdir();alias=self.target/'Cafe\u0301';alias.mkdir();(alias/'sentinel.txt').write_bytes(b'unchanged')
        with self.assertRaises(PreflightError) as caught:self.draft('Caf\u00e9/one.mp4')
        self.assertIn('parent_alias_collision',caught.exception.codes)
        blocked=self.target/'blocked';blocked.write_bytes(b'not a directory')
        with self.assertRaises(PreflightError) as caught:self.draft('blocked/one.mp4')
        self.assertIn('target_parent_not_directory',caught.exception.codes)
        self.assertEqual((alias/'sentinel.txt').read_bytes(),b'unchanged')

    def test_reserved_components_and_readonly_volume_fail_before_registration(self):
        with self.assertRaises(PreflightError) as caught:self.draft('CON/one.mp4')
        self.assertIn('invalid_component',caught.exception.codes)
        with patch('organization_preflight.probe_volume',return_value={'filesystem':'NTFS','read_only':True}):
            with self.assertRaises(PreflightError) as caught:self.draft()
        self.assertIn('target_read_only',caught.exception.codes);self.assertFalse(self.target.exists())

    def test_fat32_limit_is_checked_for_each_selected_resource(self):
        entry=self.entry();entry['fingerprint']={'size':4294967296};entry['role']='companion'
        with patch('organization_preflight.probe_volume',return_value={'filesystem':'FAT32','read_only':False}):
            with self.assertRaises(PreflightError) as caught:validate_manifest([entry],lambda path:'synthetic-volume')
        self.assertIn('fat32_file_limit',caught.exception.codes)

    def test_space_is_aggregated_for_videos_and_companions_on_same_volume(self):
        resource=self.scope/'one.srt';resource.write_bytes(b'synthetic subtitle')
        entries=[self.entry(),self.entry(self.target/'one.srt',resource)]
        available=100*1024*1024+self.source.stat().st_size+resource.stat().st_size-1
        with patch('organization_preflight.shutil.disk_usage',return_value=SimpleNamespace(free=available)):
            with self.assertRaises(PreflightError) as caught:self.jobs.prepare(entries,self.scope,self.target)
        self.assertIn('insufficient_space',caught.exception.codes);self.assertFalse(self.target.exists())

    def test_approval_rechecks_permissions_after_preview(self):
        draft=self.draft()
        with patch('organization_preflight.directory_access',return_value=False):
            with self.assertRaises(PreflightError) as caught:self.jobs.approve(draft['id'],draft['digest'])
        self.assertIn('target_permission_denied',caught.exception.codes)
        with self.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0],0)

    def test_copy_rechecks_access_before_any_temporary_file(self):
        draft=self.draft();job=self.jobs.approve(draft['id'],draft['digest']);bound=[]
        with patch('organization_preflight.directory_access',return_value=False):
            result=CopyExecutor(self.jobs,lambda *args:bound.append(args)).run(job)
        self.assertEqual(result['state'],'failed');self.assertEqual(result['steps'][0]['state'],'pending')
        self.assertIn('target_permission_denied',result['steps'][0]['details']['preflight'])
        self.assertFalse(self.target.exists());self.assertEqual(bound,[])
        self.assertEqual(self.source.read_bytes(),b'synthetic media')

    def test_alias_appearing_after_copy_verification_blocks_publication(self):
        draft=self.draft('Caf\u00e9.mp4');job=self.jobs.approve(draft['id'],draft['digest'])
        foreign=self.target/'Cafe\u0301.mp4'
        def occupy(stage,*args):
            if stage=='verified':foreign.write_bytes(b'external synthetic file')
        result=CopyExecutor(self.jobs,lambda *args:'not expected',occupy).run(job)
        self.assertNotEqual(result['state'],'done')
        self.assertIn('target_exists',result['steps'][0]['details']['preflight'])
        self.assertEqual(foreign.read_bytes(),b'external synthetic file')
        self.assertEqual(self.source.read_bytes(),b'synthetic media')
        self.assertNotIn('Caf\u00e9.mp4',[p.name for p in self.target.iterdir()])

    def test_move_requires_delete_right_but_copy_and_keep_do_not(self):
        with patch('organization_preflight.delete_access',return_value=False):
            self.draft()
            with self.assertRaises(PreflightError) as caught:self.jobs.prepare([self.entry(operation='move')],self.scope,self.target)
        self.assertIn('source_permission_denied',caught.exception.codes)
        with patch('organization_preflight.directory_access',side_effect=AssertionError('keep must not check write permissions')):
            draft=self.jobs.prepare([self.entry(self.source,operation='keep')],self.scope,self.scope)
        self.assertTrue(draft['payload']['preflight']['files_unchanged'])

    def test_move_reserves_metadata_space_without_requiring_another_full_copy(self):
        with patch('organization_preflight.shutil.disk_usage',return_value=SimpleNamespace(free=17*1024*1024)):
            draft=self.jobs.prepare([self.entry(operation='move')],self.scope,self.target)
            with self.assertRaises(PreflightError):self.draft()
        self.assertEqual(draft['payload']['preflight']['targets'][0]['required'],0)
        self.assertEqual(draft['payload']['preflight']['targets'][0]['reserve'],16*1024*1024)

    def test_linked_destination_is_rejected_without_following_it_for_writes(self):
        self.target.mkdir();linked=self.target/'linked';external=self.root/'external';external.mkdir()
        try:linked.symlink_to(external,target_is_directory=True)
        except OSError:
            engine=shutil.which('pwsh') or shutil.which('powershell.exe')
            if os.name!='nt' or not engine:self.skipTest('No supported link creation capability')
            script='$ErrorActionPreference="Stop";$r=[Console]::In.ReadToEnd() | ConvertFrom-Json;New-Item -ItemType Junction -Path $r.link -Target $r.target | Out-Null'
            result=subprocess.run([engine,'-NoProfile','-NonInteractive','-Command',script],input=json.dumps({'link':str(linked),'target':str(external)}),capture_output=True,text=True,timeout=20)
            self.assertEqual(result.returncode,0,'Synthetic NTFS junction creation failed')
        try:
            with self.assertRaises(ValueError):self.draft('linked/one.mp4')
            from storage_inventory import StorageInventory
            inventory=StorageInventory(self.db,lambda p:{'title':p.stem},lambda:[])
            device=inventory.register(str(self.scope),'Synthetic scope');inventory.scan(device)
            with self.assertRaisesRegex(ValueError,'Linked'):inventory.plan(device,str(linked))
            self.assertEqual(list(external.iterdir()),[])
            self.assertEqual(self.source.read_bytes(),b'synthetic media')
        finally:
            if linked.is_symlink():linked.unlink()
            else:linked.rmdir()

    def test_generated_temporary_names_are_included_in_path_budget(self):
        from organization_preflight import path_units
        target=self.target/('a'*(180-path_units(self.target)-1))/'one.mp4'
        self.assertLess(len(str(target)),240)
        with self.assertRaises(PreflightError) as caught:self.jobs.prepare([self.entry(target)],self.scope,self.target)
        self.assertIn('temporary_path_too_long',caught.exception.codes)
        self.assertFalse(self.target.exists())

    def test_source_read_access_is_required_even_for_keep(self):
        with patch('organization_preflight.read_access',return_value=False):
            for operation,target in [('copy',self.target/'one.mp4'),('keep',self.source)]:
                with self.subTest(operation=operation),self.assertRaises(PreflightError) as caught:
                    self.jobs.prepare([self.entry(target,operation=operation)],self.scope,self.scope if operation=='keep' else self.target)
                self.assertIn('source_read_denied',caught.exception.codes)

    @unittest.skipUnless(os.name=='nt' and (shutil.which('pwsh') or shutil.which('powershell.exe')),'Windows NTFS ACL integration')
    def test_real_ntfs_acl_denial_is_detected_without_permission_probe_writes(self):
        from storage_inventory import volume_details
        if volume_details(self.root)['filesystem']!='NTFS':self.skipTest('NTFS is required for actual DACL verification')
        self.target.mkdir()
        script="""
$ErrorActionPreference='Stop'
$request=[Console]::In.ReadToEnd() | ConvertFrom-Json
$acl=Get-Acl -LiteralPath $request.path
if($request.action -eq 'deny') {
 $original=$acl.Sddl
 $sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User
 $right=[System.Enum]::Parse([System.Security.AccessControl.FileSystemRights],$request.right)
 $rule=[System.Security.AccessControl.FileSystemAccessRule]::new($sid,$right,[System.Security.AccessControl.AccessControlType]::Deny)
 $acl.AddAccessRule($rule)
 Set-Acl -LiteralPath $request.path -AclObject $acl
 @{sddl=$original} | ConvertTo-Json -Compress
} else {
 $acl.SetSecurityDescriptorSddlForm($request.sddl)
 Set-Acl -LiteralPath $request.path -AclObject $acl
 '{}'
}
"""
        def acl(request):
            result=subprocess.run([shutil.which('pwsh') or shutil.which('powershell.exe'),'-NoProfile','-NonInteractive','-Command',script],input=json.dumps(request),capture_output=True,text=True,timeout=20)
            if result.returncode:raise AssertionError('Synthetic ACL setup/restore failed; no credential or private descriptor is printed')
            return json.loads(result.stdout)
        for path,right,code in [(self.target,'WriteData','target_permission_denied'),(self.source,'ReadData','source_read_denied')]:
            with self.subTest(right=right):
                # Both paths were created by this fixture and belong to its temporary root.
                self.assertTrue(path.is_relative_to(self.root))
                original=acl({'action':'deny','path':str(path),'right':right})['sddl']
                try:
                    with self.assertRaises(PreflightError) as caught:self.draft()
                    self.assertIn(code,caught.exception.codes)
                finally:acl({'action':'restore','path':str(path),'sddl':original})
        self.draft()
        self.assertEqual(list(self.target.iterdir()),[])
        self.assertEqual(self.source.read_bytes(),b'synthetic media')

    def test_directory_listing_is_shared_and_indexed_for_a_large_batch(self):
        from organization_preflight import path_conflicts
        from collections import Counter
        self.target.mkdir()
        for i in range(200):(self.target/f'existing-{i}.txt').write_bytes(b'sentinel')
        scanned=[];original=os.scandir
        def scan(path):scanned.append(str(path));return original(path)
        cache={};destinations=set()
        with patch('organization_preflight.os.scandir',side_effect=scan):
            for i in range(200):self.assertEqual(path_conflicts(self.target/f'new-{i}.mp4',destinations,cache=cache),[])
        self.assertTrue(scanned);self.assertTrue(all(count==1 for count in Counter(scanned).values()))
        self.assertEqual(len(list(self.target.iterdir())),200)

    def test_name_validation_does_not_rewrite_legal_existing_hidden_directories(self):
        target=self.root/'.existing-library'/'copies';target.parent.mkdir()
        draft=self.jobs.prepare([self.entry(target/'one.mp4')],self.scope,target)
        self.assertEqual(draft['payload']['entries'][0]['destination'],str(target/'one.mp4'))
        self.assertFalse(target.exists())

    def test_path_length_uses_utf16_units_including_non_bmp_names(self):
        from organization_preflight import path_conflicts,path_units,valid_component
        target=self.target/('\U0001f3ac'*100)/'one.mp4'
        self.assertLess(len(str(target)),240);self.assertGreater(path_units(target),240)
        self.assertIn('long_path',path_conflicts(target,set()))
        self.assertTrue(valid_component('.ordinary-hidden'))
        self.assertFalse(valid_component('trailing.'));self.assertFalse(valid_component('CON.mp4'))

    def test_preverified_source_proof_is_not_silently_replaced_by_current_state(self):
        from media_locations import fingerprint
        stamp=fingerprint(self.source)
        for proof in ({'source_device':'old-volume'},{'source_identity':[-1,-1]},{'fingerprint':dict(stamp,sha256='0'*64)}):
            with self.subTest(proof=list(proof)),self.assertRaises(ValueError):
                self.jobs.prepare([self.entry()|proof],self.scope,self.target)
        with self.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM organization_drafts').fetchone()[0],0)
        self.assertFalse(self.target.exists())
