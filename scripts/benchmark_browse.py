"""Synthetic HTTP benchmark for the alpha.6 SQL browse endpoint."""
import argparse,json,os,platform,statistics,subprocess,sys,tempfile,threading,time
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.parse import urlencode
from benchmark_library import peak_memory
ROOT=Path(__file__).resolve().parents[1]

def measure(count):
 with tempfile.TemporaryDirectory(prefix='velvet-browse-perf-') as temp:
  os.environ.update(VELVET_DATA=str(Path(temp)/'data'),VELVET_SERVER='0',VELVET_BIND='127.0.0.1',VELVET_PORT='8765')
  sys.path.insert(0,str(ROOT));import app
  with app.db() as c:
   c.executemany('INSERT INTO videos(path,title,size,mtime,height,duration,code) VALUES(?,?,?,?,?,?,?)',((str(Path(temp)/'synthetic'/f'{i}.mp4'),f'Example {i}',1048576,1,1080,1200,f'DEMO-{i}') for i in range(count)))
  server=app.LocalServer(('127.0.0.1',0),app.Handler);worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
  def request(query):
   started=time.perf_counter()
   with urlopen(Request(f'http://127.0.0.1:{server.server_port}/api/library/query?'+urlencode(query),headers={'Host':'127.0.0.1:8765'}),timeout=300) as response:body=response.read()
   return json.loads(body),time.perf_counter()-started,len(body)
  results=[]
  try:
   for criteria in ({},{'q':'Example 12'},{'height':'1080'},{'sort':'size'}):
    query={'limit':48,**criteria};data,cold,size=request(query);query['snapshot']=data['snapshot'];times=[]
    for i in range(20):
     query['offset']=(i%max(1,min(20,(data['total']+47)//48)))*48
     _,elapsed,size=request(query);times.append(elapsed)
    results.append({'criteria':criteria,'total':data['total'],'cold_seconds':cold,'warm_requests':20,'warm_p95_seconds':sorted(times)[18],'warm_median_seconds':statistics.median(times),'page_json_bytes':size})
  finally:server.shutdown();server.server_close();worker.join()
  return {'files':count,'queries':results,'peak_process_bytes':peak_memory(),'browser_first_paint':'not measured'}

if __name__=='__main__':
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--single',type=int);parser.add_argument('--counts',nargs='+',type=int,default=[10000,50000]);parser.add_argument('--output',type=Path);args=parser.parse_args()
 if any(not 1<=value<=100000 for value in ([args.single] if args.single is not None else args.counts)):parser.error('Counts must be 1–100000')
 if args.single is not None:print(json.dumps(measure(args.single)));sys.exit()
 report={'python':platform.python_version(),'platform':platform.platform(),'results':[]}
 for count in args.counts:
  completed=subprocess.run([sys.executable,__file__,'--single',str(count)],cwd=ROOT,capture_output=True,text=True,check=True,timeout=600)
  report['results'].append(json.loads(completed.stdout));print(f'{count} synthetic files measured',flush=True)
 output=json.dumps(report,indent=2)
 if args.output:args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(output,encoding='utf-8')
 else:print(output)
