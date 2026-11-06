"""Incremental browse projection and SQL movie/version queries.

The projection contains derived searchable fields, never a serialized hierarchy.
Legacy metadata/edit models remain authoritative. Full movie data is lazy.
"""
import hashlib
import json
import os
import re
import time
from threading import RLock
import uuid
from pathlib import Path
from file_review import FileReview
from media_libraries import canonical
from media_presentation import projection_presentation


class LibraryQueryChanged(ValueError):pass


def fold(value):return str(value or '').casefold()
def path_text(value):return str(value or '').replace('\\','/').rstrip('/')
def tokens(value):return [part.strip() for part in re.split(r'[,，;；\n]',str(value or '')) if part.strip()]


class LibraryQuery:
    def __init__(self,db,core,libraries,policy,config=lambda:{},project=None,write_guard=None):
        self.db,self.core,self.libraries,self.policy=db,core,libraries,policy
        self.config=config
        self._refresh_lock=write_guard if write_guard is not None else RLock()
        self._progress={"stage":"idle"}
        self.project=project if project is not None else lambda row,path:self.core.present(row)
        self.projection_fields=(
            'v.id,v.path,v.size,v.mtime,v.height,v.code,v.source,v.title,v.manual,v.amane_id,'
            'v.locked_fields,v.censor_override,v.amane_snapshot,v.tags,v.auto_tags,v.actors'
            if project is projection_presentation else 'v.*'
        )
        libraries.exclusions();policy.ensure()
        with db() as c:
            c.execute('CREATE TABLE IF NOT EXISTS browse_projection(video_id INTEGER PRIMARY KEY,title TEXT,vr INTEGER,censorship TEXT,resolution_label TEXT,folder TEXT,visible INTEGER,tags TEXT,actors TEXT,review TEXT)')
            c.execute('CREATE TABLE IF NOT EXISTS browse_dirty(video_id INTEGER PRIMARY KEY)')
            c.execute('CREATE TABLE IF NOT EXISTS browse_state(id INTEGER PRIMARY KEY,revision INTEGER,rebuild INTEGER)')
            c.execute('INSERT OR IGNORE INTO browse_state(id,revision,rebuild) VALUES(1,1,0)')
            if 'projected_revision' not in {r[1] for r in c.execute('PRAGMA table_info(browse_state)')}:c.execute('ALTER TABLE browse_state ADD COLUMN projected_revision INTEGER DEFAULT -1')
            if 'projection_schema' not in {r[1] for r in c.execute('PRAGMA table_info(browse_state)')}:c.execute('ALTER TABLE browse_state ADD COLUMN projection_schema INTEGER DEFAULT 0')
            c.execute('UPDATE browse_state SET rebuild=CASE WHEN projection_schema IN (1,2,3,4) THEN rebuild ELSE 1 END,projected_revision=-1,projection_schema=5,revision=revision+1 WHERE projection_schema!=5')
            c.execute('DROP TABLE IF EXISTS browse_query_members')
            c.execute('CREATE TABLE IF NOT EXISTS browse_queries(id TEXT PRIMARY KEY,created REAL,revision INTEGER,criteria TEXT,totals TEXT)')
            c.execute('CREATE INDEX IF NOT EXISTS browse_query_reuse ON browse_queries(revision,criteria,created DESC)')
            c.execute('CREATE TABLE IF NOT EXISTS browse_results(query_id TEXT,ordinal INTEGER,payload TEXT,PRIMARY KEY(query_id,ordinal))')
            c.execute('INSERT OR IGNORE INTO browse_dirty SELECT v.id FROM videos v LEFT JOIN browse_projection p ON p.video_id=v.id WHERE p.video_id IS NULL')
            for action in ('INSERT','UPDATE'):
                c.execute('DROP TRIGGER IF EXISTS browse_video_'+action)
                c.execute('CREATE TRIGGER IF NOT EXISTS browse_video_'+action+' AFTER '+action+' ON videos BEGIN INSERT INTO browse_dirty SELECT NEW.id WHERE NOT EXISTS(SELECT 1 FROM browse_dirty WHERE video_id=NEW.id); UPDATE browse_state SET revision=revision+1 WHERE id=1; END')
            c.execute('CREATE TRIGGER IF NOT EXISTS browse_video_DELETE AFTER DELETE ON videos BEGIN DELETE FROM browse_projection WHERE video_id=OLD.id; DELETE FROM browse_dirty WHERE video_id=OLD.id; UPDATE browse_state SET revision=revision+1 WHERE id=1; END')
            tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'browse_dirty_movies' not in tables:
                c.execute('CREATE TABLE browse_dirty_movies(movie_key TEXT PRIMARY KEY)')
                c.execute('UPDATE browse_state SET rebuild=1 WHERE id=1')
            for table in ('media_links','movie_preferences','duplicate_choices','media_assets','library_settings','folders','excluded_directories','collection_rules','review_settings','file_reviews'):
                if table not in tables:continue
                for action in ('INSERT','UPDATE','DELETE'):
                    new='OLD' if action=='DELETE' else 'NEW'
                    dirty=(f' INSERT INTO browse_dirty SELECT {new}.video_id WHERE {new}.video_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM browse_dirty WHERE video_id={new}.video_id);' if table in ('media_links','media_assets','file_reviews') else '')
                    revisions=('OLD','NEW') if action=='UPDATE' else (new,)
                    if table in ('media_links','movie_preferences'):
                        for row in revisions:
                            dirty+=f' INSERT INTO browse_dirty_movies SELECT {row}.movie_key WHERE {row}.movie_key IS NOT NULL AND (SELECT rebuild FROM browse_state WHERE id=1)=0 AND NOT EXISTS(SELECT 1 FROM browse_dirty_movies WHERE movie_key={row}.movie_key);'
                    if table=='duplicate_choices':
                        for row in revisions:
                            dirty+=f' INSERT INTO browse_dirty_movies SELECT l.movie_key FROM media_links l WHERE l.video_id={row}.preferred_id AND NOT EXISTS(SELECT 1 FROM browse_dirty_movies WHERE movie_key=l.movie_key);'
                    rebuild='rebuild=1,' if table in ('library_settings','folders','excluded_directories','collection_rules','review_settings') else ''
                    event='UPDATE OF path,exclude_patterns,min_size' if table=='library_settings' and action=='UPDATE' else action
                    c.execute('DROP TRIGGER IF EXISTS browse_'+table+'_'+action)
                    c.execute('CREATE TRIGGER IF NOT EXISTS browse_'+table+'_'+action+' AFTER '+event+' ON '+table+' BEGIN'+dirty+' UPDATE browse_state SET '+rebuild+'revision=revision+1 WHERE id=1; END')

    def folder(self,row,rules,short_prefixes,cache=None):
        # Directory decisions are shared by siblings; manual identity remains per file.
        parent=path_text(row['relation_path'] or row['path']).rsplit('/',1)
        parent=parent[0] if len(parent)>1 else ''
        if cache is not None and parent in cache:resolved,directory=cache[parent]
        else:
            directory=path_text(canonical(parent,short_prefixes)) if parent else ''
            normal=fold(directory)
            matches=[rule for rule in rules if normal==fold(path_text(rule['path'])) or normal.startswith(fold(path_text(rule['path']))+'/')]
            resolved=None
            if matches:
                rule=max(matches,key=lambda item:len(item['path']))
                resolved=path_text(rule['path']) if rule['mode']=='group' else ''
            else:
                segments=directory.split('/')
                for i in range(len(segments)-1,max(-1,len(segments)-7),-1):
                    name=segments[i].strip()
                    if re.search(r'合集|全集|collection|box[ _-]?set',name,re.I) and not re.fullmatch(r'合集|全集|collections?|box[ _-]?sets?',name,re.I):
                        resolved='/'.join(segments[:i+1]);break
            if cache is not None:
                if len(cache)>=1024:cache.clear()
                cache[parent]=(resolved,directory)
        if resolved is not None:return resolved
        if row['relation_manual'] or row['code'] or row['source'] in ('nfo','online','amane'):return ''
        return directory

    def progress(self):
        return dict(self._progress)

    def refresh(self):
        with self._refresh_lock:
            self._progress={"stage":"checking"}
            try:
                self._refresh_projection()
            finally:
                self._progress={"stage":"idle"}

    def _refresh_projection(self):
        self.core.reconcile()
        with self.db() as c:
            state=c.execute('SELECT rebuild,revision,projected_revision FROM browse_state WHERE id=1').fetchone()
            if not state[0] and state[1]==state[2] and not c.execute('SELECT 1 FROM browse_dirty LIMIT 1').fetchone():return
        short_prefixes={};folder_cache={}
        policies=self.libraries.policies();excluded=[canonical(root,short_prefixes) for root in self.libraries.exclusions()]
        policies=[dict(p,path=str(canonical(p['path'],short_prefixes)),excluded_directories=excluded) for p in policies]
        rules=[dict(rule,path=str(canonical(rule['path'],short_prefixes))) for rule in self.policy.rules()]
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute('SELECT revision FROM browse_state WHERE id=1').fetchone()[0]!=state[1]:raise LibraryQueryChanged('片库规则或资料正在变化，请重新查询')
            relation_state=c.execute('SELECT generation,reconciled FROM media_relation_revision WHERE id=1').fetchone()
            if relation_state[0]!=relation_state[1]:raise LibraryQueryChanged('作品关系正在变化，请重新查询')
            rebuild=bool(state[0])
            if rebuild:
                c.execute('INSERT OR IGNORE INTO browse_dirty SELECT id FROM videos')
                c.execute('UPDATE browse_state SET rebuild=0 WHERE id=1')
            if not rebuild:
                c.execute('INSERT OR IGNORE INTO browse_dirty_movies SELECT l.movie_key FROM browse_dirty q JOIN media_links l ON l.video_id=q.video_id')
            setting=c.execute("SELECT value FROM review_settings WHERE key='target'").fetchone();target=int(setting[0]) if setting else 1080
            unreviewed={}
            total=c.execute("SELECT COUNT(*) FROM browse_dirty q JOIN media_links l ON l.video_id=q.video_id").fetchone()[0]
            processed=0
            self._progress={"stage":"indexing","processed":processed,"total":total}
            # Batches bound Python memory and avoid holding full metadata graphs.
            while True:
                cursor=c.execute(f"SELECT {self.projection_fields},COALESCE(a.relation_path,'') AS relation_path,l.manual AS relation_manual FROM browse_dirty q CROSS JOIN videos v ON v.id=q.video_id JOIN media_links l ON l.video_id=v.id LEFT JOIN media_assets a ON a.video_id=v.id ORDER BY q.video_id LIMIT 500")
                columns=tuple(field[0] for field in cursor.description)
                batch=cursor.fetchall()
                if not batch:break
                ids=[r['id'] for r in batch];slots=','.join('?' for _ in ids)
                records={r['video_id']:dict(r) for r in c.execute('SELECT * FROM file_reviews WHERE video_id IN ('+slots+')',ids)}
                values=[]
                for raw in batch:
                    row=dict(zip(columns,raw));raw_path=Path(row['path']);present=self.project(row,raw_path)
                    visible=True
                    if policies or excluded:
                        path=canonical(raw_path,short_prefixes);owner=self.libraries.owner(path,policies)
                        policy=next((p for p in policies if p['path']==owner),None)
                        visible=not any(path.is_relative_to(root) for root in excluded) and (not policy or self.libraries.included(path,policy,row['size'] or 0))
                    record=records.get(row['id']);below_target=bool(target and 0<(row['height'] or 0)<target)
                    if record is None and below_target in unreviewed:review_json=unreviewed[below_target]
                    else:
                        review=FileReview.describe(row,record or {},target)
                        # Evidence/notes remain on the per-file detail endpoint.
                        summary={key:review[key] for key in ('needs_update','decision','status','result','auto_status')}
                        summary['issues']=[{'code':item['code'],'source':item['source']} for item in review['issues']]
                        summary['pending_automatic']=any(item['review']=='pending' for item in review['automatic_findings'])
                        review_json=json.dumps(summary)
                        # With no review record, only the target-height rule varies.
                        # Keep this cache local to the current target/transaction.
                        if record is None:unreviewed[below_target]=review_json
                    tag_json=json.dumps(list(dict.fromkeys(tokens(row['tags'])+tokens(row['auto_tags'])))) if row['tags'] or row['auto_tags'] else '[]'
                    actor_json=json.dumps(tokens(row['actors'])) if row['actors'] else '[]'
                    values.append((row['id'],present['title'],present['vr'],present['censorship'],present['resolution_label'],self.folder(row,rules,short_prefixes,folder_cache),visible,tag_json,actor_json,review_json))
                for start in range(0,len(values),90):
                    rows=values[start:start+90]
                    placeholders=','.join(['(?,?,?,?,?,?,?,?,?,?)']*len(rows))
                    c.execute('INSERT OR REPLACE INTO browse_projection VALUES '+placeholders,
                              [value for row in rows for value in row])
                c.execute('DELETE FROM browse_dirty WHERE video_id IN ('+slots+')',ids)
                processed+=len(batch)
                self._progress={"stage":"indexing","processed":processed,"total":total}
            self._progress={"stage":"aggregating"}
            c.execute('DELETE FROM browse_dirty WHERE video_id NOT IN (SELECT id FROM videos)')
            self._refresh_versions(c,rebuild)
            c.execute('DELETE FROM browse_dirty_movies')
            c.execute('UPDATE browse_state SET projected_revision=revision WHERE id=1')

    def _refresh_versions(self,c,rebuild):
        affected=c.execute('SELECT COUNT(*) FROM browse_dirty_movies').fetchone()[0]
        if rebuild or not affected or affected>500:
            c.execute('DROP TABLE IF EXISTS browse_version_rows')
            c.execute('CREATE TABLE browse_version_rows AS '+self.CTE+' SELECT * FROM versions')
            c.execute('CREATE INDEX browse_version_movie ON browse_version_rows(movie_key,version_key)')
            c.execute('CREATE INDEX browse_version_collection ON browse_version_rows(collection_root,movie_key)')
            c.execute('CREATE INDEX browse_version_alternatives ON browse_version_rows(movie_key,version_key) WHERE version_count>1')
            return
        scope='movie_key IN (SELECT movie_key FROM browse_dirty_movies)'
        c.execute('CREATE TEMP TABLE affected_collection_roots(root TEXT PRIMARY KEY)')
        c.execute('INSERT OR IGNORE INTO affected_collection_roots SELECT collection_root FROM browse_version_rows WHERE '+scope+" AND collection_root!=''")
        c.execute('DELETE FROM browse_version_rows WHERE '+scope)
        scoped=self.FILES_CTE+' WHERE l.'+scope+self.VERSIONS_CTE
        c.execute('INSERT INTO browse_version_rows '+scoped+' SELECT * FROM versions')
        c.execute('INSERT OR IGNORE INTO affected_collection_roots SELECT collection_root FROM browse_version_rows WHERE '+scope+" AND collection_root!=''")
        for root, in c.execute('SELECT root FROM affected_collection_roots').fetchall():
            count=c.execute('SELECT COUNT(DISTINCT movie_key) FROM browse_version_rows WHERE collection_root=?',(root,)).fetchone()[0]
            c.execute('UPDATE browse_version_rows SET collection_count=? WHERE collection_root=? AND collection_count IS NOT ?',(count,root,count))
        c.execute('DROP TABLE affected_collection_roots')


    def parameters(self,query):
        criteria={key:str(query.get(key,default)).strip() for key,default in [('q',''),('mode','all'),('length','all'),('censorship',''),('vr',''),('height','0'),('tag_logic','all'),('exclude_tag',''),('actor',''),('studio',''),('source',''),('quality',''),('sort','new'),('collections','on'),('collection',''),('facet_kind',''),('facet_name','')]}
        for key,choices in [('mode',{'all','favorite','unwatched','recent','missing'}),('length',{'all','short','long'}),('censorship',{'','censored','uncensored','unknown','mixed'}),('vr',{'','vr','flat'}),('height',{'0','720','1080','1440','2160','4320'}),('tag_logic',{'all','any'}),('source',{'','matched','pending','error','partial'}),('quality',{'','needs_update','update','watermark','damaged','replace','suspected_watermark','suspected_black','suspected_freeze','decode_error','below_target','higher_local','auto_pending','auto_error','auto_unchecked','unchecked','active','inspection','pending','metadata_error','incomplete','no_cover','accepted','ignored'}),('sort',{'new','title','duration','rating','size'}),('collections',{'on','off'}),('facet_kind',{'','tags','user_tags','technical','studio','series','director'})]:
            if criteria[key] not in choices:raise ValueError('无效片库筛选：'+key)
        try:tags=json.loads(query.get('tags','[]'))
        except (ValueError,TypeError):raise ValueError('标签筛选应为JSON数组')
        if not isinstance(tags,list) or len(tags)>50 or not all(isinstance(t,str) and 0<len(t)<=200 for t in tags):raise ValueError('标签筛选无效')
        criteria['tags']=sorted(set(tags))
        # Keep persisted result sets from older matching semantics out of reuse.
        criteria['_match_schema']=3
        if any(len(value)>500 for value in criteria.values() if isinstance(value,str)):raise ValueError('筛选内容过长')
        return criteria

    FILES_CTE="""WITH files AS (
 SELECT v.id,v.size,v.mtime,v.duration,v.height,v.missing,v.watched,v.position,v.favorite,p.folder,p.review,l.movie_key,l.version_key
 FROM videos v JOIN browse_projection p ON p.video_id=v.id AND p.visible=1 JOIN media_links l ON l.video_id=v.id"""
    VERSIONS_CTE="""
 ), stats AS (
 SELECT movie_key,version_key,MIN(id) AS first_id,COUNT(*) AS part_count,SUM(COALESCE(size,0)) AS total_size,SUM(COALESCE(duration,0)) AS total_duration,MIN(COALESCE(height,0)) AS min_height,MIN(COALESCE(watched,0)) AS all_watched,MAX(COALESCE(position,0)) AS max_position,MIN(COALESCE(missing,0)) AS all_missing,MAX(COALESCE(missing,0)) AS any_missing,
 MAX(CASE WHEN id IN (SELECT preferred_id FROM duplicate_choices) THEN 1 ELSE 0 END) AS preferred,
 MAX(CASE WHEN json_extract(review,'$.needs_update') THEN 1 ELSE 0 END) AS needs_update,
 MAX(CASE WHEN json_extract(review,'$.decision')='open' THEN 1 ELSE 0 END) AS open_parts,
 MAX(CASE WHEN json_extract(review,'$.pending_automatic') THEN 1 ELSE 0 END) AS auto_pending,MAX(mtime) AS version_mtime,
 MAX(COALESCE(favorite,0)) AS version_favorite,MIN(folder) AS first_folder,MAX(folder) AS last_folder
 FROM files GROUP BY movie_key,version_key
 ), movie_stats AS (
 SELECT movie_key,COUNT(version_key) AS version_count,MAX(version_favorite) AS movie_favorite,MAX(version_mtime) AS movie_mtime,
 CASE WHEN MIN(first_folder)=MAX(last_folder) THEN COALESCE(MIN(first_folder),'') ELSE '' END AS collection_root
 FROM stats GROUP BY movie_key
 ), available_heights AS (SELECT movie_key,MAX(min_height) AS best_height FROM stats WHERE any_missing=0 GROUP BY movie_key),
 movie_health AS (SELECT s.movie_key,MAX(s.needs_update OR (s.open_parts AND s.min_height<COALESCE(a.best_height,0))) AS needs_update,MAX(s.min_height<COALESCE(a.best_height,0)) AS higher_local,MAX(s.auto_pending) AS auto_pending FROM stats s LEFT JOIN available_heights a ON a.movie_key=s.movie_key GROUP BY s.movie_key),
 collection_stats AS (SELECT collection_root,CASE WHEN collection_root='' THEN 0 ELSE COUNT(*) END AS collection_count FROM movie_stats GROUP BY collection_root),
 versions AS (
 SELECT v.id,v.path,v.size,v.mtime,v.duration,v.favorite,v.tags,v.error,v.code,v.actors,v.studio,v.date,v.width,v.height,v.codec,v.missing,v.watched,v.rating,v.source,v.source_tags,v.amane_id,v.auto_tags,v.position,p.title AS display_title,p.vr,p.censorship,p.resolution_label,p.folder,p.tags AS tag_list,p.actors AS actor_list,p.review,l.movie_key,l.version_key,l.part_no,s.part_count,s.total_size,s.total_duration,s.min_height,s.all_watched,s.max_position,s.all_missing,s.any_missing,s.preferred,health.needs_update,health.auto_pending,
 m.version_count,m.movie_favorite,m.movie_mtime,m.collection_root,cs.collection_count,
 CASE WHEN s.part_count=1 THEN v.id ELSE (
   SELECT lr.video_id FROM media_links lr JOIN videos vr ON vr.id=lr.video_id
   JOIN browse_projection pr ON pr.video_id=lr.video_id AND pr.visible=1
   WHERE lr.movie_key=s.movie_key AND lr.version_key=s.version_key
   ORDER BY COALESCE(vr.missing,0),
            CASE WHEN COALESCE(vr.watched,0)=0 AND COALESCE(vr.position,0)>0 THEN 0 WHEN COALESCE(vr.watched,0)=0 THEN 1 ELSE 2 END,
            lr.part_no,lr.video_id LIMIT 1
 ) END AS resume_id,
 CASE WHEN s.all_missing=0 AND (preferred_link.version_key=s.version_key) THEN 0 WHEN s.all_missing=0 AND pref.version_key=s.version_key THEN 1 ELSE 2 END AS preference_rank,
 health.higher_local AS higher_local,s.version_mtime,
 CASE WHEN s.part_count=1 THEN v.id ELSE (SELECT MIN(vs.id) FROM media_links ls JOIN videos vs ON vs.id=ls.video_id JOIN browse_projection ps ON ps.video_id=vs.id AND ps.visible=1 WHERE ls.movie_key=s.movie_key AND ls.version_key=s.version_key AND vs.mtime IS s.version_mtime) END AS version_first_id
 FROM stats s JOIN videos v ON v.id=CASE WHEN s.part_count=1 THEN s.first_id ELSE (
   SELECT lf.video_id FROM media_links lf JOIN videos vf ON vf.id=lf.video_id
   JOIN browse_projection pf ON pf.video_id=lf.video_id AND pf.visible=1
   WHERE lf.movie_key=s.movie_key AND lf.version_key=s.version_key
   ORDER BY COALESCE(vf.missing,0),lf.part_no,lf.video_id LIMIT 1
 ) END
 JOIN browse_projection p ON p.video_id=v.id
 JOIN media_links l ON l.video_id=v.id AND l.version_key=s.version_key
 JOIN movie_stats m ON m.movie_key=s.movie_key
 JOIN movie_health health ON health.movie_key=s.movie_key
 JOIN collection_stats cs ON cs.collection_root=m.collection_root
 LEFT JOIN movie_preferences pref ON pref.movie_key=s.movie_key
 LEFT JOIN media_links preferred_link ON preferred_link.video_id=pref.preferred_id AND preferred_link.movie_key=s.movie_key
 )"""

    CTE=FILES_CTE+VERSIONS_CTE

    QUERY_CTE="""WITH files AS (
 SELECT v.id,v.path,v.source,v.error,v.actors,v.studio,v.source_tags,p.review,l.movie_key,l.version_key
 FROM videos v JOIN browse_projection p ON p.video_id=v.id AND p.visible=1 JOIN media_links l ON l.video_id=v.id
 )"""

    def matching(self,criteria):
        def member(condition):
            return 'EXISTS(SELECT 1 FROM media_links lf JOIN videos vf ON vf.id=lf.video_id JOIN browse_projection pf ON pf.video_id=vf.id AND pf.visible=1 WHERE lf.movie_key=v.movie_key AND lf.version_key=v.version_key AND ('+condition+'))'
        where=['v.all_missing='+('1' if criteria['mode']=='missing' else '0')];args=[]
        for key,column in [('favorite','v.movie_favorite'),('unwatched','NOT v.all_watched'),('recent','(v.max_position>0 AND NOT v.all_watched)')]:
            if criteria['mode']==key:where.append(column)
        if criteria['q']:
            metadata="instr(fold(COALESCE(pf.title,'')||' '||COALESCE(vf.code,'')||' '||COALESCE(vf.actors,'')||' '||COALESCE(vf.studio,'')||' '||COALESCE(vf.tags,'')||' '||COALESCE(vf.auto_tags,'')),?)>0"
            where.append('('+member(metadata)+" OR EXISTS(SELECT 1 FROM files p WHERE p.movie_key=v.movie_key AND instr(fold(p.path),?)>0))");args.extend([fold(criteria['q'])]*2)
        if criteria['length']!='all':where.append('v.total_duration>0 AND v.total_duration'+('<1200' if criteria['length']=='short' else '>=1200'))
        if criteria['censorship']:where.append('v.censorship=?');args.append(criteria['censorship'])
        where.append('v.min_height>=?');args.append(int(criteria['height']))
        if criteria['vr']:where.append('v.vr=?');args.append(criteria['vr']=='vr')
        if criteria['tags']:
            tests=[]
            for tag in criteria['tags']:tests.append(member('EXISTS(SELECT 1 FROM json_each(pf.tags) WHERE value=?)'));args.append(tag)
            where.append('(' + (' AND ' if criteria['tag_logic']=='all' else ' OR ').join(tests)+')')
        if criteria['exclude_tag']:where.append('NOT '+member('EXISTS(SELECT 1 FROM json_each(pf.tags) WHERE value=?)'));args.append(criteria['exclude_tag'])
        if criteria['actor']:where.append(member('EXISTS(SELECT 1 FROM json_each(pf.actors) WHERE value=?)'));args.append(criteria['actor'])
        if criteria['studio']:where.append(member('vf.studio=?'));args.append(criteria['studio'])
        source=criteria['source']
        if source=='matched':where.append("v.source IN ('nfo','online','amane')")
        elif source=='pending':where.append("v.source NOT IN ('nfo','online','amane')")
        elif source=='error':where.append("v.error!=''")
        elif source=='partial':where.append("v.amane_id>0 AND (v.actors='' OR v.studio='' OR v.source_tags='')")
        quality=criteria['quality']
        if quality in ('needs_update','update'):where.append('v.needs_update=1')
        elif quality=='higher_local':where.append('v.higher_local=1')
        elif quality=='auto_pending':where.append('v.auto_pending=1')
        elif quality=='no_cover':where.append('EXISTS(SELECT 1 FROM files p WHERE p.movie_key=v.movie_key AND p.id NOT IN (SELECT id FROM cached_covers))')
        elif quality:
            tests={
                'accepted':"json_extract(p.review,'$.decision')='accepted'",
                'ignored':"json_extract(p.review,'$.decision')='ignored'",
                'unchecked':"json_extract(p.review,'$.status') IN ('unchecked','stale')",
                'active':"json_extract(p.review,'$.status') IN ('queued','running')",
                'auto_error':"json_extract(p.review,'$.auto_status')='error'",
                'auto_unchecked':"json_extract(p.review,'$.auto_status')!='done'",
                'inspection':"json_extract(p.review,'$.result') IN ('probe_error','decode_error','unavailable','unsupported','timeout','error','changed')",
                'pending':"p.source NOT IN ('amane','online','nfo')",
                'metadata_error':"p.error!=''",
                'incomplete':"p.actors='' OR p.studio='' OR p.source_tags=''",
            }
            if quality in tests:where.append('EXISTS(SELECT 1 FROM files p WHERE p.movie_key=v.movie_key AND ('+tests[quality]+'))')
            elif quality=='watermark':where.append("EXISTS(SELECT 1 FROM files p,json_each(p.review,'$.issues') issue WHERE p.movie_key=v.movie_key AND (json_extract(issue.value,'$.code')='watermark' OR (json_extract(issue.value,'$.code')='suspected_watermark' AND json_extract(issue.value,'$.source')='automatic_confirmed')))")
            else:
                where.append("EXISTS(SELECT 1 FROM files p,json_each(p.review,'$.issues') issue WHERE p.movie_key=v.movie_key AND json_extract(issue.value,'$.code')=?)");args.append(quality)
        if criteria['facet_kind'] and criteria['facet_name']:
            kind=criteria['facet_kind'];name=criteria['facet_name']
            if kind=='tags':condition='EXISTS(SELECT 1 FROM json_each(facet_tags(vf.source_tags)) WHERE value=?)'
            elif kind=='studio':condition='vf.studio=?'
            elif kind=='series':condition="json_extract(vf.amane_snapshot,'$.series')=?"
            elif kind=='director':condition="EXISTS(SELECT 1 FROM json_each(vf.amane_snapshot,'$.directors') WHERE value=?)"
            else:
                column='source_tags' if kind=='tags' else 'tags' if kind=='user_tags' else 'auto_tags'
                condition='EXISTS(SELECT 1 FROM json_each(token_list(vf.'+column+')) WHERE value=?)'
            where.append('EXISTS(SELECT 1 FROM videos vf JOIN media_links lf ON lf.video_id=vf.id JOIN browse_projection pf ON pf.video_id=vf.id AND pf.visible=1 WHERE lf.movie_key=v.movie_key AND ('+condition+'))');args.append(name)
        if criteria['collection']:
            where.append('collection_id(v.collection_root)=?');args.append(criteria['collection'])
        return ' AND '.join(where),args

    def query(self,query):
        criteria=self.parameters(query);offset=max(0,int(query.get('offset',0)));limit=min(96,max(1,int(query.get('limit',48))))
        ident=str(query.get('snapshot',''))
        aliases=self._facet_aliases(criteria)
        if criteria['facet_kind']=='tags':criteria['_aliases']=self._alias_signature(aliases)
        covers=self.cover_ids() if criteria['quality']=='no_cover' else None
        if covers is not None:criteria['_covers']=hashlib.sha256(json.dumps(sorted(covers)).encode()).hexdigest()
        with self._refresh_lock:
            self.refresh()
            return self._query_page(criteria,offset,limit,ident,covers,aliases)

    def _facet_aliases(self,criteria):
        return dict(self.config().get('tag_aliases',{})) if criteria['facet_kind']=='tags' else {}

    @staticmethod
    def _alias_signature(aliases):
        return hashlib.sha256(json.dumps(aliases,sort_keys=True).encode()).hexdigest()

    def _query_functions(self,c,aliases):
        c.create_function('fold',1,fold,deterministic=True)
        c.create_function('token_list',1,lambda value:json.dumps(tokens(value)),deterministic=True)
        c.create_function('facet_tags',1,lambda value:json.dumps([aliases.get(t,t) for t in tokens(value)]),deterministic=True)
        c.create_function('collection_id',1,lambda root:hashlib.sha256(fold(root).encode()).hexdigest()[:24] if root else '',deterministic=True)
        c.create_function('basename',1,lambda path:path_text(path).split('/')[-1],deterministic=True)

    def _matches(self,c,criteria,covers):
        where,args=self.matching(criteria)
        if covers is not None:
            c.execute('CREATE TEMP TABLE cached_covers(id INTEGER PRIMARY KEY)')
            c.executemany('INSERT INTO cached_covers VALUES(?)',((vid,) for vid in covers))
        order='all_missing,preference_rank,preferred DESC,min_height DESC,total_size DESC,version_key'
        fields='movie_key,version_key,resume_id,display_title,part_count,total_size,collection_root,collection_count,movie_mtime,rating,total_duration'
        grouped=criteria['collections']=='on' and not criteria['collection']
        key="CASE WHEN collection_root!='' AND collection_count>1 THEN 'collection:'||collection_id(collection_root) ELSE 'movie:'||movie_key END" if grouped else "'movie:'||movie_key"
        sql=self.QUERY_CTE+f""",
            ranked AS (SELECT {fields},ROW_NUMBER() OVER(PARTITION BY movie_key ORDER BY {order}) AS chosen
                       FROM browse_version_rows v WHERE version_count>1 AND ({where})),
            matched AS (SELECT {fields} FROM ranked WHERE chosen=1
                        UNION ALL SELECT {fields} FROM browse_version_rows v WHERE version_count=1 AND ({where}))
            SELECT {fields},{key} AS card_key FROM matched"""
        c.execute('CREATE TEMP TABLE browse_matches AS '+sql,[*args,*args])

    def _summary(self,c):
        fields='movie_favorite,total_size,all_watched,all_missing'
        row=c.execute(f'''WITH ranked AS (
            SELECT {fields},ROW_NUMBER() OVER(
                PARTITION BY movie_key ORDER BY all_missing,preference_rank,preferred DESC,
                                                min_height DESC,total_size DESC,version_key
            ) AS rank FROM browse_version_rows WHERE version_count>1
        ), defaults AS (
            SELECT {fields} FROM browse_version_rows WHERE version_count=1
            UNION ALL SELECT {fields} FROM ranked WHERE rank=1
        )
        SELECT COUNT(*) AS movies,COALESCE(SUM(movie_favorite),0) AS favorites,
               COALESCE(SUM(total_size),0) AS bytes,
               COALESCE(SUM(NOT all_watched AND NOT all_missing),0) AS unwatched
        FROM defaults''').fetchone()
        return dict(row)

    def _query_page(self,criteria,offset,limit,ident,covers,aliases):
        now=time.time()
        with self.db() as c:
            self._query_functions(c,aliases)
            c.execute('BEGIN IMMEDIATE')
            revision,projected=c.execute('SELECT revision,projected_revision FROM browse_state WHERE id=1').fetchone()
            if revision!=projected:raise LibraryQueryChanged('片库正在更新，请重新查询')
            if not ident:
                reusable=c.execute('SELECT id FROM browse_queries WHERE revision=? AND criteria=? AND created>? ORDER BY created DESC,id DESC LIMIT 1',(revision,json.dumps(criteria),now-1800)).fetchone()
                if reusable:ident=reusable[0]
            if ident:
                row=c.execute('SELECT * FROM browse_queries WHERE id=?',(ident,)).fetchone()
                if not row or now-row['created']>1800:raise LibraryQueryChanged('片库查询已过期，请重新查询')
                if row['revision']!=revision or json.loads(row['criteria'])!=criteria:raise LibraryQueryChanged('片库资料或筛选已变化，请重新查询')
                totals=json.loads(row['totals'])
            else:
                totals=None
                ident=uuid.uuid4().hex

            cached=[] if totals is None else c.execute(
                'SELECT payload FROM browse_results WHERE query_id=? AND ordinal>=? AND ordinal<? ORDER BY ordinal',
                (ident,offset,offset+limit)).fetchall()
            expected=limit if totals is None else max(0,min(limit,totals['total']-offset))
            if totals is None or len(cached)!=expected:
                self._matches(c,criteria,covers)
                if c.execute("SELECT 1 FROM browse_matches WHERE card_key LIKE 'collection:%' LIMIT 1").fetchone():
                    cards_sql='''SELECT card_key,COUNT(*) AS hit_count,SUM(total_size) AS card_size,MAX(movie_mtime) AS card_mtime,
                                        MAX(rating) AS card_rating,MAX(total_duration) AS card_duration,MIN(resume_id) AS cover_id,
                                        MIN(display_title) AS card_title,MIN(collection_root) AS root,MAX(collection_count) AS collection_total,
                                        MIN(movie_key) AS movie_key,MIN(version_key) AS version_key
                                 FROM browse_matches GROUP BY card_key'''
                    c.execute('CREATE TEMP TABLE browse_cards AS '+cards_sql)
                    cards_sql='SELECT * FROM browse_cards'
                else:
                    # A matched movie already has one selected version, so no grouping is needed.
                    cards_sql='''SELECT card_key,total_size AS card_size,movie_mtime AS card_mtime,rating AS card_rating,
                                        total_duration AS card_duration,resume_id AS cover_id,display_title AS card_title,
                                        collection_root AS root,movie_key,version_key FROM browse_matches'''
                if totals is None:
                    totals=dict(c.execute('SELECT COUNT(*) AS total,COALESCE(SUM(card_size),0) AS bytes FROM ('+cards_sql+')').fetchone())
                    totals['movies']=c.execute('SELECT COUNT(*) FROM browse_matches').fetchone()[0]
                    totals['summary']=self._summary(c)
                    if criteria['collection']:
                        collection=c.execute('SELECT MIN(collection_root) AS root,MAX(collection_count) AS total FROM browse_version_rows WHERE collection_id(collection_root)=?',(criteria['collection'],)).fetchone()
                        totals['collection_info']={'id':criteria['collection'],'title':path_text(collection['root']).split('/')[-1],'total_count':collection['total'] or 0} if collection['root'] else None
                    c.execute('INSERT INTO browse_queries VALUES(?,?,?,?,?)',(ident,now,revision,json.dumps(criteria),json.dumps(totals)))
                    stale=[r[0] for r in c.execute('SELECT id FROM browse_queries WHERE created<? OR id NOT IN (SELECT id FROM browse_queries ORDER BY created DESC,id DESC LIMIT 16)',(now-1800,))]
                    for key in stale:
                        c.execute('DELETE FROM browse_results WHERE query_id=?',(key,))
                        c.execute('DELETE FROM browse_queries WHERE id=?',(key,))
                order={'new':'card_mtime DESC','title':"fold(CASE WHEN card_key LIKE 'collection:%' THEN basename(root) ELSE card_title END)",'duration':'card_duration DESC','rating':'card_rating DESC','size':'card_size DESC'}[criteria['sort']]
                cards=c.execute(cards_sql+' ORDER BY '+order+',card_key LIMIT ? OFFSET ?',(limit,offset)).fetchall()
                if any(row['card_key'].startswith('collection:') for row in cards):
                    c.execute('CREATE INDEX temp.browse_match_preview ON browse_matches(card_key,display_title,resume_id)')
                items=[]
                for row in cards:
                    key=row['card_key']
                    if key.startswith('collection:'):
                        item={'kind':'collection','key':key,'id':key.split(':',1)[1],'title':path_text(row['root']).split('/')[-1],
                              'hit_count':row['hit_count'],'total_count':row['collection_total'],'size':row['card_size'],'cover_id':row['cover_id']}
                        item['previews']=[match[0] for match in c.execute('SELECT resume_id FROM browse_matches WHERE card_key=? ORDER BY display_title,resume_id LIMIT 4',(key,))]
                    else:
                        item={'kind':'movie','key':key,'id':row['cover_id'],'movie_key':row['movie_key'],'version_key':row['version_key']}
                    items.append(item)
                c.executemany('INSERT OR IGNORE INTO browse_results VALUES(?,?,?)',((ident,index,json.dumps(item)) for index,item in enumerate(items,offset)))
            else:
                items=[json.loads(row[0]) for row in cached]
            for item in items:
                if item['kind']=='collection':continue
                row=c.execute('SELECT * FROM browse_version_rows WHERE movie_key=? AND version_key=?',(item['movie_key'],item['version_key'])).fetchone()
                if not row:raise LibraryQueryChanged('作品版本已变化，请重新查询')
                fields={'title':'display_title','height':'min_height','duration':'total_duration','size':'total_size','position':'max_position','watched':'all_watched','missing':'all_missing','favorite':'movie_favorite','mtime':'movie_mtime'}
                item.update({key:row[column] for key,column in fields.items()})
                flags=c.execute('''
                    SELECT MAX(v.domestic),MAX(v.amateur)
                    FROM media_links l JOIN browse_projection p ON p.video_id=l.video_id AND p.visible=1
                    JOIN videos v ON v.id=l.video_id
                    WHERE l.movie_key=? AND l.version_key=?
                ''',(item['movie_key'],item['version_key'])).fetchone()
                item.update(domestic=bool(flags[0]),amateur=bool(flags[1]))
                item.update({key:row[key] for key in ('code','actors','studio','date','rating','vr','censorship','resolution_label','part_count','version_count','needs_update','higher_local')})
        for item in items:
            if item['kind']=='movie':item['censorship_label']={'censored':'有码','uncensored':'无码','unknown':'未知'}.get(item['censorship'],'标记不一致')
        return dict(totals,items=items,offset=offset,limit=limit,snapshot=ident,criteria=criteria,revision=revision)

    def movie(self,video_id):
        self.refresh()
        with self.db() as c:
            row=c.execute('SELECT l.movie_key FROM media_links l JOIN browse_projection p ON p.video_id=l.video_id AND p.visible=1 WHERE l.video_id=?',(int(video_id),)).fetchone()
        if not row:raise ValueError('作品不存在或已排除')
        movies=self.core.hierarchy(movie_key=row[0])
        if not movies:raise ValueError('作品不存在或已排除')
        return movies[0]

    def cover_ids(self):
        result=set()
        with os.scandir(self.core.data) as entries:
            for entry in entries:
                match=re.fullmatch(r'(\d+)-(cover|poster)\.jpg',entry.name)
                if match and entry.is_file():result.add(int(match[1]))
        return result

    def revision(self,quality=''):
        with self.db() as c:value=c.execute('SELECT revision FROM browse_state WHERE id=1').fetchone()[0]
        return {'revision':value,'covers':hashlib.sha256(json.dumps(sorted(self.cover_ids())).encode()).hexdigest() if quality=='no_cover' else None}

    def facets(self,kind,search='',limit=100):
        if kind not in ('tags','actors','studio') or len(search)>200:raise ValueError('无效筛选建议')
        self.refresh();limit=min(100,max(1,int(limit)))
        with self.db() as c:
            c.create_function('fold',1,fold,deterministic=True)
            source="SELECT v.studio AS name,l.movie_key FROM videos v JOIN browse_projection p ON p.video_id=v.id AND p.visible=1 JOIN media_links l ON l.video_id=v.id" if kind=='studio' else 'SELECT j.value AS name,l.movie_key FROM browse_projection p JOIN media_links l ON l.video_id=p.video_id,json_each(p.'+('tags' if kind=='tags' else 'actors')+') j WHERE p.visible=1'
            items=[dict(r) for r in c.execute('SELECT name,COUNT(DISTINCT movie_key) AS movies FROM ('+source+") WHERE name!='' AND instr(fold(name),?)>0 GROUP BY name ORDER BY movies DESC,name LIMIT ?",(fold(search),limit))]
        return {'items':items,'limit':limit}

    def selection(self,snapshot,keys,offset=0,limit=100):
        if not isinstance(keys,list) or not 1<=len(keys)<=500 or not all(isinstance(key,str) and len(key)<=250 for key in keys) or len(set(keys))!=len(keys):raise ValueError('请选择1–500张卡片')
        offset=max(0,int(offset));limit=min(100,max(1,int(limit)));self.refresh()
        with self.db() as c:
            c.execute('BEGIN')
            criteria,covers,aliases=self.valid_scope(c,snapshot)
            self._query_functions(c,aliases)
            self._matches(c,criteria,covers)
            slots=','.join('?' for _ in keys);args=tuple(keys)
            rows=c.execute('SELECT COUNT(DISTINCT card_key) FROM browse_matches WHERE card_key IN ('+slots+')',args).fetchone()[0]
            if rows!=len(keys):raise ValueError('选择含有当前查询之外的卡片')
            base=' FROM browse_matches WHERE card_key IN ('+slots+')'
            summary=dict(c.execute("SELECT COUNT(*) AS total,COALESCE(SUM(part_count),0) AS files,COALESCE(SUM(total_size),0) AS bytes,COALESCE(SUM(EXISTS(SELECT 1 FROM videos v WHERE v.id=resume_id AND trim(v.code)!='')),0) AS eligible"+base,args).fetchone())
            items=[dict(r) for r in c.execute('SELECT movie_key,version_key,resume_id AS id,display_title AS title,part_count,total_size AS size,(SELECT code FROM videos v WHERE v.id=resume_id) AS code'+base+' ORDER BY movie_key LIMIT ? OFFSET ?',(*args,limit,offset))]
        return dict(summary,items=items,offset=offset,limit=limit,snapshot=snapshot)

    def valid_scope(self,c,snapshot):
        row=c.execute('SELECT created,revision,criteria FROM browse_queries WHERE id=?',(snapshot,)).fetchone();revision=c.execute('SELECT revision FROM browse_state WHERE id=1').fetchone()[0]
        if not row or time.time()-row['created']>1800 or row['revision']!=revision:raise LibraryQueryChanged('查询范围已变化，请重新查询')
        criteria=json.loads(row['criteria'])
        if criteria.get('_match_schema')!=3:raise LibraryQueryChanged('筛选规则已更新，请重新查询')
        covers=self.cover_ids() if '_covers' in criteria else None
        if covers is not None and criteria['_covers']!=hashlib.sha256(json.dumps(sorted(covers)).encode()).hexdigest():raise LibraryQueryChanged('封面缓存已变化，请重新查询')
        aliases=self._facet_aliases(criteria)
        if criteria['facet_kind']=='tags' and criteria.get('_aliases')!=self._alias_signature(aliases):raise LibraryQueryChanged('标签别名已变化，请重新查询')
        return criteria,covers,aliases

    def random(self,snapshot):
        self.refresh()
        with self.db() as c:
            c.execute('BEGIN')
            criteria,covers,aliases=self.valid_scope(c,snapshot)
            self._query_functions(c,aliases)
            self._matches(c,criteria,covers)
            row=c.execute('SELECT resume_id FROM browse_matches ORDER BY RANDOM() LIMIT 1').fetchone()
        if not row:raise ValueError('当前范围没有影片')
        return {'id':row[0]}

    def lookup(self,code):
        if not isinstance(code,str) or not code.strip() or len(code)>100:raise ValueError('编号无效')
        self.refresh()
        with self.db() as c:row=c.execute('SELECT v.id FROM videos v JOIN browse_projection p ON p.video_id=v.id AND p.visible=1 WHERE v.code=? COLLATE NOCASE ORDER BY COALESCE(v.missing,0),v.id LIMIT 1',(code.strip(),)).fetchone()
        if not row:raise ValueError('找不到对应的本地视频')
        return {'id':row[0]}

    def relationship_options(self,search=''):
        if len(search)>500:raise ValueError('关系名称过长')
        self.refresh()
        with self.db() as c:
            c.create_function('fold',1,fold,deterministic=True)
            movies=[dict(row) for row in c.execute("SELECT movie_key AS key,CASE WHEN code!='' THEN code||' · ' ELSE '' END||display_title AS label FROM browse_version_rows WHERE instr(fold(code||' · '||display_title),?)>0 GROUP BY movie_key ORDER BY display_title,movie_key LIMIT 100",(fold(search),))]
            versions=[]
            for row in c.execute("SELECT * FROM browse_version_rows WHERE instr(fold(code||' · '||display_title||' · '||resolution_label||' · '||codec),?)>0 ORDER BY movie_key,version_key LIMIT 100",(fold(search),)):
                versions.append({'key':row['version_key'],'label':(row['code'] or row['display_title'])+' · '+row['resolution_label']+' · '+(row['codec'] or '未知编码')+' · '+path_text(row['path']).split('/')[-1]})
        return {'movies':movies,'versions':versions,'limit':100}
