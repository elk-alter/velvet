"""Disposable root-rename, legacy-proof and replacement-registration UI fixture.

Uses only new synthetic directories. A rename simulates a changed location, not
physical disk unplugging or a Windows drive-letter change.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=9873)
    parser.add_argument('--report',type=Path)
    args=parser.parse_args()
    if not 1<=args.port<=65535:parser.error('Port must be 1–65535')
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    with tempfile.TemporaryDirectory(prefix='velvet-reconnect-ui-') as temporary:
        root=Path(temporary).resolve()
        os.environ.update(VELVET_DATA=str(root/'data'),VELVET_SERVER='0',VELVET_BIND='127.0.0.1',VELVET_PORT=str(args.port))
        import app
        from data_guard import DataGuard
        with DataGuard(app.DATA):
            settings=app.config();settings.update(auto_scrape=False,online=False)
            (app.DATA/'settings.json').write_text(json.dumps(settings),encoding='utf-8');app.PEOPLE.start=lambda *args,**kwargs:False
            def location(name,alias):
                path=root/name;path.mkdir();(path/'Example.mp4').write_bytes(b'synthetic root fixture')
                ident=app.INVENTORY.register(str(path),alias);app.INVENTORY.scan(ident,hashing=True)
                return path,ident
            original,device=location('Original','Synthetic Reconnect')
            actual=root/'NewMount';original.rename(actual)
            wrong=root/'OtherDirectory';wrong.mkdir();shutil.copy2(actual/'Example.mp4',wrong/'Example.mp4')
            legacy,legacy_id=location('Legacy','Synthetic Legacy')
            with app.db() as c:c.execute('DELETE FROM storage_root_proofs WHERE device_id=?',(legacy_id,))
            replacement,replacement_id=location('Replacement','Synthetic Replaced')
            replacement.rename(root/'RetainedOriginal');replacement.mkdir();(replacement/'Replacement.mp4').write_bytes(b'new independent synthetic position')
            sentinel=root/'sentinel.txt';sentinel.write_text('synthetic non-task sentinel',encoding='utf-8')
            protected=[p for directory in (actual,wrong,legacy,replacement,root/'RetainedOriginal') for p in directory.iterdir()]+[sentinel]
            before={p:(hashlib.sha256(p.read_bytes()).hexdigest(),p.stat().st_mtime_ns) for p in protected}
            server=app.LocalServer(('127.0.0.1',args.port),app.Handler)
            try:
                if args.report:
                    args.report.parent.mkdir(parents=True,exist_ok=True)
                    args.report.write_text(json.dumps({'data':str(app.DATA),'device':device,'candidate':str(actual),'wrong':str(wrong),'legacy':legacy_id,'replacement':str(replacement),'replacement_id':replacement_id}),encoding='utf-8')
                print(f'synthetic reconnect fixture ready at http://127.0.0.1:{args.port}/?manage=storage',flush=True)
                server.serve_forever()
            finally:
                server.server_close()
                assert all((hashlib.sha256(p.read_bytes()).hexdigest(),p.stat().st_mtime_ns)==before[p] for p in protected)
                print('synthetic media and sentinel hashes/mtimes unchanged',flush=True)


if __name__=='__main__':main()
