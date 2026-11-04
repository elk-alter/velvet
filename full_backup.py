"""Offline full application-data backup/restore. Never includes original media roots."""
from data_guard import exclusive_data
from restore_safety import require_reconciled_files
import argparse
from contextlib import closing
import hashlib
import json
import re
import unicodedata
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import quote


def offline(data):
    descriptor = data/'runtime-instance.json'
    if descriptor.exists():
        from service_control import local_instance
        from urllib.error import URLError
        try:local_instance(data)
        except URLError:pass
        except (ValueError, KeyError, OSError):
            raise ValueError('Cannot verify service is stopped; check the runtime descriptor') from None
        else:raise ValueError('Stop the service before full backup or restore')


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda:source.read(4*1024*1024), b''):digest.update(block)
    return digest.hexdigest()


@exclusive_data
def create(data, output, include_cache=False):
    data, output = Path(data).resolve(), Path(output).resolve()
    if not (data/'library.db').is_file():raise ValueError('Not a Velvet data directory')
    if output.is_relative_to(data) or output.exists():raise ValueError('Choose a new archive outside the data directory')
    offline(data)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='velvet-full-') as temp:
        staging = Path(temp); manifest = {'format': 'velvet-full-1', 'files': {}, 'cache': bool(include_cache), 'contains_credentials': True}
        candidates = [p for p in data.glob('*.json') if not p.is_symlink() and p.resolve().is_relative_to(data) and not any(word in p.name for word in ('runtime-instance', 'restart'))]
        for name in ('library.db', 'builtin-scraper.db'):
            path = data/name
            if not path.exists():continue
            if path.is_symlink() or not path.resolve().is_relative_to(data):raise ValueError('Database links are not supported')
            destination = staging/name
            source = sqlite3.connect('file:'+quote(path.as_posix(), safe='/:')+'?mode=ro', uri=True)
            target = sqlite3.connect(destination)
            try:source.backup(target)
            finally:source.close();target.close()
        for path in candidates:shutil.copyfile(path, staging/path.name)
        if include_cache:
            for path in data.rglob('*'):
                if path.is_symlink() or not path.resolve().is_relative_to(data) or 'backups' in path.relative_to(data).parts:continue
                if path.is_file() and path.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp'):
                    destination = staging/path.relative_to(data);destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, destination)
        for path in staging.rglob('*'):
            if path.is_file():manifest['files'][path.relative_to(staging).as_posix()] = {'sha256':file_hash(path), 'size':path.stat().st_size}
        temporary = output.with_name(output.name+'.partial-'+uuid.uuid4().hex)
        try:
            with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
                for name in manifest['files']:archive.write(staging/name, name)
                archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=True))
            offline(data)
            temporary.rename(output)
        finally:
            if temporary.exists():temporary.unlink()
    return {'files':len(manifest['files']), 'sha256':file_hash(output), 'contains_credentials':True}


def validate_extract(archive_path, target, max_bytes=100*1024**3):
    with zipfile.ZipFile(archive_path) as archive:
        infos = archive.infolist()
        names = [i.filename for i in infos]
        if len({unicodedata.normalize('NFC',name).casefold() for name in names}) != len(names):raise ValueError('Duplicate archive entries')
        if sum(i.file_size for i in infos) > max_bytes:raise ValueError('Archive exceeds configured size limit')
        if 'manifest.json' not in names or archive.getinfo('manifest.json').file_size > 10*1024*1024:raise ValueError('Invalid manifest')
        manifest = json.loads(archive.read('manifest.json'))
        if not isinstance(manifest,dict) or manifest.get('format') != 'velvet-full-1' or not isinstance(manifest.get('files'), dict):raise ValueError('Unsupported backup format')
        if set(names) != set(manifest['files'])|{'manifest.json'} or 'library.db' not in names:raise ValueError('Incomplete backup')
        for info in infos:
            name = info.filename; relative = PurePosixPath(name)
            if relative.is_absolute() or '..' in relative.parts or '\\' in name or ':' in name or info.is_dir() or (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('Unsafe backup path')
            if name == 'manifest.json':continue
            path = target/Path(*relative.parts)
            if not path.resolve().is_relative_to(target.resolve()):raise ValueError('Path escapes staging')
            path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, path.open('wb') as output:shutil.copyfileobj(source, output)
            record = manifest['files'][name]
            if not isinstance(record,dict) or type(record.get('size')) is not int or not isinstance(record.get('sha256'),str) or not re.fullmatch('[0-9a-f]{64}',record['sha256']):raise ValueError('Invalid manifest file record')
            if path.stat().st_size != record['size'] or file_hash(path) != record['sha256']:raise ValueError('Backup checksum mismatch')
        for name in ('library.db', 'builtin-scraper.db'):
            if not (target/name).exists():continue
            with closing(sqlite3.connect(target/name)) as connection:
                if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':raise ValueError('Invalid database')
        return manifest


@exclusive_data
def restore(data, archive_path):
    data, archive_path = Path(data).resolve(), Path(archive_path).resolve()
    if data == Path(data.anchor) or archive_path.is_relative_to(data):raise ValueError('Unsafe restore location')
    if data.exists() and any(data.iterdir()) and not (data/'library.db').is_file():raise ValueError('Destination contains unrelated data')
    offline(data)
    if (data/'library.db').is_file():
        with closing(sqlite3.connect(data/'library.db')) as connection:require_reconciled_files(connection)
    data.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='velvet-restore-', dir=data.parent) as temp:
        stage = Path(temp)/'data';stage.mkdir()
        manifest = validate_extract(archive_path, stage)
        with closing(sqlite3.connect(stage/'library.db')) as connection:require_reconciled_files(connection)
        offline(data)
        rollback = data.with_name(data.name+'.before-'+uuid.uuid4().hex)
        had_data = data.exists()
        if had_data:data.rename(rollback)
        try:stage.rename(data)
        except BaseException:
            if had_data and not data.exists():rollback.rename(data)
            raise
    return {'restored':len(manifest['files']), 'rollback':str(rollback) if had_data else None}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['create', 'restore'])
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--include-cache', action='store_true')
    args = parser.parse_args()
    try:print(json.dumps(create(args.data,args.archive,args.include_cache) if args.action=='create' else restore(args.data,args.archive)))
    except (OSError,ValueError,sqlite3.DatabaseError,zipfile.BadZipFile) as error:parser.exit(1,str(error)+'\n')
