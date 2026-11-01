"""Source checks follow the scraper's transport and block-page classifier.

Design follows Amane's crawlers/connectivity.py: isolate failures, check configured
sources, retain explicit skipped states. No metadata or media is changed.
"""
import concurrent.futures
import threading
import time
from scrapers.compat import fetch_bytes, RequestError
from scrapers.block import classify_block

CATALOG = {'javbus': 'https://www.javbus.com', 'jav321': 'https://www.jav321.com', 'fc2': 'https://adult.contents.fc2.com','fc2club':'https://fc2club.top','fc2ppvdb':'https://fc2ppvdb.com','freejavbt':'https://freejavbt.com','airav':'https://airav.io','xcity':'https://xcity.jp'}


def probe(source, proxy, transport=fetch_bytes):
    if source not in CATALOG:
        return {'source_id': source, 'status': 'skipped', 'reason': 'unknown_source', 'elapsed_ms': None}
    start = time.monotonic()
    result = {'source_id': source, 'url': CATALOG[source], 'status': 'ok', 'reason': '', 'http_status': None}
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        if source == 'javbus':
            headers.update({'Cookie': 'dv=1', 'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.6'})
        body, kind, status = transport(CATALOG[source], proxy, headers, with_status=True)
        result['http_status'] = status
        reason = classify_block(body.decode('utf-8', errors='replace'))
        if reason:
            result.update(status='failed', reason=reason)
        elif 'html' not in kind.lower():
            result.update(status='failed', reason='unexpected_response')
    except RequestError as exc:
        result.update(status='failed', reason=exc.reason, http_status=exc.http_status)
    except Exception:
        result.update(status='failed', reason='unexpected')
    result['elapsed_ms'] = round((time.monotonic() - start) * 1000)
    return result


class NetworkChecks:
    def __init__(self, config, external_check=None, external_probe=None, external_sources=None):
        self.config = config
        self.external_check = external_check
        self.external_probe = external_probe
        self.external_sources = external_sources
        self.lock = threading.Lock()
        self.state = {'running': False, 'checked_at': None, 'items': [], 'engine': None, 'proxy_enabled': False}

    def status(self):
        with self.lock:
            return {**self.state, 'items': [dict(item) for item in self.state['items']]}

    def start(self, sources=None):
        if sources is not None and (not isinstance(sources, list) or len(sources) > 64 or not all(isinstance(s, str) and len(s) < 100 for s in sources)):
            raise ValueError('来源列表格式无效')
        cfg = self.config()
        engine = cfg.get('scrape_engine', 'external')
        enabled = cfg.get('scrape_sources', list(CATALOG))
        ids = list(dict.fromkeys(sources if sources is not None else enabled)) if engine == 'builtin' else list(dict.fromkeys(sources if sources is not None else (self.external_sources() if self.external_sources else ['amane'])))
        if engine=='external' and not ids:ids=['amane']
        with self.lock:
            if self.state['running']:
                return False
            self.state.update(running=True, checked_at=None, engine=engine, proxy_enabled=bool(cfg.get('scrape_proxy')) if engine == 'builtin' else False,
                              items=[{'source_id': s, 'status': 'pending', 'reason': '', 'elapsed_ms': None} for s in ids])
        threading.Thread(target=self.run, args=(ids, enabled, cfg), daemon=True).start()
        return True

    def run(self, ids, enabled, cfg):
        if cfg.get('scrape_engine','external')=='external' and self.external_probe and ids!=['amane']:
            try:
                report=self.external_probe(ids)
                items=report.get('items',[])
                mapped={row['source_id']: {'source_id':row['source_id'],'status':row.get('status','failed'),'reason':row.get('reason') or row.get('skip_reason') or '', 'http_status':row.get('http_status'),'elapsed_ms':row.get('elapsed_ms'),'url':row.get('url','')} for row in items if row.get('source_id') in ids}
                results=[mapped.get(source,{'source_id':source,'status':'skipped','reason':'unknown_source','elapsed_ms':None}) for source in ids]
                with self.lock:self.state['items']=results
            except Exception:
                with self.lock:self.state['items']=[{'source_id':source,'status':'failed','reason':'service_unavailable','elapsed_ms':None} for source in ids or ['amane']]
            finally:
                with self.lock:self.state.update(running=False,checked_at=time.time())
            return
        def check(source):
            if cfg.get('scrape_engine', 'external') == 'external':
                start = time.monotonic()
                try:
                    if self.external_check is None:
                        raise ValueError('not configured')
                    self.external_check()
                    return {'source_id': 'amane', 'status': 'ok', 'reason': '', 'elapsed_ms': round((time.monotonic()-start)*1000)}
                except Exception:
                    return {'source_id': 'amane', 'status': 'failed', 'reason': 'service_unavailable', 'elapsed_ms': round((time.monotonic()-start)*1000)}
            if source in CATALOG and source not in enabled:
                return {'source_id': source, 'status': 'skipped', 'reason': 'disabled', 'elapsed_ms': None}
            if source not in CATALOG:
                return {'source_id': source, 'status': 'skipped', 'reason': 'unknown_source', 'elapsed_ms': None}
            return probe(source, cfg.get('scrape_proxy', ''))
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                futures = {pool.submit(check, s): s for s in ids}
                for future in concurrent.futures.as_completed(futures):
                    item = future.result()
                    with self.lock:
                        self.state['items'] = [item if row['source_id'] == item['source_id'] else row for row in self.state['items']]
        finally:
            with self.lock:
                self.state.update(running=False, checked_at=time.time())
