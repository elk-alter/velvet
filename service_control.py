"""Local service start/stop/restart with health checks; never force-kills processes."""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent


def request(url, token=None):
    headers = {'X-Velvet-Token': token} if token else {}
    with urlopen(Request(url, data=b'{}' if token else None, headers=headers), timeout=3) as response:
        return json.load(response)


def local_instance(data):
    record = json.loads((data/'runtime-instance.json').read_text('utf-8'))
    parsed = urlsplit(record['url'])
    if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1') or parsed.username or parsed.password:
        raise ValueError('Refusing a nonlocal control address')
    health = request(record['url']+'/api/health')
    if health.get('application') != 'velvet' or health.get('instance_id') != hashlib.sha256(record['token'].encode()).hexdigest():
        raise ValueError('Address is not a Velvet service')
    return record


def wait_exit(pid, deadline):
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.windll.kernel32
        kernel.OpenProcess.restype=wintypes.HANDLE
        handle=kernel.OpenProcess(0x00100000,False,int(pid))
        if not handle:return
        try:
            result=kernel.WaitForSingleObject(wintypes.HANDLE(handle),max(1,int((deadline-time.monotonic())*1000)))
            if result!=0:raise ValueError('HTTP stopped but process exit was not confirmed')
        finally:kernel.CloseHandle(wintypes.HANDLE(handle))
    else:
        while time.monotonic()<deadline:
            try:os.kill(int(pid),0)
            except ProcessLookupError:return
            time.sleep(.1)
        raise ValueError('HTTP stopped but process exit was not confirmed')


def stop(data, timeout):
    record = local_instance(data)
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        response = request(record['url']+'/api/runtime/stop', record['token'])
        if response.get('ok'):
            while time.monotonic() < deadline:
                try:request(record['url']+'/api/health')
                except (URLError, OSError):
                    wait_exit(record['pid'],deadline)
                    return record
                time.sleep(.2)
            break
        time.sleep(.5)
    try:request(record['url']+'/api/runtime/resume', record['token'])
    except URLError:pass
    raise ValueError('Tasks did not stop before timeout; no process was force-killed')


def start(data, port, timeout):
    url = f'http://127.0.0.1:{port}'
    try:
        health = request(url+'/api/health')
    except URLError:
        health = None
    if health:
        if health.get('application') != 'velvet':
            raise ValueError('Port occupied by another service')
        existing = local_instance(data)
        if existing['url'].rstrip('/') != url:
            raise ValueError('Existing data directory belongs to another address')
        print('Velvet is already healthy at '+url)
        return
    data.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, VELVET_DATA=str(data), VELVET_PORT=str(port))
    flags = getattr(subprocess, 'DETACHED_PROCESS', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
    with (data/'service.stdout.log').open('ab') as out, (data/'service.stderr.log').open('ab') as err:
        process = subprocess.Popen([sys.executable, str(ROOT/'app.py'), '--no-browser', '--port', str(port)], cwd=ROOT, env=env,
                                   stdin=subprocess.DEVNULL, stdout=out, stderr=err, creationflags=flags,
                                   start_new_session=os.name != 'nt', close_fds=True)
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ValueError('Startup failed; see service.stderr.log in the selected data directory')
        try:
            record = local_instance(data)
            if record['pid'] == process.pid:
                print('Velvet healthy at '+url)
                return
        except (OSError, ValueError, KeyError):pass
        time.sleep(.2)
    raise ValueError('Health check timed out; inspect logs before retrying. No process was force-killed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['start', 'stop', 'restart', 'status'])
    parser.add_argument('--data', type=Path, default=Path(os.environ.get('VELVET_DATA', str(ROOT/'data'))))
    parser.add_argument('--port', type=int, default=int(os.environ['VELVET_PORT']) if os.environ.get('VELVET_PORT') else None)
    parser.add_argument('--open',action='store_true',help='Open the verified local service in the default browser')
    parser.add_argument('--timeout', type=int, default=60)
    args = parser.parse_args()
    if (args.port is not None and not 1 <= args.port <= 65535) or not 1 <= args.timeout <= 600:parser.error('Invalid port or timeout')
    try:
        data = args.data.resolve()
        if args.action in ('stop', 'restart'):
            previous=stop(data, args.timeout)
            if args.port is None:args.port=urlsplit(previous['url']).port
        if args.action=='start' and args.port is None:
            try:args.port=urlsplit(local_instance(data)['url']).port
            except (OSError,ValueError,KeyError):args.port=8765
        if args.action in ('start', 'restart'):
            start(data, args.port or 8765, args.timeout)
            if args.open:
                import webbrowser
                webbrowser.open(local_instance(data)['url'])
        if args.action == 'status':
            record = local_instance(data); print('Velvet healthy at '+record['url'])
    except (OSError, ValueError, KeyError) as error:parser.exit(1, str(error)+'\n')
