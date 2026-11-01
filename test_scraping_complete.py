import asyncio
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from contextlib import contextmanager
from unittest.mock import patch,AsyncMock
from urllib.request import Request
from artwork_store import ArtworkStore,still_urls
from builtin_scraper import Engine,source_types
from scrapers.compat import MediaMetadata,CrawlerProfile,SourceRedirect


class IndependentScrapingTests(unittest.TestCase):
    def test_source_cache_and_forced_refresh_without_amane(self):
        calls=[]
        class Source:
            @classmethod
            def profile(cls):return CrawlerProfile('javbus','https://example.invalid')
            def __init__(self,client):pass
            async def fetch(self,query):calls.append(query.number);return MediaMetadata(number=query.number,title='Synthetic',extrafanart=['https://example.invalid/still.jpg'])
        with tempfile.TemporaryDirectory() as tmp:
            cfg={'scrape_sources':['javbus']};engine=Engine(tmp,lambda:cfg)
            with patch('builtin_scraper.source_types',return_value={'javbus':Source}),patch('builtin_scraper.asyncio.sleep',new_callable=AsyncMock):
                first,outcomes=engine.fetch('ABC-123');self.assertEqual(len(calls),1)
                second,outcomes=engine.fetch('ABC-123');self.assertTrue(outcomes[0]['cached']);self.assertEqual(len(calls),1)
                engine.current_use_cache=False;engine.fetch('ABC-123');self.assertEqual(len(calls),2)
                engine.current_use_cache=True;cfg['scrape_site_config']={'javbus':{'timeout':10}}
                engine.fetch('ABC-123');self.assertEqual(len(calls),3)
                self.assertEqual(still_urls(first),['https://example.invalid/still.jpg'])

    def test_new_source_parsers_and_login_detection(self):
        types=source_types()
        class Client:
            async def get_html(self,*args,**kwargs):return self.html
        client=Client();client.html='<a class="button is-white copy-to-clipboard" data-clipboard-text="ABC-123"></a><h2 class="title is-4"><strong class="current-title">Synthetic</strong></h2><div class="tile-images preview-images"><a class="tile-item" href="https://example.invalid/still.jpg"></a></div>'
        result=asyncio.run(types['javdb'](client)._scrape('https://example.invalid/v/1'))
        self.assertEqual(result.values['number'],'ABC-123');self.assertEqual(len(result.values['extrafanart']),1)
        client.html='<form action="/user_sessions"><input type="password"></form>'
        with self.assertRaises(ValueError) as error:asyncio.run(types['javdb'](client)._scrape('https://example.invalid/v/1'))
        self.assertEqual(error.exception.reason,'login_required')
        client.html='<div id="video_id"><table><tr><td class="text">ABC-123</td></tr></table></div><h3 class="post-title"><a>Synthetic</a></h3>'
        result=asyncio.run(types['javlibrary'](client)._scrape('https://example.invalid/1'))
        self.assertEqual(result.values['number'],'ABC-123')

    def test_redirect_does_not_forward_credentials(self):
        request=Request('https://source.invalid/a',headers={'Cookie':'secret=value','Authorization':'Bearer secret'})
        redirected=SourceRedirect().redirect_request(request,None,302,'Found',{},'https://other.invalid/b')
        self.assertIsNone(redirected.get_header('Cookie'));self.assertIsNone(redirected.get_header('Authorization'))
        with self.assertRaises(ValueError):SourceRedirect().redirect_request(request,None,302,'Found',{},'http://source.invalid/b')


class ArtworkTests(unittest.TestCase):
    def test_durable_images_retry_and_local_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            @contextmanager
            def db():
                connection=sqlite3.connect(root/'db');connection.row_factory=sqlite3.Row
                try:
                    with connection:yield connection
                finally:connection.close()
            success=False
            def fetch(url,target,engine,referer):
                if success:target.write_bytes(b'\xff\xd8synthetic')
                return success
            store=ArtworkStore(db,root,fetch)
            data={'extrafanart_urls':{'site':['https://example.invalid/1','https://example.invalid/1']},'extrafanart':['https://example.invalid/2']}
            self.assertEqual(len(still_urls(data)),2)
            store.queue(7,data,'builtin');store.process(threading.Event());self.assertEqual(store.manifest(7)['failed'],2)
            (root/'7-1.jpg').write_bytes(b'\xff\xd8local');self.assertEqual(store.manifest(7)['screenshots'],1)
            success=True;store.retry(7);store.process(threading.Event())
            manifest=store.manifest(7);self.assertEqual((manifest['stills'],manifest['screenshots']),(2,1))
            recovered=ArtworkStore(db,root,fetch);self.assertEqual(recovered.manifest(7)['stills'],2)
            store.queue(7,data,'builtin');store.process(threading.Event());self.assertEqual(store.status['processed'],0)


if __name__=='__main__':unittest.main()
