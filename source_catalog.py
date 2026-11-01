"""Sanitized source inventory. Never expose upstream configuration secrets."""
import json
import threading
import time
from pathlib import Path

NATIVE_SOURCES = ('javbus','jav321','fc2','fc2club','fc2ppvdb','freejavbt','airav','xcity')


class SourceCatalog:
    def __init__(self, request, config):
        self.request = request
        self.config = config
        self.lock = threading.Lock()
        self.cache = None

    def snapshot(self, refresh=False):
        with self.lock:
            if self.cache and not refresh and time.time()-self.cache['checked_at']<60:
                upstream = self.cache
            else:
                upstream = {'connected': False, 'version': None, 'routes': {}, 'error': '', 'checked_at': time.time()}
                try:
                    health = self.request('health')
                    config = self.request('config')
                    routes = config.get('scraping', {}).get('content_routes', {})
                    if not isinstance(routes, dict):
                        raise ValueError('invalid route schema')
                    upstream.update(connected=True,version=str(health.get('version',''))[:40],routes={str(kind):[s for s in sources if isinstance(s,str) and len(s)<100] for kind,sources in routes.items() if isinstance(sources,list)})
                except Exception:
                    upstream['error']='本机 Amane 未连接，请检查服务地址与 Token；内置来源仍可独立使用'
                self.cache=upstream
        cfg = self.config()
        rows=json.loads(Path(__file__).with_name('sources.json').read_text(encoding='utf-8'))
        configured={source for route in upstream['routes'].values() for source in route}
        known={row['id'] for row in rows}
        rows.extend({'id':source,'url':''} for source in sorted(configured-known))
        for row in rows:
            row.update(native=row['id'] in NATIVE_SOURCES,
                       native_enabled=row['id'] in cfg.get('scrape_sources',[]),
                       amane_enabled=row['id'] in configured,
                       kinds=[kind for kind,sources in upstream['routes'].items() if row['id'] in sources])
        return {'items':rows,'upstream':upstream,'engine':cfg.get('scrape_engine','external')}
