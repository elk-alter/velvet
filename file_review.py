"""Read-only media inspection and per-file replacement decisions."""
import json
import subprocess
import tempfile
import threading
import time
import sys
import shutil
import uuid
import importlib.util
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
            fields = {r[1] for r in c.execute('PRAGMA table_info(file_reviews)')}
            for name, definition in {'auto_findings':"TEXT DEFAULT '[]'", 'auto_status':"TEXT DEFAULT ''", 'auto_error':"TEXT DEFAULT ''", 'auto_updated':'REAL DEFAULT 0', 'auto_attempts':'INTEGER DEFAULT 0', 'auto_overrides':"TEXT DEFAULT '{}'", 'auto_generation':"TEXT DEFAULT ''"}.items():
                if name not in fields:
                    c.execute(f'ALTER TABLE file_reviews ADD COLUMN {name} {definition}')
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
        if old and (old['size'], old['mtime']) == (row['size'],row['mtime']) and (old['result']=='changed' or old['auto_status']=='stale'):
            raise ValueError('文件已变化，请先重新扫描目录')
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

    def auto_settings(self):
        with self.db() as c:
            row = c.execute("SELECT value FROM review_settings WHERE key='auto_enabled'").fetchone()
        return {'enabled': bool(row and row[0] == '1'), 'available': importlib.util.find_spec('rapidocr_onnxruntime') is not None and bool(self.binary('ffmpeg')) and bool(self.binary('ffprobe'))}

    def set_auto(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('自动识别开关无效')
        if enabled and not self.auto_settings()['available']:
            raise ValueError('自动识别需要 FFmpeg 及 requirements-vision.txt 中的本地OCR依赖')
        if enabled and not self.lock.locked():
            self.cancel.clear()
        with self.db() as c:
            c.execute("INSERT OR REPLACE INTO review_settings VALUES('auto_enabled',?)", ('1' if enabled else '0',))

    def review_automatic(self, ids, code, decision):
        if code not in ('suspected_watermark','suspected_black','suspected_freeze') or decision not in ('pending','confirmed','dismissed'):
            raise ValueError('无效的自动标记审阅')
        rows = self.rows(ids)
        with self.db() as c:
            for row in rows:
                self.touch(c,row)
                record = c.execute('SELECT auto_findings,auto_overrides FROM file_reviews WHERE video_id=?',(row['id'],)).fetchone()
                if not any(i['code']==code for i in json.loads(record[0])):
                    raise ValueError('自动结果已变化，请刷新')
                overrides=json.loads(record[1]);overrides[code]=decision
                c.execute("UPDATE file_reviews SET auto_overrides=?,decision=CASE WHEN ?='confirmed' THEN 'open' ELSE decision END WHERE video_id=?",(json.dumps(overrides),decision,row['id']))

    def auto_tick(self):
        settings = self.auto_settings()
        if not settings['enabled'] or not settings['available'] or self.lock.locked() or self.cancel.is_set():
            return
        with self.db() as c:
            if c.execute("SELECT 1 FROM file_reviews WHERE status IN ('queued','running') LIMIT 1").fetchone():
                return
            rows = [dict(r) for r in c.execute("SELECT v.* FROM videos v LEFT JOIN file_reviews r ON r.video_id=v.id WHERE v.missing=0 AND v.duration>0 AND v.mtime<? AND (r.video_id IS NULL OR r.size!=v.size OR r.mtime!=v.mtime OR (r.auto_status!='done' AND r.auto_attempts<3 AND r.auto_updated<? AND r.status!='cancelled' AND r.result!='changed' AND r.auto_status!='stale')) ORDER BY v.id LIMIT 100", (time.time()-120, time.time()-86400))]
        excluded=getattr(self,'excluded',lambda:[])()
        for row in rows:
            if any(Path(row['path']).is_relative_to(Path(root)) for root in excluded):continue
            if Path(row['path']).is_file():
                self.enqueue([row['id']], 'auto')
                break

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
            stale = bool(row) and ((row['size'], row['mtime']) != (video['size'], video['mtime']) or row.get('result')=='changed' or row.get('auto_status')=='stale')
            flags = [] if stale else json.loads(row.get('flags', '[]'))
            issues = [{'code': f, 'label': ISSUES[f], 'source': 'manual'} for f in flags]
            findings = [] if stale else json.loads(row.get('auto_findings') or '[]')
            overrides = {} if stale else json.loads(row.get('auto_overrides') or '{}')
            findings = [dict(item, review=overrides.get(item['code'],'pending')) for item in findings]
            for item in findings:
                if item['review'] != 'dismissed':
                    issues.append(dict(item, source='automatic_confirmed' if item['review']=='confirmed' else 'automatic'))
            outcome = 'changed' if stale and row.get('result')=='changed' else '' if stale else row.get('result', '')
            if outcome == 'decode_error':
                issues.append({'code': 'decode_error', 'label': '解码异常，疑似损坏', 'source': 'inspection'})
            if target and 0 < (video.get('height') or 0) < target:
                issues.append({'code': 'below_target', 'label': f'低于目标 {target}p', 'source': 'rule'})
            if video['id'] in higher:
                issues.append({'code': 'higher_local', 'label': '本地有更高分辨率版本（待对比）', 'source': 'rule'})
            decision = 'open' if stale else row.get('decision', 'open')
            result[video['id']] = dict(issues=issues, needs_update=bool(issues) and decision == 'open',
                                      has_cover=bool(self.data and any((self.data / f"{video['id']}-{kind}.jpg").is_file() for kind in ('cover','poster'))),
                                      automatic_findings=findings,
                                      auto_status='' if stale else row.get('auto_status',''), auto_error='' if stale else row.get('auto_error',''), auto_updated=row.get('auto_updated',0),
                                      decision=decision, status='stale' if stale else row.get('status', 'unchecked'),
                                      mode=row.get('mode', ''), result=outcome, note=row.get('note', ''),
                                      detail='' if stale else row.get('detail', ''), updated=row.get('updated', 0))
        return {'files': result, 'target': target, 'running': self.lock.locked(),
                'queued': sum(r['status'] == 'queued' for r in records.values()), 'automatic': self.auto_settings()}

    def enqueue(self, ids, mode):
        if mode not in ('quick', 'sample', 'full', 'auto'):
            raise ValueError('未知检查范围')
        if mode == 'auto' and not self.auto_settings()['available']:
            raise ValueError('请先安装 requirements-vision.txt 中的本地OCR依赖')
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
                        if '--cancel-file' in args:
                            Path(args[args.index('--cancel-file')+1]).touch()
                            try:process.wait(timeout=5)
                            except subprocess.TimeoutExpired:pass
                        return 'cancelled', '', ''
                    if time.monotonic() - start > timeout:
                        if '--cancel-file' in args:
                            Path(args[args.index('--cancel-file')+1]).touch()
                            try:process.wait(timeout=5)
                            except subprocess.TimeoutExpired:pass
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
                if mode == 'auto':
                    args += ['-threads','2']
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
        return 'passed', {'quick': '仅容器和轨道探测通过，不代表全片正常', 'sample': '开头/中间/结尾抽样解码通过，不代表全片正常', 'auto':'抽样解码通过；视觉识别结论另列，不代表全片正常', 'full': '全片解码通过；不代表无水印或视觉质量合格'}[mode]

    def analyze(self, row):
        from visual_analysis import DETECTOR_VERSION
        if not self.data:
            raise ValueError('未配置视觉缓存目录')
        with tempfile.TemporaryDirectory(dir=self.data) as temp:
            output = Path(temp)
            code, text, _ = self.command([sys.executable, str(Path(__file__).with_name('visual_analysis.py')),
                                         '--input', row['path'], '--ffmpeg', self.binary('ffmpeg'),
                                         '--duration', str(row['duration']), '--output', str(output),
                                         '--cancel-file', str(output/'cancel')], 420)
            if code == 'cancelled':
                return None
            if code != 0:
                raise ValueError('视觉识别未完成，请检查工具、媒体或重新运行；未产生无水印结论')
            data = json.loads(text)
            if data.get('version') != DETECTOR_VERSION or not isinstance(data.get('findings'), list):
                raise ValueError('视觉识别结果无效')
            current = Path(row['path']).stat()
            if current.st_size != row['size'] or abs(current.st_mtime-row['mtime']) > .01:
                raise ValueError('文件已变化，请重新扫描')
            data['generation'] = uuid.uuid4().hex
            target = self.data/'analysis'/str(row['id'])/data['generation']
            target.mkdir(parents=True, exist_ok=True)
            for image in output.glob('*.jpg'):
                shutil.copyfile(image, target/image.name)
            return data

    def run(self):
        try:
            while not self.cancel.is_set():
                with self.db() as c:
                    item = c.execute("SELECT r.video_id,r.mode FROM file_reviews r JOIN videos v ON v.id=r.video_id WHERE r.status='queued' ORDER BY CASE WHEN r.mode='auto' THEN 1 ELSE 0 END,r.updated LIMIT 1").fetchone()
                    if not item:
                        break
                    row = dict(c.execute('SELECT * FROM videos WHERE id=?', (item['video_id'],)).fetchone())
                    c.execute("UPDATE file_reviews SET status='running' WHERE video_id=?", (row['id'],))
                analysis = None
                analysis_error = ''
                try:
                    result, detail = self.inspect(row, item['mode'])
                    if result in ('passed', 'decode_error'):
                        current = Path(row['path']).stat()
                        if current.st_size != row['size'] or abs(current.st_mtime - row['mtime']) > .01:
                            result, detail = 'changed', '检查期间文件变化，请重新扫描再检查'
                    if item['mode'] == 'auto' and result == 'passed':
                        try:
                            analysis = self.analyze(row)
                            if analysis is None:result, detail = 'cancelled','视觉识别已取消'
                        except Exception as error:
                            analysis_error = str(error)[:300]
                except Exception as error:
                    result, detail = 'error', '检查未完成：' + type(error).__name__
                with self.db() as c:
                    if item['mode'] == 'auto':
                        if analysis is not None:
                            old = json.loads(c.execute('SELECT auto_findings FROM file_reviews WHERE video_id=?',(row['id'],)).fetchone()[0] or '[]')
                            added = {i['code'] for i in analysis['findings']} - {i['code'] for i in old}
                            c.execute("UPDATE file_reviews SET auto_findings=?,auto_generation=?,auto_status='done',auto_error='',auto_updated=?,auto_attempts=auto_attempts+1,decision=CASE WHEN ? THEN 'open' ELSE decision END WHERE video_id=?",(json.dumps(analysis['findings'],ensure_ascii=False),analysis['generation'],time.time(),bool(added),row['id']))
                            detail += '；'+analysis['summary']
                        else:
                            c.execute("UPDATE file_reviews SET auto_status=?,auto_error=?,auto_updated=?,auto_attempts=auto_attempts+1 WHERE video_id=?",('cancelled' if result=='cancelled' else 'stale' if result=='changed' or '文件已变化' in analysis_error else 'error',analysis_error or detail,time.time(),row['id']))
                    c.execute("UPDATE file_reviews SET status=?,result=?,detail=?,updated=?,decision=CASE WHEN ?='decode_error' THEN 'open' ELSE decision END WHERE video_id=?",
                              ('cancelled' if result == 'cancelled' else 'done', result, detail[:3000], time.time(), result, row['id']))
        finally:
            with self.db() as c:
                pending = c.execute("SELECT 1 FROM file_reviews r JOIN videos v ON v.id=r.video_id WHERE status='queued' LIMIT 1").fetchone()
            self.lock.release()
            if not self.cancel.is_set() and (pending or self.wake.is_set()):
                self.resume()
