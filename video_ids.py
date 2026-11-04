"""Monotonic numeric media IDs for cache/quality identity across forget and restore.

Call while the data directory is owned and no media writer is active. Only the
application database schema/sequence changes; source media and caches are read-only.
"""
import os
import re
import json
from pathlib import Path

MAX_ID=2**63-1
REFERENCES=(('videos','id'),('media_links','video_id'),('media_assets','video_id'),
            ('file_reviews','video_id'),('artwork_jobs','video_id'),('scrape_jobs','video_id'),
            ('movie_preferences','preferred_id'),('duplicate_choices','preferred_id'))
IMAGE_ID=re.compile(r'([1-9]\d{0,18})(?:-(?:cover|poster|still-\d+|\d+))?\.(?:jpg|jpeg|png|webp)',re.I)


def history_watermark(connection,tables):
    """Read only known local references, never remote IDs inside metadata blobs."""
    result=0
    for table in ('relationship_undo','duplicate_preference_history','organization_drafts','organization_jobs'):
        if table not in tables:continue
        for row in connection.execute('SELECT payload FROM '+table):
            payload=json.loads(row[0])
            if table=='relationship_undo':items=payload
            elif table=='duplicate_preference_history':items=payload.values()
            else:items=payload['entries']
            key='preferred_id' if table=='duplicate_preference_history' else 'video_id'
            for item in items:
                value=item.get(key) if isinstance(item,dict) else None
                if type(value) is int and 0<value<=MAX_ID:result=max(result,value)
    return result


def cache_watermark(data):
    data=Path(data);result=0
    if not data.exists():return result
    with os.scandir(data) as entries:
        for entry in entries:
            match=IMAGE_ID.fullmatch(entry.name)
            if match and entry.is_file(follow_symlinks=False):
                value=int(match[1])
                if value<=MAX_ID:result=max(result,value)
    analysis=data/'analysis'
    try:stamp=analysis.lstat()
    except FileNotFoundError:return result
    if analysis.is_symlink() or getattr(stamp,'st_file_attributes',0)&1024 or not analysis.is_dir():return result
    with os.scandir(analysis) as entries:
        for entry in entries:
            if re.fullmatch(r'[1-9]\d{0,18}',entry.name) and entry.is_dir(follow_symlinks=False):
                value=int(entry.name)
                if value<=MAX_ID:result=max(result,value)
    return result


def high_watermark(connection,data=None):
    tables={row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")};result=0
    for table,column in REFERENCES:
        if table not in tables or column not in {row[1] for row in connection.execute('PRAGMA table_info('+table+')')}:continue
        value=connection.execute('SELECT MAX('+column+') FROM '+table+" WHERE typeof("+column+")='integer' AND "+column+'>0').fetchone()[0]
        if value is not None:result=max(result,value)
    if 'sqlite_sequence' in tables:
        value=connection.execute("SELECT MAX(seq) FROM sqlite_sequence WHERE name='videos' AND typeof(seq)='integer'").fetchone()[0]
        if value is not None:result=max(result,value)
    result=max(result,history_watermark(connection,tables))
    return max(result,cache_watermark(data)) if data is not None else result


def ensure_video_ids(db,data,minimum=0):
    """Repeatable, atomic table upgrade; preserve IDs, extra columns and dependencies."""
    floor=max(0,int(minimum),cache_watermark(data))
    with db() as c:
        definition=c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='videos'").fetchone()
        if not definition:return floor
        if c.in_transaction:raise ValueError('视频ID迁移必须在独立事务执行')
        foreign_keys=c.execute('PRAGMA foreign_keys').fetchone()[0]
        legacy=c.execute('PRAGMA legacy_alter_table').fetchone()[0]
        previous_violations={tuple(row) for row in c.execute('PRAGMA foreign_key_check')}
        c.execute('PRAGMA foreign_keys=OFF');c.execute('PRAGMA legacy_alter_table=ON')
        try:
            c.execute('BEGIN IMMEDIATE')
            floor=max(floor,high_watermark(c))
            if 'AUTOINCREMENT' not in definition[0].upper():
                schema,count=re.subn(r'(?i)(?:"id"|`id`|\[id\]|\bid)\s+INTEGER\s+PRIMARY\s+KEY\b',lambda match:match[0]+' AUTOINCREMENT',definition[0],count=1)
                if not count:raise ValueError('无法安全迁移当前视频ID定义')
                schema,count=re.subn(r'(?i)^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:"videos"|`videos`|\[videos\]|videos)(?=\s*\()','CREATE TABLE videos__identity_migration',schema,count=1)
                if not count:raise ValueError('无法安全识别视频表定义')
                dependencies=[row[0] for row in c.execute("SELECT sql FROM sqlite_master WHERE tbl_name='videos' AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type,name")]
                columns=','.join('"'+row[1].replace('"','""')+'"' for row in c.execute('PRAGMA table_info(videos)'))
                c.execute(schema)
                c.execute('INSERT INTO videos__identity_migration('+columns+') SELECT '+columns+' FROM videos')
                c.execute('DROP TABLE videos')
                c.execute('ALTER TABLE videos__identity_migration RENAME TO videos')
                for statement in dependencies:c.execute(statement)
                if {tuple(row) for row in c.execute('PRAGMA foreign_key_check')}!=previous_violations:raise ValueError('视频ID迁移改变了外键关系')
            current=c.execute("SELECT 1 FROM sqlite_sequence WHERE name='videos'").fetchone()
            if current:c.execute("UPDATE sqlite_sequence SET seq=? WHERE name='videos' AND seq<?",(floor,floor))
            else:c.execute("INSERT INTO sqlite_sequence(name,seq) VALUES('videos',?)",(floor,))
            c.commit()
        except BaseException:
            c.rollback();raise
        finally:
            c.execute('PRAGMA legacy_alter_table='+str(legacy));c.execute('PRAGMA foreign_keys='+str(foreign_keys))
    return floor
