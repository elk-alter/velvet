"""Read-only media inspection and per-file replacement decisions."""
import json
import subprocess
import tempfile
import threading
import time
from pathlib import Path

ISSUES = {'watermark': '明显水印', 'damaged': '损坏（人工确认）', 'replace': '需要更新'}


class FileReview:
    def __init__(self, db, binary, data=None):
        self.db, self.binary = db, binary
        self.data = Path(data) if data else None
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.wake = threading.Event()
        self.ensure(recover=True)

    def ensure(self, recover=False):
        with self.db() as c:
            c.execute("CREATE TABLE IF NOT EXISTS file_reviews(video_id INTEGER PRIMARY KEY,size INTEGER,mtime REAL,flags TEXT DEFAULT '[]',note TEXT DEFAULT '',decision TEXT DEFAULT 'open',status TEXT DEFAULT 'unchecked',mode TEXT DEFAULT 'quick',result TEXT DEFAULT '',detail TEXT DEFAULT '',updated REAL DEFAULT 0)")
            c.execute("CREATE TABLE IF NOT EXISTS review_settings(key TEXT PRIMARY KEY,value TEXT)")
            if recover:
                c.execute("UPDATE file_reviews SET status='queued',detail='重启后等待继续检查' WHERE status='running'")

    def rows(self, ids):
        if not isinstance(ids, list) or not ids or len(ids) > 500 or any(type(i) is not int for i in ids):
            raise ValueError('请选择 1–500 个文件')
        with self.db() as c:
            rows = [dict(r) for r in c.execute('SELECT * FROM videos WHERE id IN (' + ','.join('?' for _ in ids) + ')', ids)]
        if len(rows) != len(set(ids)):
            raise ValueError('文件已移除，请刷新列表')
        return rows

    def touch(self, c, row):
        old = c.execute('SELECT * FROM file_reviews WHERE video_id=?', (row['id'],)).fetchone()
        if old and (old['size'], old['mtime']) != (row['size'], row['mtime']) and old['status'] in ('queued', 'running'):
            raise ValueError('文件检查正在进行，请先取消并等待结束')
        if old and (old['size'], old['mtime']) != (row['size'], row['mtime']):
            c.execute('DELETE FROM file_reviews WHERE video_id=?', (row['id'],))
        c.execute('INSERT OR IGNORE INTO file_reviews(video_id,size,mtime) VALUES(?,?,?)', (row['id'], row['size'], row['mtime']))

    def mark(self, ids, flag=None, enabled=True, decision=None, note=None):
        if flag is not None and flag not in ISSUES:
            raise ValueError('未知问题类型')
        if type(enabled) is not bool or decision is not None and decision not in ('open', 'accepted', 'ignored'):
            raise ValueError('无效处理状态')
        if note is not None and (not isinstance(note, str) or len(note) > 1000):
            raise ValueError('备注最多 1000 字')
        rows = self.rows(ids)
        with self.db() as c:
            for row in rows:
                self.touch(c, row)
                current = dict(c.execute('SELECT * FROM file_reviews WHERE video_id=?', (row['id'],)).fetchone())
                flags = set(json.loads(current['flags']))
                if flag:
                    flags.add(flag) if enabled else flags.discard(flag)
                c.execute('UPDATE file_reviews SET flags=?,note=?,decision=?,updated=? WHERE video_id=?',
                          (json.dumps(sorted(flags)), current['note'] if note is None else note,
                           decision or ('open' if flag and enabled else current['decision']), time.time(), row['id']))

    def target(self, height):
        if type(height) is not int or height not in (0, 720, 1080, 2160):
            raise ValueError('目标应为不限、720p、1080p 或 4K')
        with self.db() as c:
            c.execute("INSERT OR REPLACE INTO review_settings VALUES('target',?)", (str(height),))

    def snapshot(self, videos, movies=None):
        with self.db() as c:
            records = {r['video_id']: dict(r) for r in c.execute('SELECT r.* FROM file_reviews r JOIN videos v ON v.id=r.video_id')}
            setting = c.execute("SELECT value FROM review_settings WHERE key='target'").fetchone()
        target = int(setting[0]) if setting else 1080
        higher = set()
        for movie in movies or []:
            versions = movie.get('_versions', [])
            for version in versions:
                if any(other.get('height', 0) > version.get('height', 0) and not any(p.get('missing') for p in other.get('_parts', [])) for other in versions):
                    higher.update(p['id'] for p in version.get('_parts', []))
        result = {}
        for video in videos:
            row = records.get(video['id'], {})
            stale = bool(row) and (row['size'], row['mtime']) != (video['size'], video['mtime'])
            flags = [] if stale else json.loads(row.get('flags', '[]'))
            issues = [{'code': f, 'label': ISSUES[f], 'source': 'manual'} for f in flags]
            outcome = '' if stale else row.get('result', '')
            if outcome == 'decode_error':
                issues.append({'code': 'decode_error', 'label': '解码异常，疑似损坏', 'source': 'inspection'})
            if target and 0 < (video.get('height') or 0) < target:
                issues.append({'code': 'below_target', 'label': f'低于目标 {target}p', 'source': 'rule'})
            if video['id'] in higher:
                issues.append({'code': 'higher_local', 'label': '本地有更高分辨率版本（待对比）', 'source': 'rule'})
            decision = 'open' if stale else row.get('decision', 'open')
            result[video['id']] = dict(issues=issues, needs_update=bool(issues) and decision == 'open',
                                      has_cover=bool(self.data and any((self.data / f"{video['id']}-{kind}.jpg").is_file() for kind in ('cover','poster'))),
                                      decision=decision, status='stale' if stale else row.get('status', 'unchecked'),
                                      mode=row.get('mode', ''), result=outcome, note=row.get('note', ''),
                                      detail='' if stale else row.get('detail', ''), updated=row.get('updated', 0))
        return {'files': result, 'target': target, 'running': self.lock.locked(),
                'queued': sum(r['status'] == 'queued' for r in records.values())}

    def enqueue(self, ids, mode):
        if mode not in ('quick', 'sample', 'full'):
            raise ValueError('未知检查范围')
        if self.cancel.is_set() and self.lock.locked():
            raise ValueError('检查正在取消，请等待结束后重试')
        rows = self.rows(ids)
        if not self.binary('ffprobe') or mode != 'quick' and not self.binary('ffmpeg'):
            raise ValueError('请先安装 FFmpeg 和 ffprobe')
        with self.db() as c:
            for row in rows:
                self.touch(c, row)
                c.execute("UPDATE file_reviews SET status='queued',mode=?,detail='',updated=? WHERE video_id=? AND status!='running'", (mode, time.time(), row['id']))
        self.resume()

    def stop(self):
        self.cancel.set()
        with self.db() as c:
            c.execute("UPDATE file_reviews SET status='cancelled' WHERE status='queued'")

    def resume(self):
        self.wake.set()
        if not self.lock.acquire(False):
            return
        self.wake.clear()
        self.cancel.clear()
        threading.Thread(target=self.run, daemon=True).start()

    def command(self, args, timeout):
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            start = time.monotonic()
            try:
                while process.poll() is None:
                    if self.cancel.wait(.2):
                        return 'cancelled', '', ''
                    if time.monotonic() - start > timeout:
                        return 'timeout', '', '检查超时，结果不确定'
                out.seek(0); err.seek(0)
                return process.returncode, out.read(2_000_000).decode('utf-8', 'replace'), err.read(3000).decode('utf-8', 'replace')
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()

    def inspect(self, row, mode):
        path = Path(row['path'])
        try:
            before = path.stat()
            if before.st_size != row['size'] or abs(before.st_mtime - row['mtime']) > .01:
                return 'changed', '文件已变化，请先重新扫描目录'
            with path.open('rb') as stream:
                stream.read(1)
        except OSError:
            return 'unavailable', '文件离线、缺失或没有读取权限'
        code, output, error = self.command([self.binary('ffprobe'), '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)], 45)
        if code in ('cancelled', 'timeout'):
            return code, error
        if code != 0:
            return 'probe_error', '媒体探测失败，需复核：' + error
        try:
            metadata = json.loads(output)
            duration = float(metadata.get('format', {}).get('duration') or row.get('duration') or 0)
            if not any(s.get('codec_type') == 'video' for s in metadata.get('streams', [])):
                return 'probe_error', '未检测到视频轨道'
        except (ValueError, TypeError):
            return 'probe_error', '媒体参数无法解析'
        if mode != 'quick':
            points = [None] if mode == 'full' else sorted(set([0, max(0, duration / 2 - 7.5), max(0, duration - 15)]))
            for point in points:
                args = [self.binary('ffmpeg'), '-nostdin', '-v', 'error', '-xerror']
                if point is not None:
                    args += ['-ss', str(point)]
                args += ['-i', str(path)]
                if point is not None:
                    args += ['-t', '15']
                args += ['-map', '0:v:0', '-map', '0:a:0?', '-f', 'null', '-']
                code, _, error = self.command(args, 21600 if mode == 'full' else 180)
                if code in ('cancelled', 'timeout'):
                    return code, error
                if code != 0:
                    unsupported = any(s in error.lower() for s in ('decoder not found', 'unknown decoder', 'unsupported codec', 'no such file', 'permission denied'))
                    return ('unsupported' if unsupported else 'decode_error'), (f'抽样位置 {point:.1f} 秒：' if point is not None else '完整检查：') + error
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            return 'changed', '检查期间文件发生变化，结果失效'
        return 'passed', {'quick': '仅容器和轨道探测通过，不代表全片正常', 'sample': '开头/中间/结尾抽样解码通过，不代表全片正常', 'full': '全片解码通过；不代表无水印或视觉质量合格'}[mode]

    def run(self):
        try:
            while not self.cancel.is_set():
                with self.db() as c:
                    item = c.execute("SELECT r.video_id,r.mode FROM file_reviews r JOIN videos v ON v.id=r.video_id WHERE r.status='queued' ORDER BY r.updated LIMIT 1").fetchone()
                    if not item:
                        break
                    row = dict(c.execute('SELECT * FROM videos WHERE id=?', (item['video_id'],)).fetchone())
                    c.execute("UPDATE file_reviews SET status='running' WHERE video_id=?", (row['id'],))
                try:
                    result, detail = self.inspect(row, item['mode'])
                    if result in ('passed', 'decode_error'):
                        current = Path(row['path']).stat()
                        if current.st_size != row['size'] or abs(current.st_mtime - row['mtime']) > .01:
                            result, detail = 'changed', '检查期间文件变化，请重新扫描再检查'
                except Exception as error:
                    result, detail = 'error', '检查未完成：' + type(error).__name__
                with self.db() as c:
                    c.execute("UPDATE file_reviews SET status=?,result=?,detail=?,updated=?,decision=CASE WHEN ?='decode_error' THEN 'open' ELSE decision END WHERE video_id=?",
                              ('cancelled' if result == 'cancelled' else 'done', result, detail[:3000], time.time(), result, row['id']))
        finally:
            with self.db() as c:
                pending = c.execute("SELECT 1 FROM file_reviews r JOIN videos v ON v.id=r.video_id WHERE status='queued' LIMIT 1").fetchone()
            self.lock.release()
            if not self.cancel.is_set() and (pending or self.wake.is_set()):
                self.resume()
