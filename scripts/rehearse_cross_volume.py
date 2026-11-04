"""Opt-in real cross-volume rehearsal using only newly created synthetic files.

The supplied parents must already exist. Only unique TemporaryDirectory children
are modified and removed. No service is started or production data imported.
"""
import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from data_guard import DataGuard
from storage_inventory import volume_identity,volume_details,StorageInventory
from organization_service import OrganizationService
from organization_executor import CopyExecutor


def snapshot(paths):
    return {path.name:(hashlib.sha256(path.read_bytes()).hexdigest(),path.stat().st_mtime_ns) for path in paths}


def run(source_parent,target_parent):
    if not __debug__:raise RuntimeError('Run rehearsal without Python optimization flags')
    source_parent=Path(source_parent).resolve();target_parent=Path(target_parent).resolve()
    if not source_parent.is_dir() or not target_parent.is_dir():raise ValueError('Both parent directories must already exist')
    source_token,stable_source=volume_identity(source_parent);target_token,stable_target=volume_identity(target_parent)
    if source_token==target_token:raise ValueError('Different actual volumes are required')
    source_fs=volume_details(source_parent)['filesystem'];target_fs=volume_details(target_parent)['filesystem']
    with tempfile.TemporaryDirectory(prefix='velvet-volume-source-',dir=source_parent) as left,tempfile.TemporaryDirectory(prefix='velvet-volume-target-',dir=target_parent) as right:
        left=Path(left).resolve();right=Path(right).resolve();data=left/'data';source=left/'source';source.mkdir()
        os.environ.update(VELVET_DATA=str(data),VELVET_SERVER='0',VELVET_BIND='127.0.0.1',VELVET_PORT='8765')
        with DataGuard(data):
            import app
            paths=[];ids=[]
            for part in (1,2):
                path=source/f'Example part{part}.mp4';path.write_bytes((f'synthetic part {part}\n'.encode())*4096);paths.append(path)
                with app.db() as c:
                    ids.append(c.execute('INSERT INTO videos(path,title,size,mtime,code,manual,locked_fields,favorite,rating,position,tags,height) VALUES(?,?,?,?,?,1,?,1,5,23,?,1080)',(str(path),f'Manual {part}',path.stat().st_size,path.stat().st_mtime,'DEMO-1','["title"]','Synthetic')).lastrowid)
            subtitle=source/'Example part1.srt';subtitle.write_bytes(b'1\n00:00:00,000 --> 00:00:01,000\nSynthetic subtitle\n');paths.append(subtitle)
            sentinels=[left/'unrelated.txt',right/'unrelated.txt']
            for p in sentinels:p.write_bytes(b'non-task synthetic sentinel')
            untouched=[p.read_bytes() for p in sentinels];original=snapshot(paths)
            app.CORE.reconcile();app.CORE.relation([{'id':vid,'movie_key':'manual-movie','version_key':'manual-version','part_no':part} for part,vid in enumerate(ids,1)])
            app.REVIEW.mark(ids,flag='watermark')
            def protected():
                with app.db() as c:
                    return {'videos':[tuple(r) for r in c.execute('SELECT id,title,manual,locked_fields,favorite,rating,position,tags FROM videos ORDER BY id')],
                            'links':[tuple(r) for r in c.execute('SELECT * FROM media_links ORDER BY video_id')],
                            'quality':[tuple(r) for r in c.execute('SELECT video_id,flags,decision FROM file_reviews ORDER BY video_id')]}
            before=protected()
            inventory=StorageInventory(app.db,lambda p:{'title':'Synthetic','source':'filename'},lambda:[])
            device=inventory.register(str(source),'Synthetic source');inventory.scan(device,hashing=True)
            service=OrganizationService(app.db,inventory)
            def prepare(target,operation):
                plan=inventory.plan(device,str(target),operation=operation)
                draft=service.prepare(plan['id'],plan['digest'],list(range(len(plan['entries']))),operation)
                return draft,service.approve(draft['id'],draft['digest'])
            draft,job=prepare(right/'copies','copy')
            assert any(e['source_device']!=e['target_device'] for e in draft['payload']['entries'])
            def interrupt(stage,*args):
                if stage=='committed':raise SystemExit('synthetic checkpoint interruption')
            try:CopyExecutor(service.jobs,service.bind_copy,interrupt).run(job)
            except SystemExit:pass
            else:raise AssertionError('Interruption checkpoint did not run')
            assert any(step['state']=='committed' for step in service.jobs.get(job)['steps'])
            assert snapshot(paths)==original
            # Synchronous executor is unwound; DataGuard is held throughout.
            app.LEASES.recover();service.jobs.recover();service.control(job,'resume')
            assert service.run_once(job)['state']=='done'
            assert protected()==before and snapshot(paths)==original
            targets=[Path(e['destination']) for e in draft['payload']['entries']]
            for entry,path in zip(draft['payload']['entries'],targets):
                assert hashlib.sha256(path.read_bytes()).hexdigest()==entry['fingerprint']['sha256']
            with app.db() as c:
                assert c.execute('SELECT COUNT(*) FROM videos').fetchone()[0]==2
                assert c.execute('SELECT COUNT(*) FROM media_locations').fetchone()[0]==4
                assert c.execute('SELECT COUNT(*) FROM media_resources').fetchone()[0]>=1
            assert len(app.CORE.hierarchy())==1
            inverse=service.prepare_inverse(job);undo=service.approve_inverse(job,inverse['digest'])
            assert service.run_once(undo)['state']=='done'
            assert protected()==before and snapshot(paths)==original and all(p.exists() for p in targets)
            _,move=prepare(left/'renamed','move')
            assert service.run_once(move)['state']=='done'
            assert not any(p.exists() for p in paths)
            inverse=service.prepare_inverse(move);undo_move=service.approve_inverse(move,inverse['digest'])
            assert service.run_once(undo_move)['state']=='done'
            assert snapshot(paths)==original and protected()==before
            assert [p.read_bytes() for p in sentinels]==untouched
            assert not app.LEASES.active() and not app.LEASES.waiting()
            return {'different_actual_volumes':True,'stable_volume_identities':stable_source and stable_target,
                    'source_filesystem':source_fs,'target_filesystem':target_fs,'videos':2,'companions':1,
                    'cross_volume_copy_verified':True,'checkpoint_interruption_resume':True,
                    'copy_binding_undo_retains_files':True,'same_volume_move_and_inverse':True,
                    'protected_metadata_preserved':True,'source_hashes_names_mtime_restored':True,
                    'non_task_files_unchanged':True,'active_leases':0,
                    'physical_unplug':'not tested','exfat':'not tested','browser':'not tested'}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-parent',type=Path,default=Path(tempfile.gettempdir()))
    parser.add_argument('--target-parent',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(run(args.source_parent,args.target_parent),indent=2))
