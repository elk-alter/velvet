/* Visual governance editors. Saved changes always use the previewed scope. */
window.CatalogTools=(()=>{
 const endpoint='/api/catalog-tags/';
 let tagsPreview=null,personPreview=null,actorSelection=new Set(),actorOffset=0;
 const quote=value=>esc(value??'');

 function tagRow(kind,first='',second=''){
  const alias=kind==='aliases';
  return `<div class="catalogEditorRow"><label>${alias?'来源名称':'自动标签'}<input data-first maxlength="80" value="${quote(first)}"></label>
  <label>${alias?'统一名称':'关键词，用逗号分隔'}<input data-second value="${quote(second)}"></label>
  <button type="button" onclick="CatalogTools.remove(this)" aria-label="删除这条规则">删除</button></div>`;
 }
 function dirty(){
  managerDirty=true;tagsPreview=null;
  $('catalogTagPreview')?.replaceChildren();
 }
 function add(kind){$(kind==='aliases'?'catalogAliasRows':'catalogRuleRows').insertAdjacentHTML('beforeend',tagRow(kind));dirty()}
 function remove(button){button.closest('.catalogEditorRow').remove();dirty()}
 function tagValues(){
  const rows=id=>[...$(id).querySelectorAll('.catalogEditorRow')].map(row=>
   [row.querySelector('[data-first]').value.trim(),row.querySelector('[data-second]').value.trim()]);
  const aliases=rows('catalogAliasRows'),rules=rows('catalogRuleRows');
  if(new Set(aliases.map(([from])=>from)).size!==aliases.length)throw Error('来源名称重复');
  return {aliases:Object.fromEntries(aliases),rules:rules.map(([tag,words])=>({tag,keywords:words.split(/[,，]/).map(x=>x.trim()).filter(Boolean)}))};
 }
 async function tags(){
  const data=await managerGet('/api/tag-rules');tagsPreview=null;
  renderManagerShell('分类规则',`<section class="settingsSection">
    <h3>标签别名</h3><p>把不同来源的写法统一成同一个筛选标签。</p>
    <div id="catalogAliasRows" oninput="CatalogTools.dirty()">${Object.entries(data.aliases||{}).map(([from,to])=>tagRow('aliases',from,to)).join('')}</div>
    <button onclick="CatalogTools.add('aliases')">添加别名</button></section>
    <section class="settingsSection"><h3>关键词规则</h3><p>标题、路径或现有资料命中任一关键词时，添加指定自动标签。</p>
    <div id="catalogRuleRows" oninput="CatalogTools.dirty()">${data.rules.map(rule=>tagRow('rules',rule.tag,rule.keywords.join(', '))).join('')}</div>
    <button onclick="CatalogTools.add('rules')">添加关键词规则</button></section>
    <div class="row"><button class="accent" onclick="CatalogTools.previewTags(this)">预览影响</button>${data.can_undo?'<button onclick="CatalogTools.previewTags(this,true)">预览撤销上次规则变更</button>':''}</div>
    <div id="catalogTagPreview" aria-live="polite"></div>`);
 }
 async function previewTags(button,undo=false){
  const epoch=managerRouteEpoch;button.disabled=true;
  try{
   const values=undo?{}:tagValues();
   const result=await api(endpoint+(undo?'undo-preview':'preview'),values);
   if(epoch!==managerRouteEpoch||(!undo&&JSON.stringify(values)!==JSON.stringify(tagValues())))return;
   tagsPreview={result,values,undo};
   const samples=result.samples.map(row=>`<li><strong>${quote(row.title)}</strong> · ${row.added.length?'新增 '+quote(row.added.join('、')):''} ${row.removed.length?'移除 '+quote(row.removed.join('、')):''}</li>`).join('');
   $('catalogTagPreview').innerHTML=`<section class="settingsSection"><h3>${undo?'撤销影响':'规则影响'} · ${result.changed} 个文件</h3>
    ${samples?`<ul>${samples}</ul>`:'<p>没有现有文件的标签发生变化。</p>'}
    <button class="accent" onclick="CatalogTools.applyTags(this)">${undo?'确认撤销规则':'确认保存规则'}</button></section>`;
  }catch(error){if(epoch===managerRouteEpoch)toast(error.message)}
  finally{if(button.isConnected)button.disabled=false}
 }
 async function applyTags(button){
  const pending=tagsPreview,epoch=managerRouteEpoch;
  if(!pending||button.disabled)return;
  button.disabled=true;
  try{
   await api(endpoint+(pending.undo?'undo':'apply'),{...pending.values,digest:pending.result.digest});
   if(epoch!==managerRouteEpoch)return;
   if(tagsPreview===pending){managerDirty=false;await tags()}
   toast(pending.undo?'规则已撤销':'规则已保存');
  }catch(error){if(epoch===managerRouteEpoch)toast(error.message)}
  finally{if(button.isConnected)button.disabled=false}
 }

 async function people(){
  const [library,state]=await Promise.all([managerGet('/api/actors'),managerGet('/api/actor-governance')]);
  personPreview=null;actorSelection=new Set();actorOffset=0;
  renderManagerShell('演员身份',`<section class="settingsSection">
    <label>操作<select id="catalogActorMode" onchange="CatalogTools.resetPeople()"><option value="merge">合并别名</option><option value="split">拆分同名演员</option></select></label>
    <label>来源演员<input id="catalogActorSource" list="catalogActorNames" oninput="CatalogTools.resetPeople()"></label>
    <label>目标身份<input id="catalogActorTarget" list="catalogActorNames" oninput="CatalogTools.resetPeople()"></label>
    <datalist id="catalogActorNames">${library.actors.map(person=>`<option value="${quote(person.name)}"></option>`).join('')}</datalist>
    <p>合并时选择已有主名称。拆分时填写不同的显示名，再勾选属于该身份的文件。</p>
    <div class="row"><button class="accent" onclick="CatalogTools.previewPeople(this)">预览演员变更</button>
    ${state.can_undo?'<button onclick="CatalogTools.undoPeople(this)">撤销上次演员变更</button>':''}</div>
    ${Object.keys(state.aliases).length?`<p>已合并别名 · ${Object.entries(state.aliases).map(([from,to])=>quote(from)+' → '+quote(to)).join('、')}</p>`:''}
    <div id="catalogActorPreview" aria-live="polite"></div></section>`);
 }
 function resetPeople(){personPreview=null;actorSelection.clear();actorOffset=0;$('catalogActorPreview').replaceChildren()}
 function peopleValues(){return {mode:$('catalogActorMode').value,source:$('catalogActorSource').value,
   target:$('catalogActorTarget').value,ids:[...actorSelection]}}
 function chooseActorFile(box){
  if(box.checked)actorSelection.add(Number(box.value));else actorSelection.delete(Number(box.value));
  personPreview=null;
  $('catalogActorConfirm')?.replaceChildren();
 }
 async function previewPeople(button,offset=actorOffset){
  const values=peopleValues(),epoch=managerRouteEpoch;button.disabled=true;
  try{
   const result=await api('/api/actor-governance/preview',{...values,offset});
   if(epoch!==managerRouteEpoch||JSON.stringify(values)!==JSON.stringify(peopleValues()))return;
   const source=result.profiles[result.source],target=result.profiles[result.target];
   const profiles=Object.entries(result.profiles).map(([name,value])=>`<li>${value.portrait?`<img class="catalogPortrait" src="${quote(value.portrait)}" alt="">`:''}<strong>${quote(name)}</strong> · ${value.files} 个文件 · ${value.rating} 星${value.favorite?' · 已收藏':''}</li>`).join('');
   const samples=result.samples.map(row=>`<li>${values.mode==='split'?`<label><input type="checkbox" value="${row.id}" ${actorSelection.has(row.id)?'checked':''} onchange="CatalogTools.chooseActorFile(this)">${quote(row.title)} · #${row.id}</label>`:`<span>${quote(row.title)}</span>`}</li>`).join('');
   const confirm=result.selected?`<p>涉及 ${result.selected} 个文件。请选择目标身份的偏好。</p>
    <label>目标评分<select id="catalogActorRating">${[0,1,2,3,4,5].map(n=>`<option value="${n}" ${n===(target.saved?target.rating:source.rating)?'selected':''}>${n} 星</option>`).join('')}</select></label>
    <label><input id="catalogActorFavorite" type="checkbox" ${source.favorite||target.favorite?'checked':''}> 收藏目标身份</label>
    <button class="accent" onclick="CatalogTools.applyPeople(this)">确认${values.mode==='merge'?'合并':'拆分'}</button>`:'<p>选择文件后再次预览，才能确认拆分。</p>';
   actorOffset=result.offset;
   const paging=`<div class="row"><button ${actorOffset?'':'disabled'} onclick="CatalogTools.previewPeople(this,${actorOffset-25})">上一页</button>
    <span>${actorOffset+1}–${actorOffset+result.samples.length} / ${result.total}</span>
    <button ${actorOffset+25<result.total?'':'disabled'} onclick="CatalogTools.previewPeople(this,${actorOffset+25})">下一页</button></div>`;
   $('catalogActorPreview').innerHTML=`<ul>${profiles}</ul><details ${values.mode==='split'?'open':''}><summary>来源文件 · ${result.total} 项</summary>
    <ul class="catalogActorSamples">${samples}</ul>${paging}</details><div id="catalogActorConfirm">${confirm}</div>`;
   personPreview={values,result};
  }catch(error){if(epoch===managerRouteEpoch)toast(error.message)}
  finally{if(button.isConnected)button.disabled=false}
 }
 async function applyPeople(button){
  if(!personPreview||button.disabled)return;
  const {values,result}=personPreview,epoch=managerRouteEpoch;button.disabled=true;
  try{
   await api('/api/actor-governance/apply',{...values,digest:result.digest,rating:Number($('catalogActorRating').value),
                                                   favorite:$('catalogActorFavorite').checked});
   if(epoch===managerRouteEpoch){
    toast('演员身份已更新');await people();
   }
  }catch(error){if(epoch===managerRouteEpoch)toast(error.message)}
  finally{if(button.isConnected)button.disabled=false}
 }
 async function undoPeople(button){
  const epoch=managerRouteEpoch;button.disabled=true;
  try{
   await api('/api/actor-governance/undo',{});
   if(epoch===managerRouteEpoch){toast('演员身份变更已撤销');await people()}
  }catch(error){if(epoch===managerRouteEpoch)toast(error.message)}
  finally{if(button.isConnected)button.disabled=false}
 }
 return {tags,add,remove,dirty,previewTags,applyTags,people,resetPeople,chooseActorFile,previewPeople,applyPeople,undoPeople};
})();

function catalogPager(page,total,handler){
 if(total<=1)return '';
 return `<button ${page===1?'disabled':''} onclick="${handler}(${page-1})">上一页</button>
 <span role="status">第 ${page} / ${total} 页</span>
 <button ${page===total?'disabled':''} onclick="${handler}(${page+1})">下一页</button>
 <form class="row" onsubmit="event.preventDefault();${handler}(this.elements.destination.value)">
 <label>跳至 <input name="destination" type="number" min="1" max="${total}" value="${page}" required></label>
 <button>跳转</button></form>`;
}
function goActorPage(value){
 actorPage=Math.max(1,Number(value)||1);renderActors();saveViewState();
}
function goCategoryPage(value){
 categoryPage=Math.max(1,Number(value)||1);updateCategoryResults();
}

window.SavedFilters=(()=>{
 const key='velvet-saved-filters';
 function entries(){
  try{const items=JSON.parse(localStorage.getItem(key)||'[]');return Array.isArray(items)?items:[]}
  catch(error){return []}
 }
 function render(){
  const selector=$('savedFilterChoice');
  selector.innerHTML='<option value="">选择已保存的筛选</option>'+entries().map((item,index)=>
   `<option value="${index}">${esc(item.name)}</option>`).join('');
 }
 function write(items){
  try{localStorage.setItem(key,JSON.stringify(items));render();return true}
  catch(error){toast('无法保存到当前浏览器');return false}
 }
 function add(form){
  const name=form.elements.filterName.value.trim();if(!name)return;
  const items=entries(),existing=items.findIndex(item=>item.name===name);
  if(existing<0&&items.length>=30)return toast('最多保存 30 个筛选');
  const {mode,filters,tags,pageSize,facet,collection,actor}=captureView();
  const item={name,view:{mode,filters,tags,pageSize,facet,collection,actor,page:1,scroll:0}};
  if(existing<0)items.push(item);
  else items[existing]=item;
  if(write(items)){form.reset();toast('筛选已保存')}
 }
 function apply(){
  const choice=$('savedFilterChoice').value;if(choice==='')return;
  const item=entries()[Number(choice)];if(!item)return;
  saveViewState();applyView(item.view);pushViewState();
 }
 function remove(){
  const choice=$('savedFilterChoice').value;if(choice==='')return;
  const items=entries();items.splice(Number(choice),1);
  if(write(items))toast('已移除保存的筛选');
 }
 return {render,add,apply,remove};
})();

function setPrivacyText(enabled){
 document.documentElement.classList.toggle('privacyText',Boolean(enabled));
 try{localStorage.setItem('velvet-privacy-text',enabled?'on':'off')}catch(error){}
 $('privacyTextOption').checked=Boolean(enabled);
}

$('savedFilterSlot').insertAdjacentHTML('afterbegin',`<details class="savedFilterPanel">
 <summary>保存的筛选与隐私</summary>
 <div class="row"><select id="savedFilterChoice" aria-label="已保存的筛选"></select>
 <button onclick="SavedFilters.apply()">应用</button><button onclick="SavedFilters.remove()">移除</button></div>
 <form class="row" onsubmit="event.preventDefault();SavedFilters.add(this)">
 <input name="filterName" aria-label="筛选名称" placeholder="给当前筛选命名" maxlength="50" required><button>保存当前筛选</button></form>
 <label><input id="privacyTextOption" type="checkbox" onchange="setPrivacyText(this.checked)"> 隐私模式中同时隐藏标题和路径</label>
 </details>`);
SavedFilters.render();
try{
 const hideText=localStorage.getItem('velvet-privacy-text')==='on';
 $('privacyTextOption').checked=hideText;
 document.documentElement.classList.toggle('privacyText',hideText);
}catch(error){}
window.addEventListener('storage',event=>{
 if(event.key==='velvet-saved-filters')SavedFilters.render();
 if(event.key==='velvet-privacy-text'||event.key===null){
  const hideText=event.newValue==='on';
  document.documentElement.classList.toggle('privacyText',hideText);
  $('privacyTextOption').checked=hideText;
 }
});
