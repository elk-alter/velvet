"""Isolated synthetic disk-query HTTP benchmark; never reads production media."""
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
from urllib.request import Request, urlopen
from benchmark_library import peak_memory

ROOT=Path(__file__).resolve().parents[1]


def measure(count):
    with tempfile.TemporaryDirectory(prefix='velvet-inventory-perf-') as temp:
        root=Path(temp);source=root/'synthetic';source.mkdir()
        os.environ.update(VELVET_DATA=str(root/'data'),VELVET_SERVER='0',VELVET_BIND='127.0.0.1',VELVET_PORT='8765')
        sys.path.insert(0,str(ROOT))
        import app
        device=app.INVENTORY.register(str(source),'Synthetic device')
        with app.db() as c:
            c.execute("INSERT INTO inventory_snapshots(id,device_id,state,created,processed,failed) VALUES('benchmark',?,'done',1,?,0)",(device,count))
            c.executemany('INSERT INTO inventory_files VALUES(?,?,?,?,?,?,?,?)',
                ((device,f'Series {i//50:05d}/clip {i:06d}.mp4',1048576,1,f'{i%1000:064x}',
                  json.dumps({'title':f'Example {i}','code':f'DEMO-{i}','source':'filename','height':1080,'issues':[{'code':'quality'}] if i%20==0 else []}),'[]','benchmark') for i in range(count)))
        server=app.LocalServer(('127.0.0.1',0),app.Handler)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        def request(path):
            started=time.perf_counter()
            with urlopen(Request(f'http://127.0.0.1:{server.server_port}'+path,headers={'Host':'127.0.0.1:8765'}),timeout=120) as response:body=response.read()
            return json.loads(body),time.perf_counter()-started,len(body)
        results=[]
        try:
            context,context_time,context_bytes=request('/api/context')
            for criteria in ({},{'search':'Example 12'},{'quality':'issue'},{'duplicate':'historical'}):
                query={'limit':50,**criteria};data,cold,size=request('/api/storage/query?'+urlencode(query));query['snapshot']=data['snapshot'];timings=[]
                for i in range(20):
                    query['offset']=(i%max(1,min(20,(data['total']+49)//50)))*50
                    _,elapsed,size=request('/api/storage/query?'+urlencode(query));timings.append(elapsed)
                results.append({'criteria':criteria,'total':data['total'],'cold_seconds':cold,'warm_requests':20,
                                'warm_p95_seconds':sorted(timings)[18],'warm_median_seconds':statistics.median(timings),'page_json_bytes':size})
        finally:
            server.shutdown();server.server_close();worker.join()
        return {'files':count,'context_seconds':context_time,'context_bytes':context_bytes,'queries':results,'peak_process_bytes':peak_memory(),
                'browser_first_paint':'not measured','storage':'synthetic SQLite inventory, temporary system volume; no physical media files'}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--single',type=int);parser.add_argument('--counts',nargs='+',type=int,default=[10000,50000]);parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if any(not 1<=value<=100000 for value in ([args.single] if args.single is not None else args.counts)):parser.error('Counts must be 1–100000')
    if args.single is not None:print(json.dumps(measure(args.single)));sys.exit()
    report={'python':platform.python_version(),'platform':platform.platform(),'cpu':platform.processor(),'logical_processors':os.cpu_count(),'results':[]}
    for count in args.counts:
        completed=subprocess.run([sys.executable,__file__,'--single',str(count)],cwd=ROOT,capture_output=True,text=True,check=True,timeout=600)
        report['results'].append(json.loads(completed.stdout));print(f'{count} synthetic files measured',flush=True)
    output=json.dumps(report,indent=2)
    if args.output:args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(output,encoding='utf-8')
    else:print(output)
