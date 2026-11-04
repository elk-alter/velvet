"""Disposable playable WebM fixture for detail, exclusion and continuation checks.

Only generates synthetic files in a new temporary directory. Does not scan media,
start scraper workers or open production data. Requires FFmpeg on PATH.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=9873)
    parser.add_argument('--report',type=Path,help='Optional private local fixture descriptor')
    args=parser.parse_args()
    if not 1<=args.port<=65535:parser.error('Port must be 1–65535')
    encoder=shutil.which('ffmpeg')
    if not encoder:parser.error('FFmpeg is required on PATH')
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    with tempfile.TemporaryDirectory(prefix='velvet-playback-') as temporary:
        root=Path(temporary).resolve();media=root/'Synthetic-Media'
        series=media/'Synthetic Series';other=media/'Other Film'
        series.mkdir(parents=True);other.mkdir()
        os.environ.update(VELVET_DATA=str(root/'data'),VELVET_SERVER='0',VELVET_BIND='127.0.0.1',VELVET_PORT=str(args.port))
        import app
        from data_guard import DataGuard
        with DataGuard(app.DATA):
            clips=[]
            for part,color,seconds in ((20,'royalblue',6),(21,'seagreen',4)):
                path=series/f'Part-{part}.webm'
                subprocess.run([encoder,'-hide_banner','-loglevel','error','-nostdin','-f','lavfi','-i',f'color=c={color}:size=320x180:rate=12','-t',str(seconds),'-an','-c:v','libvpx','-deadline','realtime','-cpu-used','8','-b:v','160k',str(path)],check=True,capture_output=True)
                clips.append(path)
            alternate=other/'Other.webm';shutil.copyfile(clips[1],alternate)
            sentinel=media/'sentinel.txt';sentinel.write_text('independent synthetic sentinel',encoding='utf-8')
            protected=clips+[alternate,sentinel]
            before={p:(hashlib.sha256(p.read_bytes()).hexdigest(),p.stat().st_mtime_ns) for p in protected}
            with app.db() as c:
                c.execute('INSERT INTO folders(path) VALUES(?)',(str(media),))
                for part in range(1,23):
                    path=series/f'Part-{part}.webm';exists=path.is_file();stat=path.stat() if exists else None
                    c.execute('INSERT INTO videos(id,path,title,size,mtime,duration,height,width,codec,missing,actors,locked_fields,manual) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1)',(part,str(path),'Synthetic Series',stat.st_size if stat else 100,stat.st_mtime if stat else 1,6 if part==20 else 4,180,320,'vp8',0 if exists else 1,'Synthetic Actor' if part==20 else '','["title"]'))
                    c.execute('INSERT INTO media_links VALUES(?,?,?,?,1)',(part,'synthetic-series','webm',part))
                stat=alternate.stat()
                c.execute('INSERT INTO videos(id,path,title,size,mtime,duration,height,width,codec,locked_fields,manual) VALUES(?,?,?,?,?,?,?,?,?,?,1)',(30,str(alternate),'Other Synthetic Film',stat.st_size,stat.st_mtime,4,180,320,'vp8','["title"]'))
                c.execute('INSERT INTO media_links VALUES(?,?,?,?,1)',(30,'other-work','other',1))
                c.execute('INSERT INTO movie_preferences VALUES(?,?,?)',('synthetic-series','webm',20))
            settings=app.config();settings.update(auto_scrape=False,scrape_engine='builtin',online=False)
            (app.DATA/'settings.json').write_text(json.dumps(settings),encoding='utf-8')
            app.PEOPLE.start=lambda *args,**kwargs:False
            app.BROWSE.refresh()
            class Preview(app.Handler):
                def do_GET(self):
                    if self.path.startswith('/thumb/'):
                        return self.send(b'<svg xmlns="http://www.w3.org/2000/svg" width="400" height="600"><rect width="400" height="600" fill="#506674"/><text x="28" y="300" fill="white" font-size="24">SYNTHETIC MEDIA</text></svg>','image/svg+xml')
                    return super().do_GET()
            server=app.LocalServer(('127.0.0.1',args.port),Preview)
            if args.report:
                args.report.parent.mkdir(parents=True,exist_ok=True)
                args.report.write_text(json.dumps({'data':str(app.DATA),'media':str(media),'first':20,'next':21,'missing_next':22,'other':30}),encoding='utf-8')
            print(f'synthetic playable fixture ready at http://127.0.0.1:{args.port}',flush=True)
            try:server.serve_forever()
            finally:
                server.server_close()
                assert all((hashlib.sha256(p.read_bytes()).hexdigest(),p.stat().st_mtime_ns)==before[p] for p in protected),'Synthetic media or sentinel changed'
                print('synthetic media hashes and mtimes unchanged',flush=True)


if __name__=='__main__':
    main()
