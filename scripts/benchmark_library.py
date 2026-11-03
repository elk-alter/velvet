"""Reproducible synthetic library baseline; never uses production data."""
import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def peak_memory():
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD)]+[(name,ctypes.c_size_t) for name in ('PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage','QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage')]
        counters=Counters();counters.cb=ctypes.sizeof(counters)
        ctypes.windll.kernel32.GetCurrentProcess.restype=wintypes.HANDLE
        ctypes.windll.psapi.GetProcessMemoryInfo.argtypes=[wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD]
        if ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.windll.kernel32.GetCurrentProcess(),ctypes.byref(counters),counters.cb):return counters.PeakWorkingSetSize
        return None
    import resource
    value=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform=='darwin' else value*1024


def measure(count):
    with tempfile.TemporaryDirectory(prefix='velvet-perf-') as temp:
        os.environ.update(VELVET_DATA=str(Path(temp)/'data'),VELVET_SERVER='0',VELVET_BIND='127.0.0.1',VELVET_PORT='8765')
        sys.path.insert(0,str(ROOT))
        import app
        with app.db() as c:
            c.executemany('INSERT INTO videos(path,title,size,mtime,height,duration,code) VALUES(?,?,?,?,?,?,?)',
                          ((str(Path(temp)/'synthetic'/f'{i}.mp4'),f'Example {i}',1024*1024,1,1080,1200,f'DEMO-{i}') for i in range(count)))
        server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{server.server_port}'
        timings=[];sizes=[]
        try:
            for _ in range(2):
                start=time.perf_counter();size=0
                with urlopen(Request(base+'/api/library?compact=1',headers={'Host':'127.0.0.1:8765'}),timeout=300) as response:
                    for block in iter(lambda:response.read(65536),b''):size+=len(block)
                timings.append(round(time.perf_counter()-start,4));sizes.append(size)
            start=time.perf_counter()
            with urlopen(Request(base+'/api/library-revision',headers={'Host':'127.0.0.1:8765'}),timeout=30) as response:revision_bytes=len(response.read())
            revision_seconds=round(time.perf_counter()-start,4)
        finally:server.shutdown();server.server_close();thread.join()
        return {'files':count,'cold_library_http_seconds':timings[0],'warm_library_http_seconds':timings[1],
                'revision_check_seconds':revision_seconds,'revision_response_bytes':revision_bytes,'response_bytes':sizes[-1],'peak_process_bytes':peak_memory(),
                'browser_first_paint':'not measured','poll_interval_seconds':7,
                'note':'Synthetic indexed paths; no original media, real OCR or remote metadata. HTTP time is not browser first paint.'}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--counts',type=int,nargs='+',default=[10000,50000])
    parser.add_argument('--single',type=int)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if args.single:
        print(json.dumps(measure(args.single)));sys.exit()
    if any(not 1<=count<=100000 for count in args.counts):parser.error('Counts must be 1–100000')
    report={'python':platform.python_version(),'platform':platform.platform(),'results':[]}
    for count in args.counts:
        completed=subprocess.run([sys.executable,__file__,'--single',str(count)],cwd=ROOT,text=True,capture_output=True,check=True,timeout=600)
        report['results'].append(json.loads(completed.stdout));print(f'{count} synthetic files measured',flush=True)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,indent=2),encoding='utf-8')
    else:print(json.dumps(report,indent=2))
