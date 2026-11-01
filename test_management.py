import importlib.util
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from unittest.mock import patch
from network_checks import probe, NetworkChecks
from scrapers.compat import RequestError
from scrapers.block import classify_block
from builtin_scraper import merge_results


class NetworkTests(unittest.TestCase):
    def test_response_classification_and_isolation(self):
        cases = [(b'<html>normal</html>', 'ok', ''), (b'cloudflare just a moment', 'failed', 'cloudflare_challenge'), (b'', 'failed', 'empty_response'), (b'not available in your region', 'failed', 'geo_restricted')]
        for body, status, reason in cases:
            with self.subTest(reason=reason):
                result = probe('javbus', '', lambda *args, **kwargs: (body, 'text/html', 200))
                self.assertEqual((result['status'], result['reason']), (status, reason))
        def fail(*args, **kwargs):
            raise RequestError('redacted', 429)
        self.assertEqual(probe('javbus', '', fail)['reason'], 'rate_limited')
        self.assertEqual(probe('unknown', '', fail)['status'], 'skipped')

    def test_shared_block_classifier_and_field_priority(self):
        self.assertEqual(classify_block('age verification'), 'age_verification')
        result = merge_results('ABC-1', {'javbus': {'title': 'A', 'plot': 'Fill'}, 'jav321': {'title': 'B', 'plot': ''}}, ['javbus', 'jav321'], {'title': ['jav321'], 'plot': ['jav321']})
        self.assertEqual((result['title'], result['plot']), ('B', 'Fill'))

    def test_disabled_source_and_partial_results(self):
        checks = NetworkChecks(lambda: {'scrape_engine': 'builtin', 'scrape_sources': ['javbus'], 'scrape_proxy': 'http://127.0.0.1:10808'})
        with patch('network_checks.probe', side_effect=lambda s,p: {'source_id': s, 'status': 'failed', 'reason': 'timeout', 'elapsed_ms': 1}):
            self.assertTrue(checks.start(['javbus', 'jav321', 'unknown']))
            deadline = time.monotonic()+2
            while checks.status()['running'] and time.monotonic()<deadline: time.sleep(.01)
            result = {row['source_id']: row for row in checks.status()['items']}
            self.assertEqual(result['javbus']['reason'], 'timeout')
            self.assertEqual(result['jav321']['reason'], 'disabled')
            self.assertEqual(result['unknown']['reason'], 'unknown_source')
        with self.assertRaises(ValueError): checks.start('javbus')


class LibraryManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        with patch.dict(os.environ, {'VELVET_DATA': str(self.root/'data')}):
            spec = importlib.util.spec_from_file_location('velvet_management_test', Path(__file__).with_name('app.py'))
            self.app = importlib.util.module_from_spec(spec);spec.loader.exec_module(self.app)
        self.app.binary = lambda name: None
        self.media = self.root/'media';self.media.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_library_rules_offline_and_scoped_scan(self):
        libraries = self.app.LIBRARIES
        a=self.media/'ABC-1.mp4';a.write_bytes(b'keep-video')
        b=self.media/'sample.mp4';b.write_bytes(b'exclude-video')
        libraries.save({'path':str(self.media),'name':'Test','automation':'off','exclude_patterns':['sample*']})
        self.app.scan(automatic=True)
        with self.app.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM videos').fetchone()[0],0)
        self.app.scan(folder_path=str(self.media))
        with self.app.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM videos').fetchone()[0],1)
        self.assertEqual(libraries.list()[0]['file_count'],1)
        self.assertEqual(libraries.files(str(self.media),status='pending',search='ABC')['total'],1)
        self.assertEqual(libraries.files(str(self.media),status='matched')['total'],0)
        with self.assertRaises(ValueError):libraries.files(str(self.media),limit=1000)
        self.assertEqual(a.read_bytes(),b'keep-video')
        other=self.root/'other';other.mkdir();(other/'DEF-2.mp4').write_bytes(b'other')
        libraries.save({'path':str(other),'automation':'monitor'})
        self.app.scan(folder_path=str(other))
        self.assertEqual(sum(row['file_count'] for row in libraries.list()),2)
        offline=self.root/'offline';self.media.rename(offline)
        self.app.scan(folder_path=str(self.media))
        row=next(row for row in libraries.list() if row['path']==str(self.media))
        self.assertFalse(row['online']);self.assertEqual(row['missing_count'],0)
        self.assertTrue(row['last_error'])
        with self.assertRaises(ValueError):libraries.save({'path':str(other),'automation':'invalid'})
        nested=other/'nested';nested.mkdir()
        with self.assertRaises(ValueError):libraries.save({'path':str(nested)})

    def test_automation_policy_and_removed_library(self):
        video=self.media/'ABC-1.mp4';video.write_bytes(b'video')
        self.app.LIBRARIES.save({'path':str(self.media),'automation':'monitor'})
        self.app.scan()
        self.app.submit_scrapes=lambda ids:self.fail('monitor-only library must not submit')
        self.app.auto_scrape_tick()
        with self.app.db() as db:db.execute('DELETE FROM folders')
        self.app.auto_scrape_tick()

    def test_api_validation_and_network_status(self):
        app=self.app
        server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def request(path,data=None):
            headers={'Host':'127.0.0.1:8765','X-Velvet-Token':app.TOKEN}
            req=Request('http://127.0.0.1:'+str(server.server_port)+path,headers=headers,data=json.dumps(data).encode() if data is not None else None)
            with urlopen(req,timeout=5) as response:return json.load(response)
        try:
            request('/api/libraries',{'path':str(self.media),'name':'Fixture','automation':'monitor'})
            self.assertEqual(request('/api/libraries')['items'][0]['name'],'Fixture')
            request('/api/scraping-options',{'sources':['javbus'],'field_priority':{'title':['javbus']}})
            self.assertEqual(app.config()['scrape_sources'],['javbus'])
            for data in ({'sources':[{}]}, {'sources':['javbus'],'field_priority':{'title':['unknown']}}):
                with self.assertRaises(HTTPError):request('/api/scraping-options',data)
            self.assertFalse(request('/api/network')['running'])
            with self.assertRaises(HTTPError):request('/api/network/check',{'source_ids':'invalid'})
        finally:
            server.shutdown();server.server_close();thread.join()


if __name__ == '__main__':unittest.main()
