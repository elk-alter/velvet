"""Bounded detail HTTP measurements for unusually large single works; synthetic only."""
import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from benchmark_library import peak_memory

ROOT=Path(__file__).resolve().parents[1]


def measure(count,shape):
    with tempfile.TemporaryDirectory(prefix='velvet-detail-perf-') as tmp:
        root=Path(tmp).resolve()
        os.environ.update(VELVET_DATA=str(root/'data'),VELVET_SERVER='0',VELVET_BIND='127.0.0.1',VELVET_PORT='8765')
        sys.path.insert(0,str(ROOT));import app
        with app.db() as c:
            c.executemany('INSERT INTO videos(id,path,title,size,mtime,height,duration,codec,manual,locked_fields,plot) VALUES(?,?,?,?,?,?,?,?,1,?,?)',((i+1,str(root/'synthetic'/f'File-{i}.mp4'),'Synthetic work',100,1,1080,10,'h265','["title"]','Synthetic description. '*5) for i in range(count)))
            c.executemany('INSERT INTO media_links VALUES(?,?,?,?,1)',((i+1,'synthetic-work',f'cut-{i}' if shape=='versions' else 'single-cut',1 if shape=='versions' else i+1) for i in range(count)))
        began=time.perf_counter();app.BROWSE.refresh();prepare=time.perf_counter()-began
        def forbidden(*args,**kwargs):raise AssertionError('Detail hydrated an entire work')
        app.CORE.hierarchy=forbidden
        presented=0;present=app.CORE.present
        def counted(row):
            nonlocal presented
            presented+=1;return present(row)
        app.CORE.present=counted
        server=app.LocalServer(('127.0.0.1',0),app.Handler)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        base=f'http://127.0.0.1:{server.server_port}'
        def request(path):
            started=time.perf_counter()
            with urlopen(Request(base+path,headers={'Host':'127.0.0.1:8765'}),timeout=90) as response:body=response.read()
            return json.loads(body),time.perf_counter()-started,len(body)
        try:
            query={'id':1,'exact':'1'}
            first,initial,size=request('/api/library/detail?'+urlencode(query));query['revision']=first['detail']['revision']
            times=[];maximum_size=size;maximum_presented=presented
            for i in range(20):
                query['version_offset' if shape=='versions' else 'part_offset']=(i%min(20,(count+19)//20))*10 if shape=='versions' else (i%min(20,(count+19)//20))*20
                presented=0;result,elapsed,size=request('/api/library/detail?'+urlencode(query))
                assert result['detail']['total_files']==count
                assert len(result['detail']['versions']['items'])<=10 and len(result['detail']['parts']['items'])<=20
                assert presented<=21
                times.append(elapsed);maximum_size=max(maximum_size,size);maximum_presented=max(maximum_presented,presented)
            try:request('/api/library/movie?id=1&max_files=100')
            except HTTPError as error:
                assert error.code==400 and '100' in json.load(error)['error']
            else:raise AssertionError('Large edit scope was not rejected')
            return {'files':count,'shape':shape,'index_prepare_seconds':prepare,'first_detail_http_seconds':initial,'warm_requests':20,'warm_p95_seconds':sorted(times)[18],'warm_median_seconds':statistics.median(times),'max_response_bytes':maximum_size,'max_presented_files':maximum_presented,'edit_scope_rejected_before_hydration':True,'peak_process_bytes':peak_memory(),'browser_paint':'not measured'}
        finally:server.shutdown();server.server_close();worker.join()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--single',type=int);parser.add_argument('--shape',choices=['parts','versions'],default='parts')
    parser.add_argument('--counts',nargs='+',type=int,default=[10000,50000]);parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if any(not 101<=n<=100000 for n in ([args.single] if args.single is not None else args.counts)):parser.error('Counts must be 101–100000')
    if args.single is not None:print(json.dumps(measure(args.single,args.shape)));sys.exit()
    report={'python':platform.python_version(),'platform':platform.platform(),'results':[]}
    for count in args.counts:
        for shape in ('parts','versions'):
            completed=subprocess.run([sys.executable,__file__,'--single',str(count),'--shape',shape],cwd=ROOT,capture_output=True,text=True,check=True,timeout=600)
            report['results'].append(json.loads(completed.stdout));print(f'{count} synthetic files / {shape} measured',flush=True)
    output=json.dumps(report,indent=2)
    if args.output:args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(output,encoding='utf-8')
    else:print(output)
