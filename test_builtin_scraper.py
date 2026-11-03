import asyncio
import tempfile
import threading
import time
import unittest
import importlib.util
import os
import json
from unittest.mock import patch
from pathlib import Path
from builtin_scraper import Engine, merge_results, validate_code, source_types
from scrapers.compat import SearchQuery


class BuiltinTests(unittest.TestCase):
    def test_artwork_validation_and_atomic_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = Engine(tmp, lambda: {})
            target = Path(tmp) / 'poster.jpg'; target.write_bytes(b'old')
            with patch('builtin_scraper.fetch_bytes', return_value=(b'<html>blocked</html>', 'text/html')):
                self.assertFalse(engine.cache_artwork('https://example.invalid/image', target))
            self.assertEqual(target.read_bytes(), b'old')
            with patch('builtin_scraper.fetch_bytes', return_value=(b'\xff\xd8fixture', 'image/jpeg')):
                self.assertTrue(engine.cache_artwork('https://example.invalid/image', target))
            self.assertEqual(target.read_bytes(), b'\xff\xd8fixture')
            self.assertFalse(target.with_suffix('.part').exists())

    def test_library_sync_preserves_source_and_locked_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_env = os.environ.get('VELVET_DATA')
            os.environ['VELVET_DATA'] = str(Path(tmp) / 'data')
            try:
                spec = importlib.util.spec_from_file_location('velvet_builtin_test', Path(__file__).with_name('app.py'))
                app = importlib.util.module_from_spec(spec); spec.loader.exec_module(app)
            finally:
                if original_env is None: os.environ.pop('VELVET_DATA', None)
                else: os.environ['VELVET_DATA'] = original_env
            source = Path(tmp) / 'ABC-123.mp4'; source.write_bytes(b'unchanged-video')
            stamp = source.stat().st_mtime_ns
            app.DATA.joinpath('settings.json').write_text(json.dumps({'scrape_engine': 'builtin'}))
            with app.db() as db:
                video_id = db.execute('INSERT INTO videos(path,code,title,locked_fields) VALUES(?,?,?,?)', (str(source), 'ABC-123', 'Manual', '["title"]')).lastrowid
            engine = Engine(app.DATA, app.config, lambda code: (merge_results(code, {'fixture': {'title': 'Remote', 'tags': ['Tag'], 'actors': ['Actor'],'extrafanart':['https://example.invalid/still.jpg']}}, ['fixture']), []))
            app.external_amane_request=lambda *args,**kwargs:self.fail('native workflow must not call Amane')
            def download_still(url,target,mode,referer):
                self.assertEqual(mode,'builtin');target.write_bytes(b'\xff\xd8fixture');return True
            app.ARTWORK.fetcher=download_still
            app.BUILTIN_ENGINES[str(app.DATA)] = engine
            submitted = app.submit_scrapes([video_id])
            self.assertEqual(submitted['submitted'], 1)
            with engine.connect() as db: task_id = db.execute('SELECT id FROM tasks').fetchone()[0]
            self.assertEqual(self.wait(engine, task_id)['status'], 'done')
            app.poll_scrapes(); app.sync_amane()
            with app.db() as db:
                row = db.execute('SELECT * FROM videos WHERE id=?', (video_id,)).fetchone()
                self.assertEqual(row['title'], 'Manual')
                self.assertEqual(row['actors'], 'Actor')
                self.assertIn('Tag', row['auto_tags'])
                self.assertGreater(row['amane_id'], 0)
            self.assertEqual(source.read_bytes(), b'unchanged-video')
            self.assertEqual(source.stat().st_mtime_ns, stamp)
            self.assertEqual(app.ARTWORK.manifest(video_id)['stills'],1)

    def wait(self, engine, task_id):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            result = engine.request('tasks/' + str(task_id))
            if result['status'] not in ('queued', 'running'):
                # Wait for worker cleanup before temporary directory removal on Windows.
                with engine.worker_lock:
                    pass
                return result
            time.sleep(.01)
        self.fail('worker did not finish')

    def test_queue_dedup_cancel_persistence(self):
        entered, release = threading.Event(), threading.Event()
        def fetch(code):
            entered.set(); release.wait(3)
            return {'number': code, 'title': 'Synthetic'}, []
        with tempfile.TemporaryDirectory() as tmp:
            engine = Engine(tmp, lambda: {}, fetch)
            task = engine.request('tasks', {'type': 'scrape', 'number': 'abc-123'})
            self.assertTrue(entered.wait(2))
            duplicate = engine.request('tasks', {'type': 'scrape', 'number': 'ABC-123'})
            self.assertEqual(task['id'], duplicate['id'])
            engine.request('tasks/batch', {'action': 'cancel', 'task_ids': [task['id']]})
            release.set()
            self.assertEqual(self.wait(engine, task['id'])['status'], 'cancelled')
            self.assertEqual(engine.request('metadata')['total'], 0)
            second = engine.request('tasks', {'type': 'scrape', 'number': 'ABC-123'})
            self.assertEqual(self.wait(engine, second['id'])['status'], 'done')
            other = Engine(tmp, lambda: {}, fetch)
            self.assertEqual(other.request('metadata')['items'][0]['title'], 'Synthetic')
            with other.worker_lock:
                pass

    def test_recovery_and_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = Engine(tmp, lambda: {}, lambda code: ({}, [{'site': 'test', 'outcome': 'failed', 'reason': 'network'}]))
            with engine.connect() as db:
                task_id = db.execute("INSERT INTO tasks(code,status,created) VALUES('ABC-123','running',0)").lastrowid
            engine = Engine(tmp, lambda: {}, engine.fetcher)
            engine.start()
            row = self.wait(engine, task_id)
            self.assertEqual(row['status'], 'failed')
            self.assertEqual(row['result']['outcomes'][0]['reason'], 'network')
            self.assertEqual(engine.request('metadata')['total'], 0)

    def test_merge_and_invalid_input(self):
        result = merge_results('ABC-123', {'a': {'title': 'First', 'plot': ''}, 'b': {'title': 'Second', 'plot': 'Filled'}}, ['a', 'b'])
        self.assertEqual(result['title'], 'First')
        self.assertEqual(result['plot'], 'Filled')
        self.assertEqual(result['field_sources']['plot'], 'b')
        for value in ('../file', '', 'https://invalid', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_code(value)

    def test_adapted_parsers(self):
        types = source_types()
        fixtures = {
            'jav321': '<div class="panel-heading"><h3>ABC-123 Synthetic</h3></div><div class="panel-body"><img src="https://example.invalid/cover.jpg"></div>',
            'javbus': '<h3>ABC-123 Synthetic</h3><span class="header">識別碼:</span><span>ABC-123</span><a class="bigImage" href="/cover/test_b.jpg"></a>',
            'fc2': '<div class="items_article_MainitemThumb"><span><img alt="Synthetic" src="https://example.invalid/poster.jpg"></span></div>',
        }
        class Client:
            async def get_html(self, *args, **kwargs):
                return self.html
        for name, html in fixtures.items():
            client = Client(); client.html = html
            crawler = types[name](client)
            url = 'https://example.invalid/article/12345/' if name == 'fc2' else 'https://example.invalid/video/ABC-123'
            result = asyncio.run(crawler._scrape(url))
            self.assertEqual(result.values['number'], 'FC2-PPV-12345' if name == 'fc2' else 'ABC-123')
            self.assertTrue(result.values['title'])
        client.html = '<html>not found</html>'
        self.assertIsNone(asyncio.run(types['javbus'](client)._scrape('https://example.invalid/ABC-123')))

    def test_additional_source_parsers_and_identity(self):
        types=source_types()
        fixtures={
            'fc2club': ('https://example.invalid/html/FC2-123.html','<div class="show-top-grids"><h3>Synthetic</h3></div>','FC2-123'),
            'fc2ppvdb': ('https://example.invalid/articles/123','<h2 class="title">Synthetic</h2>','FC2-123'),
            'freejavbt': ('https://example.invalid/ABC-123','<h1>ABC-123 Synthetic</h1>','ABC-123'),
            'airav': ('https://example.invalid/video','<li>番号<span>ABC-123</span></li><div class="video-title"><h1>ABC-123 Synthetic</h1></div>','ABC-123'),
            'xcity': ('https://example.invalid/titles/1','<h1>Synthetic</h1><span class="kopin">ABC-123</span>','ABC-123'),
        }
        class Client:
            async def get_html(self,*args,**kwargs):return self.html
        for name,(url,html,number) in fixtures.items():
            with self.subTest(source=name):
                client=Client();client.html=html
                result=asyncio.run(types[name](client)._scrape(url))
                self.assertIsNotNone(result)
                self.assertEqual(result.values['number'],number)


if __name__ == '__main__':
    unittest.main()


class EngineResourceTests(unittest.TestCase):
    def test_paused_engine_does_not_claim_queued_work(self):
        with tempfile.TemporaryDirectory() as temp:
            engine=Engine(temp,lambda:{})
            engine.paused.set()
            with engine.connect() as db:db.execute("INSERT INTO tasks(code,status,created) VALUES('EXAMPLE-001','queued',1)")
            engine.start()
            self.assertFalse(engine.worker_lock.locked())
            self.assertEqual(engine.resource_summary()['queued'],1)
            self.assertTrue(engine.resource_summary()['paused'])

    def test_rate_limit_cooldown_survives_engine_recreation(self):
        from unittest.mock import AsyncMock
        from scrapers.compat import RequestError
        class FakeSource:
            def __init__(self,client):pass
            @staticmethod
            def profile():return type('Profile',(),{'base_url':'https://example.invalid'})()
            async def fetch(self,query):raise RequestError('fixture',http_status=429)
        with tempfile.TemporaryDirectory() as temp,patch('builtin_scraper.source_types',return_value={'javbus':FakeSource}),patch('builtin_scraper.asyncio.sleep',new_callable=AsyncMock):
            config=lambda:{'scrape_sources':['javbus']}
            engine=Engine(temp,config)
            self.assertEqual(engine.fetch('EXAMPLE-001')[1][0]['reason'],'rate_limited')
            resumed=Engine(temp,config)
            self.assertEqual(resumed.fetch('EXAMPLE-001')[1][0]['reason'],'site_cooldown')
            self.assertEqual(len(resumed.resource_summary()['cooldowns']),1)
