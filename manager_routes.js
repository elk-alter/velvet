/* The management router owns navigation. management.js functions only render pages. */
let managerRouteEpoch=0,managerController=null,managerDirty=false,managerRendering=false,duplicatesTimer;
const managerNavigationState={route:null,params:{},browseReturn:null};window.managerNavigationState=managerNavigationState;
const managerRenderers={libraries:showMediaLibraries,tasks:showTaskCenter,sources:showScrapingOptions,priorities:showNativeOptions,sites:showSiteSettings,network:showNetwork,connection:showScrapeConnection,files:showLibraryFiles,edit:editMediaLibrary};
const managerRouteInfo={
 libraries:{group:'libraries',title:'媒体库'},'library-files':{group:'libraries',title:'媒体库文件',parent:'libraries'},'library-edit':{group:'libraries',title:'配置媒体库',parent:'libraries'},'library-new':{group:'libraries',title:'添加媒体库',parent:'libraries'},duplicates:{group:'libraries',title:'重复检查'},review:{group:'libraries',title:'待处理与文件质量'},storage:{group:'storage',title:'硬盘管理'},'storage-reconnect':{group:'storage',title:'重连盘点位置'},'storage-files':{group:'storage',title:'硬盘文件'},'storage-file':{group:'storage',title:'物理文件详情'},'storage-video':{group:'storage',title:'视频的物理位置'},'organization-plan':{group:'storage',title:'制定整理计划'},'organization-review':{group:'storage',title:'审阅整理预览'},'organization-draft':{group:'storage',title:'确认执行计划'},'organization-tasks':{group:'storage',title:'整理任务'},'organization-task':{group:'storage',title:'整理任务'},'organization-cleanup':{group:'storage',title:'复制暂存清理'},'organization-skip':{group:'storage',title:'跳过文件组'},
 tasks:{group:'scrape',title:'刮削任务'},ledger:{group:'settings',title:'全部后台任务'},sources:{group:'scrape',title:'来源目录'},priorities:{group:'scrape',title:'来源顺序与字段择优',parent:'sources'},sites:{group:'scrape',title:'站点凭据'},connection:{group:'scrape',title:'执行与网络'},network:{group:'scrape',title:'网络检测'},
 general:{group:'settings',title:'常规设置'},rules:{group:'settings',title:'分类规则'},backups:{group:'settings',title:'备份与恢复'}
};
const managerGroupNames={storage:'硬盘管理',libraries:'媒体库',scrape:'刮削中心',settings:'设置'};
const managerTabs={storage:[['storage','设备与目录'],['storage-files','文件快照'],['organization-tasks','整理任务']],libraries:[['libraries','媒体库'],['duplicates','重复检查'],['review','待处理与质量']],scrape:[['tasks','刮削任务'],['sources','来源与择优'],['sites','站点凭据'],['connection','执行与网络'],['network','网络检测']],settings:[['general','常规'],['rules','分类规则'],['backups','备份恢复'],['ledger','全部后台任务']]};
const managerQueryKeys={path:'m_lib',source:'m_source',status:'m_status',reason:'m_reason',search:'m_q',offset:'m_offset',online:'m_online',quality:'m_quality',duplicate:'m_duplicate',sort:'m_sort',directory:'m_directory',snapshot:'m_snapshot',selection_device:'m_sel_device',file:'m_file',video:'m_video',resource_offset:'m_resource',location:'m_location'};
function managementLocation(){const url=new URL(location.href),raw=url.searchParams.get('manage');if(!raw)return null;const route=Object.hasOwn(managerRouteInfo,raw)?raw:'tasks';return {route,params:Object.fromEntries(Object.entries(managerQueryKeys).map(([key,param])=>[key,url.searchParams.get(param)||'']))}}
function writeManagementLocation(route,params,replace=false){const url=new URL(location.href);url.searchParams.delete('manage');for(const key of Object.values(managerQueryKeys))url.searchParams.delete(key);if(route){url.searchParams.set('manage',route);for(const [key,param]of Object.entries(managerQueryKeys))if(params[key]!==undefined&&params[key]!==''&&params[key]!==null)url.searchParams.set(param,String(params[key]));}history[replace?'replaceState':'pushState']({...(replace?history.state:{}),velvetManager:route,browseReturn:managerNavigationState.browseReturn,diskFileReturn:managerNavigationState.diskFileReturn||null},'',url)}
function sameManagerPage(route,params){return managerNavigationState.route===route&&Object.keys(managerQueryKeys).every(key=>String(managerNavigationState.params[key]||'')===String(params[key]||''))}
let managerLeavePromise=null;
function allowManagementLeave(dirty=managerDirty){if(!dirty)return Promise.resolve(true);if(managerLeavePromise)return managerLeavePromise;const dialog=document.createElement('dialog');dialog.className='leaveDialog';dialog.setAttribute('aria-labelledby','leaveManagerTitle');dialog.innerHTML='<form method="dialog"><h2 id="leaveManagerTitle">保留未保存的修改？</h2><p>离开此页会放弃尚未保存的修改。</p><div class="row"><button value="cancel" autofocus>继续编辑</button><button value="discard">放弃修改并离开</button></div></form>';managerLeavePromise=new Promise(resolve=>{dialog.addEventListener('close',()=>{const allowed=dialog.returnValue==='discard';dialog.remove();managerLeavePromise=null;resolve(allowed)},{once:true})});document.body.append(dialog);dialog.showModal();return managerLeavePromise}

