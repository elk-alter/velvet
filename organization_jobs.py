"""Immutable organization approvals, persistent steps and exclusive resource leases."""
import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from media_locations import fingerprint
from storage_inventory import volume_identity, destination_key, safe_component
from task_journal import TaskJournal

TERMINAL={'done','cancelled'}
TRANSITIONS={
 'pending':{'copying','cancelled','waiting'},
 'waiting':{'pending','cancelled'},
 'copying':{'verified','interrupted','cancelled'},
 'verified':{'committing','interrupted'},
 'committing':{'committed','interrupted'},
 'committed':{'binding','interrupted'},
 'binding':{'done','needs_binding','interrupted'},
 'needs_binding':{'binding','cancelled'},
 'interrupted':{'pending','verified','committed','needs_binding','cancelled'},
 'done':set(),'cancelled':set(),
}


def no_links(raw):
    path=Path(raw).expanduser().absolute()
    for part in (path,*path.parents):
        if part.is_symlink() or (part.exists() and getattr(part.lstat(),'st_file_attributes',0)&1024):
            raise ValueError('Linked paths require separate review')
    return path.resolve()


def identify(path):
    parent=Path(path)
    while not parent.exists() and parent!=parent.parent:parent=parent.parent
    return volume_identity(parent)[0]


def digest(payload):
    return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()).hexdigest()


