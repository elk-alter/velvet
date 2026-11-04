/* Physical selections belong to a fixed server query, never a later filter. */
window.DiskFileSelection=(()=>{
 let snapshot='',all=false,rows=new Map(),excluded=new Map(),data=null,returnParams={};
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
  if(snapshot!==next.snapshot){const had=count()>0;all=false;rows.clear();excluded.clear();if(had)toast('查询范围已变化，原文件选择已清空');}
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
 function plan(){if(!count())return;goManage('organization-plan',{...returnParams,source:'query',selection_device:returnParams.source})}
 function back(params={}){const restored=params.selection_device?{...params,source:params.selection_device}:{...returnParams};delete restored.selection_device;goManage('storage-files',restored)}
 function checkbox(file){return `<label class="diskCheck"><input type="checkbox" class="diskPhysicalCheckbox" data-ordinal="${file.query_ordinal}" data-bytes="${file.size}" aria-label="选择 ${esc(file.alias+' '+file.display_title)}" ${selected(file.query_ordinal)?'checked':''} onchange="DiskFileSelection.toggle(this)"></label>`}
 function toolbar(){return `<section class="diskScope"><strong id="diskPhysicalScope"></strong><div class="row"><button onclick="DiskFileSelection.page()">选择本页文件</button><button onclick="DiskFileSelection.everything()">选择全部匹配文件</button><button onclick="DiskFileSelection.clear()">清空文件选择</button><button id="diskPhysicalPlan" class="accent" onclick="DiskFileSelection.plan()">为所选文件制定计划</button></div><p class="muted">翻页保留选择；全部匹配后可逐项取消。筛选或查询变化会清空旧选择，离线项不会被悄悄跳过。</p></section>`}
 return {load,payload,toggle,page,everything,clear,sync,plan,back,checkbox,toolbar};
})();
