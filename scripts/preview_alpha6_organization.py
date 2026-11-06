"""Independent synthetic paused copy task for browser skip/continue verification."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlencode


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=9873)
    parser.add_argument('--report',type=Path)
    parser.add_argument('--reconnect-source',action='store_true',help='Rename the synthetic source directory after approval')
    args=parser.parse_args()
    if not 1<=args.port<=65535:parser.error('Port must be 1–65535')
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    with tempfile.TemporaryDirectory(prefix='velvet-organize-ui-') as temporary:
        root=Path(temporary).resolve();source=root/'Synthetic-Source';source.mkdir()
        files=[source/'Skip Example.mp4',source/'Skip Example.srt',source/'Continue Example.mp4',source/'sentinel.txt']
        for index,path in enumerate(files):path.write_bytes((f'independent synthetic content {index}\n'.encode())*1024)
        before={p:(hashlib.sha256(p.read_bytes()).hexdigest(),p.stat().st_mtime_ns) for p in files}
        os.environ.update(VELVET_DATA=str(root/'data'),VELVET_SERVER='0',VELVET_BIND='127.0.0.1',VELVET_PORT=str(args.port))
        import app
        from data_guard import DataGuard
        with DataGuard(app.DATA):
            settings=app.config();settings.update(auto_scrape=False,online=False)
            (app.DATA/'settings.json').write_text(json.dumps(settings),encoding='utf-8')
            app.PEOPLE.start=lambda *args,**kwargs:False
            device=app.INVENTORY.register(str(source),'Synthetic Source')
            app.INVENTORY.scan(device,hashing=True)
            plan=app.INVENTORY.plan(device,str(root/'Synthetic-Target'),layout='preserve')
            draft=app.ORGANIZATION.prepare(plan['id'],plan['digest'],list(range(len(plan['entries']))),'copy')
            job=app.ORGANIZATION.approve(draft['id'],draft['digest']);app.ORGANIZATION.control(job,'pause')
            new_source=None
            if args.reconnect_source:
                if str(source) not in draft['payload'].get('root_proofs',{}):
                    raise ValueError('Fixture requires a stable local volume and directory identity')
                new_source=root/'Reconnected-Source'
                source.rename(new_source)
                before={new_source/path.name:stamp for path,stamp in before.items()}
                app.ORGANIZATION.start()
            ordinal=next(i for i,e in enumerate(draft['payload']['entries']) if Path(e['source']).name=='Skip Example.mp4')
            url=f'http://127.0.0.1:{args.port}/?'+urlencode({'manage':'organization-task','m_source':job})
            server=app.LocalServer(('127.0.0.1',args.port),app.Handler)
            try:
                if args.report:
                    args.report.parent.mkdir(parents=True,exist_ok=True)
                    args.report.write_text(json.dumps({'data':str(app.DATA),'job':job,'ordinal':ordinal,'url':url,'reconnected_source':str(new_source) if new_source else None}),encoding='utf-8')
                print('synthetic paused copy ready at '+url,flush=True)
                server.serve_forever()
            finally:
                app.ORGANIZATION.stop()
                if app.ORGANIZATION.thread:app.ORGANIZATION.thread.join(8)
                server.server_close()
                assert not (app.ORGANIZATION.thread and app.ORGANIZATION.thread.is_alive())
                assert all((hashlib.sha256(p.read_bytes()).hexdigest(),p.stat().st_mtime_ns)==before[p] for p in before)
                print(json.dumps({'state':app.ORGANIZATION.jobs.get(job)['state'],'progress':app.ORGANIZATION.jobs.progress(job),'synthetic_sources_and_sentinel_unchanged':True}),flush=True)


if __name__=='__main__':main()
