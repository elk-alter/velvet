"""Incremental browse projection and SQL movie/version queries.

The projection contains derived searchable fields, never a serialized hierarchy.
Legacy metadata/edit models remain authoritative. Full movie data is lazy.
"""
import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from file_review import FileReview
from media_libraries import canonical


class LibraryQueryChanged(ValueError):pass


def fold(value):return str(value or '').casefold()
def path_text(value):return str(value or '').replace('\\','/').rstrip('/')
def tokens(value):return [part.strip() for part in re.split(r'[,，;；\n]',str(value or '')) if part.strip()]


class LibraryQuery:
    def __init__(self,db,core,libraries,policy):
        self.db,self.core,self.libraries,self.policy=db,core,libraries,policy
        libraries.exclusions();policy.ensure()
        with db() as c:
            c.execute('CREATE TABLE IF NOT EXISTS browse_projection(video_id INTEGER PRIMARY KEY,title TEXT,vr INTEGER,censorship TEXT,resolution_label TEXT,folder TEXT,visible INTEGER,tags TEXT,actors TEXT,review TEXT)')
            c.execute('CREATE TABLE IF NOT EXISTS browse_dirty(video_id INTEGER PRIMARY KEY)')
            c.execute('CREATE TABLE IF NOT EXISTS browse_state(id INTEGER PRIMARY KEY,revision INTEGER,rebuild INTEGER)')
            c.execute('INSERT OR IGNORE INTO browse_state(id,revision,rebuild) VALUES(1,1,0)')
            if 'projected_revision' not in {r[1] for r in c.execute('PRAGMA table_info(browse_state)')}:c.execute('ALTER TABLE browse_state ADD COLUMN projected_revision INTEGER DEFAULT -1')
            if 'projection_schema' not in {r[1] for r in c.execute('PRAGMA table_info(browse_state)')}:c.execute('ALTER TABLE browse_state ADD COLUMN projection_schema INTEGER DEFAULT 0')
            c.execute('UPDATE browse_state SET projection_schema=1,rebuild=1,revision=revision+1 WHERE projection_schema!=1')
            c.execute('CREATE TABLE IF NOT EXISTS browse_queries(id TEXT PRIMARY KEY,created REAL,revision INTEGER,criteria TEXT,totals TEXT)')
            c.execute('CREATE TABLE IF NOT EXISTS browse_results(query_id TEXT,ordinal INTEGER,payload TEXT,PRIMARY KEY(query_id,ordinal))')
            c.execute('INSERT OR IGNORE INTO browse_dirty SELECT v.id FROM videos v LEFT JOIN browse_projection p ON p.video_id=v.id WHERE p.video_id IS NULL')
            for action in ('INSERT','UPDATE'):
                c.execute('DROP TRIGGER IF EXISTS browse_video_'+action)
                c.execute('CREATE TRIGGER IF NOT EXISTS browse_video_'+action+' AFTER '+action+' ON videos BEGIN INSERT INTO browse_dirty SELECT NEW.id WHERE NOT EXISTS(SELECT 1 FROM browse_dirty WHERE video_id=NEW.id); UPDATE browse_state SET revision=revision+1 WHERE id=1; END')
            c.execute('CREATE TRIGGER IF NOT EXISTS browse_video_DELETE AFTER DELETE ON videos BEGIN DELETE FROM browse_projection WHERE video_id=OLD.id; DELETE FROM browse_dirty WHERE video_id=OLD.id; UPDATE browse_state SET revision=revision+1 WHERE id=1; END')
            tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ('media_links','movie_preferences','duplicate_choices','media_assets','library_settings','folders','excluded_directories','collection_rules','review_settings','file_reviews'):
                if table not in tables:continue
                for action in ('INSERT','UPDATE','DELETE'):
                    new='OLD' if action=='DELETE' else 'NEW'
                    dirty=(f' INSERT INTO browse_dirty SELECT {new}.video_id WHERE {new}.video_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM browse_dirty WHERE video_id={new}.video_id);' if table in ('media_links','media_assets','file_reviews') else '')
                    rebuild='rebuild=1,' if table in ('library_settings','folders','excluded_directories','collection_rules','review_settings') else ''
                    c.execute('DROP TRIGGER IF EXISTS browse_'+table+'_'+action)
                    c.execute('CREATE TRIGGER IF NOT EXISTS browse_'+table+'_'+action+' AFTER '+action+' ON '+table+' BEGIN'+dirty+' UPDATE browse_state SET '+rebuild+'revision=revision+1 WHERE id=1; END')

    def folder(self,row,rules,short_prefixes):
        path=path_text(canonical(row['relation_path'] or row['path'],short_prefixes));normal=fold(path)
        matches=[rule for rule in rules if normal.startswith(fold(path_text(rule['path']))+'/')]
        if matches:
            rule=max(matches,key=lambda item:len(item['path']))
            return path_text(rule['path']) if rule['mode']=='group' else ''
        segments=path.split('/')[:-1]
        for i in range(len(segments)-1,max(-1,len(segments)-7),-1):
            name=segments[i].strip()
            if re.search(r'合集|全集|collection|box[ _-]?set',name,re.I) and not re.fullmatch(r'合集|全集|collections?|box[ _-]?sets?',name,re.I):return '/'.join(segments[:i+1])
        if row['relation_manual'] or row['code'] or row['source'] in ('nfo','online','amane'):return ''
        return '/'.join(segments)

    def refresh(self):
        self.core.reconcile()
        with self.db() as c:
            state=c.execute('SELECT rebuild,revision,projected_revision FROM browse_state WHERE id=1').fetchone()
            if not state[0] and state[1]==state[2] and not c.execute('SELECT 1 FROM browse_dirty LIMIT 1').fetchone():return
        short_prefixes={}
        policies=self.libraries.policies();excluded=[canonical(root,short_prefixes) for root in self.libraries.exclusions()]
        policies=[dict(p,path=str(canonical(p['path'],short_prefixes)),excluded_directories=excluded) for p in policies]
        rules=[dict(rule,path=str(canonical(rule['path'],short_prefixes))) for rule in self.policy.rules()]
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute('SELECT revision FROM browse_state WHERE id=1').fetchone()[0]!=state[1]:raise LibraryQueryChanged('片库规则或资料正在变化，请重新查询')
            relation_state=c.execute('SELECT generation,reconciled FROM media_relation_revision WHERE id=1').fetchone()
            if relation_state[0]!=relation_state[1]:raise LibraryQueryChanged('作品关系正在变化，请重新查询')
            if c.execute('SELECT rebuild FROM browse_state WHERE id=1').fetchone()[0]:
                c.execute('INSERT OR IGNORE INTO browse_dirty SELECT id FROM videos')
                c.execute('UPDATE browse_state SET rebuild=0 WHERE id=1')
            setting=c.execute("SELECT value FROM review_settings WHERE key='target'").fetchone();target=int(setting[0]) if setting else 1080
            # Batches bound Python memory and avoid holding full metadata graphs.
            while True:
                batch=c.execute("SELECT v.*,COALESCE(a.relation_path,'') AS relation_path,l.manual AS relation_manual FROM browse_dirty q JOIN videos v ON v.id=q.video_id JOIN media_links l ON l.video_id=v.id LEFT JOIN media_assets a ON a.video_id=v.id ORDER BY v.id LIMIT 500").fetchall()
                if not batch:break
                ids=[r['id'] for r in batch];slots=','.join('?' for _ in ids)
                records={r['video_id']:dict(r) for r in c.execute('SELECT * FROM file_reviews WHERE video_id IN ('+slots+')',ids)}
                values=[]
                for raw in batch:
                    row=dict(raw);present=self.core.present(row);path=canonical(row['path'],short_prefixes);owner=self.libraries.owner(path,policies)
                    policy=next((p for p in policies if p['path']==owner),None)
                    visible=not any(path.is_relative_to(canonical(root)) for root in excluded) and (not policy or self.libraries.included(path,policy,row['size'] or 0))
                    review=FileReview.describe(row,records.get(row['id'],{}),target)
                    # Evidence/notes remain on the per-file detail endpoint.
                    summary={key:review[key] for key in ('needs_update','decision','status','result','auto_status')}
                    summary['issues']=[{'code':item['code'],'source':item['source']} for item in review['issues']]
                    summary['pending_automatic']=any(item['review']=='pending' for item in review['automatic_findings'])
                    values.append((row['id'],present['title'],present['vr'],present['censorship'],present['resolution_label'],self.folder(row,rules,short_prefixes),visible,json.dumps(list(dict.fromkeys(tokens(row['tags'])+tokens(row['auto_tags'])))),json.dumps(tokens(row['actors'])),json.dumps(summary)))
                c.executemany('INSERT OR REPLACE INTO browse_projection VALUES(?,?,?,?,?,?,?,?,?,?)',values)
                c.execute('DELETE FROM browse_dirty WHERE video_id IN ('+slots+')',ids)
            c.execute('DELETE FROM browse_dirty WHERE video_id NOT IN (SELECT id FROM videos)')
            # Reusable relational aggregates, indexed independently of any filter.
            # Rebuild only when authoritative rows/preferences/rules changed.
            c.execute('DROP TABLE IF EXISTS browse_version_rows')
            c.execute('CREATE TABLE browse_version_rows AS '+self.CTE+' SELECT * FROM versions')
            c.execute('CREATE INDEX browse_version_movie ON browse_version_rows(movie_key)')
            c.execute('UPDATE browse_state SET projected_revision=revision WHERE id=1')

    def parameters(self,query):
        criteria={key:str(query.get(key,default)).strip() for key,default in [('q',''),('mode','all'),('length','all'),('censorship',''),('vr',''),('height','0'),('tag_logic','all'),('exclude_tag',''),('actor',''),('studio',''),('source',''),('quality',''),('sort','new'),('collections','on'),('collection','')]}
        for key,choices in [('mode',{'all','favorite','unwatched','recent','missing'}),('length',{'all','short','long'}),('censorship',{'','censored','uncensored','unknown','mixed'}),('vr',{'','vr','flat'}),('height',{'0','720','1080','1440','2160','4320'}),('tag_logic',{'all','any'}),('source',{'','matched','pending','error','partial'}),('quality',{'','needs_update','update','watermark','damaged','replace','suspected_watermark','suspected_black','suspected_freeze','decode_error','below_target','higher_local','auto_pending','auto_error','auto_unchecked','unchecked','active','inspection','pending','metadata_error','incomplete','accepted','ignored'}),('sort',{'new','title','duration','rating','size'}),('collections',{'on','off'})]:
            if criteria[key] not in choices:raise ValueError('无效片库筛选：'+key)
        try:tags=json.loads(query.get('tags','[]'))
        except (ValueError,TypeError):raise ValueError('标签筛选应为JSON数组')
        if not isinstance(tags,list) or len(tags)>50 or not all(isinstance(t,str) and 0<len(t)<=200 for t in tags):raise ValueError('标签筛选无效')
        criteria['tags']=sorted(set(tags))
        if any(len(value)>500 for value in criteria.values() if isinstance(value,str)):raise ValueError('筛选内容过长')
        return criteria

    CTE="""WITH files AS (
 SELECT v.id,v.path,v.size,v.mtime,v.duration,v.favorite,v.tags,v.error,v.code,v.actors,v.studio,v.date,v.width,v.height,v.codec,v.missing,v.watched,v.rating,v.source,v.source_tags,v.amane_id,v.auto_tags,v.position,p.title AS display_title,p.vr,p.censorship,p.resolution_label,p.folder,p.tags AS tag_list,p.actors AS actor_list,p.review,l.movie_key,l.version_key,l.part_no,
 ROW_NUMBER() OVER(PARTITION BY l.movie_key,l.version_key ORDER BY v.missing,l.part_no,v.id) AS first_rank,
 ROW_NUMBER() OVER(PARTITION BY l.movie_key,l.version_key ORDER BY v.missing,CASE WHEN v.watched=0 AND v.position>0 THEN 0 WHEN v.watched=0 THEN 1 ELSE 2 END,l.part_no,v.id) AS resume_rank
 FROM videos v JOIN browse_projection p ON p.video_id=v.id AND p.visible=1 JOIN media_links l ON l.video_id=v.id
 ), stats AS (
 SELECT movie_key,version_key,COUNT(*) AS part_count,SUM(size) AS total_size,SUM(duration) AS total_duration,MIN(height) AS min_height,MIN(watched) AS all_watched,MAX(position) AS max_position,MIN(missing) AS all_missing,MAX(missing) AS any_missing,
 MAX(CASE WHEN id IN (SELECT preferred_id FROM duplicate_choices) THEN 1 ELSE 0 END) AS preferred,
 MAX(CASE WHEN json_extract(review,'$.needs_update') THEN 1 ELSE 0 END) AS needs_update,
 MAX(CASE WHEN json_extract(review,'$.decision')='open' THEN 1 ELSE 0 END) AS open_parts,
 MAX(CASE WHEN json_extract(review,'$.pending_automatic') THEN 1 ELSE 0 END) AS auto_pending
 FROM files GROUP BY movie_key,version_key
 ), movie_stats AS (
 SELECT movie_key,COUNT(DISTINCT version_key) AS version_count,MAX(favorite) AS movie_favorite,MAX(mtime) AS movie_mtime,
 CASE WHEN COUNT(DISTINCT folder)=1 AND MIN(length(folder))>0 THEN MIN(folder) ELSE '' END AS collection_root
 FROM files GROUP BY movie_key
 ), available_heights AS (SELECT movie_key,MAX(min_height) AS best_height FROM stats WHERE any_missing=0 GROUP BY movie_key),
 movie_health AS (SELECT s.movie_key,MAX(s.needs_update OR (s.open_parts AND s.min_height<COALESCE(a.best_height,0))) AS needs_update,MAX(s.min_height<COALESCE(a.best_height,0)) AS higher_local,MAX(s.auto_pending) AS auto_pending FROM stats s LEFT JOIN available_heights a ON a.movie_key=s.movie_key GROUP BY s.movie_key),
 collection_stats AS (SELECT collection_root,COUNT(*) AS collection_count FROM movie_stats GROUP BY collection_root),
 versions AS (
 SELECT f.*,s.part_count,s.total_size,s.total_duration,s.min_height,s.all_watched,s.max_position,s.all_missing,s.any_missing,s.preferred,health.needs_update,health.auto_pending,
 m.version_count,m.movie_favorite,m.movie_mtime,m.collection_root,cs.collection_count,r.id AS resume_id,
 CASE WHEN s.all_missing=0 AND (preferred_link.version_key=f.version_key) THEN 0 WHEN s.all_missing=0 AND pref.version_key=f.version_key THEN 1 ELSE 2 END AS preference_rank,
 health.higher_local AS higher_local
 FROM files f JOIN stats s ON s.movie_key=f.movie_key AND s.version_key=f.version_key
 JOIN movie_stats m ON m.movie_key=f.movie_key
 JOIN movie_health health ON health.movie_key=s.movie_key
 JOIN collection_stats cs ON cs.collection_root=m.collection_root
 JOIN files r ON r.movie_key=s.movie_key AND r.version_key=s.version_key AND r.resume_rank=1
 LEFT JOIN movie_preferences pref ON pref.movie_key=f.movie_key
 LEFT JOIN media_links preferred_link ON preferred_link.video_id=pref.preferred_id AND preferred_link.movie_key=f.movie_key
 LEFT JOIN videos preferred_video ON preferred_video.id=pref.preferred_id
 WHERE f.first_rank=1
 )"""

    QUERY_CTE="""WITH files AS (
 SELECT v.id,v.path,v.source,v.error,v.actors,v.studio,v.source_tags,p.review,l.movie_key,l.version_key
 FROM videos v JOIN browse_projection p ON p.video_id=v.id AND p.visible=1 JOIN media_links l ON l.video_id=v.id
 ), versions AS (SELECT * FROM browse_version_rows)"""

    def matching(self,criteria):
        where=['v.all_missing='+('1' if criteria['mode']=='missing' else '0')];args=[]
        for key,column in [('favorite','v.movie_favorite'),('unwatched','NOT v.all_watched'),('recent','(v.max_position>0 AND NOT v.all_watched)')]:
            if criteria['mode']==key:where.append(column)
        if criteria['q']:
            where.append("(instr(fold(v.display_title||' '||v.code||' '||v.actors||' '||v.studio||' '||v.tags||' '||v.auto_tags),?)>0 OR EXISTS(SELECT 1 FROM files p WHERE p.movie_key=v.movie_key AND instr(fold(p.path),?)>0))");args.extend([fold(criteria['q'])]*2)
        if criteria['length']!='all':where.append('v.total_duration>0 AND v.total_duration'+('<1200' if criteria['length']=='short' else '>=1200'))
        if criteria['censorship']:where.append('v.censorship=?');args.append(criteria['censorship'])
        where.append('v.min_height>=?');args.append(int(criteria['height']))
        if criteria['vr']:where.append('v.vr=?');args.append(criteria['vr']=='vr')
        if criteria['tags']:
            tests=[]
            for tag in criteria['tags']:tests.append('EXISTS(SELECT 1 FROM json_each(v.tag_list) WHERE value=?)');args.append(tag)
            where.append('(' + (' AND ' if criteria['tag_logic']=='all' else ' OR ').join(tests)+')')
        if criteria['exclude_tag']:where.append('NOT EXISTS(SELECT 1 FROM json_each(v.tag_list) WHERE value=?)');args.append(criteria['exclude_tag'])
        if criteria['actor']:where.append('EXISTS(SELECT 1 FROM json_each(v.actor_list) WHERE value=?)');args.append(criteria['actor'])
        if criteria['studio']:where.append('v.studio=?');args.append(criteria['studio'])
        source=criteria['source']
        if source=='matched':where.append("v.source IN ('nfo','online','amane')")
        elif source=='pending':where.append("v.source NOT IN ('nfo','online','amane')")
        elif source=='error':where.append("v.error!=''")
        elif source=='partial':where.append("v.amane_id>0 AND (v.actors='' OR v.studio='' OR v.source_tags='')")
        quality=criteria['quality']
        if quality in ('needs_update','update'):where.append('v.needs_update=1')
        elif quality=='higher_local':where.append('v.higher_local=1')
        elif quality=='auto_pending':where.append('v.auto_pending=1')
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
        if criteria['collection']:
            where.append('collection_id(v.collection_root)=?');args.append(criteria['collection'])
        return ' AND '.join(where),args

    def query(self,query):
        criteria=self.parameters(query);offset=max(0,int(query.get('offset',0)));limit=min(96,max(1,int(query.get('limit',48))))
        self.refresh();now=time.time();ident=str(query.get('snapshot',''))
        with self.db() as c:
            c.create_function('fold',1,fold,deterministic=True)
            c.create_function('collection_id',1,lambda root:hashlib.sha256(fold(root).encode()).hexdigest()[:24] if root else '',deterministic=True)
            c.create_function('basename',1,lambda path:path_text(path).split('/')[-1],deterministic=True)
            c.execute('BEGIN' if ident else 'BEGIN IMMEDIATE')
            revision=c.execute('SELECT revision FROM browse_state WHERE id=1').fetchone()[0]
            if ident:
                row=c.execute('SELECT * FROM browse_queries WHERE id=?',(ident,)).fetchone()
                if not row or now-row['created']>1800:raise LibraryQueryChanged('片库查询已过期，请重新查询')
                if row['revision']!=revision or json.loads(row['criteria'])!=criteria:raise LibraryQueryChanged('片库资料或筛选已变化，请重新查询')
                totals=json.loads(row['totals'])
            else:
                ident=uuid.uuid4().hex;where,args=self.matching(criteria)
                selected=self.QUERY_CTE+", ranked AS (SELECT v.*,ROW_NUMBER() OVER(PARTITION BY movie_key ORDER BY all_missing,preference_rank,preferred DESC,min_height DESC,total_size DESC,version_key) AS chosen FROM versions v WHERE "+where+")"
                c.execute('CREATE TEMP TABLE browse_matches AS '+selected+' SELECT * FROM ranked WHERE chosen=1',args)
                selected='WITH matched AS (SELECT * FROM browse_matches)'
                grouped=criteria['collections']=='on' and not criteria['collection']
                key="CASE WHEN collection_root!='' AND collection_count>1 THEN 'collection:'||collection_id(collection_root) ELSE 'movie:'||movie_key END" if grouped else "'movie:'||movie_key"
                cards=selected+", keyed AS (SELECT *,"+key+" AS card_key FROM matched), grouped AS (SELECT card_key,COUNT(*) AS hit_count,SUM(total_size) AS card_size,MAX(movie_mtime) AS card_mtime,MAX(rating) AS card_rating,MAX(total_duration) AS card_duration,MIN(resume_id) AS cover_id,MIN(display_title) AS card_title,MIN(collection_root) AS root,MAX(collection_count) AS collection_total FROM keyed GROUP BY card_key)"
                # SQL JSON contains only card fields; full plot/locks/evidence remain lazy.
                fields="'id',k.resume_id,'title',k.display_title,'movie_key',k.movie_key,'version_key',k.version_key,'code',k.code,'actors',k.actors,'studio',k.studio,'date',k.date,'rating',k.rating,'height',k.min_height,'duration',k.total_duration,'size',k.total_size,'position',k.max_position,'watched',k.all_watched,'missing',k.all_missing,'favorite',k.movie_favorite,'mtime',k.movie_mtime,'vr',k.vr,'censorship',k.censorship,'resolution_label',k.resolution_label,'part_count',k.part_count,'version_count',k.version_count,'needs_update',k.needs_update,'higher_local',k.higher_local"
                payload="CASE WHEN g.card_key LIKE 'collection:%' THEN json_object('kind','collection','key',g.card_key,'id',collection_id(g.root),'title',basename(g.root),'hit_count',g.hit_count,'total_count',g.collection_total,'size',g.card_size,'cover_id',g.cover_id,'previews',json((SELECT json_group_array(resume_id) FROM (SELECT resume_id FROM keyed p WHERE p.card_key=g.card_key ORDER BY p.display_title,p.resume_id LIMIT 4)))) ELSE json_object('kind','movie','key',g.card_key,"+fields+") END"
                order={'new':'g.card_mtime DESC','title':"fold(CASE WHEN g.card_key LIKE 'collection:%' THEN basename(g.root) ELSE g.card_title END)",'duration':'g.card_duration DESC','rating':'g.card_rating DESC','size':'g.card_size DESC'}[criteria['sort']]+',g.card_key'
                totals=dict(c.execute(cards+' SELECT COUNT(*) AS total,COALESCE(SUM(card_size),0) AS bytes FROM grouped').fetchone())
                totals['movies']=c.execute(selected+' SELECT COUNT(*) FROM matched').fetchone()[0]
                c.execute('INSERT INTO browse_queries VALUES(?,?,?,?,?)',(ident,now,revision,json.dumps(criteria),json.dumps(totals)))
                c.execute(cards+' INSERT INTO browse_results SELECT ?,ROW_NUMBER() OVER(ORDER BY '+order+')-1,'+payload+' FROM grouped g LEFT JOIN keyed k ON k.card_key=g.card_key AND k.resume_id=g.cover_id',(ident,))
                stale=[r[0] for r in c.execute('SELECT id FROM browse_queries WHERE created<? OR id NOT IN (SELECT id FROM browse_queries ORDER BY created DESC,id DESC LIMIT 16)',(now-1800,))]
                for key in stale:
                    c.execute('DELETE FROM browse_results WHERE query_id=?',(key,));c.execute('DELETE FROM browse_queries WHERE id=?',(key,))
            items=[json.loads(r[0]) for r in c.execute('SELECT payload FROM browse_results WHERE query_id=? AND ordinal>=? AND ordinal<? ORDER BY ordinal',(ident,offset,offset+limit))]
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
