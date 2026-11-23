/* Shared dialog actions and the directory chooser. */
function workflowHeader(id,title,close='this.closest("dialog").close()'){
 return `<header class="dialogChrome"><h2 id="${id}">${esc(title)}</h2><button class="close" type="button" onclick='${close}' aria-label="关闭" title="关闭 · Esc">×</button></header>`;
}

const DirectoryPicker=(()=>{
 const dialog=document.createElement('dialog');
 dialog.id='directoryPicker';
 dialog.setAttribute('aria-labelledby','directoryPickerTitle');
 dialog.innerHTML=workflowHeader('directoryPickerTitle','选择目录')+`
  <div class="dialogBody">
   <div class="row directoryTools"><button type="button" onclick="DirectoryPicker.browse('')">所有位置</button><button type="button" id="directoryUp" onclick="DirectoryPicker.up()">上一级</button>
    <button id="directoryNative" type="button" onclick="DirectoryPicker.native()">用 Windows 选择器</button></div>
   <div id="directoryCurrent" class="path"></div>
   <div id="directoryEntries" class="directoryEntries"></div>
   <form class="row" onsubmit="event.preventDefault();DirectoryPicker.browse(this.elements.directoryPath.value.trim())">
    <label class="directoryPathLabel">粘贴目录路径<input type="text" name="directoryPath" id="directoryPath" spellcheck="false"></label>
    <button type="submit">前往</button></form>
   <div id="directoryConfirm"></div><p id="directoryStatus" role="status"></p>
  </div>
  <div class="dialogFooter"><button type="button" onclick="DirectoryPicker.back()" id="directoryBack" hidden>重新选择</button>
   <button type="button" onclick="DirectoryPicker.commit(false)" id="directoryOnly" hidden>仅添加</button>
   <button type="button" class="accent" onclick="DirectoryPicker.commit(true)" id="directoryUse" disabled>选择此目录</button></div>`;
 document.body.append(dialog);
 let session=null,request=null,page=null,preview=null,pending=false,serial=0;
 const status=value=>$('directoryStatus').textContent=value;
 function resetConfirm(){
  preview=null;$('directoryConfirm').replaceChildren();$('directoryBack').hidden=true;$('directoryOnly').hidden=true;
  $('directoryUse').textContent='选择此目录';$('directoryUse').disabled=!page?.path;
 }
 function open(options={}){
  if(dialog.open)return;
  session={...options,opener:document.activeElement};page=null;pending=false;
  $('directoryPickerTitle').textContent=options.register?'添加盘点目录':options.title||'选择目录';
  $('directoryNative').hidden=Boolean(library.server_mode);$('directoryPath').value='';
  $('directoryCurrent').textContent='';$('directoryEntries').replaceChildren();resetConfirm();
  if(!dialog.querySelector('.privacyToggle')&&typeof privacyButton==='function')dialog.querySelector('.dialogChrome').append(privacyButton());
  dialog.showModal();browse(options.start||'');
 }
 dialog.addEventListener('close',()=>{
  ++serial;request?.abort();pending=false;
  dialog.querySelector('.close').disabled=false;
  $('directoryOnly').disabled=false;
  if(session?.opener?.isConnected)session.opener.focus({preventScroll:true});
  session=null;
 });
 dialog.addEventListener('cancel',event=>{if(dialog.querySelector('.close').disabled)event.preventDefault()});
 function controls(locked){
  dialog.querySelectorAll('.directoryTools button, #directoryEntries button, #directoryPath, .directoryPathLabel + button').forEach(button=>button.disabled=locked);
  $('directoryUp').disabled=locked||page?.parent==null;
 }
 async function browse(path='',offset=0){
  if(!dialog.open||pending)return;
  const key=++serial;
  request?.abort();request=new AbortController();
  resetConfirm();$('directoryUse').disabled=true;status('正在读取目录…');
  try{
   const response=await fetch('/api/storage/directories?'+new URLSearchParams({path,offset}),{signal:request.signal});
   const result=await response.json();
   if(key!==serial)return;
   if(!response.ok)throw Error(result.error||'目录暂不可用');
   page=result;$('directoryCurrent').textContent=result.path||'可浏览位置';
   const roots=result.crumbs||[];
   const crumb=roots.length?`<nav class="row directoryCrumbs" aria-label="目录层级">${roots.map(item=>`<button type="button" data-path="${esc(item.path)}" onclick="DirectoryPicker.browse(this.dataset.path)">${esc(item.name)}</button>`).join('')}</nav>`:'';
   $('directoryEntries').innerHTML=crumb+result.items.map(item=>`<button type="button" class="directoryEntry" data-path="${esc(item.path)}" onclick="DirectoryPicker.browse(this.dataset.path)">${esc(item.name)} <span aria-hidden="true">›</span></button>`).join('')+
    (result.next===null?'':`<button type="button" onclick="DirectoryPicker.more(${result.next})">下一页目录</button>`)+
    (offset?` <button type="button" onclick="DirectoryPicker.more(${Math.max(0,offset-100)})">上一页目录</button>`:'');
   $('directoryPath').value=result.path;resetConfirm();controls(false);
   status(result.items.length?'':result.path?'此位置没有子目录。':'当前没有在线位置。');
  }catch(error){if(key===serial&&error.name!=='AbortError'){status(error.message);controls(false)}}
 }
 function more(offset){if(page)browse(page.path,offset)}
 function up(){if(page?.parent!=null)browse(page.parent)}
 async function native(){
  if(pending)return;
  const key=++serial;pending=true;request?.abort();controls(true);status('请在系统窗口选择目录…');
  let path='';
  try{path=(await api('/api/pick-folder',{})).path||'';if(key===serial)status('')}
  catch(error){if(key===serial)status(error.message)}
  finally{if(key===serial){pending=false;controls(false);if(path)browse(path)}}
 }
 function back(){if(!pending){resetConfirm();controls(false);status('');$('directoryUse').focus()}}
 function showConfirmation(result){
  preview=result;controls(true);$('directoryBack').hidden=false;
  const registered=result.state==='registered',changed=result.state==='changed';
  $('directoryConfirm').innerHTML=`<section class="diskScope"><strong>${registered?'此目录已登记':changed?'发现既有位置':'确认添加位置'}</strong>
   <p class="path">${esc(result.path)}</p>
   ${registered?`<p>${esc(result.alias)}</p>`: `<label>显示名称<input id="directoryAlias" maxlength="100" value="${esc(result.alias)}"></label>`}
   ${changed?`<p>可重连原登记，或保留原快照并添加独立位置。</p><button type="button" onclick="DirectoryPicker.reconnect()">重连原位置</button>`:''}</section>`;
  $('directoryOnly').hidden=registered;
  $('directoryUse').textContent=registered?'查看已登记位置':changed?'登记新位置并盘点':'添加并盘点';
  $('directoryOnly').textContent=changed?'仅登记新位置':'仅添加';
  $('directoryUse').disabled=false;($('directoryAlias')||$('directoryUse')).focus();
 }
 function reconnect(){
  const id=preview?.id;if(!id)return;
  dialog.close();diskGo('storage-reconnect',id);
 }
 async function commit(scan){
  if(!session||pending||!page?.path)return;
  if(!session.register){const {select}=session,path=page.path;dialog.close();select(path);return}
  if(preview?.state==='registered'){const id=preview.id;dialog.close();diskGo('storage-files',id);return}
  const alias=$('directoryAlias')?.value.trim();
  if(preview&&!alias){status('请填写显示名称');$('directoryAlias').focus();return}
  const key=++serial;pending=true;$('directoryUse').disabled=true;$('directoryOnly').disabled=true;
  try{
   if(!preview){
    const result=await api('/api/storage/register-preview',{path:page.path});
    if(key===serial){showConfirmation(result);status('')}
    return;
   }
   dialog.querySelector('.close').disabled=true;
   $('directoryBack').disabled=true;
   if($('directoryAlias'))$('directoryAlias').disabled=true;
   status('正在登记目录…');
   const result=await api('/api/storage/register',{path:preview.path,alias,new_location:preview.state==='changed'});
   if(key!==serial)return;
   let message='目录已添加';
   if(scan){
    try{await api('/api/storage/scan',{id:result.id,hashing:true});message='目录已添加，盘点任务已提交'}
    catch(error){message='目录已添加，可在设备卡重试盘点：'+error.message}
   }
   if(key===serial){dialog.close();toast(message);await goManage('storage',{source:result.id},{replace:true})}
  }catch(error){if(key===serial)status(error.message)}
  finally{
   if(key===serial){
    pending=false;dialog.querySelector('.close').disabled=false;
    for(const id of ['directoryUse','directoryOnly','directoryBack','directoryAlias'])if($(id))$(id).disabled=false;
   }else $('directoryBack').disabled=false;
  }
 }
 return {open,browse,up,more,native,back,commit,reconnect};
})();

