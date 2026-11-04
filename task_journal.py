"""Persistent task identities and transitions shared by existing executors."""
import json
import time
import uuid

STATES = {'queued', 'waiting', 'running', 'done', 'partial', 'failed', 'cancelled', 'interrupted'}
KINDS = {'scan', 'scrape', 'sync', 'images', 'review', 'duplicates', 'inventory', 'plan', 'organize'}


class TaskJournal:
    def __init__(self, db):
        self.db = db
        self.ensure()

    def ensure(self):
        with self.db() as c:
            c.execute("CREATE TABLE IF NOT EXISTS task_journal(id TEXT PRIMARY KEY,kind TEXT,external_key TEXT UNIQUE,state TEXT,created REAL,updated REAL,summary TEXT DEFAULT '{}')")
            c.execute('CREATE TABLE IF NOT EXISTS task_events(sequence INTEGER PRIMARY KEY,task_id TEXT,state TEXT,created REAL)')

    def observe(self, kind, key, state, summary=None):
        if kind not in KINDS or state not in STATES:
            raise ValueError('Unknown task kind or state')
        # No filenames, paths, source exceptions, credentials or OCR text.
        allowed = {'processed', 'bytes_processed', 'total', 'failed', 'skipped', 'phase', 'reason', 'cancellable'}
        summary = {k: v for k, v in (summary or {}).items() if k in allowed}
        stamp = json.dumps(summary, sort_keys=True)
        external = kind + ':' + str(key)
        now = time.time()
        with self.db() as c:
            previous = c.execute('SELECT * FROM task_journal WHERE external_key=?', (external,)).fetchone()
            if previous and previous['state'] == state and previous['summary'] == stamp:
                return previous['id']
            task_id = previous['id'] if previous else uuid.uuid4().hex
            c.execute('INSERT INTO task_journal VALUES(?,?,?,?,?,?,?) ON CONFLICT(external_key) DO UPDATE SET state=excluded.state,updated=excluded.updated,summary=excluded.summary',
                      (task_id, kind, external, state, now, now, stamp))
            c.execute('INSERT INTO task_events(task_id,state,created) VALUES(?,?,?)', (task_id, state, now))
        return task_id

    def start(self, kind):
        key = uuid.uuid4().hex
        self.observe(kind, key, 'running', {'cancellable': True})
        return key

    def recover(self):
        with self.db() as c:
            rows = c.execute("SELECT kind,external_key FROM task_journal WHERE state='running'").fetchall()
        for row in rows:
            self.observe(row['kind'], row['external_key'].split(':', 1)[1], 'interrupted', {'reason': 'restart_reconciliation_required'})

    def list(self, limit=100):
        limit = min(500, max(1, int(limit)))
        with self.db() as c:
            rows = [dict(r) for r in c.execute('SELECT id,kind,external_key,state,created,updated,summary FROM task_journal ORDER BY updated DESC LIMIT ?', (limit,))]
        for row in rows:
            row['object_ref']=row.pop('external_key')
            row['summary'] = json.loads(row['summary'])
        return rows

    def reconcile_sources(self, uncertain_remote=False):
        with self.db() as c:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            jobs = [dict(r) for r in c.execute('SELECT * FROM scrape_jobs ORDER BY id DESC LIMIT 100')] if 'scrape_jobs' in tables else []
            images = [dict(r) for r in c.execute('SELECT video_id,ordinal,status FROM artwork_jobs WHERE active=1')] if 'artwork_jobs' in tables else []
            reviews = [dict(r) for r in c.execute('SELECT video_id,status,result FROM file_reviews')] if 'file_reviews' in tables else []
        mapping = {'submitting': 'waiting', 'cancelling': 'waiting', 'success': 'done', 'completed': 'done', 'canceled': 'cancelled', 'unchecked': 'waiting'}
        for kind, rows in [('scrape', jobs), ('images', images), ('review', reviews)]:
            for row in rows:
                if kind == 'review' and row['status'] == 'unchecked':
                    continue
                state = mapping.get(row['status'], row['status'])
                if state not in STATES:
                    state = 'failed'
                if kind == 'review' and state == 'done' and row['result'] not in ('passed', ''):
                    state = 'failed'
                key = row['id'] if kind == 'scrape' else f"{row['video_id']}:{row.get('ordinal', '')}"
                reason='executor_state'
                if kind=='scrape' and state in ('queued','running','waiting') and (uncertain_remote or (row.get('updated') and time.time()-row['updated']>90)):
                    state='waiting';reason='remote_state_unconfirmed'
                if kind=='scrape' and row.get('retry_kind')=='needs_review':reason='manual_reconciliation_required'
                self.observe(kind, key, state, {'cancellable': state in ('queued', 'running'), 'reason':reason})
