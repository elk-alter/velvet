let managedLibraries=[];
const networkReason={network:'连接失败，请检查代理和网络',timeout:'请求超时',dns:'域名解析失败',tls:'TLS 证书或握手失败',not_found:'检测入口不存在',rate_limited:'来源限流',http_error:'来源拒绝请求',server_error:'来源服务异常',cloudflare_challenge:'需要访问验证',cloudflare_blocked:'访问被拦截',ip_banned:'IP 被限制',geo_restricted:'地区限制',age_verification:'年龄确认页面',empty_response:'空响应',unexpected_response:'响应格式异常',unexpected:'检测异常',unknown_source:'未知来源',disabled:'来源已禁用',service_unavailable:'外部服务不可用，请检查地址和 Token'};
async function showNetwork(){
 openTool('网络检测',`<p class="muted">使用已保存的引擎、来源与代理配置。检测不会提交刮削任务；连通不代表每个编号都能匹配。</p><div class="row"><button class="accent" id="networkRun" onclick="runNetwork()">检测当前来源</button><button onclick="showScrapingOptions()">来源与字段优先级</button></div><div id="networkResults" aria-live="polite"></div>`);
 await renderNetwork();
}
async function renderNetwork(){
 if(!$('networkResults'))return;
 try{
  const r=await fetch('/api/network');if(!r.ok)throw Error('服务未更新或无法访问');const d=await r.json();
  if(!$('networkResults'))return;
  $('networkRun').disabled=d.running;
  $('networkResults').innerHTML=`<p class="muted">${d.engine?`检测模式：${d.engine==='builtin'?'内置引擎':'外部服务'} · ${d.proxy_enabled?'已使用代理':'直连或外部服务自身网络配置'}`:'尚未检测'}${d.checked_at?' · '+new Date(d.checked_at*1000).toLocaleString():''}</p>`+d.items.map(item=>`<div class="folder"><div><strong>${esc(item.source_id)}</strong><p>${({ok:'可访问',failed:'不可用于当前访问',skipped:'未检测',pending:'检测中…'})[item.status]||esc(item.status)}${item.elapsed_ms!=null?' · '+item.elapsed_ms+' ms':''}${item.http_status?' · HTTP '+item.http_status:''}</p><small>${esc(networkReason[item.reason]||item.reason||'')} ${esc(item.url||'')}</small></div><button data-source="${esc(item.source_id)}" onclick="runNetwork([this.dataset.source])" ${d.running?'disabled':''}>重新检测</button></div>`).join('');
  if(d.running)setTimeout(renderNetwork,1000);
 }catch(e){if($('networkResults'))$('networkResults').textContent=e.message;}
}
async function runNetwork(source_ids){try{await api('/api/network/check',source_ids?{source_ids}:{});await renderNetwork()}catch(e){toast(e.message)}}
async function showScrapingOptions(){
 await refresh();const c=library.settings;
 openTool('刮削来源与字段优先级',`<p class="muted">来源顺序决定默认取值；同字段按指定来源优先，空值再向后补全。禁用来源也会从网络检测范围中移除。配置对下一次执行的任务生效。</p><label>启用来源（从上到下表示默认优先级）<textarea id="scrapeSources">${esc((c.scrape_sources||['javbus','jav321','fc2']).join('\n'))}</textarea></label><p class="muted">当前支持 javbus、jav321、fc2；FC2 编号使用 fc2，其余编号使用另外两个来源。</p><label>字段优先级（JSON）<textarea id="scrapePriorities">${esc(JSON.stringify(c.scrape_field_priority||{},null,2))}</textarea></label><p class="muted">例如 {"title":["javbus"],"plot":["jav321"]}。只能指定已启用来源；图片字段使用 poster_urls、thumb_urls。</p>`,`<button onclick="showNetwork()">网络检测</button><button class="accent" onclick="saveScrapingOptions()">保存</button>`);
}
async function saveScrapingOptions(){try{const sources=$('scrapeSources').value.split(/[\n,]/).map(s=>s.trim()).filter(Boolean),field_priority=JSON.parse($('scrapePriorities').value);await api('/api/scraping-options',{sources,field_priority});toast('刮削配置已保存');await showScrapingOptions()}catch(e){toast(e.message)}}
async function showMediaLibraries(){
 try{
  const r=await fetch('/api/libraries');if(!r.ok)throw Error('媒体库接口不可用，请确认已重启新版服务');managedLibraries=(await r.json()).items;
  openTool('媒体库',`<p class="muted">每个媒体库对应服务端的一个目录。关闭自动化仍可手动扫描；移除媒体库只停止监控，保留现有索引与原文件。</p><button class="accent" onclick="editMediaLibrary(-1)">添加媒体库</button><div style="margin-top:20px">`+managedLibraries.map((item,i)=>`<div class="folder"><div><strong>${esc(item.name)}</strong> <small>${item.online?'在线':'离线 · 保留记录'}</small><p class="path">${esc(item.path)}</p><p>${item.file_count} 个文件 · 已匹配 ${item.matched_count} · 缺失 ${item.missing_count} · ${({off:'自动化关闭',monitor:'仅扫描',scrape:'扫描并自动刮削'})[item.automation]}</p><small>${item.last_scan?'上次扫描 '+new Date(item.last_scan*1000).toLocaleString():'尚未扫描'} ${esc(item.last_error||'')}</small></div><div class="row"><button onclick="showLibraryFiles(${i})">查看文件</button><button onclick="scanMediaLibrary(${i})" ${!item.online?'disabled':''}>扫描本库</button><button onclick="editMediaLibrary(${i})">配置</button><button onclick="removeMediaLibrary(${i})">停止监控</button></div></div>`).join('')+'</div>');
 }catch(e){toast(e.message)}
}
function editMediaLibrary(index){
 const item=index<0?{name:'',path:'',automation:'scrape',exclude_patterns:[],min_size:0,settle_seconds:30}:managedLibraries[index];
 openTool(index<0?'添加媒体库':'配置媒体库',`<label>名称<input id="mlName" value="${esc(item.name)}"></label><label>服务端目录<input id="mlPath" value="${esc(item.path)}" ${index>=0?'readonly':''}></label>${index<0?'<button onclick="pickMediaLibrary()">选择目录</button>':''}<label>自动化<select id="mlAutomation">${[['off','关闭'],['monitor','仅扫描'],['scrape','扫描并刮削']].map(([value,label])=>`<option value="${value}" ${item.automation===value?'selected':''}>${label}</option>`).join('')}</select></label><label>排除规则（每行一条相对路径通配符）<textarea id="mlExclude">${esc(item.exclude_patterns.join('\n'))}</textarea></label><p class="muted">例如 sample*、*/sample/*；规则只影响扫描和自动刮削，不删除已存在的索引。</p><div class="fields"><label>最小视频大小（MiB）<input id="mlMinSize" type="number" min="0" step="any" value="${item.min_size/1048576}"></label><label>距最后修改的等待时间（秒）<input id="mlSettle" type="number" min="0" max="86400" value="${item.settle_seconds}"></label></div><p class="muted">等待时间降低正在拷贝的文件被提前处理的概率。自动化关闭不影响手动扫描和刮削。</p>`,`<button onclick="showMediaLibraries()">返回</button><button class="accent" onclick="saveMediaLibrary()">保存</button>`);
}
async function pickMediaLibrary(){try{const r=await api('/api/pick-folder',{});if(r.path)$('mlPath').value=r.path}catch(e){toast(e.message)}}
async function saveMediaLibrary(){try{await api('/api/libraries',{name:$('mlName').value,path:$('mlPath').value,automation:$('mlAutomation').value,exclude_patterns:$('mlExclude').value.split('\n').map(s=>s.trim()).filter(Boolean),min_size:Math.round(Number($('mlMinSize').value)*1048576),settle_seconds:Number($('mlSettle').value)});toast('媒体库配置已保存');await showMediaLibraries()}catch(e){toast(e.message)}}
async function scanMediaLibrary(index){try{const r=await api('/api/library-scan',{path:managedLibraries[index].path});toast(r.busy?'已有扫描进行中':'已开始扫描此媒体库')}catch(e){toast(e.message)}}
async function removeMediaLibrary(index){try{await api('/api/remove-folder',{path:managedLibraries[index].path});toast('已停止监控，索引和原文件保留');await showMediaLibraries()}catch(e){toast(e.message)}}
let libraryFileView={index:0,offset:0};
async function showLibraryFiles(index){
 libraryFileView={index,offset:0};
 openTool(managedLibraries[index].name,`<div class="row"><select id="mlFileStatus" onchange="loadLibraryFiles(0)"><option value="all">全部文件</option><option value="matched">已匹配资料</option><option value="pending">待匹配资料</option><option value="missing">文件缺失</option><option value="error">处理提示</option></select><input id="mlFileSearch" placeholder="搜索标题或编号" onkeydown="if(event.key==='Enter')loadLibraryFiles(0)"><button onclick="loadLibraryFiles(0)">搜索</button></div><div id="mlFiles" aria-live="polite"></div>`,`<button onclick="showMediaLibraries()">返回媒体库</button>`);
 await loadLibraryFiles(0);
}
async function loadLibraryFiles(offset){
 try{
  const index=libraryFileView.index;
  const params=new URLSearchParams({path:managedLibraries[index].path,status:$('mlFileStatus').value,search:$('mlFileSearch').value,offset,limit:30});
  const r=await fetch('/api/library-files?'+params);if(!r.ok)throw Error('读取媒体库文件失败');const d=await r.json();
  if(!$('mlFiles')||libraryFileView.index!==index)return;
  libraryFileView.offset=offset;
  $('mlFiles').innerHTML=`<p class="muted">${d.total} 个文件 · 当前 ${d.items.length} 个</p>`+d.items.map(item=>`<div class="folder"><div><strong>${esc(item.title)}</strong><p>${esc(item.code||'编号待确认')} · ${item.missing?'文件缺失':item.amane_id?'已匹配资料':'待匹配资料'} · ${item.height?item.height+'p':'分辨率待提取'}</p><small>${esc(item.path)}</small>${item.error?'<p class="notice">'+esc(item.error)+'</p>':''}</div><button onclick="toolDialog.close();openVideo(${item.id},true)">详情</button></div>`).join('')+`<div class="row"><button onclick="loadLibraryFiles(${Math.max(0,offset-30)})" ${offset===0?'disabled':''}>上一页</button><button onclick="loadLibraryFiles(${offset+30})" ${offset+30>=d.total?'disabled':''}>下一页</button></div>`;
 }catch(e){toast(e.message)}
}
