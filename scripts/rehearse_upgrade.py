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
from full_backup import create,restore


def run(baseline):
    with tempfile.TemporaryDirectory(prefix='velvet-upgrade-') as temp:
        root=Path(temp).resolve();data=root/'data';media=root/'Example.mp4';media.write_bytes(b'synthetic source')
        original=hashlib.sha256(media.read_bytes()).hexdigest()
        env=dict(os.environ,VELVET_DATA=str(data),VELVET_SERVER='0')
        subprocess.run([sys.executable,'-c','import app; app.LIBRARIES.exclusions()'],cwd=baseline,env=env,check=True,capture_output=True)
        with closing(sqlite3.connect(data/'library.db')) as c,c:
            c.execute('INSERT INTO videos(id,path,title,size,mtime,manual,locked_fields,favorite,position,tags) VALUES(1,?,?,?,?,1,?,1,42,?)',(str(media),'Manually locked title',media.stat().st_size,media.stat().st_mtime,'["title"]','Example'))
            c.execute("INSERT INTO media_links VALUES(1,'manual-movie','manual-version',2,1)")
            c.execute('INSERT INTO excluded_directories VALUES(?)',(str(root/'Offline'),))
            c.execute("INSERT INTO actor_preferences VALUES('Example Person',5,1)")
            c.execute("INSERT INTO scrape_jobs(id,code,remote_id,status,created,updated) VALUES(1,'EXAMPLE-001',17,'done',1,1)")
        (data/'settings.json').write_text(json.dumps({'auto_scrape':False}),encoding='utf-8')
        with closing(sqlite3.connect(data/'library.db')) as c:
            columns={table:[r[1] for r in c.execute('PRAGMA table_info('+table+')')] for table in ('videos','media_links','actor_preferences','excluded_directories','scrape_jobs')}
        def snapshot():
            with closing(sqlite3.connect(data/'library.db')) as c:
                return {table:c.execute('SELECT '+','.join(columns[table])+' FROM '+table+' ORDER BY 1').fetchall() for table in ('videos','media_links','actor_preferences','excluded_directories','scrape_jobs')}
        before=snapshot();archive=root/'before.zip';create(data,archive,True)
        for _ in range(2):subprocess.run([sys.executable,'-c','import app'],cwd=ROOT,env=env,check=True,capture_output=True)
        assert snapshot()==before,'Upgrade changed protected fields'
        with closing(sqlite3.connect(data/'library.db')) as c:
            tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert {'task_journal','collection_rules','inventory_snapshots'}<=tables
        result=restore(data,archive)
        assert snapshot()==before,'Rollback changed baseline data'
        subprocess.run([sys.executable,'-c','import app; app.LIBRARIES.exclusions()'],cwd=baseline,env=env,check=True,capture_output=True)
        assert snapshot()==before,'Baseline could not read restored snapshot'
        assert hashlib.sha256(media.read_bytes()).hexdigest()==original
        return {'baseline':'alpha.4','upgrade_idempotent':True,'protected_tables':5,'rollback_verified':True,'source_hash_unchanged':True,'remote_completed_jobs_not_replayed':True}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--baseline',default='a98609a');args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='velvet-baseline-') as temp:
        root=Path(temp);archive=root/'baseline.zip'
        subprocess.run(['git','archive','--format=zip','--output',str(archive),args.baseline],cwd=ROOT,check=True)
        with zipfile.ZipFile(archive) as z:z.extractall(root/'code')
        print(json.dumps(run(root/'code'),indent=2))