const WorkflowFilters=(()=>{
 const panel=document.querySelector('#libraryPage .filterPanel');
 const dialog=document.createElement('dialog');
 dialog.id='filterDialog';dialog.setAttribute('aria-labelledby','filterDialogTitle');
 dialog.innerHTML=workflowHeader('filterDialogTitle','筛选影片','WorkflowFilters.cancel()')+
  '<div class="dialogBody"></div><div class="dialogFooter"><button type="button" onclick="WorkflowFilters.cancel()">取消</button><button type="button" class="accent" onclick="WorkflowFilters.apply()">应用筛选</button></div>';
 dialog.querySelector('.dialogBody').append(...panel.querySelectorAll(':scope > .toolbar, :scope > .advancedFilters'));
 document.body.append(dialog);
 const openButton=document.createElement('button');openButton.id='openFilters';openButton.type='button';
 openButton.textContent='筛选';openButton.setAttribute('aria-haspopup','dialog');
 openButton.onclick=open;panel.querySelector('.searchRow').append(openButton);
 let original=null;
 const values=()=>original||{filters:Object.fromEntries(viewIds.map(id=>[id,$(id).value])),tags:[...activeTags]};
 function open(){
  original=values();
  if(!dialog.querySelector('.privacyToggle')&&typeof privacyButton==='function')dialog.querySelector('.dialogChrome').append(privacyButton());
  dialog.showModal();$('tagPanel').open=true;window.serverBrowse?.refreshTagOptions();
 }
 dialog.addEventListener('change',event=>{if(viewIds.includes(event.target.id))event.stopImmediatePropagation()},true);
 function finish(approved){
  if(!original)return;
  const previous=original;original=null;
  if(!approved){
   for(const [id,value] of Object.entries(previous.filters))$(id).value=value;
   activeTags=new Set(previous.tags);
  }
  window.serverBrowse?.syncTags();
  openButton.focus({preventScroll:true});
  if(approved){page=1;render();pushViewState()}
 }
 function dismiss(approved){dialog.close();finish(approved)}
 dialog.addEventListener('close',()=>{if(!dialog.open)finish(false)});
 dialog.addEventListener('cancel',event=>{event.preventDefault();dismiss(false)});
 return {values,apply:()=>dismiss(true),cancel:()=>dismiss(false),get editing(){return dialog.open}};
})();
window.WorkflowFilters=WorkflowFilters;

