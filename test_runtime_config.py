import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from runtime_config import load_runtime, diagnostics


class RuntimeConfigTests(unittest.TestCase):
    def test_precedence_and_host_validation(self):
        cfg = load_runtime(['--port', '18766', '--no-browser'], {'VELVET_PORT': '18765'})
        self.assertEqual(cfg.port, 18766)
        self.assertTrue(cfg.no_browser)
        self.assertTrue(cfg.accepts_host('127.0.0.1:18766'))
        self.assertFalse(cfg.accepts_host('127.0.0.1:8765'))
        self.assertFalse(cfg.accepts_host('attacker.invalid:18766'))
        self.assertEqual(load_runtime([], {}).port, 8765)

    def test_validation_does_not_expand_network_exposure(self):
        for env in ({'VELVET_PORT': '0'}, {'VELVET_PORT': '65536'}, {'VELVET_PORT': 'bad'}, {'VELVET_BIND': '0.0.0.0'}, {'VELVET_BIND': 'http://localhost'}):
            with self.subTest(env=env), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                load_runtime([], env)
        self.assertEqual(load_runtime([], {'VELVET_SERVER': '1'}).bind, '0.0.0.0')

    def test_diagnostics_use_an_allowlist(self):
        result = diagnostics('fixture', load_runtime([], {}), lambda _: '/private/tool', {'scan': {'running': True, 'path': '/private/media', 'token': 'secret', 'error': 'private title'}})
        serialized = json.dumps(result)
        for value in ('/private', 'secret', 'private title'):
            self.assertNotIn(value, serialized)
        self.assertTrue(result['tasks']['scan']['running'])

    def test_real_isolated_service_and_port_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / 'data'; data.mkdir()
            (data / 'settings.json').write_text(json.dumps({'auto_scrape': False, 'scrape_engine': 'builtin', 'scrape_sources': [], 'api_key': 'fixture-private-secret'}), encoding='utf-8')
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1', 0)); port = reservation.getsockname()[1]
            env = dict(os.environ, VELVET_DATA=str(data), VELVET_SERVER='0', VELVET_BIND='127.0.0.1')
            args = [sys.executable, str(Path(__file__).with_name('app.py')), '--no-browser', '--port', str(port)]
            with tempfile.TemporaryFile() as log:
                process = subprocess.Popen(args, env=env, stdout=log, stderr=log, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                try:
                    base = f'http://127.0.0.1:{port}'
                    deadline = time.monotonic() + 15
                    while True:
                        try:
                            with urlopen(base + '/api/health', timeout=1) as response:health = json.load(response)
                            break
                        except (URLError, TimeoutError):
                            if process.poll() is not None or time.monotonic() > deadline:
                                self.fail('Isolated service failed to become healthy')
                            time.sleep(.1)
                    self.assertEqual(health['application'], 'velvet')
                    self.assertEqual(health['port'], port)
                    with urlopen(base + '/api/diagnostics') as response:report = response.read().decode()
                    self.assertNotIn(str(data), report)
                    self.assertNotIn('fixture-private-secret', report)
                    with self.assertRaises(HTTPError) as rejected:
                        urlopen(Request(base + '/api/health', headers={'Host': 'foreign.invalid'}))
                    self.assertEqual(rejected.exception.code, 403)
                    env2 = dict(env, VELVET_DATA=str(Path(tmp) / 'second'))
                    conflict = subprocess.run(args, env=env2, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
                    self.assertNotEqual(conflict.returncode, 0)
                    self.assertIsNone(process.poll())
                    with urlopen(base + '/api/health') as response:self.assertTrue(json.load(response)['ok'])
                finally:
                    process.terminate()
                    process.wait(timeout=10)


if __name__ == '__main__':
    unittest.main()
