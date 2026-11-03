"""Conservative title fallback; never writes original media."""
import re
from pathlib import Path

GENERIC=re.compile(r"(?i)^(?:(?:video|vid|movie|clip|file|sample|untitled|unknown|影片|视频|無題|无标题|新建视频)(?:[ _.-]*\d+)*|\d+|(?:part|cd|pt|disc)[ _.-]*\d+)$")
STRUCTURAL={'videos','video','movies','movie','media','download','downloads','影片','电影','视频','合集','新建文件夹','bdmv','stream','video_ts'}

def generic_name(value):
    return bool(GENERIC.fullmatch(str(value or '').strip()))

def directory_title(path):
    path=Path(path)
    for parent in list(path.parents)[:3]:
        name=parent.name.strip()
        if not name or name.casefold() in STRUCTURAL or generic_name(name):
            continue
        return name
    return path.stem

def fallback_title(path):
    path=Path(path)
    return directory_title(path) if generic_name(path.stem) else path.stem
