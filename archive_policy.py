"""Mandatory archive exclusions shared by library, inventory and playback paths."""
from pathlib import Path

ARCHIVE_NAME='.velvet-archive'


def archive_roots(db):
    with db() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='organization_archive_roots'").fetchone():return []
        return [r[0] for r in c.execute('SELECT path FROM organization_archive_roots ORDER BY path')]


def is_archive_path(path,roots=()):
    path=Path(path)
    return any(part.casefold()==ARCHIVE_NAME for part in path.parts) or any(path.is_relative_to(Path(root)) for root in roots)
