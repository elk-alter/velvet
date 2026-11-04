"""Bounded detail adaptation and navigation state, without a real browser or media."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


class MovieDetailClientTests(unittest.TestCase):
    def test_current_part_is_retained_once_and_version_page_survives_known_navigation(self):
        source=Path(__file__).with_name('movie_detail_view.js')
        script=r'''
const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const nodes={};const node=()=>({innerHTML:'',classList:{add(){}},setAttribute(){},querySelectorAll(){return []}});
const context={window:{},detail:{open:true,addEventListener(){},querySelector:()=>({hidden:false})},$:id=>nodes[id]||(nodes[id]=node()),esc:String,AbortController};
vm.createContext(context);vm.runInContext(fs.readFileSync(SOURCE,'utf8'),context);
const view=context.window.MovieDetailView,parts=Array.from({length:20},(_,i)=>({id:i+1}));
const data={movie_key:'work',default_version_key:'preferred',default_version:{version_key:'preferred',resume_id:60},selected:{id:50},next:{id:51},active_version:{version_key:'active',resume_id:50},total_files:10000,edit_file_count:10000,
 versions:{offset:20,limit:10,total:100,items:Array.from({length:10},(_,i)=>({version_key:'v'+i,resume_id:100+i}))},parts:{offset:0,limit:20,total:65,items:parts}};
const movie=view.movie(data),files=movie._versions.flatMap(v=>v._parts);
assert.equal(movie._versions.length,12);assert.equal(files.length,21);assert.equal(files.filter(p=>p.id===50).length,1);assert.equal(movie._edit_file_count,10000);
view.show(data);
for(const id of [50,51,60,1,105])assert.equal(view.parameters(id).version_offset,20);
assert.equal(view.parameters(50,{preservePartPage:true}).part_offset,0);
assert.equal(view.parameters(51,{preservePartPage:true}).part_offset,undefined);
assert.equal(Object.keys(view.parameters(999)).length,0);
'''.replace('SOURCE',json.dumps(str(source)))
        with tempfile.TemporaryDirectory() as root:
            target=Path(root)/'detail-client.cjs';target.write_text(script,encoding='utf-8')
            result=subprocess.run(['node',str(target)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
