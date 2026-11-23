/* Keep the existing playback/edit handlers; make reading the default detail view. */
let filmArtwork=[],artworkFilter='all',detailGeneration=0,artworkPollTimer,artworkPollRounds=0;
const detailBody=detail.querySelector('.dialogBody');
const originalFigure=$('coverPreview').parentElement;
const playbackActions=detailBody.querySelector('.playback-actions');
const playbackHint=$('player').nextElementSibling;
playbackHint.id='playbackFeedback';playbackHint.setAttribute('role','status');
const hero=document.createElement('section');hero.className='filmHero';hero.innerHTML='<div class="filmPoster"></div><div class="filmSummary"><div id="filmSummary"></div></div>';
hero.querySelector('.filmPoster').append($('coverPreview'));originalFigure.remove();hero.querySelector('.filmSummary').append(playbackActions,$('actorLinks'));
const playbackSection=document.createElement('section');playbackSection.id='filmPlayback';playbackSection.hidden=true;playbackSection.append($('player'),playbackHint);
playbackSection.insertAdjacentHTML('beforeend','<div id="playbackRecovery" class="row"></div><div class="row playbackTracks"><div id="playbackSubtitles"></div><div id="playbackAudio"></div></div>');
playbackActions.after(detail.querySelector('.dialogTools'));
const gallerySection=document.createElement('section');gallerySection.className='filmGallery';gallerySection.innerHTML='<div class="filmSectionHeader"><div><h3>剧照与预览</h3><small id="filmGalleryStatus" class="muted">正在加载图片…</small></div><button onclick="retryFilmArtwork()">获取 / 重试剧照</button></div><nav id="filmGalleryFilters" class="filmGalleryFilters"></nav>';gallerySection.append($('shots'));
const editor=document.createElement('details');editor.id='filmEditor';editor.className='filmEditor filmDisclosure';editor.innerHTML='<summary>编辑资料与文件信息</summary><div id="filmEditorBody"></div>';
while(detailBody.firstChild)editor.querySelector('#filmEditorBody').append(detailBody.firstChild);
editor.querySelector('#filmEditorBody').append($('autoTagDetail'));
detailBody.append(hero,playbackSection,gallerySection,editor);
const saveButton=detail.querySelector('button[onclick="saveVideo()"]');saveButton.hidden=true;
const forgetButton=detail.querySelector('button[onclick="forget()"]');editor.querySelector('#filmEditorBody').append(forgetButton);
const identification=document.createElement('p');
identification.className='muted';
identification.setAttribute('role','status');
$('fields').before(identification);
let identificationRequest;
editor.addEventListener('toggle',()=>{
 saveButton.hidden=!editor.open;
 if(editor.open&&selected?.id)loadIdentification();
 else identificationRequest?.abort();
});
detail.addEventListener('close',()=>identificationRequest?.abort());
async function loadIdentification(){
 identificationRequest?.abort();
 const request=identificationRequest=new AbortController();
 const id=selected.id,generation=detailGeneration;
 identification.textContent='正在读取编号识别依据…';
 try{
  const response=await fetch('/api/identification/'+id,{signal:request.signal});
  if(!response.ok)throw Error('读取失败');
  const result=await response.json();
  if(request.signal.aborted||generation!==detailGeneration||!editor.open)return;
  const {filename,directory,nfo}=result.candidates;
  identification.textContent=`当前编号：${result.code||'未识别'}${result.locked?'（已锁定）':''} · 文件名：${filename||'无'} · 父目录：${directory||'无'} · NFO：${nfo||'无'}${result.warning?' · '+result.warning:''}。修改编号并保存后会锁定。`;
 }catch(error){if(!request.signal.aborted&&generation===detailGeneration)identification.textContent='暂时无法读取编号识别依据，仍可手动填写编号。'}
}
const imageDialog=document.createElement('dialog');imageDialog.id='artworkLightbox';imageDialog.setAttribute('aria-labelledby','artworkLightboxTitle');imageDialog.innerHTML=workflowHeader('artworkLightboxTitle','图片预览','closeArtworkLightbox()')+'<div class="dialogBody"><img id="artworkLightboxImage" alt=""></div><div class="lightboxNav"><button onclick="moveArtwork(-1)" aria-label="上一张">← 上一张</button><button onclick="moveArtwork(1)" aria-label="下一张">下一张 →</button></div>';document.body.append(imageDialog);
let lightboxIndex=0,lightboxOpener;
imageDialog.addEventListener('close',()=>{if(lightboxOpener?.isConnected)lightboxOpener.focus({preventScroll:true})});
function closeArtworkLightbox(){imageDialog.close()}
function openArtwork(index){lightboxIndex=index;const item=filmArtwork[index];if(!item)return;$('artworkLightboxImage').src=item.url;$('artworkLightboxImage').alt=item.label;$('artworkLightboxTitle').textContent=item.label+' · '+(index+1)+' / '+filmArtwork.length;if(!imageDialog.open){lightboxOpener=document.activeElement;imageDialog.showModal()}}
function moveArtwork(delta){if(filmArtwork.length)openArtwork((lightboxIndex+delta+filmArtwork.length)%filmArtwork.length)}
imageDialog.addEventListener('keydown',event=>{if(event.key==='ArrowLeft'){event.preventDefault();moveArtwork(-1)}if(event.key==='ArrowRight'){event.preventDefault();moveArtwork(1)}});
const previousOpenVideo=openVideo;
openVideo=async function(id,exact=false){if(window.serverBrowse)return serverBrowse.openMovie(id,exact,showMovieDetail);return showMovieDetail(id,exact)};
function showMovieDetail(id,exact=false){
 FilmPlayback.reset();identificationRequest?.abort();identification.textContent='';previousOpenVideo(id,exact,false);clearTimeout(artworkPollTimer);artworkPollRounds=0;const generation=++detailGeneration;editor.open=false;saveButton.hidden=true;saveButton.disabled=false;playbackSection.hidden=true;artworkFilter='all';filmArtwork=[];
 const v=selected;
 const external=$("externalPlayback");if(external)external.hidden=Boolean(library.server_mode);
 $('coverPreview').style.display='block';$('coverPreview').dataset.fallback='0';$('coverPreview').src='/thumb/'+v.id+'-poster.jpg';$('coverPreview').onerror=function(){if(this.dataset.fallback==='0'){this.dataset.fallback='1';this.src='/thumb/'+v.id+'-cover.jpg'}else this.style.display='none'};
 renderSelectedFilmSummary();
 $('shots').innerHTML='<div class="galleryEmpty">正在读取剧照与本地截图…</div>';$('filmGalleryStatus').textContent='正在加载';$('filmGalleryFilters').innerHTML='';
 const movie=(library.movies||[]).find(m=>m._versions.some(version=>version._parts.some(part=>part.id===v.id)));
 detail.querySelector('.dialogTools').hidden=!(movie?._versions.length>1||movie?._versions.some(version=>version._parts.length>1));
 loadFilmArtwork(v.id,generation);
};
async function loadFilmArtwork(id,generation){
 clearTimeout(artworkPollTimer);if(!detail.open||generation!==detailGeneration)return;
 try{const response=await fetch('/api/video-artwork/'+id);if(!response.ok)throw Error();const data=await response.json();if(generation!==detailGeneration||selected.id!==id)return;
 filmArtwork=data.items;$('filmGalleryStatus').textContent=`${data.stills} 张剧照 · ${data.screenshots} 张本地截图${data.pending?' · '+data.pending+' 张待下载':''}${data.failed?' · '+data.failed+' 张失败，可重试':''}`;renderFilmArtwork();if(data.pending&&artworkPollRounds++<30)artworkPollTimer=setTimeout(()=>loadFilmArtwork(id,generation),4000);
 }catch(e){if(generation!==detailGeneration)return;filmArtwork=Array.from({length:6},(_,i)=>({url:'/thumb/'+id+(i?'-'+i:'')+'.jpg',kind:'screenshot',label:'本地截图 '+(i+1)}));$('filmGalleryStatus').textContent='剧照接口暂不可用，显示本地截图';renderFilmArtwork()}
}
function renderFilmArtwork(){
 $('filmGalleryFilters').innerHTML=[['all','全部'],['still','剧照'],['screenshot','本地截图']].map(([kind,label])=>`<button class="${artworkFilter===kind?'active':''}" onclick="artworkFilter='${kind}';renderFilmArtwork()">${label}</button>`).join('');
 $('shots').innerHTML=filmArtwork.filter(item=>artworkFilter==='all'||item.kind===artworkFilter).map(item=>`<figure><button onclick="openArtwork(${filmArtwork.indexOf(item)})" aria-label="放大${esc(item.label)}"><img src="${esc(item.url)}" loading="lazy" alt="${esc(item.label)}" onerror="this.closest('figure').hidden=true"></button><figcaption>${esc(item.label)}</figcaption></figure>`).join('')||'<div class="galleryEmpty">暂无这类图片。刮削有图片的来源或安装 FFmpeg 后扫描，可补充剧照与本地截图。</div>';
}
const previousInternalPlay=internalPlay;
function fitPlayerToDetail(){
 if(!detail.open||playbackSection.hidden)return;
 const body=getComputedStyle(detailBody),hint=getComputedStyle(playbackHint),number=value=>parseFloat(value)||0;
 const available=detailBody.clientHeight-number(body.paddingTop)-number(body.paddingBottom)-playbackHint.offsetHeight-number(hint.marginTop)-number(hint.marginBottom)-24;
 const maximum=Math.max(72,Math.floor(available))+'px';if($('player').style.maxHeight!==maximum)$('player').style.maxHeight=maximum;
}
internalPlay=function(){playbackSection.hidden=false;fitPlayerToDetail();previousInternalPlay();playbackSection.scrollIntoView({behavior:'smooth',block:'start'})};
if(typeof ResizeObserver==='function')new ResizeObserver(fitPlayerToDetail).observe(detailBody);else window.addEventListener('resize',fitPlayerToDetail);
async function retryFilmArtwork(){try{await api('/api/artwork-retry',{id:selected.id});toast('已加入剧照下载队列；不重新刮削文字资料');artworkPollRounds=0;loadFilmArtwork(selected.id,detailGeneration)}catch(e){toast(e.message)}}

