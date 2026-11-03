import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.request import urlopen
from service_control import local_instance, stop


class ServiceControlTests(unittest.TestCase):
    def test_detached_lifecycle_preserves_user_switches(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)/'data'; data.mkdir()
            (data/'settings.json').write_text(json.dumps({'auto_scrape': False, 'scrape_engine': 'builtin', 'scrape_sources': []}), encoding='utf-8')
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
            args = [sys.executable, str(Path(__file__).with_name('service_control.py'))]
            env = dict(os.environ, VELVET_SERVER='0', VELVET_BIND='127.0.0.1')
            def command(action):
                return subprocess.run(args+[action, '--data', str(data), '--timeout', '15']+([] if action=='restart' else ['--port',str(port)]), env=env, capture_output=True, timeout=25)
            try:
                self.assertEqual(command('start').returncode, 0)
                first = local_instance(data)
                self.assertEqual(command('start').returncode, 0)
                self.assertEqual(first['pid'], local_instance(data)['pid'])
                self.assertEqual(command('status').returncode, 0)
                self.assertEqual(command('restart').returncode, 0)
                self.assertNotEqual(first['token'], local_instance(data)['token'])
                with urlopen(f'http://127.0.0.1:{port}/api/library') as response:
                    self.assertFalse(json.load(response)['settings']['auto_scrape'])
                self.assertEqual(command('stop').returncode, 0)
                self.assertFalse((data/'runtime-instance.json').exists())
            finally:
                if (data/'runtime-instance.json').exists():
                    stop(data, 15)

    def test_descriptor_cannot_send_control_token_to_remote_hosts(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)
            (data/'runtime-instance.json').write_text(json.dumps({'url': 'http://example.invalid', 'token': 'fixture', 'pid': 1}))
            with self.assertRaises(ValueError):local_instance(data)


class StopTimeoutTests(unittest.TestCase):
    def test_timeout_resumes_without_force_killing(self):
        from unittest.mock import patch
        record={'url':'http://127.0.0.1:18765','token':'synthetic','pid':999999}
        calls=[]
        def fake_request(url,token=None):calls.append(url);return {'ok':False,'waiting':True}
        with patch('service_control.local_instance',return_value=record),patch('service_control.request',side_effect=fake_request),patch('service_control.time.monotonic',side_effect=[0,0,.02]),patch('service_control.time.sleep'):
            with self.assertRaisesRegex(ValueError,'no process was force-killed'):stop(Path('example'),.01)
        self.assertTrue(calls[-1].endswith('/api/runtime/resume'))
