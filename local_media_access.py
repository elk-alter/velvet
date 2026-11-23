"""Read-only directory browsing and playback companion access."""
import ctypes
import mimetypes
import os
import re
import stat
from pathlib import Path
from organization_preflight import no_links


def directory_roots(server_mode):
    if server_mode:
        return [Path('/media')]
    if os.name != 'nt':
        return [Path('/')]
    kernel = ctypes.windll.kernel32
    drives = kernel.GetLogicalDrives()
    roots = [Path(chr(65 + bit) + ':\\') for bit in range(26) if drives & (1 << bit)]
    return [root for root in roots if kernel.GetDriveTypeW(ctypes.c_wchar_p(str(root))) in (2, 3)]


def local_directory(raw, roots):
    requested = Path(raw).expanduser()
    canonical_roots = [root.resolve() for root in roots]
    if not requested.is_absolute() or '..' in requested.parts or not any(requested.is_relative_to(root) for root in [*roots,*canonical_roots]):
        raise ValueError('请选择可访问盘符内的目录')
    try:
        directory = no_links(requested)
    except OSError as error:
        raise ValueError('无法读取此目录，请检查设备连接或权限') from error
    if not any(directory.is_relative_to(root) for root in canonical_roots):
        raise ValueError('目录不在允许浏览的卷内')
    if not directory.is_dir():
        raise ValueError('目录不存在或设备离线')
    return directory


def directory_page(raw, roots, offset=0):
    if not raw:
        return {'path':'','parent':None,'items':[{'name':str(root),'path':str(root)} for root in roots],'next':None}
    directory = local_directory(raw, roots)
    roots = [root.resolve() for root in roots]
    offset = max(0, int(offset))
    try:
        with os.scandir(directory) as entries:
            names = sorted((item.name for item in entries if item.is_dir(follow_symlinks=False)
                            and not getattr(item.stat(follow_symlinks=False),'st_file_attributes',0)&1024), key=str.casefold)
    except OSError as error:
        raise ValueError('无法浏览此目录，请检查设备连接或权限') from error
    root=next(root for root in roots if directory.is_relative_to(root))
    crumbs=[{'name':str(root),'path':str(root)}]
    current=root
    for part in directory.relative_to(root).parts:
        current=current/part
        crumbs.append({'name':part,'path':str(current)})
    return {'path':str(directory),'parent':str(directory.parent) if directory not in roots else '',
            'crumbs':crumbs,
            'items':[{'name':name,'path':str(directory/name)} for name in names[offset:offset+100]],
            'next':offset+100 if offset+100<len(names) else None}


def playback_state(path):
    path = Path(path)
    try:
        with path.open('rb') as stream:
            stream.read(1)
    except (FileNotFoundError, NotADirectoryError):
        return 'offline'
    except OSError:
        return 'unreadable'
    return 'ready'


def subtitle_files(video):
    video = no_links(video)
    candidates = (video.with_suffix('.vtt'), video.with_suffix('.srt'))
    tracks = {}
    for candidate in candidates:
        try:
            info = candidate.lstat()
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and not getattr(info,'st_file_attributes',0)&1024 and info.st_size<=2_000_000:
            tracks[candidate.suffix[1:]] = candidate
    return tracks


TIMING = re.compile(r'^(\d{2,}:\d{2}:\d{2}),(\d{3})(\s+-->\s+\d{2,}:\d{2}:\d{2}),(\d{3})$', re.M)


def read_subtitle(path, kind):
    with path.open('rb') as stream:
        payload = stream.read(2_000_001)
    if len(payload)>2_000_000:
        raise ValueError('字幕超过 2 MB')
    try:
        text = payload.decode('utf-16' if payload.startswith((b'\xff\xfe',b'\xfe\xff')) else 'utf-8-sig')
    except UnicodeDecodeError:
        text = payload.decode('gb18030')
    text = text.replace('\r\n','\n').replace('\r','\n')
    if kind=='srt':
        text, count = TIMING.subn(r'\1.\2\3.\4',text)
        if not count:raise ValueError('无法读取 SRT 时间轴')
        text = 'WEBVTT\n\n'+text
    elif text.split('\n',1)[0].rstrip()!='WEBVTT':
        raise ValueError('字幕缺少 WebVTT 标头')
    return text.encode('utf-8')


def playback_info(video):
    state = playback_state(video)
    try:
        tracks = subtitle_files(video) if state=='ready' else {}
    except (OSError, ValueError):
        tracks = {}
    return {'state':state,'type':mimetypes.guess_type(video)[0] or '',
            'tracks':[{'id':kind,'label':path.name} for kind,path in tracks.items()]}