function editFilmCategories(){
 editor.open=true;
 $('edit_category_tags').focus();
}

function renderSelectedFilmSummary(){
 const v=selected,source=v.source_summary||{};
 const categories=v.catalog_tags||[...new Set(String(v.category_tags||'').split(/[,，]/).map(tag=>tag.trim()).filter(Boolean))];
 const score=source.score
  ?`<span class="filmScore">★ ${esc(source.score)}${source.score_max?` / ${esc(source.score_max)}`:''} <small>${esc(source.score_source?.toLowerCase()==='javdb'?'JavDB 评分':source.score_source?source.score_source+' 评分':'来源评分')}</small></span>`
  :v.rating?`<span>★ ${esc(v.rating)} <small>个人评分</small></span>`:'';
 const badges=[['国产',v.domestic,v.domestic_reason],['素人',v.amateur,v.amateur_reason]].filter(([,shown])=>shown);
 const facts=[v.date,source.runtime?`${source.runtime} 分钟`:v.duration>0?duration(v.duration):'',v.height?v.resolution_label:'',v.vr?'VR':'',v.censorship_label==='未知'?'':v.censorship_label].filter(Boolean);
 const credits=[['片商',v.studio],['发行商',source.publisher!==v.studio?source.publisher:''],['系列',source.series],['导演',source.directors?.join('、')]].filter(([,value])=>value);
 $('filmSummary').innerHTML=`${v.code?`<div class="filmEyebrow">${esc(v.code)}</div>`:''}
  <h2>${esc(v.title)}</h2>
  ${score||facts.length||badges.length?`<div class="filmFacts">${score}${facts.map(value=>`<span>${esc(value)}</span>`).join('')}${badges.map(([label,,reason])=>`<span title="${esc(reason)}">${label}</span>`).join('')}</div>`:''}
  <div class="filmCategories" aria-label="类别标签">${categories.map(tag=>`<button type="button" data-tag="${esc(tag)}" onclick="openDetailTag(this.dataset.tag)">${esc(tag)}</button>`).join('')}<button type="button" onclick="editFilmCategories()">编辑类别标签</button></div>
  ${credits.length?`<dl class="filmCredits">${credits.map(([label,value])=>`<div><dt>${label}</dt><dd>${esc(value)}</dd></div>`).join('')}</dl>`:''}
  <details class="filmRecognition"><summary>自动识别依据${v.recognition&&Object.values(v.recognition).some(mark=>mark.conflict)?' · 有冲突待确认':''}</summary>${Object.entries(v.recognition||{}).map(([key,mark])=>`<p><strong>${esc(({censorship:'有码／无码',domestic:'国产',amateur:'素人'})[key])}</strong> · ${esc(mark.reason)}${mark.evidence?.length?`<small>${mark.evidence.map(item=>esc(item.source)+' '+esc(item.labels.join('、'))).join(' · ')}</small>`:''}</p>`).join('')}<button type="button" onclick="editor.open=true;$('editCensor').focus()">修正识别结果</button></details>
  ${v.plot?`<p class="filmPlot">${esc(v.plot)}</p>`:''}`;
}



const physicalFileButton=document.createElement('button');physicalFileButton.id='filmPhysicalFiles';physicalFileButton.textContent='物理文件与副本';physicalFileButton.onclick=()=>DiskFileView.fromVideo(selected.id);playbackActions.append(physicalFileButton);
