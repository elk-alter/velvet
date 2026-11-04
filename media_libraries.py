"""Library policies layered over the existing folder index. Source files stay read-only."""
import hashlib
import fnmatch
import json
import os
import time
from pathlib import Path


def canonical(path,short_prefixes=None):
    path=Path(path)
    if os.name!='nt' or '~' not in str(path):return path
    if short_prefixes is None:return path.resolve()
    # Query-local cache: expand only the last short-name prefix once. The
    # remaining indexed suffix may be offline/nonexistent and needs no stat.
    parts=path.parts;last=max(i for i,part in enumerate(parts) if '~' in part)
    prefix=Path(*parts[:last+1]);key=str(prefix)
    if key not in short_prefixes:short_prefixes[key]=prefix.resolve()
    return short_prefixes[key].joinpath(*parts[last+1:])


class MediaLibraries:
    def __init__(self, db):
        self.db = db
        with db() as c:
            c.execute("CREATE TABLE IF NOT EXISTS library_settings(path TEXT PRIMARY KEY,name TEXT DEFAULT '',automation TEXT DEFAULT 'scrape',exclude_patterns TEXT DEFAULT '[]',min_size INTEGER DEFAULT 0,settle_seconds INTEGER DEFAULT 0,last_scan REAL DEFAULT 0,last_error TEXT DEFAULT '')")

    def exclusions(self):
        with self.db() as c:
            c.execute("CREATE TABLE IF NOT EXISTS excluded_directories(path TEXT PRIMARY KEY)")
            return [r[0] for r in c.execute('SELECT path FROM excluded_directories ORDER BY path')]

    def exclude(self, raw, enabled=True):
        if not isinstance(raw,str) or not raw.strip() or type(enabled) is not bool:
            raise ValueError('请选择有效目录')
        path=Path(raw).expanduser().resolve()
        if enabled and not any(path.is_relative_to(canonical(p['path'])) for p in self.policies()):
            raise ValueError('只能排除媒体库内的目录')
        self.exclusions()
        with self.db() as c:
            if enabled:c.execute('INSERT OR IGNORE INTO excluded_directories VALUES(?)',(str(path),))
            else:c.execute('DELETE FROM excluded_directories WHERE path=?',(str(path),))
        return str(path)

    def exclusion_preview(self, raw):
        if not isinstance(raw,str) or not raw.strip():raise ValueError('请选择有效目录')
        path=Path(raw).expanduser().resolve()
        if not any(path.is_relative_to(canonical(p['path'])) for p in self.policies()):
            raise ValueError('只能排除媒体库内的目录')
        with self.db() as c:
            rows=[dict(r) for r in c.execute('SELECT id,path,size,mtime FROM videos') if canonical(r['path']).is_relative_to(path)]
            linked=c.execute("SELECT 1 FROM sqlite_master WHERE name='media_links'").fetchone()
            links={r['video_id']:r['movie_key'] for r in c.execute('SELECT video_id,movie_key FROM media_links')} if linked else {}
        stamp=json.dumps([str(path),rows,self.exclusions()],sort_keys=True)
        return {'path':str(path),'files':len(rows),'movies':len({links.get(r['id'],str(r['id'])) for r in rows}),
                'bytes':sum(r['size'] or 0 for r in rows),'digest':hashlib.sha256(stamp.encode()).hexdigest(),
                'includes_subdirectories':True,'source_files_modified':False}

    def policies(self):
        with self.db() as c:
            if not c.execute("SELECT 1 FROM sqlite_master WHERE name='library_settings'").fetchone():
                self.__init__(self.db)
            rows = [dict(row) for row in c.execute("SELECT f.path,COALESCE(s.name,'') AS name,COALESCE(s.automation,'scrape') AS automation,COALESCE(s.exclude_patterns,'[]') AS exclude_patterns,COALESCE(s.min_size,0) AS min_size,COALESCE(s.settle_seconds,0) AS settle_seconds,COALESCE(s.last_scan,0) AS last_scan,COALESCE(s.last_error,'') AS last_error FROM folders f LEFT JOIN library_settings s ON f.path=s.path ORDER BY f.path")]
        excluded=self.exclusions()
        for row in rows:
            row['excluded_directories']=excluded
            row['exclude_patterns'] = json.loads(row['exclude_patterns'] or '[]')
        return rows

    def list(self):
        rows = self.policies()
        with self.db() as c:
            videos = [dict(v) for v in c.execute('SELECT path,missing,amane_id,code,size FROM videos')]
        visible=self.filter_rows(videos)
        for row in rows:
            row['indexed_count']=sum(self.owner(v['path'],rows)==row['path'] for v in videos)
            members = [v for v in visible if self.owner(v['path'], rows) == row['path']]
            row.update(name=row['name'] or Path(row['path']).name or row['path'], online=Path(row['path']).is_dir(),
                       file_count=len(members), missing_count=sum(bool(v['missing']) for v in members),
                       matched_count=sum(bool(v['amane_id']) for v in members), total_size=sum(v['size'] or 0 for v in members))
        return rows

    @staticmethod
    def owner(path, policies):
        matches = [r['path'] for r in policies if canonical(path).is_relative_to(canonical(r['path']))]
        return max(matches, key=len) if matches else None

    def save(self, data):
        raw = data.get('path')
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError('请选择媒体库目录')
        path = Path(raw).expanduser().resolve()
        if not path.is_dir():
            raise ValueError('媒体库目录不存在或离线')
        name = data.get('name', '')
        mode = data.get('automation', 'scrape')
        patterns = data.get('exclude_patterns', [])
        size, settle = data.get('min_size', 0), data.get('settle_seconds', 0)
        if not isinstance(name, str) or len(name) > 100 or mode not in ('off', 'monitor', 'scrape'):
            raise ValueError('媒体库名称或自动化模式无效')
        if not isinstance(patterns, list) or len(patterns) > 50 or not all(isinstance(p, str) and 0 < len(p) <= 200 for p in patterns):
            raise ValueError('排除规则应为最多 50 条路径通配符')
        if type(size) is not int or not 0 <= size <= 10**13 or type(settle) is not int or not 0 <= settle <= 86400:
            raise ValueError('最小大小或稳定等待时间无效')
        # Reject overlaps for new libraries; legacy overlaps remain readable, longest root owns files.
        for existing in self.policies():
            other = canonical(existing['path'])
            if other != path and (path.is_relative_to(other) or other.is_relative_to(path)):
                raise ValueError('媒体库目录不能相互包含，请使用现有媒体库的排除规则')
        with self.db() as c:
            c.execute('INSERT OR IGNORE INTO folders(path) VALUES(?)', (str(path),))
            c.execute('INSERT INTO library_settings(path,name,automation,exclude_patterns,min_size,settle_seconds) VALUES(?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET name=excluded.name,automation=excluded.automation,exclude_patterns=excluded.exclude_patterns,min_size=excluded.min_size,settle_seconds=excluded.settle_seconds',
                      (str(path), name.strip(), mode, json.dumps(patterns), size, settle))
        return str(path)

    def files(self, path, status='all', search='', offset=0, limit=30):
        policies = self.policies()
        if path not in [p['path'] for p in policies]:
            raise ValueError('媒体库不存在')
        if status not in ('all', 'matched', 'pending', 'missing', 'error') or offset < 0 or not 1 <= limit <= 100:
            raise ValueError('媒体库筛选或分页无效')
        with self.db() as c:
            rows = [dict(row) for row in c.execute('SELECT id,path,title,code,amane_id,missing,error,size,width,height FROM videos ORDER BY id')]
        members=[]
        for row in self.filter_rows(rows):
            if self.owner(row['path'], policies) != path:
                continue
            if search.casefold() not in ((row['title'] or '')+' '+(row['code'] or '')).casefold():
                continue
            if status=='matched' and not row['amane_id'] or status=='pending' and (row['amane_id'] or row['missing']) or status=='missing' and not row['missing'] or status=='error' and not row['error']:
                continue
            members.append(row)
        return {'items': members[offset:offset+limit], 'total': len(members), 'offset': offset, 'limit': limit}

    @staticmethod
    def included(path, policy, size):
        if any(canonical(path).is_relative_to(canonical(root)) for root in policy.get('excluded_directories',[])):return False
        relative = canonical(path).relative_to(canonical(policy['path'])).as_posix()
        relative = os.path.normcase(relative)
        return size >= policy['min_size'] and not any(fnmatch.fnmatchcase(relative, os.path.normcase(pattern.replace('\\', '/'))) or fnmatch.fnmatchcase(Path(relative).name, os.path.normcase(pattern)) for pattern in policy['exclude_patterns'])

    def filter_rows(self, rows):
        policies=self.policies();excluded=self.exclusions()
        result=[]
        for row in rows:
            path=canonical(row['path'])
            if any(path.is_relative_to(canonical(root)) for root in excluded):continue
            owner=self.owner(path,policies)
            policy=next((p for p in policies if p['path']==owner),None)
            if policy and not self.included(path,policy,row.get('size',0) or 0):continue
            result.append(row)
        return result

    def finish(self, path, error=''):
        with self.db() as c:
            c.execute('INSERT INTO library_settings(path,last_scan,last_error) VALUES(?,?,?) ON CONFLICT(path) DO UPDATE SET last_scan=excluded.last_scan,last_error=excluded.last_error', (path, time.time(), error))
