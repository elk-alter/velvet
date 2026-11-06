"""Rehearse baseline initialization, upgrade and full-data rollback using disposable files."""
import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from contextlib import closing

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from full_backup import create,restore,preview_restore


def run(baseline):
    with tempfile.TemporaryDirectory(prefix='velvet-upgrade-') as temp:
        root=Path(temp).resolve();data=root/'data';source=root/'source';source.mkdir();media=source/'Example.mp4';media.write_bytes(b'synthetic source')
        original=hashlib.sha256(media.read_bytes()).hexdigest()
        stamp=media.stat().st_mtime_ns
        env=dict(os.environ,VELVET_DATA=str(data),VELVET_SERVER='0')
        fixture_dir=str(ROOT/'scripts')
        seed_code="import app,sys;sys.path.insert(0,sys.argv[1]);import upgrade_fixture as f;f.seed(app,sys.argv[2]);f.assert_browse(app)"
        result=subprocess.run([sys.executable,'-c',seed_code,fixture_dir,str(source)],cwd=baseline,env=env,capture_output=True,text=True)
        if result.returncode:raise RuntimeError(result.stderr)
        from scripts.upgrade_fixture import snapshot,files,cache_snapshot,protected_columns,TABLES
        image_hashes=files(data)
        cache_before=cache_snapshot(data)
        configuration={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in data.glob('*.json')}
        originals={p.name:(hashlib.sha256(p.read_bytes()).hexdigest(),p.stat().st_mtime_ns) for p in source.iterdir()}
        columns=protected_columns(data)
        before=snapshot(data,columns)
        expected=root/'expected.json';expected.write_text(json.dumps({'tables':before,'images':image_hashes,'columns':columns}),encoding='utf-8')
        archive=root/'before.zip';create(data,archive,True)
        boot_code="import app,sys;sys.path.insert(0,sys.argv[1]);import upgrade_fixture as f;f.start_without_workers(app);f.assert_browse(app)"
        for _ in range(2):
            completed=subprocess.run([sys.executable,'-c',boot_code,fixture_dir],cwd=ROOT,env=env,capture_output=True,text=True)
            if completed.returncode:raise RuntimeError(completed.stderr)
        assert snapshot(data,columns)==before,'Upgrade changed protected fields'
        with closing(sqlite3.connect(data/'library.db')) as c:
            tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert {'task_journal','collection_rules','inventory_snapshots'}<=tables
            for field in {'domestic','amateur'}-set(columns['videos']):
                assert c.execute('SELECT COUNT(*) FROM videos WHERE '+field+' IS NOT 0').fetchone()[0]==0
        physical=r'''
import app,sys,json
from pathlib import Path
sys.path.insert(0,sys.argv[3]);import upgrade_fixture as fixture
expected=json.loads(Path(sys.argv[4]).read_text('utf-8'))
source,target=map(Path,sys.argv[1:3]);inventory=app.INVENTORY;service=app.ORGANIZATION
device=inventory.register(str(source),'Synthetic upgrade disk');inventory.scan(device,hashing=True)
def execute(operation,destination):
    plan=inventory.plan(device,str(destination),layout='preserve')
    draft=service.prepare(plan['id'],plan['digest'],[0],operation)
    job=service.approve(draft['id'],draft['digest'])
    assert service.run_once(job)['state']=='done'
    fixture.assert_protected(app,expected['tables'],expected['images'],expected['columns'])
    return job
copy=execute('copy',target/'copy')
move=execute('move',target/'move')
blocked=app.CORE.preview_restore(app.CORE.backup('post-move'))
assert blocked['ready'],blocked['problems']
from full_backup import preview_restore,restore
old_report=preview_restore(app.DATA,Path(sys.argv[5]))
assert not old_report['ready'],'Pre-move snapshot unexpectedly matched moved files'
try:restore(app.DATA,Path(sys.argv[5]),old_report)
except ValueError:pass
else:raise AssertionError('Old database could replace unresolved physical state')
assert not (source/'Example.mp4').exists() and (target/'move'/'Example.mp4').exists()
reverse=service.move.prepare_undo(move);undo=service.move.approve_undo(move,reverse['digest'])
assert service.run_once(undo)['state']=='done'
fixture.assert_protected(app,expected['tables'],expected['images'],expected['columns'])
assert (source/'Example.mp4').exists() and (target/'copy'/'Example.mp4').exists()
fixture.drain_builtin_with_mock(app)
'''
        completed=subprocess.run([sys.executable,'-c',physical,str(source),str(root/'organized'),fixture_dir,str(expected),str(archive)],cwd=ROOT,env=env,capture_output=True,text=True)
        if completed.returncode:raise RuntimeError(completed.stderr)
        try:restore(data,archive)
        except ValueError:pass
        else:raise AssertionError('Physical history restored without reconciliation')
        report=preview_restore(data,archive)
        assert report['ready'],report['problems']
        assert len(report['retained_files'])==3
        result=restore(data,archive,report)
        assert (Path(result['rollback'])/'library.db').is_file()
        assert list((data/'restore-audits').glob('*.json'))
        assert snapshot(data)==before,'Rollback changed baseline data'
        assert cache_snapshot(data)==cache_before,'Rollback changed scraper cache/queue/cooldown data'
        assert all(hashlib.sha256((data/name).read_bytes()).hexdigest()==digest for name,digest in configuration.items()),'Rollback changed configuration'
        completed=subprocess.run([sys.executable,'-c',boot_code,fixture_dir],cwd=baseline,env=env,capture_output=True,text=True)
        if completed.returncode:raise RuntimeError(completed.stderr)
        assert snapshot(data)==before,'Baseline could not read restored snapshot'
        assert hashlib.sha256(media.read_bytes()).hexdigest()==original
        assert media.stat().st_mtime_ns==stamp
        assert hashlib.sha256((root/'organized'/'copy'/'Example.mp4').read_bytes()).hexdigest()==original
        assert files(data)==image_hashes,'Restored image/evidence bytes differ'
        for name,proof in originals.items():
            path=source/name
            assert (hashlib.sha256(path.read_bytes()).hexdigest(),path.stat().st_mtime_ns)==proof
        return {'image_evidence_files':len(image_hashes),'queue_reconciliation_verified':True,'builtin_tables_restored':len(cache_before),'configuration_files_restored':len(configuration),'baseline':'selected Git ref','upgrade_idempotent':True,'protected_tables':len(TABLES),'copy_move_inverse_verified':True,'rollback_verified':True,'journal_receipt_preserved':True,'source_hash_mtime_unchanged':True,'copy_retained':True,'remote_completed_jobs_not_replayed':True,'browse_and_detail_verified':True}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--baseline',default='44f7373');args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='velvet-baseline-') as temp:
        root=Path(temp);archive=root/'baseline.zip'
        subprocess.run(['git','archive','--format=zip','--output',str(archive),args.baseline],cwd=ROOT,check=True)
        with zipfile.ZipFile(archive) as z:z.extractall(root/'code')
        result=run(root/'code');result['baseline']=args.baseline;print(json.dumps(result,indent=2))
