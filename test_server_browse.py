"""Client data-identity regression, without browser/network or production data."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


class ServerBrowseTests(unittest.TestCase):
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
const responses=[],context={window:{},library:{},detail:{addEventListener(){}},document:{createElement:()=>element()},$:id=>nodes[id],fetch:async()=>({ok:true,json:async()=>responses.shift()}),URL,URLSearchParams,AbortController,setTimeout,clearTimeout,console};
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
