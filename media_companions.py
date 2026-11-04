"""Conservative per-video ownership and filename mapping for companion files."""
import hashlib
import re
from pathlib import Path

RESOURCE_EXTENSIONS={'.srt','.ass','.ssa','.vtt','.sub','.idx','.sup','.nfo','.jpg','.jpeg','.png','.webp'}
GENERIC={'movie.nfo','poster.jpg','folder.jpg','fanart.jpg','thumb.jpg','cover.jpg'}


def owners(resource,videos):
    stem=resource.stem.casefold()
    exact=[video for video in videos if video.stem.casefold()==stem]
    if exact:return exact
    result=[]
    for video in videos:
        prefix=video.stem.casefold()
        if not stem.startswith(prefix):continue
        suffix=stem[len(prefix):]
        if resource.suffix.lower() in {'.jpg','.jpeg','.png','.webp'}:
            matched=bool(re.fullmatch(r'[.\-_](?:poster|fanart|thumb|cover|landscape)',suffix))
        else:
            matched=bool(re.fullmatch(r'(?:\.(?:[a-z]{2,3}(?:[-_][a-z0-9]{2,4})?|forced|default|commentary|signs))+',suffix))
        if matched:result.append(video)
    return result


def discover(base,names,video_extensions,allowed,hashing=False,cancel=lambda:False):
    base=Path(base)
    videos=[base/name for name in names if Path(name).suffix.lower() in video_extensions]
    result={video.name:[] for video in videos}
    for name in names:
        resource=base/name
        if resource.suffix.lower() not in RESOURCE_EXTENSIONS:continue
        candidates=owners(resource,videos)
        if not candidates and resource.name.casefold() not in GENERIC:continue
        shared=len(candidates)!=1
        recipients=candidates if candidates else videos
        record={'name':name,'shared':shared,'available':False,'reason':'shared_directory_resource' if shared else 'matching_video_stem'}
        try:
            if resource.is_symlink() or getattr(resource.lstat(),'st_file_attributes',0)&1024 or not allowed(resource):
                record['reason']='linked_or_excluded_resource'
            else:
                before=resource.stat();sha=hashlib.sha256()
                if hashing:
                    with resource.open('rb') as source:
                        for block in iter(lambda:source.read(4*1024*1024),b''):
                            if cancel():raise InterruptedError('Inventory cancelled')
                            sha.update(block)
                after=resource.stat()
                if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise ValueError('Resource changed')
                record.update(size=after.st_size,mtime_ns=after.st_mtime_ns,digest=sha.hexdigest() if hashing else '',available=True)
        except (OSError,ValueError) as error:record['error']=type(error).__name__
        for video in recipients:result[video.name].append(dict(record))
    return result


def destination(video_source,video_target,resource_name):
    source=Path(video_source);target=Path(video_target);resource=Path(resource_name)
    if resource.name!=resource_name or not resource.stem.casefold().startswith(source.stem.casefold()):raise ValueError('Companion name does not match its owner')
    return target.parent/(target.stem+resource_name[len(source.stem):])
