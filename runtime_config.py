"""Side-effect-free runtime configuration and privacy-safe diagnostics."""
import argparse
import ipaddress
import os
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeConfig:
    bind: str
    port: int
    no_browser: bool

    @property
    def local_url(self):
        host = self.bind if self.bind not in ('0.0.0.0', '::') else '127.0.0.1'
        return f'http://[{host}]:{self.port}' if ':' in host else f'http://{host}:{self.port}'

    def accepts_host(self, value):
        return value in (f'localhost:{self.port}', f'127.0.0.1:{self.port}', f'[::1]:{self.port}')


def load_runtime(argv=(), environ=None):
    env = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description='Velvet local library service')
    parser.add_argument('--port', type=int, default=env.get('VELVET_PORT', '8765'))
    parser.add_argument('--bind', default=env.get('VELVET_BIND', '0.0.0.0' if env.get('VELVET_SERVER') == '1' else '127.0.0.1'))
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error('port must be between 1 and 65535')
    try:
        ipaddress.ip_address(args.bind)
    except ValueError:
        parser.error('bind must be an IP address')
    if env.get('VELVET_SERVER') != '1' and not ipaddress.ip_address(args.bind).is_loopback:
        parser.error('non-loopback binding requires VELVET_SERVER=1 and server authentication')
    return RuntimeConfig(args.bind, args.port, args.no_browser)


def diagnostics(version, runtime, binary, states):
    # Deliberate allowlist: never serialize settings, task errors or source objects.
    return {'application': 'velvet', 'version': version, 'python': '.'.join(map(str, sys.version_info[:3])),
            'platform': sys.platform, 'port': runtime.port,
            'tools': {name: bool(binary(name)) for name in ('ffmpeg', 'ffprobe')},
            'tasks': {name: {'running': bool(state.get('running'))} for name, state in states.items()}}
