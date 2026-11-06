"""Read-only display and relationship traits, independent of app startup."""
import json,re
from pathlib import Path
from media_naming import fallback_title,generic_name

PART_PATTERN=re.compile(r'(?i)(?<![a-z0-9])(?:part|pt|cd|disc|disk)[ ._-]*(\d{1,2})(?![a-z0-9])|第\s*(\d{1,2})\s*(?:部分|段|集)')
UNCENSORED_PATTERN=re.compile(r'无码|無碼|无修正|無修正|无码破解|無碼破解|(?i:\buncensored\b)')
CENSORED_PATTERN=re.compile(r'有码|有碼|(?i:\bcensored\b)')
VR_PATTERN=re.compile(r'(?i)(?<![a-z])vr(?![a-z])|虚拟现实|虛擬實境')

def resolution_label(height):
 h=int(height or 0)
 return '8K' if h>=4320 else '4K' if h>=2160 else '1440p' if h>=1440 else '1080p' if h>=1080 else '720p' if h>=720 else str(h)+'p' if h else '画质待解析'

def censorship(row,metadata=None,path=None,*,name_context=None):
 override=row.get('censor_override','auto')
 if override in ('censored','uncensored','unknown'):return override,'手动指定'
 if name_context is None:
  p=path if path is not None else Path(row['path'])
  name_context=p.stem+' '+p.parent.name
 uncensored=UNCENSORED_PATTERN.search(name_context)
 censored=CENSORED_PATTERN.search(name_context)
 if uncensored and censored:return 'unknown','文件名或目录标记冲突，请手动确认'
 if uncensored:return 'uncensored','文件名/目录明确标记'
 if censored:return 'censored','文件名/目录明确标记'
 if metadata is None:
  try:metadata=json.loads(row.get('amane_snapshot') or '{}')
  except ValueError:metadata={}
 kind=metadata.get('content_type')
 if kind in ('censored','uncensored'):return kind,'Amane 内容类型（未验证实际画面）'
 return 'unknown','缺少可靠资料，请手动确认'

def identity_presentation(row,path=None):
 p=path if path is not None else Path(row['path'])
 stem=p.stem;match=PART_PATTERN.search(stem)
 raw=row.get('amane_snapshot')
 try:metadata=json.loads(raw) if raw and raw!='{}' else {}
 except ValueError:metadata={}
 name_context=stem+' '+p.parent.name
 named=bool(VR_PATTERN.search(name_context))
 kind,reason=censorship(row,metadata,name_context=name_context)
 return {'part_index':int(match[1] or match[2]) if match else 0,'vr':bool(metadata.get('vr')) or named,
         'vr_source':'Amane' if metadata.get('vr') else '文件名/目录' if named else '',
         'censorship':kind,'censorship_source':reason}


def display_title(row,path):
 if row.get('source') in ('','filename') and not row.get('manual') and not row.get('amane_id') and generic_name(row.get('title')) and 'title' not in json.loads(row.get('locked_fields') or '[]'):return fallback_title(path)
 return row.get('title')


def projection_presentation(row,path=None):
 p=path if path is not None else Path(row['path']);traits=identity_presentation(row,p)
 return {'title':display_title(row,p),'vr':traits['vr'],'censorship':traits['censorship'],'resolution_label':resolution_label(row.get('height'))}


def presentation(row):
 row=dict(row);p=Path(row['path']);title=display_title(row,p)
 # Preserve absent title keys on sparse inputs used by legacy callers.
 if 'title' in row or title is not None:row['title']=title
 row.update(identity_presentation(row,p))
 base=PART_PATTERN.sub(' ',p.stem);base=re.sub(r'[ ._\-]+',' ',base).strip().casefold()
 row['part_key']=str(p.parent).casefold()+'|'+base if PART_PATTERN.search(p.stem) else ''
 row['censorship_label']={'censored':'有码','uncensored':'无码','unknown':'未知'}[row['censorship']]
 h=int(row.get('height') or 0)
 row.pop('amane_snapshot',None)
 row['resolution_label']=resolution_label(h)
 return row
