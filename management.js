let managedLibraries=[],managerSection="sources",managerTaskTimer,managerNetworkTimer;
function showScrapeHub(){showTaskCenter()}
let nativeSiteSettings=[];
async function showSiteSettings(){try{managerSection='sources';nativeSiteSettings=(await(await managerFetch('/api/scrape-sites')).json()).items;openManager('内置站点设置',`<p class="muted">独立配置站点地址、超时、重试和登录 Cookie。Cookie 只保存在服务端，留空保留；不会回传到页面。访问验证仍需你在来源站点完成。</p><label>来源<select id="nativeSite" onchange="renderSiteSettings()">${nativeSiteSettings.map(s=>`<option value="${esc(s.id)}">${esc(s.id)}</option>`).join('')}</select></label><div id="nativeSiteForm"></div>`,`<button class="accent" onclick="saveSiteSettings()">保存站点设置</button>`);renderSiteSettings()}catch(e){if(e.name==='AbortError')return;toast(e.message)}}
function renderSiteSettings(){const s=nativeSiteSettings.find(s=>s.id===$('nativeSite').value);$('nativeSiteForm').innerHTML=`<label>自定义 HTTPS 地址（留空使用默认）<input id="siteBase" value="${esc(s.base_url)}" placeholder="https://来源站点"></label><div class="fields"><label>请求超时（秒）<input id="siteTimeout" type="number" min="5" max="60" value="${s.timeout}"></label><label>失败重试次数<input id="siteRetries" type="number" min="0" max="2" value="${s.retries}"></label></div><label>Cookie <small>${s.cookie_configured?'已配置 · 不显示原值':'尚未配置'}</small><textarea id="siteCookie" autocomplete="off" placeholder="留空保留已保存的 Cookie"></textarea></label><label style="flex-direction:row;align-items:center"><input type="checkbox" id="clearSiteCookie" style="width:auto"> 清除已保存 Cookie</label><p class="muted">自动刮削复用七天内的来源缓存；手动“重新刮削”会强制重新获取。修改站点配置后，该站旧缓存失效。</p>`}
async function saveSiteSettings(){const saveTicket=managerRouteEpoch;try{await api('/api/scrape-sites',{source:$('nativeSite').value,base_url:$('siteBase').value.trim(),timeout:Number($('siteTimeout').value),retries:Number($('siteRetries').value),cookie:$('siteCookie').value.trim(),clear_cookie:$('clearSiteCookie').checked});toast('站点设置已保存');if(saveTicket!==managerRouteEpoch)return;await showSiteSettings()}catch(e){if(e.name==='AbortError')return;toast(e.message)}}
async function showScrapeConnection(){const renderTicket=managerRouteEpoch;await refresh();if(renderTicket!==managerRouteEpoch)return;managerSection='sources';const cfg=library.settings;openManager('刮削连接',`<label>执行引擎<select id="hubEngine"><option value="builtin" ${cfg.scrape_engine==='builtin'?'selected':''}>Velvet 内置</option><option value="external" ${cfg.scrape_engine!=='builtin'?'selected':''}>Amane 完整引擎</option></select></label><label>内置代理<input id="hubProxy" value="${esc(cfg.scrape_proxy||'')}" placeholder="http://127.0.0.1:10808"></label><label>Amane 地址<input id="hubUrl" value="${esc(cfg.amane_url||'http://127.0.0.1:18000')}"></label><label>Amane Token<input type="password" autocomplete="new-password" id="hubToken" placeholder="留空保留现有 Token"></label><p class="muted">内置代理只用于 Velvet 的采集与头像下载。完整引擎使用 Amane 自己的网络设置。</p>`,`<button class="accent" onclick="saveHubConnection()">保存执行设置</button>`)}
async function saveHubConnection(){const saveTicket=managerRouteEpoch;try{const advanced=$('advancedOnline')?{online:$('advancedOnline').checked,provider:$('advancedProvider').value.trim(),api_key:$('advancedKey').value}:null;await api('/api/amane-settings',{engine:$('hubEngine').value,proxy:$('hubProxy').value.trim(),url:$('hubUrl').value.trim(),token:$('hubToken').value.trim(),aliases:library.settings.tag_aliases||{}});if(advanced)await api('/api/settings',advanced);toast('执行设置已保存');if(saveTicket!==managerRouteEpoch)return;showScrapeConnection()}catch(e){if(e.name==='AbortError')return;toast(e.message)}}
function closeManager(){return exitManager()}
function openManager(title,body,footer=''){renderManagerShell(title,body,footer)}
let sourceCatalog=null;
async function showScrapingOptions(force=false){managerSection="sources";
 try{
  const r=await managerFetch('/api/source-catalog'+(force?'?refresh=1':''));if(!r.ok)throw Error('来源目录需要新版后端，请重启服务');sourceCatalog=await r.json();
  const d=sourceCatalog,u=d.upstream;
  const kindNames={censored:'有码',uncensored:'无码',chinese:'中文',western:'欧美',fc2:'FC2',amateur:'素人',hentai:'动画'};
  openManager('刮削来源',`<div class="row"><span class="sourceTag">${d.engine==='builtin'?'当前：内置引擎':'当前：Amane 完整引擎'}</span><span class="muted">${u.connected?'本机 Amane '+esc(u.version)+' 已连接':esc(u.error)}</span></div><div class="sourceSummary"><div><b>${d.items.length}</b><small>来源目录</small></div><div><b>${d.items.filter(s=>s.native).length}</b><small>已移植，可独立执行</small></div><div><b>${d.items.filter(s=>s.amane_enabled).length}</b><small>本机路线启用来源</small></div></div><div class="row"><button class="accent" onclick="showNativeOptions()">配置内置来源</button><button onclick="showScrapeConnection()">执行引擎与连接</button><button onclick="showScrapingOptions(true)">刷新本机配置</button></div><p class="muted">内置来源直接由 Velvet 执行。其余来源通过本机 Amane 使用已有配置、凭据和采集能力；未复制账号信息。切换引擎前须完成或取消当前任务。</p><div style="overflow:auto"><table class="sourceTable"><thead><tr><th>来源</th><th>执行方式</th><th>当前启用</th><th class="sourceHost">类型路线 / 站点</th></tr></thead><tbody>${d.items.map(s=>`<tr><td><strong>${esc(s.id)}</strong></td><td><span class="sourceTag ${s.native?'':'remote'}">${s.native?'内置 + Amane':'经 Amane'}</span></td><td>${d.engine==='builtin'?(s.native&&s.native_enabled?'内置已启用':'未启用'):(s.amane_enabled?'路线已启用':'路线未启用')}</td><td class="sourceHost">${s.kinds.map(k=>esc(kindNames[k]||k)).join(' · ')||'—'}<small>${esc(s.url)}</small></td></tr>`).join('')}</tbody></table></div><h3>本机 Amane 的分类路线</h3><p class="muted">完整引擎按这些路线择源，保留它原有的字段优先级、语言和浏览器设置。此处只读取；更改完整引擎路线请到 Amane 配置。</p><dl class="routeList">${Object.entries(u.routes).map(([kind,sources])=>`<dt>${esc(kindNames[kind]||kind)}</dt><dd>${sources.map(esc).join(' → ')}</dd>`).join('')}</dl>`);
 }catch(e){if(e.name==='AbortError')return;toast(e.message)}
}
async function useAmaneSources(){try{
 await refresh();const cfg=library.settings;
 await api('/api/amane-settings',{engine:'external',url:cfg.amane_url||'http://127.0.0.1:18000',proxy:cfg.scrape_proxy||'',aliases:cfg.tag_aliases||{}});
 toast('已切换本机 Amane 完整引擎，沿用现有来源路线');await showScrapingOptions();
}catch(e){if(e.name==='AbortError')return;toast(e.message)}}
let managerTaskFilter='all';
async function showTaskCenter(){managerSection="tasks";
 openManager('任务',`<div class="row"><label style="margin:0;flex-direction:row;align-items:center"><input id="managerAuto" type="checkbox" style="width:auto" onchange="setManagerAuto(this.checked)"> 自动刮削</label><button onclick="syncManagerMetadata()">同步资料与图片</button><span id="managerTaskStatus" class="muted"></span></div><div class="taskFilters">${[['all','全部'],['queued','排队中'],['running','运行中'],['done','已完成'],['failed','失败']].map(([value,label])=>`<button class="${value===managerTaskFilter?'active':''}" onclick="managerTaskFilter='${value}';showTaskCenter()">${label}</button>`).join('')}</div><div id="managerTasks" aria-live="polite"></div>`);
 await renderManagerTasks();
}
async function renderManagerTasks(){clearTimeout(managerTaskTimer);
 if($('managerView').hidden||!$('managerTasks'))return;
 try{
  const r=await managerFetch('/api/amane-jobs');if(!r.ok)throw Error('任务读取失败');const d=await r.json();
  if($('managerView').hidden||!$('managerTasks'))return;
  $('managerAuto').checked=d.auto.enabled;$('managerTaskStatus').textContent=d.auto.message;
  const jobs=d.jobs.filter(j=>managerTaskFilter==='all'||j.status===managerTaskFilter);
  $('managerTasks').innerHTML=`<p class="muted">最近 ${d.jobs.length} 条任务中匹配 ${jobs.length} 条 · ${esc(d.status.message||'')}</p><div style="overflow:auto"><table class="sourceTable"><thead><tr><th>任务 / 编号</th><th>状态</th><th>更新时间</th><th>操作</th></tr></thead><tbody>${jobs.map(j=>`<tr><td><strong>${esc(j.code)}</strong><small>#${j.id} · 引擎任务 ${j.remote_id??'待确认'}</small>${j.error?'<small>'+esc(j.error)+'</small>':''}${j.details?'<details><summary>来源结果</summary><small>'+esc(readableReasons(j.details))+'</small></details>':''}</td><td><span class="taskPill ${esc(j.status)}">${statusNames[j.status]||esc(j.status)}</span></td><td>${j.updated?new Date(j.updated*1000).toLocaleString():'—'}</td><td>${['queued','running'].includes(j.status)?`<button onclick="cancelManagerTask(${j.id})">取消</button>`:['submitting','cancelling'].includes(j.status)?'<small>等待当前操作</small>':`<button data-code="${esc(j.code)}" onclick="retryManagerTask(this.dataset.code)">重新刮削</button>`}</td></tr>`).join('')}</tbody></table></div>`;
  managerTaskTimer=setTimeout(renderManagerTasks,3000);
 }catch(e){if(e.name==='AbortError')return;if($('managerTasks'))$('managerTasks').textContent=e.message;}
}
async function setManagerAuto(enabled){try{await api('/api/auto-scrape',{enabled});toast(enabled?'自动刮削已开启':'自动刮削已暂停')}catch(e){if(e.name==='AbortError')return;toast(e.message)}}
async function cancelManagerTask(id){try{await api('/api/task-cancel',{kind:'scrape',id});await renderManagerTasks()}catch(e){if(e.name==='AbortError')return;toast(e.message)}}
async function retryManagerTask(code){try{await refresh();const video=library.videos.find(v=>(v.code||'').toUpperCase()===code.toUpperCase());if(!video)throw Error('未找到对应文件');await api('/api/amane-submit',{ids:[video.id]});await renderManagerTasks()}catch(e){if(e.name==='AbortError')return;toast(e.message)}}
async function syncManagerMetadata(){try{toast('正在同步资料与图片');await api('/api/amane-sync',{});await refresh();if($('managerTasks'))await renderManagerTasks()}catch(e){if(e.name==='AbortError')return;toast(e.message)}}
const networkReason={network:'连接失败，请检查代理和网络',timeout:'请求超时',dns:'域名解析失败',tls:'TLS 证书或握手失败',not_found:'检测入口不存在',rate_limited:'来源限流',http_error:'来源拒绝请求',server_error:'来源服务异常',cloudflare_challenge:'需要访问验证',cloudflare_blocked:'访问被拦截',ip_banned:'IP 被限制',geo_restricted:'地区限制',age_verification:'年龄确认页面',empty_response:'空响应',unexpected_response:'响应格式异常',unexpected:'检测异常',unknown_source:'未知来源',disabled:'来源已禁用',service_unavailable:'外部服务不可用，请检查地址和 Token'};
async function showNetwork(){managerSection="network";
 openManager('网络检测',`<p class="muted">使用已保存的引擎、来源与代理配置。检测不会提交刮削任务；连通不代表每个编号都能匹配。</p><div class="row"><button class="accent" id="networkRun" onclick="runNetwork()">检测当前来源</button><button onclick="showScrapingOptions()">来源与字段优先级</button></div><div id="networkResults" aria-live="polite"></div>`);
 await renderNetwork();
}
async function renderNetwork(){clearTimeout(managerNetworkTimer);
 if(!$('networkResults')||$('managerView').hidden)return;
 try{
  const r=await managerFetch('/api/network');if(!r.ok)throw Error('服务未更新或无法访问');const d=await r.json();
  if(!$('networkResults')||$('managerView').hidden)return;
  $('networkRun').disabled=d.running;
  $('networkResults').innerHTML=`<p class="muted">${d.engine?`检测模式：${d.engine==='builtin'?'内置引擎':'外部服务'} · ${d.proxy_enabled?'已使用代理':'直连或外部服务自身网络配置'}`:'尚未检测'}${d.checked_at?' · '+new Date(d.checked_at*1000).toLocaleString():''}</p>`+d.items.map(item=>`<div class="folder"><div><strong>${esc(item.source_id)}</strong><p>${({ok:'可访问',failed:'不可用于当前访问',skipped:'未检测',pending:'检测中…'})[item.status]||esc(item.status)}${item.elapsed_ms!=null?' · '+item.elapsed_ms+' ms':''}${item.http_status?' · HTTP '+item.http_status:''}</p><small>${esc(networkReason[item.reason]||item.reason||'')} ${esc(item.url||'')}</small></div><button data-source="${esc(item.source_id)}" onclick="runNetwork([this.dataset.source])" ${d.running?'disabled':''}>重新检测</button></div>`).join('');
  if(d.running)managerNetworkTimer=setTimeout(renderNetwork,1000);
 }catch(e){if(e.name==='AbortError')return;if($('networkResults'))$('networkResults').textContent=e.message;}
}
async function runNetwork(source_ids){try{await api('/api/network/check',source_ids?{source_ids}:{});await renderNetwork()}catch(e){if(e.name==='AbortError')return;toast(e.message)}}
let nativeSourceOrder=[],nativeSourceEnabled=new Set(),nativeFieldPriority={};
const nativeFields={title:'标题',actors:'演员',tags:'分类标签',studio:'厂商',release:'发行日期',plot:'简介',poster_urls:'海报',thumb_urls:'封面',runtime:'片长',series:'系列',directors:'导演',publisher:'发行商'};
async function showNativeOptions(){
 const renderTicket=managerRouteEpoch;managerSection='sources';await refresh();if(renderTicket!==managerRouteEpoch)return;const c=library.settings;
 const available=sourceCatalog?sourceCatalog.items.filter(s=>s.native).map(s=>s.id):['javbus','jav321','fc2','fc2club','fc2ppvdb','freejavbt','airav','xcity'];
 nativeSourceOrder=[...new Set([...(c.scrape_sources||[]),...available])].filter(s=>available.includes(s));nativeSourceEnabled=new Set(c.scrape_sources||[]);nativeFieldPriority=c.scrape_field_priority||{};
 openManager('内置来源配置',`<h3>来源与默认顺序</h3><p class="muted">启用需要的来源，使用上下按钮调整默认优先级。按编号类型选择适用来源；关闭的来源不会被字段择优重新启用。</p><div id="nativeSources"></div><h3 style="margin-top:32px">字段择优</h3><p class="muted">指定某个字段的首选来源；没有值时沿默认顺序继续补全。</p><div class="fields">${Object.entries(nativeFields).map(([key,label])=>`<label>${label}<select data-field-source="${key}" aria-label="${label}首选来源"></select></label>`).join('')}</div>`,`<button class="accent" onclick="saveScrapingOptions()">保存配置</button>`);
 renderNativeSources();refreshNativeFieldChoices(true);
}
function renderNativeSources(){
 $('nativeSources').innerHTML=nativeSourceOrder.map((source,index)=>`<div class="nativeSourceRow"><label><input type="checkbox" ${nativeSourceEnabled.has(source)?'checked':''} data-source="${esc(source)}" onchange="toggleNativeSource(this.dataset.source,this.checked)"><strong>${esc(source)}</strong></label><div class="row"><span class="muted">${index+1}</span><button onclick="moveNativeSource(${index},-1)" aria-label="上移 ${esc(source)}" ${index===0?'disabled':''}>↑</button><button onclick="moveNativeSource(${index},1)" aria-label="下移 ${esc(source)}" ${index===nativeSourceOrder.length-1?'disabled':''}>↓</button></div></div>`).join('');
}
function toggleNativeSource(source,enabled){if(enabled)nativeSourceEnabled.add(source);else nativeSourceEnabled.delete(source);refreshNativeFieldChoices(false)}
function moveNativeSource(index,delta){const next=index+delta;[nativeSourceOrder[index],nativeSourceOrder[next]]=[nativeSourceOrder[next],nativeSourceOrder[index]];renderNativeSources();refreshNativeFieldChoices(false)}
function refreshNativeFieldChoices(initial){for(const select of document.querySelectorAll('[data-field-source]')){const current=initial?(nativeFieldPriority[select.dataset.fieldSource]||[])[0]||'':select.value;select.innerHTML='<option value="">跟随默认顺序</option>'+nativeSourceOrder.filter(s=>nativeSourceEnabled.has(s)).map(s=>`<option value="${esc(s)}">${esc(s)}</option>`).join('');select.value=nativeSourceEnabled.has(current)?current:''}}
async function saveScrapingOptions(){const saveTicket=managerRouteEpoch;try{
 const sources=nativeSourceOrder.filter(s=>nativeSourceEnabled.has(s)),field_priority={};
 for(const select of document.querySelectorAll('[data-field-source]')){if(select.value)field_priority[select.dataset.fieldSource]=[...new Set([select.value,...(nativeFieldPriority[select.dataset.fieldSource]||[]).filter(s=>sources.includes(s))])];}
 await api('/api/scraping-options',{sources,field_priority});toast('刮削配置已保存');if(saveTicket!==managerRouteEpoch)return;await showNativeOptions();
}catch(e){if(e.name==='AbortError')return;toast(e.message)}}
async function showMediaLibraries(){managerSection="libraries";
 try{
  const r=await managerFetch('/api/libraries');if(!r.ok)throw Error('媒体库接口不可用，请确认已重启新版服务');managedLibraries=(await r.json()).items;
  openManager('媒体库',`<p class="muted">每个媒体库对应服务端的一个目录。关闭自动化仍可手动扫描；移除媒体库只停止监控，保留现有索引与原文件。</p><button class="accent" onclick="editMediaLibrary(-1)">添加媒体库</button><div style="margin-top:20px">`+managedLibraries.map((item,i)=>`<div class="folder"><div><strong>${esc(item.name)}</strong> <small>${item.online?'在线':'离线 · 保留记录'}</small><p class="path">${esc(item.path)}</p><p>${item.file_count} 个文件 · 已匹配 ${item.matched_count} · 缺失 ${item.missing_count} · ${({off:'自动化关闭',monitor:'仅扫描',scrape:'扫描并自动刮削'})[item.automation]}</p><small>${item.last_scan?'上次扫描 '+new Date(item.last_scan*1000).toLocaleString():'尚未扫描'} ${esc(item.last_error||'')}</small></div><div class="row"><button onclick="showLibraryFiles(${i})">查看文件</button><button onclick="scanMediaLibrary(${i})" ${!item.online?'disabled':''}>扫描本库</button><button onclick="editMediaLibrary(${i})">配置</button><button onclick="removeMediaLibrary(${i})">停止监控</button></div></div>`).join('')+'</div>');
 }catch(e){if(e.name==='AbortError')return;toast(e.message)}
}
function editMediaLibrary(index){
 const item=index<0?{name:'',path:'',automation:'scrape',exclude_patterns:[],min_size:0,settle_seconds:30}:managedLibraries[index];
 openManager(index<0?'添加媒体库':'配置媒体库',`<label>名称<input id="mlName" value="${esc(item.name)}"></label><label>服务端目录<input id="mlPath" value="${esc(item.path)}" ${index>=0?'readonly':''}></label>${index<0?'<button onclick="pickMediaLibrary()">选择目录</button>':''}<label>自动化<select id="mlAutomation">${[['off','关闭'],['monitor','仅扫描'],['scrape','扫描并刮削']].map(([value,label])=>`<option value="${value}" ${item.automation===value?'selected':''}>${label}</option>`).join('')}</select></label><label>排除规则（每行一条相对路径通配符）<textarea id="mlExclude">${esc(item.exclude_patterns.join('\n'))}</textarea></label><p class="muted">例如 sample*、*/sample/*；规则只影响扫描和自动刮削，不删除已存在的索引。</p><div class="fields"><label>最小视频大小（MiB）<input id="mlMinSize" type="number" min="0" step="any" value="${item.min_size/1048576}"></label><label>距最后修改的等待时间（秒）<input id="mlSettle" type="number" min="0" max="86400" value="${item.settle_seconds}"></label></div><p class="muted">等待时间降低正在拷贝的文件被提前处理的概率。自动化关闭不影响手动扫描和刮削。</p>`,`<button onclick="showMediaLibraries()">返回</button><button class="accent" onclick="saveMediaLibrary()">保存</button>`);
}
async function pickMediaLibrary(){try{const r=await api('/api/pick-folder',{});if(r.path){$('mlPath').value=r.path;managerDirty=true}}catch(e){if(e.name==='AbortError')return;toast(e.message)}}
async function saveMediaLibrary(){const saveTicket=managerRouteEpoch;try{await api('/api/libraries',{name:$('mlName').value,path:$('mlPath').value,automation:$('mlAutomation').value,exclude_patterns:$('mlExclude').value.split('\n').map(s=>s.trim()).filter(Boolean),min_size:Math.round(Number($('mlMinSize').value)*1048576),settle_seconds:Number($('mlSettle').value)});toast('媒体库配置已保存');if(saveTicket!==managerRouteEpoch)return;await showMediaLibraries()}catch(e){if(e.name==='AbortError')return;toast(e.message)}}
async function scanMediaLibrary(index){try{const r=await api('/api/library-scan',{path:managedLibraries[index].path});toast(r.busy?'已有扫描进行中':'已开始扫描此媒体库')}catch(e){if(e.name==='AbortError')return;toast(e.message)}}
async function removeMediaLibrary(index){try{await api('/api/remove-folder',{path:managedLibraries[index].path});toast('已停止监控，索引和原文件保留');await showMediaLibraries()}catch(e){if(e.name==='AbortError')return;toast(e.message)}}
let libraryFileView={index:0,offset:0};
async function showLibraryFiles(index){
 libraryFileView={index,offset:0};
 openManager(managedLibraries[index].name,`<div class="row"><select id="mlFileStatus" onchange="loadLibraryFiles(0)"><option value="all">全部文件</option><option value="matched">已匹配资料</option><option value="pending">待匹配资料</option><option value="missing">文件缺失</option><option value="error">处理提示</option></select><input id="mlFileSearch" placeholder="搜索标题或编号" onkeydown="if(event.key==='Enter')loadLibraryFiles(0)"><button onclick="loadLibraryFiles(0)">搜索</button></div><div id="mlFiles" aria-live="polite"></div>`);
 await loadLibraryFiles(0);
}
async function loadLibraryFiles(offset){
 try{
  const index=libraryFileView.index;
  const params=new URLSearchParams({path:managedLibraries[index].path,status:$('mlFileStatus').value,search:$('mlFileSearch').value,offset,limit:30});
  const r=await managerFetch('/api/library-files?'+params);if(!r.ok)throw Error('读取媒体库文件失败');const d=await r.json();
  if(!$('mlFiles')||libraryFileView.index!==index)return;
  libraryFileView.offset=offset;
  $('mlFiles').innerHTML=`<p class="muted">${d.total} 个文件 · 当前 ${d.items.length} 个</p>`+d.items.map(item=>`<div class="folder"><div><strong>${esc(item.title)}</strong><p>${esc(item.code||'编号待确认')} · ${item.missing?'文件缺失':item.amane_id?'已匹配资料':'待匹配资料'} · ${item.height?item.height+'p':'分辨率待提取'}</p><small>${esc(item.path)}</small>${item.error?'<p class="notice">'+esc(item.error)+'</p>':''}</div><button onclick="openVideo(${item.id},true)">详情</button></div>`).join('')+`<div class="row"><button onclick="loadLibraryFiles(${Math.max(0,offset-30)})" ${offset===0?'disabled':''}>上一页</button><button onclick="loadLibraryFiles(${offset+30})" ${offset+30>=d.total?'disabled':''}>下一页</button></div>`;
 }catch(e){if(e.name==='AbortError')return;toast(e.message)}
}
