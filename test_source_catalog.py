import json
import time
import unittest
from source_catalog import SourceCatalog, NATIVE_SOURCES
from network_checks import NetworkChecks


class SourceCatalogTests(unittest.TestCase):
    def test_inventory_does_not_expose_secrets(self):
        def request(path):
            if path=='health':return {'version':'0.24.1'}
            return {'scraping':{'content_routes':{'censored':['javdb','dmm'],'fc2':['fc2','plugin.test']},'site_config':{'javdb':{'cookie':'private-cookie'}}},'network':{'proxy':'private-proxy'}}
        catalog=SourceCatalog(request,lambda:{'scrape_sources':list(NATIVE_SOURCES),'scrape_engine':'builtin'})
        result=catalog.snapshot()
        self.assertTrue(result['upstream']['connected'])
        self.assertEqual(len([row for row in result['items'] if row['native']]),10)
        self.assertTrue(next(row for row in result['items'] if row['id']=='plugin.test')['amane_enabled'])
        self.assertNotIn('private-cookie',json.dumps(result))
        self.assertNotIn('private-proxy',json.dumps(result))

    def test_offline_service_preserves_native_catalog(self):
        def request(path):raise ValueError('private-token')
        result=SourceCatalog(request,lambda:{}).snapshot()
        self.assertFalse(result['upstream']['connected'])
        self.assertEqual(len(result['items']),24)
        self.assertNotIn('private-token',json.dumps(result))

    def test_external_network_results_preserved(self):
        check=NetworkChecks(lambda:{'scrape_engine':'external'},external_probe=lambda ids:{'items':[{'source_id':'javdb','status':'failed','reason':'login_required','elapsed_ms':12},{'source_id':'dmm','status':'ok','http_status':200,'elapsed_ms':8}]},external_sources=lambda:['javdb','dmm'])
        check.start()
        deadline=time.monotonic()+2
        while check.status()['running'] and time.monotonic()<deadline:time.sleep(.01)
        items=check.status()['items']
        self.assertEqual(items[0]['reason'],'login_required')
        self.assertEqual(items[1]['http_status'],200)


if __name__=='__main__':unittest.main()
