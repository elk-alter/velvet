"""Reconnection uses recorded IDs, explicit confirmation and current proofs."""
import unittest
import test_organization_http as fixtures


class ReconnectHTTPTests(unittest.TestCase):
    setUp=fixtures.OrganizationHTTPTests.setUp
    tearDown=fixtures.OrganizationHTTPTests.tearDown
    shutdown=fixtures.OrganizationHTTPTests.shutdown
    request=fixtures.OrganizationHTTPTests.request

    def test_reconnect_preserves_snapshot_and_requires_token_confirmation_and_digest(self):
        self.source.rename(self.root/'New mount');target=self.root/'New mount'
        body={'id':self.device,'path':str(target)}
        self.assertEqual(self.request('/api/storage/remap-preview',body,token=False)[0],403)
        status,preview=self.request('/api/storage/remap-preview',body);self.assertEqual(status,200,preview)
        body.update(identity=preview['previous_identity'],digest=preview['digest'],manual_confirmed=True)
        self.assertEqual(self.request('/api/storage/remap',body)[0],400)
        self.assertEqual(self.request('/api/storage/remap',dict(body,confirmed=True),token=False)[0],403)
        self.assertEqual(self.request('/api/storage/remap',dict(body,confirmed=True,digest='stale'))[0],400)
        self.assertEqual(self.request('/api/storage/remap',dict(body,confirmed=True))[0],200)
        status,context=self.request('/api/storage/device?id='+self.device)
        self.assertEqual(status,200);self.assertTrue(context['device']['online']);self.assertEqual(context['device']['files'],1)
        self.assertEqual(self.request('/api/storage/device?id=unknown')[0],400)
        self.app.NAS=True
        try:
            from http.client import HTTPConnection
            connection=HTTPConnection('127.0.0.1',self.server.server_address[1],timeout=5)
            connection.request('GET','/api/storage/device?id='+self.device,headers={'Host':'127.0.0.1:'+str(self.app.RUNTIME.port)})
            response=connection.getresponse();response.read();self.assertEqual(response.status,401);connection.close()
        finally:self.app.NAS=False

    def test_old_registration_needs_separate_manual_confirmation(self):
        with self.app.db() as c:c.execute('DELETE FROM storage_root_proofs WHERE device_id=?',(self.device,))
        status,preview=self.request('/api/storage/remap-preview',{'id':self.device,'path':str(self.source)})
        self.assertEqual(status,200);self.assertTrue(preview['manual_required'])
        body={'id':self.device,'path':str(self.source),'identity':preview['previous_identity'],'digest':preview['digest'],'confirmed':True}
        self.assertEqual(self.request('/api/storage/remap',body)[0],400)
        self.assertEqual(self.request('/api/storage/remap',dict(body,manual_confirmed=True))[0],200)
