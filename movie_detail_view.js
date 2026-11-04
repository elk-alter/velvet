/* Bounded work navigation; paging never discards the editor or expands edit scope. */
window.MovieDetailView=(()=>{
 let current=null,epoch=0,controller=null;
 function movie(data){
  const headers=[...data.versions.items];for(const pinned of [data.active_version,data.default_version])if(pinned&&!headers.some(v=>v.version_key===pinned.version_key))headers.push(pinned);
  const files=[...data.parts.items];if(!files.some(p=>p.id===data.selected.id))files.push(data.selected);
  return {...data.selected,movie_key:data.movie_key,id:data.selected.id,version_key:data.default_version_key,favorite:data.favorite,mtime:data.mtime,_total_files:data.total_files,_edit_file_count:data.edit_file_count,_version_count:data.versions.total,_detail:data,
   _versions:headers.map(v=>({...v,_parts:v.version_key===data.active_version.version_key?files:[]}))};
 }
 function busy(value){for(const id of ['versionPicker','partPicker']){const node=$(id);node.setAttribute('aria-busy',String(value));node.querySelectorAll('button,select').forEach(input=>input.disabled=value||input.dataset.boundary==='true')}}
 function cancelPending(){++epoch;controller?.abort();controller=null;busy(false)}
 function count(page,label){return `${page.offset+1}–${Math.min(page.offset+page.limit,page.total)} / ${page.total} ${label}`}
 function pager(kind,page,label){return `<div class="movieDetailPager"><span tabindex="-1">${count(page,label)}</span><button data-boundary="${page.offset===0}" ${page.offset===0?'disabled':''} onclick="MovieDetailView.page('${kind}',${Math.max(0,page.offset-page.limit)})" aria-label="上一页${label}">上一页</button><button data-boundary="${page.offset+page.limit>=page.total}" ${page.offset+page.limit>=page.total?'disabled':''} onclick="MovieDetailView.page('${kind}',${page.offset+page.limit})" aria-label="下一页${label}">下一页</button></div>`}
 function render(){
  if(!current||!detail.open)return;
  const d=current,versions=[...d.versions.items];for(const pinned of [d.default_version,d.active_version])if(pinned&&!versions.some(v=>v.version_key===pinned.version_key))versions.unshift(pinned);
  $('versionPicker').innerHTML=d.versions.total>1?`<label>播放版本 <select id="versionSelect" onchange="MovieDetailView.chooseVersion(this)">${versions.map(v=>`<option value="${v.resume_id}" ${v.version_key===d.active_version.version_key?'selected':''}>${esc(v.version_label)} · ${v.part_count} 段${v.version_key===d.default_version_key?' · 默认':''}</option>`).join('')}</select></label><button onclick="MovieDetailView.prefer()">设为默认版本</button>${d.versions.total>d.versions.limit?pager('versions',d.versions,'版本'):''}${versions.length>d.versions.items.length?'<small class="moviePinnedHint">当前 / 默认版本固定列出</small>':''}`:'';
  $('partPicker').classList.add('pagedParts');
  $('partPicker').innerHTML=d.parts.total>1?`${pager('parts',d.parts,'分段')}<div class="moviePartButtons">${d.parts.items.map(part=>`<button class="${part.id===d.selected.id?'accent':''}" ${part.id===d.selected.id?'aria-current="true"':''} onclick="openVideo(${part.id},true)">Part ${part.part_index} · ${esc(part.resolution_label)}${part.vr?' · VR':''}${part.missing?' · 离线':''}</button>`).join('')}</div>`:'';
  detail.querySelector('.dialogTools').hidden=d.versions.total<2&&d.parts.total<2;
 }
 function show(data){cancelPending();current=data;render()}
 function parameters(id){
  if(!current)return {};const d=current;id=Number(id);
  const known=d.selected.id===id||d.next?.id===id||d.default_version?.resume_id===id||d.active_version.resume_id===id||d.parts.items.some(p=>p.id===id)||d.versions.items.some(v=>v.resume_id===id);
  return known?{version_offset:d.versions.offset,version_limit:d.versions.limit,part_limit:d.parts.limit}:{};
 }
 async function chooseVersion(select){if(!select.value)return;const success=await openVideo(Number(select.value),true);if(success===false)render()}
 async function prefer(){
  if(!current)return;const d=current;cancelPending();const ticket=epoch;busy(true);
  try{await api('/api/preferred-version',{id:d.selected.id});await refresh();const work=await serverBrowse.loadMovie(d.selected.id,{exact:true,...parameters(d.selected.id),part_offset:d.parts.offset,accept:()=>ticket===epoch&&detail.open&&current===d});if(work&&ticket===epoch){current=work._detail;render();toast('默认展示与播放已切换至该版本')}}catch(error){toast(error.message)}finally{if(ticket===epoch)busy(false)}
 }
 async function page(kind,offset){
  if(!current)return;const d=current;cancelPending();const ticket=epoch;controller=new AbortController();busy(true);
  const options={exact:true,version_offset:d.versions.offset,version_limit:d.versions.limit,part_offset:d.parts.offset,part_limit:d.parts.limit,revision:d.revision,signal:controller.signal,accept:()=>ticket===epoch&&detail.open&&current===d};
  if(kind==='versions')options.version_offset=offset;else options.part_offset=offset;
  try{
   const work=await serverBrowse.loadMovie(d.selected.id,options);if(!work||ticket!==epoch||!detail.open||current!==d)return;current=work._detail;render();
   const controls=$(kind==='versions'?'versionPicker':'partPicker');controls.querySelector('.movieDetailPager')?.setAttribute('role','status');controls.querySelector('.movieDetailPager span')?.focus({preventScroll:true});
  }catch(error){
   if(error.name==='AbortError'||ticket!==epoch)return;
   let notice=$('movieDetailPageError');if(!notice){notice=document.createElement('div');notice.id='movieDetailPageError';notice.className='notice';$('partPicker').append(notice)}
   notice.innerHTML=`${esc(error.message)} <button onclick="openVideo(${d.selected.id},true)">重新打开作品</button>`;
  }finally{if(ticket===epoch)busy(false)}
 }
 detail.addEventListener('close',()=>{cancelPending();current=null});
 return {movie,show,page,prefer,chooseVersion,cancelPending,parameters,restore:render,get generation(){return epoch}};
})();
