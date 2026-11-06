"""Client data-identity regression, without browser/network or production data."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


class ServerBrowseTests(unittest.TestCase):
    def test_closed_detail_rejects_late_movie_response_before_cache_or_reopen(self):
        source=Path(__file__).with_name('server_browse.js')
        script=r'''
const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const nodes={};function element(id=''){const e={id,textContent:'',options:[{textContent:'Any'}],children:[],value:'',setAttribute(){},insertAdjacentHTML(){},addEventListener(){}};e.replaceWith=input=>nodes[id]=input;return e}
for(const id of ['tag','excludeTag','actor','studio','editDirty'])nodes[id]=element(id);
let release,shown=0,generation=0;
const context={window:{MovieDetailView:{cancelPending(){++generation},get generation(){return generation}}},library:{},detailHasChanges:()=>false,detail:{open:true,addEventListener(){}},document:{createElement:()=>element()},$:id=>nodes[id],fetch:()=>new Promise(resolve=>release=resolve),URL,URLSearchParams,AbortController,setTimeout,clearTimeout,console,toast:message=>{throw Error(message)}};
vm.createContext(context);vm.runInContext(fs.readFileSync(SOURCE,'utf8'),context);
(async()=>{
 const pending=context.window.serverBrowse.openMovie(1,true,()=>++shown);
 context.detail.open=false;context.window.MovieDetailView.cancelPending();
 release({ok:true,json:async()=>({movie:{movie_key:'work',id:1,_versions:[{_parts:[{id:1}]}]},review:{files:{}}})});
 assert.equal(await pending,false);assert.equal(shown,0);assert.equal(context.library.movies,undefined);
})().catch(error=>{console.error(error);process.exitCode=1});
'''.replace('SOURCE',json.dumps(str(source)))
        with tempfile.TemporaryDirectory() as root:
            target=Path(root)/'late-detail.cjs';target.write_text(script,encoding='utf-8')
            result=subprocess.run(['node',str(target)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_open_retries_when_saved_detail_page_is_stale(self):
        source=Path(__file__).with_name('server_browse.js')
        script=r"""
const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const nodes={},urls=[];
function element(){const node={dataset:{},options:[{textContent:'Any'}],replaceWith(){},addEventListener(){},setAttribute(){},insertAdjacentHTML(){}};return node}
const context={window:{MovieDetailView:{parameters:()=>({version_offset:20}),cancelPending(){}}},
 library:{},detail:{open:false,addEventListener(){}},document:{createElement:element},$:id=>nodes[id]??=element(),
 URL,URLSearchParams,AbortController,setTimeout,clearTimeout,console,
 fetch:async url=>{urls.push(url);return urls.length===1?{ok:false,status:400,json:async()=>({error:'stale page'})}:
  {ok:true,json:async()=>({movie:{id:7,movie_key:'synthetic',_versions:[{_parts:[{id:7}]}]},review:{files:{}}})}},
 toast:message=>{throw Error(message)}};
vm.createContext(context);vm.runInContext(fs.readFileSync(SOURCE,'utf8'),context);
(async()=>{
 let opened=0;
 assert.equal(await context.window.serverBrowse.openMovie(7,true,id=>{opened=id}),true);
 assert.equal(opened,7);assert.equal(urls.length,2);
 assert.match(urls[0],/version_offset=20/);assert.doesNotMatch(urls[1],/version_offset/);
 console.log('passed');
})().catch(error=>{console.error(error);process.exitCode=1});
""".replace('SOURCE',json.dumps(str(source)))
        with tempfile.TemporaryDirectory() as root:
            target=Path(root)/'stale-page.cjs';target.write_text(script,encoding='utf-8')
            result=subprocess.run(['node',str(target)],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('passed',result.stdout)

    def test_merge_and_split_do_not_leave_one_file_in_two_cached_movies(self):
        source=Path(__file__).with_name('server_browse.js')
        script=r'''
const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const nodes={};
function element(id=''){
 const e={id,options:[{textContent:'Any'}],children:[],value:'',setAttribute(){},insertAdjacentHTML(){},addEventListener(){}};
 e.replaceWith=input=>nodes[id]=input;return e;
}
for(const id of ['tag','excludeTag','actor','studio'])nodes[id]=element(id);
const responses=[],context={window:{},library:{},detailHasChanges:()=>false,detail:{addEventListener(){}},document:{createElement:()=>element()},$:id=>nodes[id],fetch:async()=>({ok:true,json:async()=>responses.shift()}),URL,URLSearchParams,AbortController,setTimeout,clearTimeout,console};
vm.createContext(context);vm.runInContext(fs.readFileSync(SOURCE,'utf8'),context);
const movie=(key,ids)=>({movie_key:key,_versions:[{_parts:ids.map(id=>({id}))}]});
async function load(key,ids){responses.push({movie:movie(key,ids),review:{files:Object.fromEntries(ids.map(id=>[id,{issues:[]}]))}});await context.window.serverBrowse.loadMovie(ids[0])}
(async()=>{
 await load('A',[1]);await load('B',[2]);assert.equal(context.library.movies.length,2);
 await load('B',[1,2]);assert.equal(context.library.movies.length,1);assert.equal(context.library.movies[0].movie_key,'B');
 assert.equal(context.library.videos.filter(part=>part.id===1).length,1);
 await load('A',[1]);assert.equal(context.library.movies.length,1);assert.equal(context.library.movies[0].movie_key,'A');
 assert.equal(Object.hasOwn(context.library.review.files,'2'),false);
 await load('B',[2]);assert.equal(context.library.movies.length,2);
 assert.equal(new Set(context.library.videos.map(part=>part.id)).size,2);
})().catch(error=>{console.error(error);process.exitCode=1});
'''.replace('SOURCE',json.dumps(str(source)))
        with tempfile.TemporaryDirectory() as root:
            target=Path(root)/'client-cache.cjs';target.write_text(script,encoding='utf-8')
            result=subprocess.run(['node',str(target)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
