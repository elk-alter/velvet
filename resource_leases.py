"""Persistent shared-reader/exclusive-writer leases across media workflows."""
import time
import uuid
from contextlib import contextmanager


class ResourceBusy(ValueError):
    def __init__(self):super().__init__('文件整理或其它媒体任务正在占用资源，请等待后重试')


class ResourceLeases:
    def __init__(self,db):
        self.db=db
        self.ensure()

    def ensure(self):
        with self.db() as c:
            c.execute('CREATE TABLE IF NOT EXISTS resource_claims(owner TEXT PRIMARY KEY,kind TEXT,mode TEXT,created REAL)')

    def try_acquire(self,kind,mode='read',owner=None):
        if mode not in ('read','write') or not isinstance(kind,str) or not kind:raise ValueError('Invalid resource lease')
        owner=owner or uuid.uuid4().hex
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            previous=c.execute('SELECT * FROM resource_claims WHERE owner=?',(owner,)).fetchone()
            if previous:
                if previous['mode']!=mode or previous['kind']!=kind:raise ValueError('Lease owner cannot change mode')
                return owner
            if c.execute("SELECT 1 FROM resource_claims WHERE mode='write' OR ?='write' LIMIT 1",(mode,)).fetchone():return None
            c.execute('INSERT INTO resource_claims VALUES(?,?,?,?)',(owner,kind,mode,time.time()))
        return owner

    def release(self,owner):
        if owner is not None:
            with self.db() as c:c.execute('DELETE FROM resource_claims WHERE owner=?',(owner,))

    @contextmanager
    def hold(self,kind,mode='read'):
        owner=self.try_acquire(kind,mode)
        if owner is None:raise ResourceBusy()
        try:yield owner
        finally:self.release(owner)

    def active(self):
        with self.db() as c:return [dict(row) for row in c.execute('SELECT kind,mode,created FROM resource_claims ORDER BY created')]

    def recover(self):
        """Only under exclusive DataGuard at startup or a fully quiesced restore."""
        with self.db() as c:
            count=c.execute('SELECT COUNT(*) FROM resource_claims').fetchone()[0]
            c.execute('DELETE FROM resource_claims')
        return count
