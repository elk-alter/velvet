"""Read-only physical file details from fixed inventory queries and indexed videos."""
import json
from pathlib import Path
from inventory_query import InventoryQueryChanged
from organization_jobs import no_links
from resource_leases import path_resource
from storage_inventory import volume_identity


class StorageFileDetails:
    def __init__(self,inventory,query,leases):
        self.inventory,self.query,self.db,self.leases=inventory,query,inventory.db,leases

    def binding(self,path,device):
        with self.db() as c:
            row=c.execute("SELECT a.id AS asset_id,a.video_id,v.title,l.movie_key,l.version_key,l.part_no,l.manual FROM media_assets a JOIN media_locations p ON p.asset_id=a.id LEFT JOIN videos v ON v.id=a.video_id LEFT JOIN media_links l ON l.video_id=v.id WHERE p.path=? AND p.device_token IN (?,'legacy-unknown') ORDER BY p.device_token=? DESC LIMIT 1",(str(path),device,device)).fetchone()
            if not row:
                row=c.execute('SELECT NULL AS asset_id,v.id AS video_id,v.title,l.movie_key,l.version_key,l.part_no,l.manual FROM videos v LEFT JOIN media_links l ON l.video_id=v.id WHERE v.path=?',(str(path),)).fetchone()
        return dict(row) if row else None

    def file(self,snapshot,ordinal,resource_offset=0):
        ordinal=int(ordinal);resource_offset=max(0,int(resource_offset))
        member=self.query.members(snapshot,[ordinal])['items'][0]
        device=self.inventory.device(member['device_id']);relative=Path(member['relative_path'])
        if relative.is_absolute() or '..' in relative.parts:raise ValueError('盘点路径无效，请重新盘点')
        path=Path(device['root'])/relative
        binding=self.binding(path,device['identity'])
        scopes=[path_resource(path)]
        if binding and binding['asset_id']:scopes.append('asset:'+binding['asset_id'])
        if binding and binding['video_id'] is not None:scopes.append('video:'+str(binding['video_id']))
        with self.leases.hold('file-details',resources=scopes):
            with self.db() as c:
                row=c.execute('SELECT f.*,s.created AS snapshot_created,s.state AS snapshot_state FROM inventory_files f JOIN inventory_snapshots s ON s.id=f.snapshot WHERE f.device_id=? AND f.snapshot=? AND f.relative_path=?',(member['device_id'],member['file_snapshot'],member['relative_path'])).fetchone()
                annotation=c.execute('SELECT payload FROM inventory_annotations WHERE device_id=? AND relative_path=?',(member['device_id'],member['relative_path'])).fetchone()
            if not row:raise InventoryQueryChanged('盘点条目已失效，请返回文件列表刷新')
            metadata=json.loads(row['metadata']);companions=json.loads(row['companions'])
            status='offline';current=None
            try:
                if Path(device['root']).is_dir():
                    if volume_identity(Path(device['root']))[0]!=device['identity']:status='device_changed'
                    else:
                        no_links(path);stat=path.stat()
                        status='current' if (stat.st_size,stat.st_mtime_ns)==(row['size'],row['mtime_ns']) else 'changed'
                        current={'size':stat.st_size,'mtime_ns':stat.st_mtime_ns}
            except PermissionError:status='permission_denied'
            except FileNotFoundError:status='missing'
            except ValueError:status='linked_or_unsafe'
            except OSError:status='unavailable'
            fields=('title','code','actors','studio','width','height','duration','codec','source','confidence','candidates','locked_fields','relationship','issues','hash_verified_at')
            return {'source_type':'inventory','snapshot':snapshot,'ordinal':ordinal,'device':{'id':device['id'],'alias':device['alias'],'root':device['root'],'stable':bool(device['stable'])},
                    'relative_path':member['relative_path'],'path':str(path),'size':row['size'],'mtime_ns':row['mtime_ns'],
                    'file_snapshot':member['file_snapshot'],'snapshot_created':row['snapshot_created'],'snapshot_state':row['snapshot_state'],
                    'historical_sha256':row['digest'],'current_content_verified':False,'status':status,'current':current,
                    'metadata':{key:metadata[key] for key in fields if key in metadata},'annotation':json.loads(annotation[0]) if annotation else {},
                    'binding':binding,'resources':{'items':companions[resource_offset:resource_offset+50],'total':len(companions),'offset':resource_offset,'limit':50}}

    def locations(self,video_id,offset=0,limit=20):
        video_id=int(video_id);offset=max(0,int(offset));limit=min(50,max(1,int(limit)))
        with self.db() as c:
            video=c.execute('SELECT id,path,title FROM videos WHERE id=?',(video_id,)).fetchone()
            if not video:raise ValueError('作品文件不存在，请返回片库刷新')
            asset=c.execute('SELECT id FROM media_assets WHERE video_id=?',(video_id,)).fetchone()
            total=c.execute('SELECT COUNT(*) FROM media_locations WHERE asset_id=?',(asset[0],)).fetchone()[0] if asset else 0
            rows=[dict(r) for r in c.execute('SELECT id,path,device_token,preferred,verified FROM media_locations WHERE asset_id=? ORDER BY preferred DESC,id LIMIT ? OFFSET ?',(asset[0],limit,offset))] if asset else []
            if not total:
                total=1;rows=[{'id':'indexed','path':video['path'],'device_token':None,'preferred':1,'verified':0}] if not offset else []
        devices=self.inventory.devices()
        for row in rows:
            row['inventory']=self.inventory_location(row['path'],row['device_token'],devices)
        return {'video':dict(video),'asset_id':asset[0] if asset else None,'items':rows,'total':total,'offset':offset,'limit':limit}

    def inventory_location(self,path,identity,devices):
        matches=[]
        for device in devices:
            if identity not in (None,'legacy-unknown',device['identity']):continue
            try:relative=Path(path).relative_to(Path(device['root'])).as_posix()
            except ValueError:continue
            with self.db() as c:
                row=c.execute("SELECT 1 FROM inventory_files WHERE device_id=? AND relative_path=? AND snapshot=(SELECT id FROM inventory_snapshots WHERE device_id=? AND state IN ('done','partial') ORDER BY created DESC,id DESC LIMIT 1)",(device['id'],relative,device['id'])).fetchone()
            if row:matches.append({'device_id':device['id'],'alias':device['alias'],'relative_path':relative,'online':device['online'],'root_length':len(device['root'])})
        if not matches:return None
        result=max(matches,key=lambda item:item['root_length']);result.pop('root_length');return result

    def indexed_record(self,video_id,location_id):
        video_id=int(video_id)
        with self.db() as c:
            video=c.execute('SELECT * FROM videos WHERE id=?',(video_id,)).fetchone()
            if not video:raise ValueError('作品文件不存在，请返回片库刷新')
            if location_id=='indexed':
                if c.execute('SELECT 1 FROM media_locations p JOIN media_assets a ON a.id=p.asset_id WHERE a.video_id=?',(video_id,)).fetchone():raise InventoryQueryChanged('文件位置已变化，请刷新物理位置列表')
                return dict(video),{'id':'indexed','path':video['path'],'device_token':None,'asset_id':None,'size':video['size'],'mtime_ns':None,'verified':0,'sha256':''}
            row=c.execute('SELECT p.*,a.sha256 FROM media_locations p JOIN media_assets a ON a.id=p.asset_id WHERE a.video_id=? AND p.id=?',(video_id,location_id)).fetchone()
            if not row:raise ValueError('位置不属于此作品文件')
            return dict(video),dict(row)

    def indexed_file(self,video_id,location_id,resource_offset=0):
        video,location=self.indexed_record(video_id,location_id)
        path=Path(location['path']);identity=location['device_token'];resource_offset=max(0,int(resource_offset))
        scopes=['video:'+str(video['id']),path_resource(path)]
        if location['asset_id']:scopes.append('asset:'+location['asset_id'])
        with self.leases.hold('file-details',resources=scopes):
            binding=self.binding(path,identity)
            if not binding:
                with self.db() as c:link=c.execute('SELECT * FROM media_links WHERE video_id=?',(video['id'],)).fetchone()
                binding=dict(link or {},video_id=video['id'],title=video['title'],asset_id=location['asset_id'])
            status='offline';current=None
            known_identity=bool(identity and identity!='legacy-unknown')
            try:
                volume=Path(path.anchor) if path.anchor else path.parent
                if volume.is_dir():
                    if known_identity and volume_identity(volume)[0]!=identity:status='device_changed'
                    else:
                        no_links(path);stat=path.stat();current={'size':stat.st_size,'mtime_ns':stat.st_mtime_ns}
                        mtime=location['mtime_ns']/1e9 if location['mtime_ns'] is not None else video['mtime']
                        unchanged=stat.st_size==location['size'] and abs(stat.st_mtime-mtime)<.01
                        status=('current' if known_identity else 'identity_unverified') if unchanged else 'changed'
            except PermissionError:status='permission_denied'
            except FileNotFoundError:status='missing'
            except ValueError:status='linked_or_unsafe'
            except OSError:status='unavailable'
            with self.db() as c:
                count=c.execute('SELECT COUNT(*) FROM media_resources WHERE asset_id=? AND device_token=?',(location['asset_id'],identity)).fetchone()[0]
                resources=[dict(row) for row in c.execute('SELECT path,size FROM media_resources WHERE asset_id=? AND device_token=? ORDER BY path,id LIMIT 50 OFFSET ?',(location['asset_id'],identity,resource_offset))]
            for resource in resources:resource.update(name=Path(resource['path']).name,shared=False,role='已绑定资源')
            metadata={key:video[key] for key in ('title','code','actors','studio','width','height','duration','codec','source','locked_fields')}
            metadata.update(candidates={'filename':path.stem,'directory':path.parent.name,'existing_metadata':video['title']},hash_verified_at=location['verified'])
            return {'source_type':'index','snapshot':None,'ordinal':None,'device':{'id':None,'alias':'片库已有位置','root':str(path.parent),'stable':known_identity},
                    'relative_path':path.name,'path':str(path),'size':location['size'],'mtime_ns':location['mtime_ns'] if location['mtime_ns'] is not None else int(video['mtime']*1e9),
                    'file_snapshot':None,'snapshot_created':None,'snapshot_state':'indexed','historical_sha256':location['sha256'],
                    'current_content_verified':False,'status':status,'current':current,'metadata':metadata,'annotation':{},'binding':binding,
                    'resources':{'items':resources,'total':count,'offset':resource_offset,'limit':50}}

    def locate(self,video_id,location_id):
        video,location=self.indexed_record(video_id,location_id)
        path=location['path'];identity=location['device_token']
        mapping=self.inventory_location(path,identity,self.inventory.devices())
        if not mapping:return {'indexed':True,'video_id':video['id'],'location':location_id}
        criteria={'device':mapping['device_id'],'search':mapping['relative_path']}
        page=self.query.query(criteria)
        with self.db() as c:row=c.execute('SELECT ordinal FROM inventory_query_items WHERE query_id=? AND device_id=? AND relative_path=?',(page['snapshot'],mapping['device_id'],mapping['relative_path'])).fetchone()
        if not row:raise ValueError('文件已排除或快照已变化，请检查排除规则并重新盘点')
        return {'snapshot':page['snapshot'],'ordinal':row[0],'criteria':page['criteria']}
