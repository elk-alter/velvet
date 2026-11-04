/* Paged browsing owns requests, fixed selection and lazy movie hydration. */
const serverBrowse=(()=>{
 let timer,controller,epoch=0,movieEpoch=0,snapshot='',scope='',lastRevision='',total=0,collectionReturn=null;
 const chosen=new Map(),details=new Map();let data=null,selectionBusy=false,returnCardKey='',pending=false,queryFailed=false;
 const collectionId=()=>new URL(location.href).searchParams.get('collection')||'';
 function criteria(){return {q:$('q').value.trim(),mode,length:$('length').value,censorship:$('censorFilter').value,vr:$('vrFilter').value==='nonvr'?'flat':$('vrFilter').value,height:$('resolution').value,tags:JSON.stringify([...activeTags,...($('tag').value?[$('tag').value]:[])]),tag_logic:$('tagLogic').value,exclude_tag:$('excludeTag').value,actor:currentActor||$('actor').value,studio:$('studio').value,source:$('scrapeState').value,quality:$('reviewFilter').value,sort:$('sort').value,collections:folderCollectionsEnabled?'on':'off',collection:collectionId(),facet_kind:activeCatalogFacet?.kind||'',facet_name:activeCatalogFacet?.name||''}}
 async function get(url,signal){const response=await fetch(url,{signal});const result=await response.json();if(!response.ok){const error=Error(result.error||'读取失败，请稍后重试');error.status=response.status;throw error}return result}
 function busy(value,failed=false){pending=value;queryFailed=failed;$('grid').setAttribute('aria-busy',String(value));const chooser=document.querySelector('button[onclick="choosePage()"]');if(chooser)chooser.disabled=value||failed||!data?.items.length;document.querySelectorAll('#grid input[type=checkbox]').forEach(input=>input.disabled=value||failed)}
 function render(){++movieEpoch;clearTimeout(timer);if(actorsView||window.managerNavigationState?.route)return;controller?.abort();++epoch;busy(true);timer=setTimeout(()=>load(false).catch(showError),80)}
 function showError(error){if(error.name==='AbortError')return;busy(false,true);$('grid').innerHTML=`<div class="empty"><h2>暂时无法加载</h2><p>${esc(error.message)}</p><button onclick="serverBrowse.refresh(true)">重新加载</button></div>`}
 async function refresh(force=false){
  const context=await get('/api/context');library={...library,...context,_paged:true};token=context.token;updateLibraryChrome();
  if(window.managerNavigationState?.route||new URL(location.href).searchParams.has('manage'))return;
  restoreInitialView();clearTimeout(timer);if(actorsView){await loadActors();return}
  const revision=JSON.stringify(await get('/api/library/query-revision?quality='+encodeURIComponent(criteria().quality)));
  if(!force&&data&&revision===lastRevision&&JSON.stringify(criteria())===scope&&data.offset===(page-1)*pageSize&&data.limit===pageSize)return;
  lastRevision=revision;await load(force||!!data);
  if(detail.open&&selected?.id)await loadMovie(selected.id);
  if(currentActor)loadActors();
 }
 async function load(force=false){
  if(window.managerNavigationState?.route||actorsView)return;
  clearTimeout(timer);controller?.abort();controller=new AbortController();const signal=controller.signal,ticket=++epoch,filters=criteria(),key=JSON.stringify(filters);
  if(force||key!==scope)snapshot='';busy(true);
  let result;
  for(let attempt=0;attempt<2;attempt++){
   try{result=await get('/api/library/query?'+new URLSearchParams({...filters,offset:(Math.max(1,page)-1)*pageSize,limit:pageSize,snapshot}),signal);break}
   catch(error){if(error.status!==409||attempt)throw error;snapshot='';clearSelection('片库资料已变化，已清空过期选择')}
  }
  if(ticket!==epoch||signal.aborted||window.managerNavigationState?.route||actorsView)return;
  if(chosen.size&&(key!==scope||data?.revision!==result.revision))clearSelection('筛选或资料已变化，已清空原选择范围');
  scope=key;snapshot=result.snapshot;data=result;total=result.total;maxpage=Math.max(1,Math.ceil(total/pageSize));
  if(page>maxpage){page=maxpage;return load(false)}
  filtered=result.items;busy(false);draw();loadSuggestions();saveViewState();
 }
 function draw(){
  const focusedKey=document.activeElement?.closest('.movieCard')?.dataset.cardKey;
  for(const interval of hoverTimers.values())clearInterval(interval);hoverTimers.clear();
  $('grid').setAttribute('aria-busy','false');$('grid').innerHTML=data.items.length?data.items.map(item=>item.kind==='collection'?collectionCard(item):videoCard(item)).join(''):`<div class="empty"><h2>没有符合条件的视频</h2><p>可以调整搜索或筛选条件，或导入新的媒体目录。</p><button onclick="resetFilters()">清除筛选</button><button onclick="folderDialog.showModal()">导入目录</button></div>`;
  const summary=data.summary;$('stats').innerHTML=[[summary.movies,'部作品'],[summary.favorites,'个收藏'],(summary.bytes/1073741824).toFixed(1)+' GB',summary.unwatched].map((item,i)=>{const value=Array.isArray(item)?item[0]:item,label=Array.isArray(item)?item[1]:i===2?'总容量':'部尚未观看';return `<div class="stat"><b>${value}</b><small>${label}</small></div>`}).join('');
  $('clearFiltersButton').hidden=!activeTags.size&&[['q',''],['length','all'],['vrFilter',''],['censorFilter',''],['resolution','0'],['tag',''],['excludeTag',''],['actor',''],['studio',''],['scrapeState',''],['reviewFilter','']].every(([id,value])=>$(id).value===value);
  $('activeTags').innerHTML=[...activeTags].map(tag=>`<button data-tag="${esc(tag)}" onclick="removeTag(this.dataset.tag)">${esc(tag)} ×</button>`).join('');
  renderPagination();selectionBar();renderCollectionHeader();if(focusedKey)focusCard(focusedKey);
  if(!document.body.dataset.libraryReadyMs)requestAnimationFrame(()=>requestAnimationFrame(()=>document.body.dataset.libraryReadyMs=String(Math.round(performance.now()))));
 }
 function collectionCard(item){return `<article class="card movieCard" data-card-key="${esc(item.key)}" role="button" tabindex="0" data-id="${item.id}" onclick="serverBrowse.openCollection(this.dataset.id)" onkeydown="activateMovieCard(event,this)"><div class="picture collectionMosaic">${item.previews.map((id,i)=>`<div class="collectionTile"><span>${String(i+1).padStart(2,'0')}</span><img loading="lazy" src="/thumb/${id}-cover.jpg" alt="" onerror="collectionImageFallback(this,${id})"></div>`).join('')}<label class="cardSelection ${chosen.has(item.key)?'selected':''}" onclick="event.stopPropagation()"><input type="checkbox" aria-label="选择视频集 ${esc(item.title)}" ${chosen.has(item.key)?'checked':''} data-key="${esc(item.key)}" onchange="serverBrowse.toggleKey(this.dataset.key,this.checked);this.closest('label').classList.toggle('selected',this.checked)"></label><span class="collectionCount">${item.hit_count} / ${item.total_count} 集</span></div><div class="info"><div class="movieCode">视频集</div><b>${esc(item.title)}</b><small class="movieActors">命中 ${item.hit_count} 个条目 · 全集 ${item.total_count} 个</small></div></article>`}
 function toggleKey(key,value){if(selectionBusy||pending||queryFailed)return;const item=data?.items.find(row=>row.key===key);if(!item)return;if(value){if(chosen.size>=500){toast('一次最多选择500张卡片');return}chosen.set(key,item);if(item.kind==='movie')chosenIds.add(item.id)}else{chosen.delete(key);chosenIds.delete(item.id)}selectionBar()}
 function toggle(id,value){const item=data?.items.find(row=>row.kind==='movie'&&row.id===id);if(item)toggleKey(item.key,value)}
 function selectPage(){if(pending||queryFailed||!data?.items.length)return;const remove=data.items.every(item=>chosen.has(item.key));for(const item of data.items)toggleKey(item.key,!remove);draw()}
 function clearSelection(message=''){chosen.clear();chosenIds.clear();selectionBar();if(message)toast(message)}
 function selectionBar(){
  let bar=$('pagedSelection');if(!bar){bar=document.createElement('div');bar.id='pagedSelection';bar.className='pagedSelection';$('grid').before(bar)}
  const entries=[...chosen.values()],movies=entries.reduce((n,item)=>n+(item.kind==='collection'?item.hit_count:1),0),bytes=entries.reduce((n,item)=>n+item.size,0);
  $('chosenCount').textContent=movies;$('batchScrapeAction').hidden=!movies;bar.hidden=!entries.length;
  bar.innerHTML=`<strong>跨页已选 ${entries.length} 张卡片 · ${movies} 部作品 · ${diskBytes(bytes)}</strong><button onclick="serverBrowse.clearSelection();serverBrowse.redraw()">清空选择</button><details><summary>查看及移除已选条目</summary>${entries.map(item=>`<div class="row"><span>${esc(item.title)}</span><button data-key="${esc(item.key)}" onclick="serverBrowse.remove(this.dataset.key)">移除</button></div>`).join('')}</details>`;
 }
 async function members(offset=0,limit=100){if(!chosen.size)throw Error('请先选择视频');return api('/api/library/selection',{snapshot,keys:[...chosen.keys()],offset,limit})}
 async function scrape(){
  if(selectionBusy||!chosen.size)return;selectionBusy=true;
  const fixedSnapshot=snapshot,fixedKeys=[...chosen.keys()];let submitted=0,skipped=0,failures=[];
  const requestRange=offset=>api('/api/library/selection',{snapshot:fixedSnapshot,keys:fixedKeys,offset,limit:50});
  try{const first=await requestRange(0);skipped=first.total-first.eligible;
   for(let offset=0;offset<first.total;offset+=50){const batch=offset?await requestRange(offset):first,ids=batch.items.filter(item=>item.code?.trim()).map(item=>item.id);if(!ids.length)continue;const result=await api('/api/amane-submit',{ids});submitted+=result.submitted;skipped+=result.skipped;failures.push(...result.failures)}
   toast(`已按固定范围提交 ${submitted} 个编号 · 跳过 ${skipped}${failures.length?' · 失败 '+failures.length:''}`);if(submitted)showJobs()
  }catch(error){toast(`已提交 ${submitted} 个编号；后续停止：${error.message}`)}finally{selectionBusy=false}
 }
 async function loadMovie(id){
  const result=await get('/api/library/movie?id='+encodeURIComponent(id));const movie=result.movie;library.review={...library.review,...result.review,files:{...library.review?.files,...result.review?.files}};const incoming=new Set(movie._versions.flatMap(version=>version._parts.map(part=>part.id)));for(const [key,cached]of details)if(key===movie.movie_key||cached._versions.some(version=>version._parts.some(part=>incoming.has(part.id))))details.delete(key);details.set(movie.movie_key,movie);while(details.size>20)details.delete(details.keys().next().value);
  library.movies=[...details.values()];library.videos=library.movies.flatMap(work=>work._versions.flatMap(version=>version._parts));const cachedIds=new Set(library.videos.map(part=>String(part.id)));library.review.files=Object.fromEntries(Object.entries(library.review.files||{}).filter(([id])=>cachedIds.has(id)));return movie;
 }
 async function openMovie(id,exact,show){const request=++movieEpoch;try{const movie=await loadMovie(id);returnCardKey='movie:'+movie.movie_key;if(request!==movieEpoch)return;show(id,exact)}catch(error){toast(error.message)}}
 async function relations(){const range=chosen.size?await members():null;if(range&&range.files>100)throw Error('关系编辑一次最多100个文件，请缩小选择范围');if(range){const works=[];for(let offset=0;offset<range.items.length;offset+=4)works.push(...await Promise.all(range.items.slice(offset,offset+4).map(item=>loadMovie(item.id))));if(works.reduce((n,work)=>n+work._versions.reduce((sum,version)=>sum+version._parts.length,0),0)>100)throw Error('所选作品含其它版本，合计超过100个文件，请缩小选择范围');library.movies=works;library.videos=works.flatMap(work=>work._versions.flatMap(version=>version._parts));chosenIds.clear();for(const item of range.items)chosenIds.add(item.id)}else if(selected)await loadMovie(selected.id)}
 async function random(){try{const result=await get('/api/library/random?snapshot='+snapshot);openVideo(result.id,true)}catch(error){toast(error.message)}}
 function openCollection(id){collectionReturn=captureView();const url=new URL(location.href);url.searchParams.set('collection',id);history.pushState(null,'',url);page=1;render()}
 function focusCard(key){document.querySelector('.movieCard[data-card-key="'+CSS.escape(key)+'"]')?.focus({preventScroll:true})}
 detail.addEventListener('close',()=>{if(returnCardKey)focusCard(returnCardKey)});
 function leaveCollection(){const previous=collectionReturn;const url=new URL(location.href);url.searchParams.delete('collection');history.pushState(null,'',url);collectionReturn=null;if(previous)applyView(previous);else{page=1;render()}}
 function renderCollectionHeader(){let header=$('pagedCollectionHeader');if(!header){header=document.createElement('section');header.id='pagedCollectionHeader';$('topPagination').before(header)}const info=data.collection_info;header.hidden=!collectionId();if(!collectionId())return;header.innerHTML=`<div class="facetHeader"><button onclick="serverBrowse.leaveCollection()">← 返回片库</button><h3>${esc(info?.title||'视频集')}</h3><span>当前命中 ${data.movies} / 全集 ${info?.total_count||0} 个条目</span><button onclick="resetFilters()">查看全集 · 清除筛选</button></div>`}
 function restoreCollection(state){const url=new URL(location.href);if(state?.collection)url.searchParams.set('collection',state.collection);else url.searchParams.delete('collection');history.replaceState(history.state,'',url);collectionReturn=state?.collectionReturn||null}
 let relationTimer,relationController;
 async function relationOptions(query=''){
  relationController?.abort();relationController=new AbortController();
  try{const result=await get('/api/library/relationship-options?q='+encodeURIComponent(query),relationController.signal);
   for(const [kind,map,id] of [['movies',relationMovieNames,'relationMovieOptions'],['versions',relationVersionNames,'relationVersionOptions']]){
    for(const option of result[kind])if(!map.has(option.key))map.set(option.key,option.label);
    const list=$(id);if(list)list.innerHTML=[...map.values()].map(label=>`<option value="${esc(label)}">`).join('');
   }
  }catch(error){if(error.name!=='AbortError')throw error}
 }
 function bindRelationSuggestions(){document.querySelectorAll('input[list="relationMovieOptions"],input[list="relationVersionOptions"]').forEach(input=>input.addEventListener('input',()=>{clearTimeout(relationTimer);relationTimer=setTimeout(()=>relationOptions(input.value).catch(error=>toast(error.message)),180)}))}
 async function ensureRelationOptions(){clearTimeout(relationTimer);for(const input of document.querySelectorAll('input[list="relationMovieOptions"],input[list="relationVersionOptions"]')){const map=input.list.id==='relationMovieOptions'?relationMovieNames:relationVersionNames;if(![...map.values()].includes(input.value.trim()))await relationOptions(input.value.trim())}}
 async function retryCode(code){try{const result=await get('/api/library/lookup?code='+encodeURIComponent(code));await submitIds([result.id])}catch(error){toast(error.message)}}
 let suggestTimer;
 async function suggest(id,kind){try{const result=await get('/api/library/facets?'+new URLSearchParams({kind,q:$(id).value}));$('suggest-'+id).innerHTML=result.items.map(item=>`<option value="${esc(item.name)}" label="${item.movies} 部作品">`).join('')}catch(error){}}
 function loadSuggestions(){for(const [id,kind]of [['tag','tags'],['excludeTag','tags'],['actor','actors'],['studio','studio']])if(!$('suggest-'+id).children.length)suggest(id,kind)}
 for(const [id,kind]of [['tag','tags'],['excludeTag','tags'],['actor','actors'],['studio','studio']]){
  const old=$(id),input=document.createElement('input');input.id=id;input.placeholder=old.options[0].textContent;input.setAttribute('list','suggest-'+id);input.onchange=old.onchange||(()=>{page=1;render()});input.oninput=()=>{clearTimeout(suggestTimer);suggestTimer=setTimeout(()=>suggest(id,kind),180)};old.replaceWith(input);input.insertAdjacentHTML('afterend',`<datalist id="suggest-${id}"></datalist>`);
 }
 return {render,refresh,error:showError,relationOptions,bindRelationSuggestions,ensureRelationOptions,retryCode,resetCollection(){const url=new URL(location.href);url.searchParams.delete('collection');history.replaceState(history.state,'',url);collectionReturn=null},loadMovie,openMovie,toggle,toggleKey,selectPage,clearSelection,scrape,random,relations,openCollection,leaveCollection,restoreCollection,collectionCard,get collectionId(){return collectionId()},get collectionReturn(){return collectionReturn},get total(){return total},get ready(){return !!data},cancel(){clearTimeout(timer);controller?.abort();busy(false);++epoch;++movieEpoch},redraw:()=>data&&draw(),remove(key){const item=chosen.get(key);chosen.delete(key);if(item)chosenIds.delete(item.id);draw()}};
})();window.serverBrowse=serverBrowse;
