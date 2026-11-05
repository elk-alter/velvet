"""Disposable cold browser fixture for 10k/50k synthetic library measurements.

Does not prebuild the browse index. The first browser query pays relationship,
projection and fixed-result construction costs. Covers are simple synthetic SVGs;
no real media, production data or background scraper workers are used.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlencode


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--count',type=int,default=50000)
    parser.add_argument('--port',type=int,default=9873)
    parser.add_argument('--report',type=Path,help='Optional private local descriptor')
    args=parser.parse_args()
    if not 1<=args.count<=100000:parser.error('Count must be 1–100000')
    if not 1<=args.port<=65535:parser.error('Port must be 1–65535')
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    with tempfile.TemporaryDirectory(prefix='velvet-browse-ui-') as temporary:
        root=Path(temporary).resolve()
        os.environ.update(VELVET_DATA=str(root/'data'),VELVET_SERVER='0',VELVET_BIND='127.0.0.1',VELVET_PORT=str(args.port))
        import app
        from data_guard import DataGuard
        with DataGuard(app.DATA):
            class Preview(app.Handler):
                def do_GET(self):
                    if (self.path=='/' or self.path.startswith('/?')) and not getattr(self.server,'fixture_browser_seen',False):
                        self.server.fixture_browser_seen=True
                        print('Browser user agent: '+self.headers.get('User-Agent','unspecified')[:240],flush=True)
                    if self.path.startswith('/thumb/'):
                        return self.send(b'<svg xmlns="http://www.w3.org/2000/svg" width="400" height="600"><rect width="400" height="600" fill="#506674"/><text x="24" y="300" fill="white" font-size="24">SYNTHETIC MEDIA</text></svg>','image/svg+xml')
                    return super().do_GET()
            server=app.LocalServer(('127.0.0.1',args.port),Preview)
            try:
                with app.db() as c:
                    c.executemany('INSERT INTO videos(path,title,size,mtime,height,duration,code) VALUES(?,?,?,?,?,?,?)',((str(root/'Synthetic-Media'/f'{index}.mp4'),f'Example {index}',1048576,1,1080,1200,f'DEMO-{index}') for index in range(args.count)))
                settings=app.config();settings.update(auto_scrape=False,online=False)
                (app.DATA/'settings.json').write_text(json.dumps(settings),encoding='utf-8')
                app.PEOPLE.start=lambda *args,**kwargs:False
                with app.db() as c:
                    assert c.execute('SELECT COUNT(*) FROM browse_projection').fetchone()[0]==0
                    assert c.execute('SELECT COUNT(*) FROM media_links').fetchone()[0]==0
                view={'mode':'all','page':1,'pageSize':48,'actor':'','actorsView':False,'tags':[],'filters':{}}
                url=f'http://127.0.0.1:{args.port}/?'+urlencode({'view':json.dumps(view,separators=(',',':'))})
                if args.report:
                    args.report.parent.mkdir(parents=True,exist_ok=True)
                    args.report.write_text(json.dumps({'data':str(app.DATA),'files':args.count,'prebuilt':False,'url':url}),encoding='utf-8')
                print(f'{args.count} synthetic files ready without browse prebuild at {url}',flush=True)
                server.serve_forever()
            finally:server.server_close()


if __name__=='__main__':main()