function clearManagerTimers(){clearTimeout(diskTaskTimer);clearTimeout(reviewTimer);clearTimeout(managerTaskTimer);clearTimeout(managerNetworkTimer);clearTimeout(duplicatesTimer);managerController?.abort();}
async function managerFetch(url){const ticket=managerRouteEpoch;const response=await fetch(url,{signal:managerController?.signal});const read=response.json.bind(response);response.json=async()=>{const data=await read();if(ticket!==managerRouteEpoch)throw new DOMException('页面已切换','AbortError');return data};return response}
async function managerGet(url){const response=await managerFetch(url);if(!response.ok)throw Error('读取失败，请检查服务后重试');return response.json()}
function renderManagerShell(title,body,footer=''){
 const route=managerNavigationState.route,info=managerRouteInfo[route];if(!info)return;
 if(toolDialog.open)toolDialog.close();if(settingsDialog.open)settingsDialog.close();if(duplicatesDialog.open)duplicatesDialog.close();
 document.querySelector('main').hidden=true;document.body.classList.add('manager-open');const view=$('managerView');view.hidden=false;
 const activeTab=route==='storage-reconnect'?'storage':route==='priorities'?'sources':route.startsWith('library-')?'libraries':['storage-file','storage-video'].includes(route)?'storage-files':route.startsWith('organization-')?(['organization-tasks','organization-task','organization-cleanup','organization-skip'].includes(route)?'organization-tasks':'storage-files'):route;
 const parent=info.parent;
 view.innerHTML=`<header class="managerHeader"><div><div class="managerBreadcrumb"><span>管理</span><span>/</span><button onclick="goManage('${managerTabs[info.group][0][0]}')">${managerGroupNames[info.group]}</button>${parent&&parent!==managerTabs[info.group][0][0]?`<span>/</span><button onclick="goManage('${parent}')">${esc(managerRouteInfo[parent].title)}</button>`:''}</div><h2 tabindex="-1">${esc(title)}</h2></div><div class="row">${parent?`<button onclick="goManage('${parent}')">返回${parent==='sources'?'来源目录':'媒体库'}</button>`:''}<button onclick="exitManager()">返回片库</button></div></header><nav class="managerNav" aria-label="${managerGroupNames[info.group]}分区">${managerTabs[info.group].map(([page,label])=>`<button class="${activeTab===page?'active':''}" ${activeTab===page?'aria-current="page"':''} onclick="goManage('${page}')">${label}</button>`).join('')}</nav><div class="managerBody" data-route="${route}">${body}</div>${footer?'<footer class="managerFooter">'+footer+'</footer>':''}`;
 if(typeof privacyButton==='function')view.querySelector('.managerHeader > .row').prepend(privacyButton());
 for(const button of document.querySelectorAll('aside button')){button.toggleAttribute('data-manager-active',button.dataset.manage===info.group);if(button.dataset.manage===info.group)button.setAttribute('aria-current','page');else button.removeAttribute('aria-current')}
}
async function goManage(route,params={},options={}){
 if(!Object.hasOwn(managerRouteInfo,route))route='tasks';params={...params};if(params.status==='all')delete params.status;if(!Number(params.offset))delete params.offset;
 const same=sameManagerPage(route,params);
 const restorePosition=options.restorePosition||(options.history?history.state?.managerPosition:null);
 if(!options.history&&managerNavigationState.route)saveManagerPosition();
 if(options.history)managerNavigationState.diskFileReturn=history.state?.diskFileReturn||null;
 const retainingDiskEdits=route==='organization-review'&&managerNavigationState.route===route&&params.source===managerNavigationState.params.source;
 if(!options.history&&managerDirty&&!retainingDiskEdits&&!await allowManagementLeave())return false;
 if(!managerNavigationState.route){const state=captureView();managerNavigationState.browseReturn=(options.history?(history.state?.browseReturn||managerNavigationState.browseReturn):null)||{state,scroll:window.scrollY||0};saveViewState();history.replaceState({...history.state,browseReturn:managerNavigationState.browseReturn},'',location.href)}
 window.serverBrowse?.cancel();clearManagerTimers();const ticket=++managerRouteEpoch;managerController=new AbortController();managerDirty=false;managerNavigationState.route=route;managerNavigationState.params={...params};
 if(!options.history)writeManagementLocation(route,params,same||options.replace);
 renderManagerShell(managerRouteInfo[route].title,'<div class="managerLoading" role="status">正在加载…</div>');window.scrollTo({top:0});managerRendering=true;
 try{
  if(['library-files','library-edit','library-new'].includes(route)){
   managedLibraries=(await managerGet('/api/libraries')).items;let index=managedLibraries.findIndex(item=>item.path===params.path);
   if(route!=='library-new'&&index<0){renderManagerShell('媒体库不可用','<p class="muted">这个媒体库已移除或路径已变更，请返回媒体库列表选择。</p>');return true}
   if(route==='library-files'){const desired={...params};await managerRenderers.files(index);if(ticket!==managerRouteEpoch)return false;$('mlFileStatus').value=['all','matched','pending','missing','error'].includes(desired.status)?desired.status:'all';$('mlFileSearch').value=desired.search||'';await loadLibraryFiles(Math.max(0,Number(desired.offset)||0));}
   else managerRenderers.edit(route==='library-new'?-1:index);
  }else if(route==='duplicates'){await renderDuplicatesManager()}
  else if(managerRouteInfo[route].group==='storage'){await renderDiskWorkspace(route,params)}
  else if(route==='ledger'){await renderTaskLedger()}
  else if(route==='review'){await renderReviewManager()}
  else if(route==='general'){await renderGeneralSettings()}
  else if(route==='rules'){await renderClassificationSettings()}
  else if(route==='backups'){await renderBackupSettings()}
  else{
   if(route==='tasks'){
    const status=['all','queued','running','done','failed','images'].includes(params.status)?params.status:'all';
    const reason=status==='failed'&&Object.hasOwn(scrapeFailureNames,params.reason)?params.reason:'';
    if(managerTaskFilter!==status||managerRetryFilter!==reason)managerRetrySelection.clear();
    managerTaskFilter=status;managerRetryFilter=reason;
    managerTaskOffset=Math.max(0,Number(params.offset)||0);
   }
   await managerRenderers[route](options.force||false);
   if(ticket!==managerRouteEpoch)return false;
   if(route==='sites'&&params.source){if(nativeSiteSettings.some(site=>site.id===params.source)){$('nativeSite').value=params.source;renderSiteSettings()}else renderManagerShell('来源不存在','<p>此来源未安装或已移除，请从站点凭据列表重新选择。</p>')}
   if(route==='sites'&&$('nativeSite')){$('nativeSite').onchange=async()=>{const previous=managerNavigationState.params.source;const selected=$('nativeSite').value;if(await goManage('sites',{source:selected})===false&&$('nativeSite'))$('nativeSite').value=previous};managerNavigationState.params.source=$('nativeSite').value;writeManagementLocation(route,managerNavigationState.params,true)}
   if(route==='connection')improveConnectionSections();
  }
  if(ticket===managerRouteEpoch){$('managerView').querySelector('h2')?.focus({preventScroll:true});if(restorePosition)requestAnimationFrame(()=>{window.scrollTo(0,Number(restorePosition.scroll)||0);if(restorePosition.focus)document.getElementById(restorePosition.focus)?.focus({preventScroll:true})})}
  return true;
 }catch(error){if(error.name==='AbortError')return false;if(ticket===managerRouteEpoch)renderManagerShell(managerRouteInfo[route].title,`<div class="managerError"><h3>页面暂时无法加载</h3><p>${esc(error.message)}</p><button onclick="retryManagerPage()">重新加载</button></div>`);return false}
 finally{if(ticket===managerRouteEpoch)managerRendering=false}
}
function saveManagerPosition(){if(!managerNavigationState.route)return;history.replaceState({...history.state,managerPosition:{scroll:window.scrollY||0,focus:document.activeElement?.id||''}},'',location.href)}
let managerPositionFrame;
window.addEventListener('scroll',()=>{if(managerPositionFrame||!managerNavigationState.route||managerRendering)return;managerPositionFrame=requestAnimationFrame(()=>{managerPositionFrame=null;saveManagerPosition()})},{passive:true});
function retryManagerPage(){goManage(managerNavigationState.route,managerNavigationState.params,{replace:true})}
function hideManagement(){clearManagerTimers();++managerRouteEpoch;managerNavigationState.route=null;managerNavigationState.params={};managerDirty=false;$('managerView').hidden=true;document.querySelector('main').hidden=false;document.body.classList.remove('manager-open');for(const button of document.querySelectorAll('aside [data-manage]')){button.removeAttribute('data-manager-active');button.removeAttribute('aria-current')}}
async function exitManager(options={}){
 if(!managerNavigationState.route)return true;
 if(!options.history&&managerDirty&&!await allowManagementLeave())return false;
 const snapshot=managerNavigationState.browseReturn;
 hideManagement();
 if(!options.history)writeManagementLocation(null,{},false);
 if(options.restore!==false&&snapshot)applyView({...snapshot.state,scroll:snapshot.scroll});
 if(window.serverBrowse?!window.serverBrowse.ready:!library._fullFetched)await refresh();
 return true;
}
function restoreManagerRoute(){const target=managementLocation();if(target){let state=captureView();try{state=JSON.parse(new URL(location.href).searchParams.get('view')||localStorage.getItem('velvet-view')||'null')||state}catch(error){}managerNavigationState.browseReturn=history.state?.browseReturn||{state,scroll:state.scroll||0};goManage(target.route,target.params,{history:true});}}
async function handleManagerHistory(){if(managerNavigationState.route&&managerDirty&&!await allowManagementLeave()){writeManagementLocation(managerNavigationState.route,managerNavigationState.params,false);return true}const target=managementLocation();if(target){goManage(target.route,target.params,{history:true});return true}if(managerNavigationState.route){exitManager({history:true});return true}return false}
document.querySelector('aside').addEventListener('click',event=>{const button=event.target.closest('button');if(!button||button.dataset.manage||!managerNavigationState.route)return;if(managerDirty){event.preventDefault();event.stopImmediatePropagation();allowManagementLeave().then(allowed=>{if(allowed){managerDirty=false;exitManager({restore:false});button.click()}})}else exitManager({restore:false})},true);
const managerDirtyIds=new Set(['hubEngine','hubProxy','hubUrl','hubToken','siteBase','siteTimeout','siteRetries','siteCookie','clearSiteCookie','mlName','mlPath','mlAutomation','mlExclude','mlMinSize','mlSettle','diskRoot','diskAlias','diskTarget','diskLayout','diskPlanDevice','systemInterval','classificationRules','classificationAliases','advancedOnline','advancedProvider','advancedKey']);
for(const type of ['input','change'])document.addEventListener(type,event=>{if(managerNavigationState.route&&event.target.closest('.managerBody')&&(managerDirtyIds.has(event.target.id)||event.target.matches('[data-field-source],#nativeSources input')))managerDirty=true});
window.addEventListener('beforeunload',event=>{if(managerDirty||(detail.open&&detailHasChanges())){event.preventDefault();event.returnValue=''}});
let managerRuleBaseline={};
const managerOriginalApi=api;api=async function(path,data){const ticket=managerRouteEpoch;const response=await managerOriginalApi(path,data);if(ticket===managerRouteEpoch&&['/api/settings','/api/amane-settings','/api/scrape-sites','/api/scraping-options','/api/libraries','/api/tag-rules'].includes(path)){if(managerNavigationState.route==='rules'){const id=path==='/api/tag-rules'?'classificationRules':'classificationAliases';if($(id))managerRuleBaseline[id]=$(id).value;managerDirty=['classificationRules','classificationAliases'].some(key=>$(key)&&$(key).value!==managerRuleBaseline[key])}else managerDirty=false}return response};
const previousMoveNativeSource=moveNativeSource;moveNativeSource=function(...args){managerDirty=true;return previousMoveNativeSource(...args)};
const previousToggleNativeSource=toggleNativeSource;toggleNativeSource=function(...args){managerDirty=true;return previousToggleNativeSource(...args)};
showMediaLibraries=()=>goManage('libraries');showScrapeHub=()=>goManage('tasks');showTaskCenter=()=>goManage('tasks',{status:managerTaskFilter});
showScrapingOptions=(force=false)=>goManage('sources',{}, {force});showNativeOptions=()=>goManage('priorities');showSiteSettings=()=>goManage('sites',managerNavigationState.route==='sites'?managerNavigationState.params:{});
showNetwork=()=>goManage('network');showScrapeConnection=()=>goManage('connection');
showLibraryFiles=index=>goManage('library-files',{path:managedLibraries[index]?.path});editMediaLibrary=index=>goManage(index<0?'library-new':'library-edit',index<0?{}:{path:managedLibraries[index]?.path});
const managerLoadLibraryFiles=loadLibraryFiles;loadLibraryFiles=async function(offset){if(!managerRendering&&managerNavigationState.route==='library-files'){Object.assign(managerNavigationState.params,{status:$('mlFileStatus').value,search:$('mlFileSearch').value,offset});writeManagementLocation('library-files',managerNavigationState.params,true)}return managerLoadLibraryFiles(offset)};
function showAppSettings(){return goManage('general')}settings=showAppSettings;showBackups=()=>goManage('backups');showDuplicates=()=>goManage('duplicates');

