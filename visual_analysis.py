"""Local OCR/visual heuristics. Executed as an isolated, cancellable worker."""
import argparse
import contextlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

DETECTOR_VERSION = 'ocr-sample-1'
CANCEL_FILE = None

def check_cancel():
    if CANCEL_FILE and CANCEL_FILE.exists():
        raise ValueError('视觉检测已取消')


def watermark_findings(frames):
    """Require persistent text in similar positions; suppress ordinary subtitles."""
    groups = defaultdict(list)
    for frame in frames:
        seen = set()
        for item in frame.get('text', []):
            text = re.sub(r'[^\w\u4e00-\u9fff]', '', item['text']).casefold()
            x, y, width, height = item['box']
            domain = bool(re.search(r'(?:www\.|https?://|\.(?:com|net|org|tv|cc|xyz)\b)', item['text'], re.I))
            if len(text) < 4 or item['confidence'] < .65 or height > .18:
                continue
            if not domain and (y > .72 or .18 < x < .82 and y > .35):
                continue
            key = (text, round(x * 8), round(y * 8))
            if key in seen:
                continue
            seen.add(key)
            groups[key].append(dict(item, at=frame['at'], frame=frame['frame']))
    matches = []
    minimum = max(3, math.ceil(len(frames) * .5))
    for occurrences in groups.values():
        if len(occurrences) < minimum:
            continue
        ratio = len(occurrences) / max(1, len(frames))
        confidence = min(.95, .6 + .25 * ratio + .1 * sum(i['confidence'] for i in occurrences) / len(occurrences))
        matches.append({'code': 'suspected_watermark', 'label': '疑似持续文字水印',
                        'confidence': round(confidence, 2), 'text': occurrences[0]['text'][:100],
                        'reason': f'{len(frames)} 个采样点中 {len(occurrences)} 个在相近位置出现相同文字；可能是台标或固定画面文字',
                        'samples': [{'at': round(i['at'], 2), 'frame': i['frame'], 'box': i['box']} for i in occurrences[:8]]})
    return sorted(matches, key=lambda i: i['confidence'], reverse=True)[:4]


def temporal_findings(log, offset, length, frame):
    findings = []
    for match in re.finditer(r'black_start:([\d.]+)\s+black_end:([\d.]+)\s+black_duration:([\d.]+)', log):
        start, end, duration = map(float, match.groups())
        if duration >= 2:
            findings.append(dict(code='suspected_black', label='抽样发现长黑屏', confidence=.8,
                                 reason='至少2秒近黑画面；可能是片头、转场或正常创作内容',
                                 samples=[dict(at=round(offset + start, 2), end=round(offset + end, 2), frame=frame)]))
    starts = [float(s) for s in re.findall(r'freeze_start:\s*([\d.]+)', log)]
    ends = [float(s) for s in re.findall(r'freeze_end:\s*([\d.]+)', log)]
    for start in starts:
        end = next((e for e in ends if e > start), length)
        if end - start >= 5:
            findings.append(dict(code='suspected_freeze', label='抽样发现长静止画面', confidence=.65,
                                 reason='至少5秒画面变化很小；静态镜头和片尾也会触发，不能据此确认损坏',
                                 samples=[dict(at=round(offset + start, 2), end=round(offset + end, 2), frame=frame)]))
    return findings


def run_ffmpeg(args, timeout):
    check_cancel()
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=log, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        start = time.monotonic()
        try:
            while process.poll() is None:
                check_cancel()
                if time.monotonic() - start > timeout:
                    raise ValueError('视觉采样超时')
                time.sleep(.2)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
        log.seek(0)
        text = log.read(64000).decode('utf-8', 'replace')
        if process.returncode:
            raise ValueError('视觉采样或解码失败，请先检查文件')
        return text


