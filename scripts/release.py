"""Validate public sources and archive a clean Git revision (no runtime imports)."""
import argparse
import ast
import hashlib
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT)


def check():
    paths = [Path(p) for p in git('ls-files', '-z').decode('utf-8').split('\0') if p]
    version = (ROOT / 'VERSION').read_text('utf-8-sig').strip()
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:-[a-z]+\.\d+)?', version):
        raise ValueError('Invalid VERSION')
    if not shutil.which('node'):
        raise ValueError('Release validation requires Node.js for JavaScript syntax checks')
    forbidden = {'data', 'work', 'tools', 'dist', 'node_modules', '__pycache__', '.venv'}
    credentials = re.compile(r'gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{20,}')
    for relative in paths:
        if forbidden.intersection(relative.parts) or relative.name == '.env':
            raise ValueError(f'Private/build path tracked: {relative}')
        if relative.suffix.lower() in {'.db', '.sqlite', '.mp4', '.mkv', '.nfo', '.log', '.zip', '.exe'}:
            raise ValueError(f'Runtime/media file tracked: {relative}')
        path = ROOT / relative
        try:
            source = path.read_text('utf-8-sig')
        except UnicodeDecodeError:
            continue
        if credentials.search(source):
            raise ValueError(f'Credential pattern found: {relative}')
        if relative.suffix == '.py':
            compile(source, str(relative), 'exec')
        if relative.suffix == '.js':
            subprocess.run(['node', '--check', str(path)], check=True, cwd=ROOT)
        if relative.suffix == '.md':
            for target in re.findall(r'!?\[[^\]]*\]\(([^\s)]+)', source):
                parsed = urlsplit(target)
                if parsed.scheme or not parsed.path:
                    continue
                linked = (path.parent / unquote(parsed.path)).resolve()
                if not linked.is_relative_to(ROOT) or not linked.exists():
                    raise ValueError(f'Broken local link in {relative}: {target}')
    # Check scripts embedded in the page, without evaluating them or importing app.py.
    html = (ROOT / 'index.html').read_text('utf-8')
    with tempfile.TemporaryDirectory() as tmp:
        for index, script in enumerate(re.findall(r'<script(?:\s[^>]*)?>(.*?)</script>', html, re.S)):
            path = Path(tmp) / f'inline-{index}.js'
            path.write_text(script, encoding='utf-8')
            subprocess.run(['node', '--check', str(path)], check=True)
    tree = ast.parse((ROOT / 'app.py').read_text('utf-8-sig'))
    assets = next(ast.literal_eval(node.value) for node in tree.body
                  if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'UI_ASSETS' for t in node.targets))
    tracked = {p.as_posix() for p in paths}
    dockerfile = (ROOT / 'Dockerfile').read_text('utf-8')
    for filename, _ in assets.values():
        if filename not in tracked or filename not in dockerfile:
            raise ValueError(f'UI asset missing from Git or Docker: {filename}')
    for name in re.findall(r'(?:src|href)="/([^"?#]+\.(?:js|css))"', html):
        if '/' + name not in assets:
            raise ValueError(f'Page asset not served: {name}')
    if f'## {version}' not in (ROOT / 'CHANGELOG.md').read_text('utf-8'):
        raise ValueError('VERSION missing from CHANGELOG')
    print(f'Validated {len(paths)} tracked files; version {version}')
    return version


def build(output):
    version = check()
    if git('status', '--porcelain').strip():
        raise ValueError('Commit all public changes before packaging')
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    name = f'velvet-{version}'
    archive = output / f'{name}-source.zip'
    subprocess.run(['git', 'archive', '--format=zip', f'--prefix={name}/',
                    f'--output={archive}', 'HEAD'], cwd=ROOT, check=True)
    with zipfile.ZipFile(archive) as package:
        if package.testzip() is not None:
            raise ValueError('Corrupt source archive')
        if package.read(f'{name}/VERSION').decode('utf-8-sig').strip() != version:
            raise ValueError('Archive version mismatch')
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (output / 'SHA256SUMS.txt').write_text(f'{digest}  {archive.name}\n', encoding='utf-8')
    print(f'Built {archive.name}; commit {git("rev-parse", "HEAD").decode().strip()}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Validate tracked working files only')
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    args = parser.parse_args()
    try:
        check() if args.check else build(args.output)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'Release validation failed: {error}\n')