const duplicatePage=document.createElement('div');while(duplicatesDialog.querySelector('.dialogBody').firstChild)duplicatePage.append(duplicatesDialog.querySelector('.dialogBody').firstChild);
async function renderDuplicatesManager(){renderManagerShell('重复检查','<div id="managerDuplicateBody"></div>');$('managerDuplicateBody').append(duplicatePage);await loadDuplicates();scheduleDuplicateRefresh()}
function scheduleDuplicateRefresh(){clearTimeout(duplicatesTimer);if(managerNavigationState.route==='duplicates')duplicatesTimer=setTimeout(async()=>{if(managerNavigationState.route!=='duplicates')return;await loadDuplicates();scheduleDuplicateRefresh()},5000)}
async function renderGeneralSettings(){const data=await managerGet('/api/context');library={...library,...data};token=data.token;const cfg=data.settings;renderManagerShell('常规设置',`<section class="settingsSection"><h3>扫描与运行</h3><p class="muted">媒体目录和单库自动化在“媒体库”中配置。这里仅控制全局扫描间隔。</p><label>扫描间隔（秒）<input id="systemInterval" type="number" min="30" value="${Number(cfg.interval)||300}"></label></section><section class="settingsSection"><h3>运行状态</h3><dl class="settingsFacts"><dt>运行方式</dt><dd>${data.server_mode?'NAS / 网页访问':'本机服务'}</dd><dt>FFmpeg</dt><dd>${data.ffmpeg?'可用':'未安装'}</dd><dt>ffprobe</dt><dd>${data.ffprobe?'可用':'未安装'}</dd><dt>媒体库</dt><dd>${data.folders.length} 个目录 · <button onclick="showMediaLibraries()">管理媒体库</button></dd></dl></section>`,`<button class="accent" onclick="saveGeneralSettings()">保存常规设置</button>`)}
async function saveGeneralSettings(){const ticket=managerRouteEpoch;try{await api('/api/settings',{interval:Number($('systemInterval').value)});toast('常规设置已保存');if(ticket===managerRouteEpoch)goManage('general',{}, {replace:true})}catch(error){toast(error.message)}}
async function renderClassificationSettings(){const rules=await managerGet('/api/tag-rules');const data=await managerGet('/api/context');library={...library,...data};token=data.token;renderManagerShell('分类规则',`<section class="settingsSection"><h3>题材别名</h3><p class="muted">将来源中的不同题材名称映射到统一名称，保留原始元数据。</p><label>别名映射（JSON 对象）<textarea id="classificationAliases">${esc(JSON.stringify(data.settings.tag_aliases||{},null,2))}</textarea></label><button onclick="saveClassificationAliases()">保存题材别名</button></section><section class="settingsSection"><h3>关键词规则</h3><p class="muted">为标题、路径和已有资料命中指定关键词的影片添加自动标签。</p><label>规则（JSON 数组）<textarea id="classificationRules">${esc(JSON.stringify(rules.rules,null,2))}</textarea></label><button onclick="saveClassificationRules()">保存规则并重新分类</button></section>`);managerRuleBaseline={classificationRules:$('classificationRules').value,classificationAliases:$('classificationAliases').value}}
async function saveClassificationRules(){try{const rules=JSON.parse($('classificationRules').value);await api('/api/tag-rules',{rules});toast('关键词规则已保存并重新分类')}catch(error){toast(error.message)}}
async function saveClassificationAliases(){try{const aliases=JSON.parse($('classificationAliases').value),cfg=library.settings;await api('/api/amane-settings',{engine:cfg.scrape_engine,url:cfg.amane_url,proxy:cfg.scrape_proxy,aliases});toast('题材别名已保存')}catch(error){toast(error.message)}}
async function renderBackupSettings(){const data=await managerGet('/api/backups');renderManagerShell('备份与恢复',`<p class="muted">备份主片库、偏好与配置。图片缓存和独立来源缓存库不包含在主片库备份中。</p><button class="accent" onclick="createBackup()">创建备份</button><div>${data.backups.map(backup=>`<div class="folder"><div><strong>${esc(backup.name)}</strong><small>${(backup.size/1048576).toFixed(2)} MB</small></div><button data-name="${esc(backup.name)}" onclick="restoreBackup(this.dataset.name)">校验并恢复</button></div>`).join('')||'<p class="muted">尚无备份</p>'}</div>`)}
createBackup=async function(){const ticket=managerRouteEpoch;try{const result=await api('/api/backup',{});toast('备份已创建：'+result.name);if(ticket===managerRouteEpoch)goManage('backups',{}, {replace:true})}catch(error){toast(error.message)}};
const managerExecuteRestore=executeRestore;executeRestore=async function(name,reconciliation){if(await managerExecuteRestore(name,reconciliation)&&managerNavigationState.route==='backups')goManage('backups',{}, {replace:true})};
function improveConnectionSections(){const engine=$('hubEngine');if(!engine)return;const proxy=$('hubProxy').closest('label'),url=$('hubUrl').closest('label'),credential=$('hubToken').closest('label');const update=()=>{const native=engine.value==='builtin';proxy.hidden=!native;url.hidden=native;credential.hidden=native};engine.addEventListener('change',update);update();const cfg=library.settings;const advanced=document.createElement('details');advanced.className='settingsSection';advanced.innerHTML=`<summary>高级：扫描时补充 JSON 资料接口</summary><p class="muted">仅用于目录扫描的补充资料查询；日常刮削请使用来源目录。</p><label><input type="checkbox" id="advancedOnline" ${cfg.online?'checked':''}>启用补充接口</label><label>HTTPS 地址（包含 {code}）<input id="advancedProvider" value="${esc(cfg.provider||'')}"></label><label>Bearer API Key<input type="password" autocomplete="new-password" id="advancedKey" placeholder="留空保留现有值"></label>`;$('managerView').querySelector('.managerBody').append(advanced);const paragraph=$('managerView').querySelector('.managerBody>p:last-child');if(paragraph)paragraph.textContent='只有选择 Amane 完整引擎时才需要 Amane 地址和 Token。内置引擎的来源、凭据和网络检测都在本中心配置。'}