def analyze(path, ffmpeg, output, duration):
    # Models ship with the optional wheel. Never accept remote image/model URLs here.
    os.environ.setdefault('OMP_NUM_THREADS', '1')
    with contextlib.redirect_stdout(sys.stderr):
        from rapidocr_onnxruntime import RapidOCR
        import cv2
        import numpy as np
        engine = RapidOCR(intra_op_num_threads=1, inter_op_num_threads=1)
    output.mkdir(parents=True, exist_ok=True)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('缺少有效时长，请重新扫描媒体参数')
    points = sorted(set(round(min(duration - .05, duration * f), 2) for f in (.08, .2, .32, .44, .56, .68, .8, .92)))
    points = [max(0, p) for p in points]
    frames = []
    base = [ffmpeg, '-hide_banner', '-nostdin', '-nostats', '-threads', '1']
    for index, at in enumerate(points):
        target = output / f'{index}.jpg'
        run_ffmpeg(base + ['-v', 'error', '-ss', str(at), '-i', str(path), '-map', '0:v:0',
                          '-frames:v', '1', '-vf', "scale='min(960,iw)':-2", '-q:v', '3', '-y', str(target)], 45)
        image = cv2.imdecode(np.frombuffer(target.read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError('采样图片无法读取')
        height, width = image.shape[:2]
        check_cancel()
        with contextlib.redirect_stdout(sys.stderr):
            text, _ = engine(image)
        items = []
        for box, value, confidence in text or []:
            xs, ys = [p[0] for p in box], [p[1] for p in box]
            items.append(dict(text=str(value), confidence=float(confidence),
                              box=[round((min(xs) + max(xs)) / 2 / width, 3),
                                   round((min(ys) + max(ys)) / 2 / height, 3),
                                   round((max(xs) - min(xs)) / width, 3), round((max(ys) - min(ys)) / height, 3)]))
        frames.append(dict(at=at, frame=index, text=items))
    findings = watermark_findings(frames)
    for index in (0, len(points) // 2, len(points) - 1):
        at = min(points[index], max(0, duration - 15))
        length = min(15, max(.05, duration - at))
        log = run_ffmpeg(base + ['-v', 'info', '-ss', str(at), '-i', str(path), '-t', str(length),
                                '-an', '-vf', 'scale=640:-2,blackdetect=d=2:pix_th=0.1:pic_th=0.98,freezedetect=n=-50dB:d=5',
                                '-f', 'null', '-'], 90)
        findings.extend(temporal_findings(log, at, length, index))
    # Deduplicate overlapping sample windows on short clips.
    unique = {}
    for item in findings:
        key = (item['code'], item.get('text', ''), round(item['samples'][0]['at']))
        unique[key] = item
    selected = list(unique.values())[:16]
    for index, item in enumerate(selected):
        if item['code'] == 'suspected_watermark':
            continue
        sample = item['samples'][0]
        at = min(duration - .05, (sample['at'] + sample.get('end', sample['at'])) / 2)
        frame = len(frames) + index
        run_ffmpeg(base + ['-v','error','-ss',str(max(0,at)),'-i',str(path),'-frames:v','1',
                          '-vf',"scale='min(960,iw)':-2",'-q:v','3','-y',str(output/f'{frame}.jpg')],45)
        sample['frame'] = frame
        sample['evidence_at'] = round(at,2)
    check_cancel()
    return dict(version=DETECTOR_VERSION, findings=selected,
                samples=[dict(at=f['at'], frame=f['frame']) for f in frames],
                summary=f'{len(frames)} 帧本地OCR、3段黑屏/静止抽样；未发现不代表不存在问题。置信度是规则评分，未经统计校准。')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--ffmpeg', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cancel-file', type=Path)
    parser.add_argument('--duration', type=float, required=True)
    args = parser.parse_args()
    CANCEL_FILE = args.cancel_file
    try:
        print(json.dumps(analyze(args.input, args.ffmpeg, args.output, args.duration), ensure_ascii=True))
    except Exception as error:
        print(json.dumps({'error': str(error)[:300]}, ensure_ascii=True))
        sys.exit(1)
