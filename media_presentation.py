"""Read-only display and relationship traits, independent of app startup."""
import json,re
from pathlib import Path
from media_naming import fallback_title,generic_name

PART_PATTERN=re.compile(r'(?i)(?<![a-z0-9])(?:part|pt|cd|disc|disk)[ ._-]*(\d{1,2})(?![a-z0-9])|第\s*(\d{1,2})\s*(?:部分|段|集)')
UNCENSORED_PATTERN=re.compile(r'无码|無碼|无修正|無修正|无码破解|無碼破解|(?i:\buncensored\b)')
CENSORED_PATTERN=re.compile(r'有码|有碼|(?i:\bcensored\b)')
VR_PATTERN=re.compile(r'(?i)(?<![a-z])vr(?![a-z])|虚拟现实|虛擬實境')
DOMESTIC_LABELS={'国产','國產','国产片','國產片','国产视频','國產影片','中国大陆','中國大陸','中国内地','中國內地'}
CHINA_REGIONS=DOMESTIC_LABELS|{'中国','中國','大陆','大陸','china','mainland china','cn','chn','prc'}
OTHER_REGIONS={'日本','japan','jp','jpn','韩国','韓國','south korea','korea','kr','kor',
               '美国','美國','united states','us','usa','台湾','臺灣','台灣','taiwan','tw',
               '香港','hong kong','hk','澳门','澳門','macau','mo'}
HAN_PATTERN=re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff]')
JAPANESE_KANA_PATTERN=re.compile(r'[\u3041-\u3096\u30a1-\u30fa\uff66-\uff9d]')
CHINESE_SUBTITLE_PATTERN=re.compile(r'中文字幕|中字|中字幕|简中字幕|簡中字幕|繁中字幕|中日双字|中日雙字')
AMATEUR_LABELS={'素人','素人片','素人作品','素人動画','amateur'}
CENSOR_LABELS={'censored':{'有码','有碼','有修正','censored'},'uncensored':{'无码','無碼','无修正','無修正','无码破解','無碼破解','uncensored'}}
TOKYO_HOT_STUDIOS={'tokyo hot','tokyo-hot','tokyohot','東京熱','东京热'}
TOKYO_HOT_FILENAME=re.compile(r'(?<![a-z0-9])(?:tokyo[ _-]?hot|東京熱|东京热)[ _-]+n\d{4,5}(?![a-z0-9])',re.I)


def source_metadata(row):
 try:
  metadata=json.loads(row.get('amane_snapshot') or '{}')
 except ValueError:
  return {}
 return metadata if isinstance(metadata,dict) else {}


def source_labels(row):
 return {tag.strip().casefold() for tag in re.split(r'[,，;；\n]',row.get('source_tags') or '') if tag.strip()}


def card_flags(row,path=None):
 p=path if path is not None else Path(row['path'])
 names=set(re.split(r'[\W_]+',f'{p.parent.name} {p.stem}'.casefold()))
 labels=source_labels(row)
 metadata=source_metadata(row)
 region={str(metadata.get(field) or '').strip().casefold() for field in ('country','region')}
 kind=str(metadata.get('content_type') or '').strip().casefold()
 chinese_filename=bool(HAN_PATTERN.search(p.stem) and not JAPANESE_KANA_PATTERN.search(p.stem)
                       and not CHINESE_SUBTITLE_PATTERN.search(p.stem) and not region & OTHER_REGIONS)
 results={}
 for field,scraped,named in (
  ('domestic',bool(labels & DOMESTIC_LABELS or region & CHINA_REGIONS),bool(names & DOMESTIC_LABELS)),
  ('amateur',bool(labels & AMATEUR_LABELS or kind in AMATEUR_LABELS),bool(names & AMATEUR_LABELS)),
 ):
  override=row.get(field) or 0
  inferred=field=='domestic' and chinese_filename
  results[field]=override==1 if override else scraped or named or inferred
  results[field+'_override']=override
  results[field+'_reason']='手动显示' if override==1 else '手动隐藏' if override==-1 else '刮削资料' if scraped else '文件名或直接父目录' if named else '按中文文件名推定' if inferred else '缺少明确标记'
 return results

def category_tags(row):
 manual = row.get('tags') or ''
 if 'category_tags' in json.loads(row.get('locked_fields') or '[]'):
  return manual
 raw = (row.get('source_tags') or '') + ',' + manual
 return ', '.join(dict.fromkeys(tag.strip() for tag in re.split(r'[,，;；\n]', raw) if tag.strip()))


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
 if metadata is None:metadata=source_metadata(row)
 kind=str(metadata.get('content_type') or '').strip().casefold()
 labels=source_labels(row)|{kind}
 matched=[value for value,names in CENSOR_LABELS.items() if labels & names]
 if len(matched)>1:return 'unknown','刮削资料的有码／无码标记冲突'
 if matched:return matched[0],'刮削资料的明确标记'
 studios={str(value or '').strip().casefold() for value in (row.get('studio'),metadata.get('studio'))}
 code=str(row.get('code') or '').strip()
 if studios & TOKYO_HOT_STUDIOS and re.fullmatch(r'n\d{4,5}',code,re.I):
  return 'uncensored','根据片商 Tokyo-Hot 和作品编号推定'
 if TOKYO_HOT_FILENAME.search(name_context):
  return 'uncensored','根据文件名中的 Tokyo-Hot 和作品编号推定'
 return 'unknown','缺少可靠资料，请手动确认'

def identity_presentation(row,path=None):
 p=path if path is not None else Path(row['path'])
 stem=p.stem;match=PART_PATTERN.search(stem)
 metadata=source_metadata(row)
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
 row['category_tags']=category_tags(row)
 row.update(card_flags(row,p))
 row.pop('amane_snapshot',None)
 row['resolution_label']=resolution_label(h)
 return row
