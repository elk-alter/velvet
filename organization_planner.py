"""Turn an immutable physical-query selection into a multi-root preview."""
import hashlib
import json
import time
import uuid
from pathlib import Path
from storage_inventory import destination_conflicts,target_space


class QueryPlanner:
    def __init__(self,inventory,query,leases):self.inventory,self.query,self.leases=inventory,query,leases

    def selection(self,snapshot,selected):
        members=self.query.members(snapshot,selected);groups={}
        online={d['id']:d['online'] for d in self.inventory.devices()}
        for item in members['items']:
            group=groups.setdefault(item['device_id'],{'id':item['device_id'],'alias':item['alias'],'files':0,'bytes':0,'online':online[item['device_id']]})
            group['files']+=1;group['bytes']+=item['size']
        return {key:members[key] for key in ('total','bytes','snapshot','criteria','query_total')}|{'devices':list(groups.values()),'scope':'all' if selected=='all' else 'all_except' if isinstance(selected,dict) else 'selected'}

    def plan(self,snapshot,selected,target='',layout='title',operation='copy'):
        with self.leases.hold('organization-query-preview'):
            members=self.query.members(snapshot,selected)
            if not members['items']:raise ValueError('当前固定范围为空，请重新选择')
            grouped={}
            for item in members['items']:grouped.setdefault((item['device_id'],item['file_snapshot']),[]).append(item['relative_path'])
            entries=[];devices={};roots=[];destinations=set();created=[]
            for (device_id,file_snapshot),relatives in grouped.items():
                child=self.inventory.plan(device_id,target,layout,operation,snapshot_id=file_snapshot,relatives=relatives,persist=False)
                device=self.inventory.device(device_id);devices[device_id]={'root':device['root'],'identity':device['identity'],'alias':device['alias'],'file_snapshot':file_snapshot}
                roots.append(child['source_root']);created.append(child['snapshot_created'])
                for entry in child['entries']:
                    entry.update(device_id=device_id,device_alias=device['alias'],source_root=child['source_root'],source_device_identity=device['identity'],file_snapshot=file_snapshot)
                    if operation!='keep':
                        entry['conflicts']=list(dict.fromkeys(entry['conflicts']+destination_conflicts(Path(entry['destination']),destinations)))
                    entries.append(entry)
            # No expansion to a later inventory or changed annotation/device context.
            self.query.members(snapshot,selected)
            roots=list(dict.fromkeys(roots));target_path=roots[0] if operation=='keep' else str(Path(target).expanduser().resolve())
            space={'required':0,'reserve':0,'sufficient':True,'files_unchanged':True} if operation=='keep' else target_space(entries,Path(target_path))
            payload={'entries':entries,'bytes':members['bytes'],'source_roots':roots,'source_root':roots[0],'source_devices':devices,
                     'source_unchanged':not any(e['conflicts'] for e in entries),'device_id':'multi','device_alias':'跨位置固定选择',
                     'snapshot_created':min(created),'executable':False,'layout':layout,'space':space,
                     'operation':'keep' if operation=='keep' else 'relocate','requested_operation':operation,
                     'selection':{key:members[key] for key in ('snapshot','criteria','total','bytes','query_total')}|{'scope':'all' if selected=='all' else 'all_except' if isinstance(selected,dict) else 'selected'}}
            payload['digest']=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest();ident=uuid.uuid4().hex
            with self.inventory.db() as c:c.execute('INSERT INTO organization_plans VALUES(?,?,?,?,?,?,?)',(ident,'multi',target_path,snapshot,'preview_only',json.dumps(payload),time.time()))
            return dict(payload,id=ident,target=target_path)