const FilmPlayback=(()=>{
 let active=null;
 const player=$('player');
 const messages={offline:'文件离线或位置已变化。请检查物理位置或重连硬盘。',
  unreadable:'暂时无法读取视频。请检查目录权限、挂载和文件状态。',
  unsupported:'浏览器无法播放此格式。可尝试系统播放器。',
  blocked:'浏览器暂停了自动播放，请点击播放器继续。'};
 function status(state){
  const label=$('playbackFeedback'),actions=$('playbackRecovery');
  if(!label||!actions)return;
  label.textContent=messages[state]||(state==='loading'?'正在准备播放…':'');
  const id=active?.id;
  actions.innerHTML=state==='offline'&&id?`<button onclick="DiskFileView.fromVideo(${id})">查看物理位置</button>`:
   state==='unsupported'||state==='unreadable'?library.server_mode?'':'<button onclick="FilmPlayback.external()">使用系统播放器</button>':'';
 }
 function reset(){
  active?.controller.abort();active=null;
  player.querySelectorAll('track').forEach(track=>track.remove());
  for(const id of ['playbackSubtitles','playbackAudio']){const host=$(id);if(host)host.replaceChildren()}
  status('');
 }
 function showSubtitles(tracks){
  const host=$('playbackSubtitles');if(!host)return;
  if(!tracks.length){host.replaceChildren();return}
  host.innerHTML=`<label>外挂字幕 <select onchange="FilmPlayback.chooseSubtitle(this.value)"><option value="">关闭</option>${tracks.map(track=>`<option value="${esc(track.id)}">${esc(track.label)}</option>`).join('')}</select></label>`;
 }
 function showAudio(){
  const host=$('playbackAudio'),tracks=player.audioTracks;
  if(!host||!tracks||tracks.length<2)return;
  host.innerHTML=`<label>音轨 <select onchange="FilmPlayback.chooseAudio(this.value)">${Array.from(tracks,(track,index)=>`<option value="${index}" ${track.enabled?'selected':''}>${esc(track.label||track.language||('音轨 '+(index+1)))}</option>`).join('')}</select></label>`;
 }
 function chooseAudio(value){Array.from(player.audioTracks||[]).forEach((track,index)=>track.enabled=index===Number(value))}
 function chooseSubtitle(value){
  player.querySelectorAll('track').forEach(track=>track.remove());
  if(!active||!active.tracks.some(track=>track.id===value))return;
  const owner=active,track=document.createElement('track');track.kind='subtitles';
  track.src='/api/playback-subtitle?'+new URLSearchParams({id:owner.id,track:value});
  track.addEventListener('error',()=>{if(active===owner&&track.isConnected)$('playbackFeedback').textContent='字幕暂无法读取，可关闭或切换字幕'});
  player.append(track);track.track.mode='showing';
 }
 async function consult(owner){
  const result=await fetch('/api/playback-info?'+new URLSearchParams({id:owner.id}),{signal:owner.controller.signal});
  if(!result.ok)throw Error('读取播放信息失败');
  return result.json();
 }
 function begin(id){
  reset();
  const owner=active={id,controller:new AbortController(),tracks:[],error:false,blocked:false};status('loading');
  consult(owner).then(data=>{
   if(active!==owner||owner.error)return;
   owner.tracks=data.tracks;
   showSubtitles(data.tracks);status(data.state==='ready'?(owner.blocked?'blocked':''):data.state);
  }).catch(error=>{if(active===owner&&!owner.error&&error.name!=='AbortError')status('unreadable')});
 }
 function matches(id){return active?.id===id&&detail.open&&selected?.id===id}
 async function failed(id,code){
  if(!matches(id))return;
  const owner=active;owner.error=true;
  try{
   const data=await consult(owner);
   if(active!==owner)return;
   status(data.state==='ready'?(code===2?'unreadable':'unsupported'):data.state);
  }catch(error){if(active===owner&&error.name!=='AbortError')status('unreadable')}
 }
 function blocked(id){if(matches(id)&&!active.error){active.blocked=true;status('blocked')}}
 async function external(){
  if(library.server_mode)return;
  try{await api('/api/play',{id:selected.id})}catch(error){toast(error.message)}
 }
 player.addEventListener('loadedmetadata',()=>{if(active)showAudio()});
 player.addEventListener('playing',()=>{if(active?.blocked){active.blocked=false;status('')}});
 detail.addEventListener('close',reset);
 return {begin,failed,blocked,reset,chooseSubtitle,chooseAudio,external};
})();
