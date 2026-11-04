"""Same-device move staging, cyclic renames and explicitly approved inverse jobs."""
from organization_preflight import validate_manifest,path_conflicts,PreflightError
import os
import uuid
from pathlib import Path
from media_locations import fingerprint,binding_key
from organization_jobs import no_links,digest
from organization_executor import CopyCancelled,CopyPaused,file_identity,publish_no_replace


class MoveExecutor:
    def __init__(self,jobs,binder,checkpoint=lambda *args:None):
        self.jobs,self.binder,self.checkpoint=jobs,binder,checkpoint
        self.validator=None
        with jobs.db() as c:
            c.execute('CREATE TABLE IF NOT EXISTS organization_inverses(original_job_id TEXT PRIMARY KEY,draft_id TEXT,inverse_job_id TEXT)')

    def verify(self,path,entry,check=None,validate_target=True):
        if entry['operation']=='archive' and validate_target:
            if not self.validator:raise ValueError('归档校验器未配置')
            self.validator(entry,check)
        path=Path(path)
        if no_links(path)!=path:raise ValueError('Move path changed')
        if self.jobs.device_identity(path)!=entry['source_device']:raise ValueError('Move device changed')
        if file_identity(path)!=entry['source_identity']:raise ValueError('Move file ownership changed')
        stamp=fingerprint(path,check)
        if stamp['sha256']!=entry['fingerprint']['sha256'] or stamp['size']!=entry['fingerprint']['size']:raise ValueError('Move file content changed')

    def target(self,entry):
        raw=Path(entry['destination']);target=no_links(raw.parent)/raw.name
        if str(target)!=entry['destination'] or self.jobs.device_identity(target.parent)!=entry['target_device']:raise ValueError('Target path or device changed')
        return target

    def run(self,job_id,cancel=lambda:False):
        job=self.jobs.get(job_id)
        if job['state']=='done':return job
        if digest(job['payload'])!=job['approval_digest'] or any(e['operation'] not in ('move','archive') for e in job['payload']['entries']):raise ValueError('Invalid move approval')
        self.jobs.claim(job_id);current=None
        def check():
            if cancel() or self.jobs.cancel_requested(job_id):raise CopyCancelled()
            if self.jobs.pause_requested(job_id):raise CopyPaused()
        try:
            pending=[job['payload']['entries'][step['ordinal']] for step in job['steps'] if step['state']=='pending']
            if pending:current=next(step['ordinal'] for step in job['steps'] if step['state']=='pending')
            validate_manifest(pending,self.jobs.device_identity)
            # Stage every source first, so cyclic destinations are vacant before publishing.
            for ordinal,entry in enumerate(job['payload']['entries']):
                check();current=ordinal;step=self.jobs.get(job_id)['steps'][ordinal]
                state,details=step['state'],step['details']
                if state=='pending':
                    self.verify(entry['source'],entry,check)
                    temporary=Path(entry['source']).with_name('.velvet-move-'+job_id+'-'+uuid.uuid4().hex+'.partial')
                    details={'temporary':str(temporary),'file_identity':entry['source_identity']}
                    self.jobs.transition(job_id,ordinal,'pending','staging',details);state='staging'
                    self.checkpoint('staging',job_id,ordinal)
                if state=='staging':
                    temporary=Path(details['temporary'])
                    if not temporary.exists():
                        self.verify(entry['source'],entry,check);publish_no_replace(Path(entry['source']),temporary)
                    self.verify(temporary,entry,check)
                    self.jobs.transition(job_id,ordinal,'staging','staged',details)
                    self.checkpoint('staged',job_id,ordinal)
            for ordinal,entry in enumerate(job['payload']['entries']):
                check();current=ordinal;step=self.jobs.get(job_id)['steps'][ordinal]
                state,details=step['state'],step['details'];target=self.target(entry)
                if state=='staged':
                    self.jobs.transition(job_id,ordinal,'staged','committing',details);state='committing'
                    self.checkpoint('committing',job_id,ordinal)
                if state=='committing':
                    if not target.exists():
                        temporary=Path(details['temporary']);self.verify(temporary,entry,check)
                        issues=path_conflicts(target,set(),archive=entry['operation']=='archive')
                        if issues:raise PreflightError(issues)
                        target.parent.mkdir(parents=True,exist_ok=True);self.target(entry)
                        issues=path_conflicts(target,set(),archive=entry['operation']=='archive')
                        if issues:raise PreflightError(issues)
                        publish_no_replace(temporary,target)
                        self.checkpoint('published',job_id,ordinal)
                    self.verify(target,entry,check)
                    self.jobs.transition(job_id,ordinal,'committing','committed',details)
                    self.checkpoint('committed',job_id,ordinal)
            # Bind as a single DB transaction through the adapter, including path swaps.
            for ordinal,entry in enumerate(job['payload']['entries']):
                self.verify(entry['destination'],entry,check)
                step=self.jobs.get(job_id)['steps'][ordinal]
                if step['state'] in ('committed','needs_binding'):self.jobs.transition(job_id,ordinal,step['state'],'binding',step['details'])
            try:
                if not self.binder:raise ValueError('Move binder not configured')
                bindings=self.binder(job['payload']['entries'],job_id)
                if not bindings or any(not bindings.get(binding_key(e)) for e in job['payload']['entries']):raise ValueError('Move binding incomplete')
            except Exception:
                for step in self.jobs.get(job_id)['steps']:
                    if step['state']=='binding':self.jobs.transition(job_id,step['ordinal'],'binding','needs_binding',step['details'])
                raise
            for entry in job['payload']['entries']:self.verify(entry['destination'],entry,check)
            self.checkpoint('bound',job_id,None)
            for ordinal,entry in enumerate(job['payload']['entries']):
                step=self.jobs.get(job_id)['steps'][ordinal]
                if step['state']=='binding':self.jobs.transition(job_id,ordinal,'binding','done',dict(step['details'],binding_id=bindings[binding_key(entry)]))
            self.jobs.finish(job_id,'done')
        except CopyPaused:self.jobs.finish(job_id,'waiting')
        except CopyCancelled:self.jobs.finish(job_id,'cancelled')
        except Exception as error:
            states=[step['state'] for step in self.jobs.get(job_id)['steps']]
            if current is not None:
                import json
                step=self.jobs.get(job_id)['steps'][current]
                with self.jobs.db() as c:
                    c.execute('UPDATE organization_steps SET details=? WHERE job_id=? AND ordinal=?',(json.dumps(dict(step['details'],error=type(error).__name__,**({'preflight':error.codes} if isinstance(error,PreflightError) else {}))),job_id,current))
            self.jobs.finish(job_id,'partial' if any(state!='pending' for state in states) else 'failed')
        return self.jobs.get(job_id)

    def prepare_undo(self,job_id):
        job=self.jobs.get(job_id)
        if any(entry['operation'] not in ('move','archive') for entry in job['payload']['entries']):raise ValueError('Only same-device moves have a reverse file plan')
        if job['state'] in ('running','queued'):raise ValueError('Stop the move before preparing rollback')
        with self.jobs.db() as c:
            existing=c.execute('SELECT * FROM organization_inverses WHERE original_job_id=?',(job_id,)).fetchone()
            if existing:
                row=c.execute('SELECT * FROM organization_drafts WHERE id=?',(existing['draft_id'],)).fetchone()
                import json
                return {'id':row['id'],'digest':row['digest'],'payload':json.loads(row['payload'])}
        reverse=[]
        for step,entry in zip(job['steps'],job['payload']['entries']):
            state=step['state']
            candidates=([entry['destination']] if state in ('committed','binding','needs_binding','done') else [entry['destination'],step['details'].get('temporary'),entry['source']] if state=='committing' else [step['details'].get('temporary'),entry['source']])
            location=None
            for candidate in candidates:
                if not candidate or not Path(candidate).exists():continue
                try:self.verify(candidate,entry,validate_target=False)
                except (OSError,ValueError):continue
                location=candidate;break
            if not location:raise ValueError('Cannot prove rollback ownership; review required')
            origin=Path(entry.get('source_root',job['payload']['source_root']));target=Path(entry.get('target_root',job['payload']['target_root']))
            source_root=target if Path(location).is_relative_to(target) else origin
            reverse.append({'video_id':entry['video_id'],'asset_id':entry.get('asset_id'),'metadata':entry.get('metadata',{}),'role':entry.get('role','media'),'parent_asset_id':entry.get('parent_asset_id'),'operation':'move','source':location,'destination':entry['source'],'source_root':str(source_root),'target_root':str(origin)})
        roots=list(dict.fromkeys(e['source_root'] for e in reverse));targets=list(dict.fromkeys(e['target_root'] for e in reverse))
        draft=self.jobs.prepare(reverse,roots[0],targets[0],retained=job['payload'].get('retained',[]),source_roots=roots,target_roots=targets)
        with self.jobs.db() as c:c.execute('INSERT INTO organization_inverses VALUES(?,?,NULL)',(job_id,draft['id']))
        return draft

    def approve_undo(self,job_id,expected_digest):
        with self.jobs.db() as c:row=c.execute('SELECT * FROM organization_inverses WHERE original_job_id=?',(job_id,)).fetchone()
        if not row:raise ValueError('Preview rollback before approving it')
        inverse=self.jobs.approve(row['draft_id'],expected_digest)
        with self.jobs.db() as c:c.execute('UPDATE organization_inverses SET inverse_job_id=? WHERE original_job_id=?',(inverse,job_id))
        return inverse