class OrganizationJobs:
    def __init__(self,db,device_identity=identify):
        self.db,self.device_identity=db,device_identity
        self.journal=TaskJournal(db)
        with db() as c:
            c.execute('CREATE TABLE IF NOT EXISTS organization_drafts(id TEXT PRIMARY KEY,digest TEXT UNIQUE,payload TEXT,created REAL)')
            c.execute('CREATE TABLE IF NOT EXISTS organization_jobs(id TEXT PRIMARY KEY,approval_digest TEXT UNIQUE,payload TEXT,state TEXT,created REAL,updated REAL)')
            c.execute("CREATE TABLE IF NOT EXISTS organization_steps(job_id TEXT,ordinal INTEGER,state TEXT,details TEXT DEFAULT '{}',updated REAL,PRIMARY KEY(job_id,ordinal))")
            c.execute('CREATE TABLE IF NOT EXISTS organization_step_events(sequence INTEGER PRIMARY KEY,job_id TEXT,ordinal INTEGER,state TEXT,created REAL)')
            c.execute('CREATE TABLE IF NOT EXISTS organization_control(job_id TEXT PRIMARY KEY,cancel_requested INTEGER DEFAULT 0)')
            c.execute('CREATE TABLE IF NOT EXISTS organization_leases(resource TEXT PRIMARY KEY,job_id TEXT)')

    def prepare(self,entries,source_root,target_root,rules_version=1):
        if type(rules_version) is not int or rules_version<1:raise ValueError('Invalid rule version')
        source_root=no_links(source_root);target_root=no_links(target_root)
        if source_root.is_relative_to(target_root) or target_root.is_relative_to(source_root):raise ValueError('Copy target must be independent of source scope')
        if not isinstance(entries,list) or not entries:raise ValueError('Choose files before approval')
        checked=[];destinations=set()
        for entry in entries:
            if entry.get('operation')!='copy':raise ValueError('This approval schema currently supports copy; move approval is not implemented')
            source=no_links(entry['source']);target=no_links(entry['destination'])
            if not source.is_relative_to(source_root) or not target.is_relative_to(target_root):raise ValueError('Entry escapes approved roots')
            key=destination_key(target)
            if any(safe_component(part)!=part for part in target.parts[1:]):raise ValueError('Invalid target path component')
            if target.exists() or key in destinations:raise ValueError('Target collision')
            destinations.add(key)
            checked.append({'source':str(source),'destination':str(target),'video_id':entry.get('video_id'),
                            'operation':'copy','fingerprint':fingerprint(source),'source_device':self.device_identity(source),
                            'target_device':self.device_identity(target.parent)})
        payload={'entries':checked,'source_root':str(source_root),'target_root':str(target_root),'rules_version':rules_version}
        stamp=digest(payload);draft_id=uuid.uuid4().hex
        with self.db() as c:
            c.execute('INSERT OR IGNORE INTO organization_drafts VALUES(?,?,?,?)',(draft_id,stamp,json.dumps(payload),time.time()))
            draft_id=c.execute('SELECT id FROM organization_drafts WHERE digest=?',(stamp,)).fetchone()[0]
        return {'id':draft_id,'digest':stamp,'payload':payload,'files':len(checked),'bytes':sum(e['fingerprint']['size'] for e in checked)}

    def approve(self,draft_id,expected_digest):
        with self.db() as c:
            draft=c.execute('SELECT * FROM organization_drafts WHERE id=?',(draft_id,)).fetchone()
            if not draft or draft['digest']!=expected_digest:raise ValueError('Approval does not match the stored plan')
            existing=c.execute('SELECT id FROM organization_jobs WHERE approval_digest=?',(expected_digest,)).fetchone()
        if existing:return existing['id']
        payload=json.loads(draft['payload'])
        if digest(payload)!=expected_digest:raise ValueError('Stored approval content changed')
        for entry in payload['entries']:
            source=no_links(entry['source']);target=no_links(entry['destination'])
            if str(source)!=entry['source'] or str(target)!=entry['destination'] or target.exists():raise ValueError('Path changed since preview')
            if self.device_identity(source)!=entry['source_device'] or self.device_identity(target.parent)!=entry['target_device']:raise ValueError('Device changed since preview')
            if fingerprint(source)!=entry['fingerprint']:raise ValueError('Source changed since preview')
        job_id=uuid.uuid4().hex;now=time.time()
        with self.db() as c:
            c.execute('INSERT OR IGNORE INTO organization_jobs VALUES(?,?,?,?,?,?)',(job_id,expected_digest,json.dumps(payload),'queued',now,now))
            job_id=c.execute('SELECT id FROM organization_jobs WHERE approval_digest=?',(expected_digest,)).fetchone()[0]
            for ordinal in range(len(payload['entries'])):c.execute('INSERT OR IGNORE INTO organization_steps(job_id,ordinal,state,updated) VALUES(?,?,?,?)',(job_id,ordinal,'pending',now))
        self.journal.observe('organize',job_id,'queued',{'total':len(payload['entries']),'cancellable':True})
        return job_id

    def get(self,job_id):
        with self.db() as c:
            job=c.execute('SELECT * FROM organization_jobs WHERE id=?',(job_id,)).fetchone()
            if not job:raise ValueError('Unknown organization job')
            result=dict(job);result['steps']=[dict(r) for r in c.execute('SELECT * FROM organization_steps WHERE job_id=? ORDER BY ordinal',(job_id,))]
        result['payload']=json.loads(result['payload'])
        for step in result['steps']:step['details']=json.loads(step['details'])
        return result

    def claim(self,job_id):
        job=self.get(job_id)
        resources=sorted({e[key] for e in job['payload']['entries'] for key in ('source_device','target_device')})
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            state=c.execute('SELECT state FROM organization_jobs WHERE id=?',(job_id,)).fetchone()[0]
            if state=='running' or state in TERMINAL:raise ValueError('Job already claimed or finished')
            for resource in resources:
                owner=c.execute('SELECT job_id FROM organization_leases WHERE resource=?',(resource,)).fetchone()
                if owner and owner['job_id']!=job_id:raise ValueError('Device is leased by another job')
                c.execute('INSERT OR REPLACE INTO organization_leases VALUES(?,?)',(resource,job_id))
            c.execute("UPDATE organization_jobs SET state='running',updated=? WHERE id=?",(time.time(),job_id))
        self.journal.observe('organize',job_id,'running',{'total':len(job['steps']),'cancellable':True})

    def transition(self,job_id,ordinal,expected,state,details=None):
        if state not in TRANSITIONS.get(expected,set()):raise ValueError('Invalid execution transition')
        with self.db() as c:
            job=c.execute('SELECT state FROM organization_jobs WHERE id=?',(job_id,)).fetchone()
            if not job or job['state']!='running':raise ValueError('Execution lease not held')
            changed=c.execute('UPDATE organization_steps SET state=?,details=?,updated=? WHERE job_id=? AND ordinal=? AND state=?',
                              (state,json.dumps(details or {}),time.time(),job_id,ordinal,expected)).rowcount
            if changed!=1:raise ValueError('Step changed; reconciliation required')
            c.execute('INSERT INTO organization_step_events(job_id,ordinal,state,created) VALUES(?,?,?,?)',(job_id,ordinal,state,time.time()))

    def finish(self,job_id,state):
        if state not in ('done','partial','interrupted','waiting','cancelled','failed'):raise ValueError('Invalid job outcome')
        with self.db() as c:
            rows=[r[0] for r in c.execute('SELECT state FROM organization_steps WHERE job_id=?',(job_id,))]
            if not rows:raise ValueError('Unknown job')
            if state=='done' and any(value!='done' for value in rows):raise ValueError('Unfinished files cannot be reported as complete')
            c.execute('UPDATE organization_jobs SET state=?,updated=? WHERE id=?',(state,time.time(),job_id))
            c.execute('DELETE FROM organization_leases WHERE job_id=?',(job_id,))
        self.journal.observe('organize',job_id,state,{'processed':rows.count('done'),'total':len(rows)})

    def request_cancel(self,job_id):
        self.get(job_id)
        with self.db() as c:c.execute('INSERT OR REPLACE INTO organization_control VALUES(?,1)',(job_id,))

    def cancel_requested(self,job_id):
        with self.db() as c:row=c.execute('SELECT cancel_requested FROM organization_control WHERE job_id=?',(job_id,)).fetchone()
        return bool(row and row[0])

    def resume(self,job_id):
        with self.db() as c:
            changed=c.execute("UPDATE organization_jobs SET state='queued',updated=? WHERE id=? AND state IN ('cancelled','interrupted','partial','failed','waiting')",(time.time(),job_id)).rowcount
            if changed!=1:raise ValueError('Only stopped jobs can be resumed')
            c.execute('DELETE FROM organization_control WHERE job_id=?',(job_id,))
        self.journal.observe('organize',job_id,'queued',{'reason':'explicit_resume'})

    def recover(self):
        """Startup only, after the application has acquired exclusive DataGuard."""
        with self.db() as c:
            jobs=[r[0] for r in c.execute("SELECT id FROM organization_jobs WHERE state='running'")]
            c.execute("UPDATE organization_jobs SET state='interrupted' WHERE state='running'")
            c.execute('DELETE FROM organization_leases')
        for job_id in jobs:self.journal.observe('organize',job_id,'interrupted',{'reason':'restart_reconciliation_required'})
        return jobs
