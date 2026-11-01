"""Persistent, single-worker metadata engine. Never receives a media file path."""
import asyncio
import datetime
import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from urllib.parse import parse_qs, urlparse
from pathlib import Path
from scrapers.compat import HttpClient, SearchQuery, fold_number, fetch_bytes


def source_types():
    try:
        from scrapers.jav321 import Jav321Crawler
        from scrapers.javbus import JavBusCrawler
        from scrapers.fc2 import FC2Crawler
        from scrapers.fc2club import FC2ClubCrawler
        from scrapers.fc2ppvdb import FC2PPVDBCrawler
        from scrapers.freejavbt import FreejavbtCrawler
        from scrapers.airav import AiravCrawler
        from scrapers.xcity import XCityCrawler
    except ImportError:
        raise ValueError('内置刮削需要安装依赖：python -m pip install -r requirements.txt') from None
    return {'javbus': JavBusCrawler, 'jav321': Jav321Crawler, 'fc2': FC2Crawler, 'fc2club': FC2ClubCrawler, 'fc2ppvdb': FC2PPVDBCrawler, 'freejavbt': FreejavbtCrawler, 'airav': AiravCrawler, 'xcity': XCityCrawler}


def validate_code(code):
    if not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{1,79}', code):
        raise ValueError('编号格式无效，请先核对编号')
    return code.upper()


def merge_results(code, snapshots, priority, field_priority=None):
    result = {'number': code, 'field_sources': {}, 'raw': snapshots}
    keys = {key for values in snapshots.values() for key in values} - {'number'}
    for key in keys:
        chain = list(dict.fromkeys((field_priority or {}).get(key, []) + priority))
        for source in chain:
            value = snapshots.get(source, {}).get(key)
            if source in priority and value:
                result[key] = value
                result['field_sources'][key] = source
                break
    result['poster_url'] = next(iter(result.get('poster_urls') or []), '')
    result['thumb_url'] = next(iter(result.get('thumb_urls') or []), '')
    return result


