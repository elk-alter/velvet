"""Mount changes keep snapshots; replacement directories never impersonate roots."""
import unittest
import shutil
from pathlib import Path
from unittest.mock import patch
import test_storage_inventory as fixtures


class ReconnectTests(unittest.TestCase):
    setUp=fixtures.StorageTests.setUp
    db=fixtures.StorageTests.db
    inventory=fixtures.StorageTests.inventory

    def registered(self):
        self.probe=patch('storage_inventory.volume_identity',return_value=('synthetic-volume',True));self.probe.start();self.addCleanup(self.probe.stop)
        source=self.root/'Media';source.mkdir();(source/'one.mp4').write_bytes(b'synthetic')
        inventory=self.inventory();device=inventory.register(str(source),'Synthetic disk');inventory.scan(device,hashing=True)
        return inventory,device,source

    def test_same_directory_rename_keeps_snapshot_annotations_and_invalidates_old_preview(self):
        inventory,device,source=self.registered();old=inventory.device(device)
        inventory.annotate(device,'one.mp4',{'title':'Manual suggestion'})
        moved=self.root/'NewMount';source.rename(moved)
        preview=inventory.remap_preview(device,str(moved))
        self.assertFalse(preview['manual_required']);self.assertTrue(preview['directory_matches'])
        inventory.remap(device,str(moved),old['identity'],expected_digest=preview['digest'])
        self.assertEqual(len(inventory.files(device)),1)
        with self.db() as c:self.assertIn('Manual suggestion',c.execute('SELECT payload FROM inventory_annotations').fetchone()[0])
        with self.assertRaises(ValueError):inventory.remap(device,str(moved),old['identity'],expected_digest=preview['digest'])

    def test_same_volume_different_directory_is_rejected_even_with_equal_content(self):
        inventory,device,source=self.registered();other=self.root/'Other';other.mkdir()
        (other/'one.mp4').write_bytes((source/'one.mp4').read_bytes())
        with self.assertRaises(ValueError):inventory.remap_preview(device,str(other))
        with self.assertRaises(ValueError):inventory.remap(device,str(other),inventory.device(device)['identity'])
        self.assertEqual(inventory.device(device)['root'],str(source.resolve()))

    def test_replaced_directory_is_not_online_and_new_registration_keeps_old_snapshot(self):
        inventory,device,source=self.registered();source.rename(self.root/'Retained original')
        source.mkdir();(source/'different.mp4').write_bytes(b'another synthetic file')
        status=inventory.devices()[0]
        self.assertFalse(status['online']);self.assertEqual(status['status'],'directory_changed')
        with self.assertRaises(ValueError):inventory.scan(device)
        with self.assertRaises(ValueError):inventory.register(str(source),'Replacement')
        replacement=inventory.register(str(source),'Replacement',new_location=True)
        self.assertNotEqual(replacement,device)
        inventory.scan(replacement,hashing=True)
        self.assertEqual(len(inventory.files(device)),1);self.assertEqual(len(inventory.files(replacement)),1)
        self.assertEqual(inventory.register(str(source),'Replacement'),replacement)
        preview=inventory.remap_preview(device,str(self.root/'Retained original'))
        inventory.remap(device,preview['root'],preview['previous_identity'],expected_digest=preview['digest'])
        self.assertTrue(all(row['online'] for row in inventory.devices()))

    def test_replaced_root_cannot_plan_even_if_file_properties_and_hash_are_equal(self):
        inventory,device,source=self.registered();retained=self.root/'Original';source.rename(retained);source.mkdir()
        shutil.copy2(retained/'one.mp4',source/'one.mp4')
        plan=inventory.plan(device,str(self.root/'Target'))
        self.assertFalse(plan['source_unchanged']);self.assertIn('device_mismatch',plan['entries'][0]['conflicts'])

    def test_busy_scan_and_destination_replacement_invalidate_confirmation(self):
        inventory,device,source=self.registered();moved=self.root/'NewMount';source.rename(moved)
        preview=inventory.remap_preview(device,str(moved));identity=inventory.device(device)['identity']
        moved.rename(self.root/'Actual');moved.mkdir()
        with self.assertRaises(ValueError):inventory.remap(device,str(moved),identity,expected_digest=preview['digest'])
        with self.db() as c:c.execute("UPDATE inventory_snapshots SET state='running' WHERE device_id=?",(device,))
        with self.assertRaises(ValueError):inventory.remap_preview(device,str(self.root/'Actual'))

    def test_legacy_missing_proof_requires_separate_manual_acknowledgment(self):
        inventory,device,source=self.registered()
        with self.db() as c:c.execute('DELETE FROM storage_root_proofs WHERE device_id=?',(device,))
        preview=inventory.remap_preview(device,str(source));self.assertTrue(preview['manual_required'])
        with self.assertRaises(ValueError):inventory.remap(device,str(source),'synthetic-volume',expected_digest=preview['digest'])
        inventory.remap(device,str(source),'synthetic-volume',expected_digest=preview['digest'],manual_confirmed=True)
        self.assertFalse(inventory.remap_preview(device,str(source))['manual_required'])

    def test_volume_change_during_metadata_read_preserves_the_last_complete_snapshot(self):
        inventory,device,source=self.registered()
        def metadata(path):
            self.probe.stop()
            self.probe=patch('storage_inventory.volume_identity',return_value=('replacement-volume',True));self.probe.start();self.addCleanup(self.probe.stop)
            return {'title':'Should not enter old device'}
        inventory.metadata=metadata
        with self.assertRaises(ValueError):inventory.scan(device,hashing=True)
        self.assertEqual(len(inventory.files(device)),1)
        with self.db() as c:
            latest=c.execute('SELECT id,state FROM inventory_snapshots ORDER BY created DESC LIMIT 1').fetchone()
            self.assertEqual(latest['state'],'interrupted')
            self.assertEqual(c.execute('SELECT COUNT(*) FROM inventory_files WHERE snapshot=?',(latest['id'],)).fetchone()[0],0)

    def test_permission_error_on_one_device_does_not_break_the_device_page(self):
        inventory,device,source=self.registered()
        with patch('storage_inventory.shutil.disk_usage',side_effect=PermissionError('synthetic denial')):
            row=inventory.devices()[0]
        self.assertEqual(row['status'],'permission_denied');self.assertFalse(row['online'])
        self.assertEqual(row['files'],1)

    def test_same_volume_roots_are_separate_and_replacement_volume_gets_new_identity(self):
        inventory,device,source=self.registered();other=self.root/'Other root';other.mkdir()
        other_id=inventory.register(str(other),'Other root');self.assertNotEqual(other_id,device)
        with self.assertRaises(ValueError):inventory.remap_preview(device,str(other))
        def volume(path):return ('replacement-volume',True) if Path(path).resolve()==source.resolve() else ('synthetic-volume',True)
        with patch('storage_inventory.volume_identity',side_effect=volume):
            with self.assertRaises(ValueError):inventory.register(str(source),'Replacement')
            replacement=inventory.register(str(source),'Replacement',new_location=True)
            self.assertNotEqual(replacement,device)
            states={row['id']:row for row in inventory.devices()}
            self.assertEqual(states[device]['status'],'identity_changed');self.assertTrue(states[replacement]['online'])
        self.assertEqual(len(inventory.files(device)),1)

    def test_legacy_schema_upgrade_is_idempotent_and_does_not_invent_proof(self):
        inventory,device,source=self.registered()
        with self.db() as c:c.execute('DROP TABLE storage_root_proofs')
        inventory.ensure();inventory.ensure()
        self.assertEqual(len(inventory.files(device)),1)
        self.assertFalse(inventory.devices()[0]['directory_verified'])
        self.assertTrue(inventory.remap_preview(device,str(source))['manual_required'])
