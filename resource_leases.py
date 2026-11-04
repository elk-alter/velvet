"""Persistent scoped reader/writer leases with FIFO admission for waiting writers."""
import json
import os
import time
import uuid
from bisect import bisect_left
from pathlib import Path
from contextlib import contextmanager


def path_resource(raw,tree=False):
    path=Path(raw).expanduser().resolve(strict=False)
    return ('tree:' if tree else 'file:')+os.path.normcase(str(path)).replace('\\','/').rstrip('/')


def video_resources(db,ids):
    ids=sorted({int(value) for value in ids});resources=['video:'+str(value) for value in ids]
    with db() as c:
        for offset in range(0,len(ids),400):
            batch=ids[offset:offset+400]
            resources.extend(path_resource(row[0]) for row in c.execute('SELECT path FROM videos WHERE id IN ('+','.join('?' for _ in batch)+')',batch))
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='media_assets'").fetchone():
                resources.extend('asset:'+row[0] for row in c.execute('SELECT id FROM media_assets WHERE video_id IN ('+','.join('?' for _ in batch)+')',batch))
    return resources or ['metadata']


def intersects(first,second):
    first=set(first);second=set(second)
    if '*' in first or '*' in second or first.intersection(second):return True
    for trees,others in ((first,second),(second,first)):
        paths=sorted(value.split(':',1)[1] for value in others if value.startswith(('tree:','file:')))
        for value in trees:
            if not value.startswith('tree:'):continue
            root=value[5:];index=bisect_left(paths,root)
            if index<len(paths) and paths[index]==root:return True
            prefix=root.rstrip('/')+'/';index=bisect_left(paths,prefix)
            if index<len(paths) and paths[index].startswith(prefix):return True
    return False


class ResourceBusy(ValueError):
    def __init__(self):super().__init__('文件整理或其它媒体任务正在占用相关资源，请等待后重试')


class ResourceLeases:
    def __init__(self,db):
        self.db=db
        self.ensure()

    def ensure(self):
        with self.db() as c:
            c.execute('CREATE TABLE IF NOT EXISTS resource_claims(owner TEXT PRIMARY KEY,kind TEXT,mode TEXT,created REAL)')
            c.execute('CREATE TABLE IF NOT EXISTS resource_scopes(owner TEXT,resource TEXT,PRIMARY KEY(owner,resource))')
            c.execute('CREATE TABLE IF NOT EXISTS resource_waiters(ticket INTEGER PRIMARY KEY AUTOINCREMENT,owner TEXT UNIQUE,kind TEXT,mode TEXT,resources TEXT,created REAL)')
            c.execute('CREATE TRIGGER IF NOT EXISTS release_resource_scopes AFTER DELETE ON resource_claims BEGIN DELETE FROM resource_scopes WHERE owner=OLD.owner; END')

    def scopes(self,c,owner):
        return [row[0] for row in c.execute('SELECT resource FROM resource_scopes WHERE owner=?',(owner,))] or ['*']

    def acquire_in(self,c,kind,mode,owner,resources=None,queue=False,allowed_reconcile=()):
        """Use inside the caller's IMMEDIATE transaction; commit a denied queue ticket."""
        if mode not in ('read','write') or not isinstance(kind,str) or not kind:raise ValueError('Invalid resource lease')
        resources=['*'] if resources is None else sorted(set(resources))
        if not resources or not all(isinstance(value,str) and value for value in resources):raise ValueError('Invalid resource scope')
        if queue and mode!='write':raise ValueError('Only writers use the admission queue')
        previous=c.execute('SELECT * FROM resource_claims WHERE owner=?',(owner,)).fetchone()
        if previous:
            if previous['mode']!=mode or previous['kind']!=kind or set(self.scopes(c,owner))!=set(resources):raise ValueError('Lease owner cannot change kind, mode or scope')
            return owner
        ticket=None
        if queue:
            c.execute('INSERT OR IGNORE INTO resource_waiters(owner,kind,mode,resources,created) VALUES(?,?,?,?,?)',(owner,kind,mode,json.dumps(resources),time.time()))
            waiter=c.execute('SELECT * FROM resource_waiters WHERE owner=?',(owner,)).fetchone()
            if waiter['kind']!=kind or waiter['mode']!=mode or json.loads(waiter['resources'])!=resources:raise ValueError('Waiting owner cannot change scope')
            ticket=waiter['ticket']
        for row in c.execute('SELECT * FROM resource_claims').fetchall():
            if row['kind']=='organize-reconcile' and (row['owner'] in allowed_reconcile or kind=='backup' and mode=='write'):continue
            if (mode=='write' or row['mode']=='write') and intersects(resources,self.scopes(c,row['owner'])):return None
        for row in c.execute('SELECT * FROM resource_waiters WHERE owner!=? ORDER BY ticket',(owner,)):
            if ticket is not None and row['ticket']>ticket:continue
            if intersects(resources,json.loads(row['resources'])):return None
        c.execute('INSERT INTO resource_claims VALUES(?,?,?,?)',(owner,kind,mode,time.time()))
        c.executemany('INSERT INTO resource_scopes VALUES(?,?)',((owner,resource) for resource in resources))
        c.execute('DELETE FROM resource_waiters WHERE owner=?',(owner,))
        return owner

    def try_acquire(self,kind,mode='read',owner=None,*,resources=None,queue=False,allowed_reconcile=()):
        if queue and owner is None:raise ValueError('Queued writers need a stable owner')
        owner=owner or uuid.uuid4().hex
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            return self.acquire_in(c,kind,mode,owner,resources,queue,allowed_reconcile)

    def release(self,owner):
        if owner is not None:
            with self.db() as c:
                c.execute('DELETE FROM resource_claims WHERE owner=?',(owner,))
                c.execute('DELETE FROM resource_waiters WHERE owner=?',(owner,))

    def has_waiting_write(self,owner):
        with self.db() as c:
            resources=self.scopes(c,owner)
            return any(intersects(resources,json.loads(row[0])) for row in c.execute('SELECT resources FROM resource_waiters'))

    @contextmanager
    def hold(self,kind,mode='read',*,resources=None):
        owner=self.try_acquire(kind,mode,resources=resources)
        if owner is None:raise ResourceBusy()
        try:yield owner
        finally:self.release(owner)

    def active(self):
        with self.db() as c:return [dict(row) for row in c.execute('SELECT kind,mode,created FROM resource_claims ORDER BY created')]

    def waiting(self):
        with self.db() as c:return [dict(row) for row in c.execute('SELECT kind,mode,created FROM resource_waiters ORDER BY ticket')]

    def recover(self):
        """Only under exclusive DataGuard at startup or a fully quiesced restore."""
        with self.db() as c:
            count=c.execute('SELECT COUNT(*) FROM resource_claims').fetchone()[0]
            c.execute('DELETE FROM resource_claims');c.execute('DELETE FROM resource_scopes');c.execute('DELETE FROM resource_waiters')
        return count
