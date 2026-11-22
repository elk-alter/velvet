/* Physical selections belong to a fixed server query, never a later filter. */
window.DiskFileSelection=(()=>{
 let snapshot='',all=false,rows=new Map(),excluded=new Map(),data=null,returnParams={};
 const planOrigins=new Map();
 let returnPosition=null,pendingOrigin=null;
 function count(){return all?(data?.total||0)-excluded.size:rows.size}
 function bytes(){return all?(data?.bytes||0)-[...excluded.values()].reduce((a,b)=>a+b,0):[...rows.values()].reduce((a,b)=>a+b,0)}
 function selected(ordinal){return all?!excluded.has(ordinal):rows.has(ordinal)}
 function sync(){
  document.querySelectorAll('.diskPhysicalCheckbox').forEach(box=>box.checked=selected(Number(box.dataset.ordinal)));
  const text=document.getElementById('diskPhysicalScope'),button=document.getElementById('diskPhysicalPlan');
  if(text)text.textContent=`${all?'全部匹配'+(excluded.size?'，已排除 '+excluded.size+' 项':''):'跨页已选'} · ${count().toLocaleString()} 个文件 · ${diskBytes(bytes())}`;
  if(button)button.disabled=count()===0;
 }
 function clear(){all=false;rows.clear();excluded.clear();sync()}
 function load(next,params){
  if(pendingOrigin?.params.snapshot===next.snapshot){
   all=pendingOrigin.all;rows=new Map(pendingOrigin.rows);excluded=new Map(pendingOrigin.excluded);
  }else if(snapshot!==next.snapshot){const had=count()>0;all=false;rows.clear();excluded.clear();if(had)toast('查询范围已变化，原文件选择已清空');}
  snapshot=next.snapshot;data=next;returnParams={...params,source:next.criteria.device,snapshot};
  sync();
 }
 function payload(expected=snapshot){
  if(!data||snapshot!==expected||!count())throw Error('文件选择已清空或不属于此查询，请返回重新选择');
  return {snapshot,selected:all?(excluded.size?{exclude:[...excluded.keys()]}:'all'):[...rows.keys()]};
 }
 function toggle(box){const ordinal=Number(box.dataset.ordinal),size=Number(box.dataset.bytes);if(all){if(box.checked)excluded.delete(ordinal);else excluded.set(ordinal,size)}else if(box.checked)rows.set(ordinal,size);else rows.delete(ordinal);sync()}
 function page(){for(const item of data.items){if(all)excluded.delete(item.query_ordinal);else rows.set(item.query_ordinal,item.size)}sync()}
 function everything(){all=true;rows.clear();excluded.clear();sync()}
 function plan(){
  if(!count())return;
  returnPosition={scroll:window.scrollY||0,focus:document.activeElement?.id||''};
  return goManage('organization-plan',{...returnParams,source:'query',selection_device:returnParams.source});
 }
 function back(params={}){
  const restored=params.selection_device?{...params,source:params.selection_device}:{...returnParams};
  delete restored.selection_device;
  return goManage('storage-files',restored,{restorePosition:returnPosition});
 }
 function rememberPlan(id){
  planOrigins.set(id,{params:{...returnParams},position:returnPosition,all,rows:new Map(rows),
   excluded:new Map(excluded),count:count(),bytes:bytes()});
  while(planOrigins.size>8)planOrigins.delete(planOrigins.keys().next().value);
 }
 function linkPlan(id,revised){
  if(planOrigins.has(id))planOrigins.set(revised,planOrigins.get(id));
  while(planOrigins.size>8)planOrigins.delete(planOrigins.keys().next().value);
 }
 function originPanel(id){
  const origin=planOrigins.get(id);if(!origin)return '';
  const {search,directory,status,online,quality,duplicate}=origin.params;
  const labels=[search&&'搜索 '+search,directory&&'目录 '+directory,
   {offline:'离线快照',online:'设备在线'}[online],{unknown:'待识别',identified:'已识别'}[status],
   {issue:'有已记录问题',no_known_issue:'无已记录问题'}[quality],duplicate==='historical'?'历史同哈希候选':''].filter(Boolean);
  return `<section class="diskScope diskOrigin"><strong>来自硬盘筛选 · ${origin.count} 个文件 · ${diskBytes(origin.bytes)}</strong>
   ${labels.length?`<p class="path">${esc(labels.join(' · '))}</p>`:''}
   <button data-id="${esc(id)}" onclick="DiskFileSelection.fromPlan(this.dataset.id)">返回原文件列表与选择</button></section>`;
 }
 async function fromPlan(id){
  const origin=planOrigins.get(id);if(!origin)return;
  pendingOrigin=origin;
  try{return await goManage('storage-files',origin.params,{restorePosition:origin.position})}
  finally{if(pendingOrigin===origin)pendingOrigin=null}
 }
 function checkbox(file){return `<label class="diskCheck"><input type="checkbox" class="diskPhysicalCheckbox" data-ordinal="${file.query_ordinal}" data-bytes="${file.size}" aria-label="选择 ${esc(file.alias+' '+file.display_title)}" ${selected(file.query_ordinal)?'checked':''} onchange="DiskFileSelection.toggle(this)"></label>`}
 function toolbar(){return `<section class="diskScope"><strong id="diskPhysicalScope"></strong><div class="row"><button onclick="DiskFileSelection.page()">选择本页文件</button><button onclick="DiskFileSelection.everything()">选择全部匹配文件</button><button onclick="DiskFileSelection.clear()">清空文件选择</button><button id="diskPhysicalPlan" class="accent" onclick="DiskFileSelection.plan()">为所选文件制定计划</button></div><p class="muted">翻页保留选择；全部匹配后可逐项取消。筛选或查询变化会清空旧选择，离线项不会被悄悄跳过。</p></section>`}
 return {load,payload,toggle,page,everything,clear,sync,plan,back,rememberPlan,linkPlan,originPanel,fromPlan,checkbox,toolbar};
})();