class Engine:
    def __init__(self, data_dir, config, fetcher=None):
        self.root = Path(data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'builtin-scraper.db'
        self.config = config
        self.fetcher = fetcher or self.fetch
        self.lock = threading.Lock()
        self.worker_lock = threading.Lock()
        with self.connect() as db:
            db.executescript('CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY,code TEXT,status TEXT,error TEXT,result TEXT,created REAL); CREATE TABLE IF NOT EXISTS metadata(id INTEGER PRIMARY KEY,code TEXT UNIQUE,data TEXT);')
            db.execute("UPDATE tasks SET status='queued' WHERE status='running'")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def task(self, row):
        return {'id': row['id'], 'title': row['code'], 'status': row['status'], 'error': row['error'],
                'result': json.loads(row['result'] or '{}'),
                'created_at': datetime.datetime.fromtimestamp(row['created'], datetime.timezone.utc).isoformat()}

    def request(self, path, data=None):
        parsed = urlparse(path)
        query = parse_qs(parsed.query)
        with self.lock, self.connect() as db:
            if parsed.path == 'tasks' and data is not None:
                code = validate_code(data.get('number'))
                if data.get('type') != 'scrape':
                    raise ValueError('内置引擎仅接受元数据刮削任务')
                row = db.execute("SELECT * FROM tasks WHERE code=? AND status IN ('queued','running')", (code,)).fetchone()
                if not row:
                    task_id = db.execute("INSERT INTO tasks(code,status,error,result,created) VALUES(?,'queued','','{}',?)", (code, time.time())).lastrowid
                    row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
                result = self.task(row)
            elif parsed.path == 'tasks/batch':
                if data.get('action') != 'cancel':
                    raise ValueError('未知任务操作')
                for task_id in data.get('task_ids', []):
                    db.execute("UPDATE tasks SET status='cancelled' WHERE id=? AND status IN ('queued','running')", (int(task_id),))
                result = {'ok': True}
            elif parsed.path.startswith('tasks/'):
                row = db.execute('SELECT * FROM tasks WHERE id=?', (int(parsed.path.split('/')[1]),)).fetchone()
                if not row:
                    raise ValueError('内置任务不存在 HTTP 404')
                result = self.task(row)
            elif parsed.path == 'tasks':
                result = {'items': [self.task(row) for row in db.execute('SELECT * FROM tasks ORDER BY id DESC LIMIT 200')]}
            elif parsed.path == 'metadata':
                offset = max(0, int(query.get('offset', ['0'])[0]))
                limit = min(200, max(1, int(query.get('limit', ['200'])[0])))
                items = []
                for row in db.execute('SELECT * FROM metadata ORDER BY id LIMIT ? OFFSET ?', (limit, offset)):
                    items.append(dict(json.loads(row['data']), id=row['id']))
                result = {'items': items, 'total': db.execute('SELECT COUNT(*) FROM metadata').fetchone()[0]}
            else:
                raise ValueError('内置引擎不支持此接口')
            pending = db.execute("SELECT 1 FROM tasks WHERE status='queued'").fetchone()
        if pending:
            self.start()
        return result

    def start(self):
        if self.worker_lock.acquire(False):
            threading.Thread(target=self.work, daemon=True).start()

    def work(self):
        try:
            while True:
                with self.lock, self.connect() as db:
                    row = db.execute("SELECT * FROM tasks WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
                    if row is None:
                        return
                    db.execute("UPDATE tasks SET status='running' WHERE id=?", (row['id'],))
                try:
                    metadata, outcomes = self.fetcher(row['code'])
                    error = '' if metadata.get('title') else '未匹配到可信资料，请检查编号或来源网络'
                    with self.lock, self.connect() as db:
                        if db.execute('SELECT status FROM tasks WHERE id=?', (row['id'],)).fetchone()[0] != 'running':
                            continue
                        if not error:
                            db.execute('INSERT INTO metadata(code,data) VALUES(?,?) ON CONFLICT(code) DO UPDATE SET data=excluded.data', (row['code'], json.dumps(metadata, ensure_ascii=False)))
                        db.execute('UPDATE tasks SET status=?,error=?,result=? WHERE id=?', ('failed' if error else 'done', error, json.dumps({'outcomes': outcomes}), row['id']))
                except Exception:
                    with self.lock, self.connect() as db:
                        db.execute("UPDATE tasks SET status='failed',error='内置引擎执行失败，请检查依赖与配置' WHERE id=? AND status='running'", (row['id'],))
        finally:
            # Serialize releasing the worker lease with queue submission: no lost wakeup.
            with self.lock:
                self.worker_lock.release()
                with self.connect() as db:
                    pending = db.execute("SELECT 1 FROM tasks WHERE status='queued'").fetchone()
                if pending:
                    self.start()

    def fetch(self, code):
        return asyncio.run(self.fetch_async(code))

    async def fetch_async(self, code):
        types = source_types()
        cfg = self.config()
        enabled = cfg.get('scrape_sources', ['javbus', 'jav321', 'fc2'])
        route = {'fc2','fc2club','fc2ppvdb','freejavbt','airav'} if re.fullmatch(r'FC2-(?:PPV-)?\d+', code) else {'javbus','jav321','freejavbt','airav','xcity'}
        priority = [s for s in enabled if s in route]
        client = HttpClient(cfg.get('scrape_proxy', ''))
        snapshots, outcomes = {}, []
        for source in priority:
            try:
                value = await types[source](client).fetch(SearchQuery(code))
                if value is None:
                    outcomes.append({'site': source, 'outcome': 'not_found'})
                    continue
                values = value.values
                if fold_number(values.get('number', '')) != fold_number(code):
                    outcomes.append({'site': source, 'outcome': 'failed', 'reason': 'identity_mismatch'})
                    continue
                snapshots[source] = values
                outcomes.append({'site': source, 'outcome': 'success'})
            except Exception as exc:
                status = getattr(exc, 'http_status', None)
                reason = getattr(exc, 'reason', 'unexpected')
                outcomes.append({'site': source, 'outcome': 'failed', 'reason': reason})
            # Bound source request rate; never parallelize an entire library at once.
            await asyncio.sleep(1)
        if not priority:
            outcomes.append({'site': 'configuration', 'outcome': 'failed', 'reason': 'no_enabled_source'})
        return merge_results(code, snapshots, priority, cfg.get('scrape_field_priority')), outcomes

    def cache_artwork(self, url, target, referer=''):
        try:
            host = urlparse(url).netloc
            page = referer if urlparse(referer).scheme == 'https' else 'https://' + host + '/'
            payload, kind = fetch_bytes(url, self.config().get('scrape_proxy', ''), {'Referer': page, 'User-Agent': 'Mozilla/5.0'}, 12_000_000)
            if not kind.startswith('image/') or not (payload.startswith(b'\xff\xd8') or payload.startswith(b'\x89PNG') or payload.startswith(b'RIFF')):
                return False
            temp = target.with_suffix('.part')
            temp.write_bytes(payload)
            temp.replace(target)
            return True
        except (OSError, ValueError):
            return False
