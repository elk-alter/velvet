"""Snapshot-bound SQL queries over the latest completed inventory of every device."""
import hashlib
import json
import time
import uuid
from archive_policy import archive_roots


class InventoryQueryChanged(ValueError):pass


def normalized(value):return str(value).replace('\\','/').rstrip('/').casefold()


class InventoryQuery:
    def __init__(self,inventory):
        self.inventory,self.db=inventory,inventory.db
        with self.db() as c:
            c.execute('CREATE INDEX IF NOT EXISTS inventory_snapshot_lookup ON inventory_files(device_id,snapshot,relative_path)')
            c.execute('CREATE INDEX IF NOT EXISTS inventory_finished_lookup ON inventory_snapshots(device_id,state,created DESC,id DESC)')
            c.execute('CREATE TABLE IF NOT EXISTS inventory_query_snapshots(id TEXT PRIMARY KEY,created REAL,payload TEXT)')
            c.execute('CREATE TABLE IF NOT EXISTS inventory_query_items(query_id TEXT,ordinal INTEGER,device_id TEXT,file_snapshot TEXT,relative_path TEXT,historical_copies INTEGER,PRIMARY KEY(query_id,ordinal))')
            c.execute('CREATE TABLE IF NOT EXISTS inventory_query_revision(id INTEGER PRIMARY KEY,revision INTEGER)')
            c.execute('INSERT OR IGNORE INTO inventory_query_revision VALUES(1,0)')
            tables={row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ('inventory_annotations','inventory_preferences','storage_devices','excluded_directories'):
                if table not in tables:continue
                for action in ('INSERT','UPDATE','DELETE'):
                    c.execute('CREATE TRIGGER IF NOT EXISTS query_changed_'+table+'_'+action+' AFTER '+action+' ON '+table+' BEGIN UPDATE inventory_query_revision SET revision=revision+1 WHERE id=1; END')

    def parameters(self,query):
        values={key:str(query.get(key,default)).strip() for key,default in [('device','all'),('online','all'),('status','all'),('quality','all'),('duplicate','all'),('search',''),('directory',''),('sort','path')]}
        for key,choices in [('online',{'all','online','offline'}),('status',{'all','identified','unknown'}),('quality',{'all','issue','no_known_issue'}),('duplicate',{'all','historical'}),('sort',{'path','size','mtime','title'})]:
            if values[key] not in choices:raise ValueError('无效筛选条件：'+key)
        if len(values['search'])>500 or len(values['directory'])>1000:raise ValueError('查询条件过长')
        directory=values['directory'].replace('\\','/').strip('/')
        if '..' in directory.split('/') or ':' in directory:raise ValueError('请输入相对于盘点位置的目录')
        values['directory']=directory
        return values

    def query(self,query):
        criteria=self.parameters(query);offset=max(0,int(query.get('offset',0)));limit=min(100,max(1,int(query.get('limit',50))))
        # Probe each device once, never each file; actual execution always rechecks identity.
        devices=self.inventory.devices()
        if criteria['device']!='all' and not any(device['id']==criteria['device'] for device in devices):raise ValueError('未知盘点位置')
        device_state=[(d['id'],d['identity'],d['root'],d['alias'],d['online']) for d in devices]
        excluded=sorted(set(normalized(path) for path in self.inventory.excluded()+archive_roots(self.db)))
        now=time.time();snapshot_id=str(query.get('snapshot',''));created=now
        with self.db() as c:
            c.create_function('casefold',1,lambda value:str(value or '').casefold(),deterministic=True)
            c.execute('BEGIN' if snapshot_id else 'BEGIN IMMEDIATE')
            current_devices=[tuple(row) for row in c.execute('SELECT id,identity,root,alias FROM storage_devices ORDER BY id')]
            if sorted(current_devices)!=sorted(tuple(state[:4]) for state in device_state):raise InventoryQueryChanged('盘点位置已变化，请刷新查询')
            revision=c.execute('SELECT revision FROM inventory_query_revision WHERE id=1').fetchone()[0]
            signature=hashlib.sha256(json.dumps([device_state,excluded,revision],sort_keys=True).encode()).hexdigest()
            if snapshot_id:
                row=c.execute('SELECT * FROM inventory_query_snapshots WHERE id=?',(snapshot_id,)).fetchone()
                if not row or now-row['created']>1800:raise InventoryQueryChanged('查询快照已过期，请刷新后重新选择')
                payload=json.loads(row['payload']);created=row['created']
                if payload['criteria']!=criteria or payload['signature']!=signature:raise InventoryQueryChanged('筛选、设备状态或整理建议已变化，请刷新查询')
                pinned=payload['pinned']
                if 'totals' in payload:
                    rows=self.page(c,snapshot_id,offset,limit)
                    return self.response(rows,devices,payload['totals'],offset,limit,snapshot_id,created,criteria)
            else:
                pinned=[dict(row) for row in c.execute("SELECT device_id,id FROM (SELECT device_id,id,ROW_NUMBER() OVER(PARTITION BY device_id ORDER BY created DESC,id DESC) AS rank FROM inventory_snapshots WHERE state IN ('done','partial')) WHERE rank=1")]
                snapshot_id=uuid.uuid4().hex
                c.execute('INSERT INTO inventory_query_snapshots VALUES(?,?,?)',(snapshot_id,now,json.dumps({'criteria':criteria,'signature':signature,'pinned':pinned})))
                stale=[r[0] for r in c.execute('SELECT id FROM inventory_query_snapshots WHERE created<? OR id NOT IN (SELECT id FROM inventory_query_snapshots ORDER BY created DESC,id DESC LIMIT 16)',(now-1800,))]
                for ident in stale:
                    c.execute('DELETE FROM inventory_query_items WHERE query_id=?',(ident,))
                    c.execute('DELETE FROM inventory_query_snapshots WHERE id=?',(ident,))
            visibility=[];visibility_args=[]
            for device in devices:
                root=normalized(device['root'])
                for path in excluded:
                    if root==path or root.startswith(path+'/'):visibility.append('f.device_id!=?');visibility_args.append(device['id'])
                    elif path.startswith(root+'/'):
                        relative=path[len(root)+1:]
                        visibility.append("NOT (f.device_id=? AND (casefold(f.relative_path)=? OR substr(casefold(f.relative_path),1,?)=?))")
                        visibility_args.extend([device['id'],relative,len(relative)+1,relative+'/'])
            source=' OR '.join('(f.device_id=? AND f.snapshot=?)' for _ in pinned) or '0'
            source_args=[value for item in pinned for value in (item['device_id'],item['id'])]
            # Avoid a full-file window/materialization when roots cannot overlap.
            # Nested roots still need physical-path deduplication before hash counts.
            roots=[(d['identity'],normalized(d['root'])) for d in devices]
            overlap=any(identity==other_identity and (root==other or root.startswith(other+'/') or other.startswith(root+'/'))
                        for i,(identity,root) in enumerate(roots) for other_identity,other in roots[i+1:])
            rank=("ROW_NUMBER() OVER(PARTITION BY d.identity,casefold(replace(d.root,'\\','/')||'/'||f.relative_path) ORDER BY length(d.root) DESC,d.id)"
                  if overlap else '1')
            if visibility:
                source='('+source+') AND '+' AND '.join(visibility);source_args.extend(visibility_args)
            cte="""WITH raw AS (
 SELECT f.*,d.alias,d.root,d.identity,a.payload AS annotation,
 COALESCE(NULLIF(json_extract(a.payload,'$.title'),''),json_extract(f.metadata,'$.title'),f.relative_path) AS display_title,
 casefold(replace(d.root,'\\','/')||'/'||f.relative_path) AS physical_path,
 """+rank+""" AS location_rank
 FROM inventory_files f JOIN storage_devices d ON d.id=f.device_id
 LEFT JOIN inventory_annotations a ON a.device_id=f.device_id AND a.relative_path=f.relative_path WHERE """+source+"""
 ), unique_files AS (SELECT * FROM raw WHERE location_rank=1),
 hashes AS (SELECT digest,COUNT(*) AS copies FROM unique_files WHERE digest!='' GROUP BY digest)
 """
            table='unique_files' if criteria['device']=='all' else 'raw'
            where=[];args=list(source_args)
            if criteria['device']!='all':where.append('f.device_id=?');args.append(criteria['device'])
            if criteria['online']!='all':
                ids=[device['id'] for device in devices if device['online']==(criteria['online']=='online')]
                where.append('f.device_id IN ('+','.join('?' for _ in ids)+')' if ids else '0');args.extend(ids)
            identified="(COALESCE(json_extract(f.annotation,'$.title'),'')!='' OR COALESCE(json_extract(f.metadata,'$.code'),'')!='' OR COALESCE(json_extract(f.metadata,'$.source'),'filename') IN ('nfo','online','amane'))"
            issues="COALESCE(json_array_length(json_extract(f.metadata,'$.issues')),0)>0"
            if criteria['status']!='all':where.append(identified if criteria['status']=='identified' else 'NOT '+identified)
            if criteria['quality']!='all':where.append(issues if criteria['quality']=='issue' else 'NOT ('+issues+')')
            if criteria['duplicate']=='historical':where.append('COALESCE(h.copies,0)>1')
            if criteria['search']:
                where.append("instr(casefold(f.display_title||' '||f.relative_path||' '||COALESCE(json_extract(f.metadata,'$.code'),'')||' '||COALESCE(json_extract(f.metadata,'$.actors'),'')),?)>0")
                args.append(criteria['search'].casefold())
            if criteria['directory']:
                directory=criteria['directory'].casefold()+'/'
                where.append('substr(casefold(f.relative_path),1,?)=?');args.extend([len(directory),directory])
            clause=' WHERE '+' AND '.join(where) if where else ''
            historical=criteria['duplicate']=='historical'
            base=' FROM '+table+' f'+(' LEFT JOIN hashes h ON h.digest=f.digest' if historical else '')+clause
            totals=dict(c.execute(cte+'SELECT COUNT(*) AS total,COALESCE(SUM(f.size),0) AS bytes'+base,args).fetchone())
            order={'path':'f.device_id,f.relative_path COLLATE NOCASE,f.relative_path','title':'f.display_title COLLATE NOCASE,f.device_id,f.relative_path','size':'f.size DESC,f.device_id,f.relative_path','mtime':'f.mtime_ns DESC,f.device_id,f.relative_path'}[criteria['sort']]
            # Persist only the fixed query's ordered identifiers, never a browser-sized
            # metadata graph. Warm page requests join at most `limit` physical rows.
            copies='COALESCE(h.copies,0)' if historical else '0'
            c.execute(cte+'INSERT INTO inventory_query_items SELECT ?,ROW_NUMBER() OVER(ORDER BY '+order+')-1,f.device_id,f.snapshot,f.relative_path,'+copies+base,(*source_args,snapshot_id,*args[len(source_args):]))
            c.execute('UPDATE inventory_query_snapshots SET payload=? WHERE id=?',(json.dumps({'criteria':criteria,'signature':signature,'pinned':pinned,'totals':totals}),snapshot_id))
            rows=self.page(c,snapshot_id,offset,limit)
        return self.response(rows,devices,totals,offset,limit,snapshot_id,created,criteria)

    def page(self,c,snapshot,offset,limit):
        return [dict(row) for row in c.execute("""SELECT f.*,d.alias,d.root,d.identity,a.payload AS annotation,
 COALESCE(NULLIF(json_extract(a.payload,'$.title'),''),json_extract(f.metadata,'$.title'),f.relative_path) AS display_title,
 casefold(replace(d.root,'\\','/')||'/'||f.relative_path) AS physical_path,q.historical_copies
 FROM inventory_query_items q JOIN inventory_files f ON f.device_id=q.device_id AND f.snapshot=q.file_snapshot AND f.relative_path=q.relative_path
 JOIN storage_devices d ON d.id=f.device_id LEFT JOIN inventory_annotations a ON a.device_id=f.device_id AND a.relative_path=f.relative_path
 WHERE q.query_id=? AND q.ordinal>=? AND q.ordinal<? ORDER BY q.ordinal""",(snapshot,offset,offset+limit))]

    def response(self,rows,devices,totals,offset,limit,snapshot_id,created,criteria):
        online={device['id']:device['online'] for device in devices}
        for row in rows:
            metadata=json.loads(row['metadata']);annotation=json.loads(row.pop('annotation') or '{}')
            if annotation:
                metadata['organization_annotation']=annotation
                metadata['relationship']=dict(metadata.get('relationship',{}),**{key:value for key,value in annotation.items() if key!='title'},manual_required=False)
            fields={'title','code','actors','studio','width','height','duration','codec','source','confidence','issues','relationship','organization_annotation'}
            row['metadata']={key:value for key,value in metadata.items() if key in fields};resources=json.loads(row.pop('companions'));row['companion_count']=len(resources);row['resource_preview']=resources[:3];row['online']=online[row['device_id']]
            row['current_content_verified']=False;row.pop('location_rank',None)
        return dict(totals,items=rows,offset=offset,limit=limit,snapshot=snapshot_id,snapshot_created=created,criteria=criteria,
                    devices=[{'id':device['id'],'alias':device['alias'],'online':device['online']} for device in devices])
