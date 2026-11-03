"""Explicit display rules and versioned directory-operation previews."""
from media_libraries import canonical
import hashlib
import json
from pathlib import Path


class DirectoryPolicy:
    def __init__(self, db, libraries):
        self.db, self.libraries = db, libraries
        self.ensure()

    def ensure(self):
        with self.db() as c:
            c.execute('CREATE TABLE IF NOT EXISTS collection_rules(path TEXT PRIMARY KEY,mode TEXT NOT NULL)')

    def rules(self):
        self.ensure()
        with self.db() as c:
            return [dict(r) for r in c.execute('SELECT path,mode FROM collection_rules ORDER BY path')]

    def preview(self, raw):
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError('请选择目录')
        path = Path(raw).expanduser().resolve()
        if not any(path.is_relative_to(Path(p['path']).resolve()) for p in self.libraries.policies()):
            raise ValueError('目录必须位于媒体库中')
        with self.db() as c:
            rows = [dict(r) for r in c.execute('SELECT id,path,size,mtime FROM videos') if canonical(r['path']).is_relative_to(path)]
            has_links = c.execute("SELECT 1 FROM sqlite_master WHERE name='media_links'").fetchone()
            links = {r['video_id']: r['movie_key'] for r in c.execute('SELECT video_id,movie_key FROM media_links')} if has_links else {}
        stamp = json.dumps([str(path), sorted((r['id'], r['path'], r['size'], r['mtime']) for r in rows)], ensure_ascii=True)
        return {'path': str(path), 'files': len(rows), 'movies': len({links.get(r['id'], 'file:'+str(r['id'])) for r in rows}),
                'bytes': sum(r['size'] or 0 for r in rows), 'online': path.is_dir(),
                'digest': hashlib.sha256(stamp.encode()).hexdigest(), 'source_files_modified': False}

    def set_rule(self, raw, mode, digest):
        if mode not in ('group', 'ungroup', 'inherit'):
            raise ValueError('规则应为归组、禁止归组或恢复自动')
        preview = self.preview(raw)
        if digest != preview['digest']:
            raise ValueError('目录内容已变化，请重新预览')
        self.ensure()
        with self.db() as c:
            if mode == 'inherit':
                c.execute('DELETE FROM collection_rules WHERE path=?', (preview['path'],))
            else:
                c.execute('INSERT OR REPLACE INTO collection_rules VALUES(?,?)', (preview['path'], mode))
        return preview

    @staticmethod
    def collection_root(path, rules):
        matches = [r for r in rules if canonical(path).is_relative_to(canonical(r['path']))]
        if not matches:
            return None
        closest = max(matches, key=lambda r: len(Path(r['path']).parts))
        return closest['path'] if closest['mode'] == 'group' else ''
