"""Exercise the actual inline detail departure handlers without browser/media."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


class DetailNavigationTests(unittest.TestCase):
    def test_cancel_discard_escape_actor_and_stale_confirmation(self):
        source = Path(__file__).with_name('index.html')
        script = r'''
const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const html=fs.readFileSync(SOURCE,'utf8');
const handlers=html.match(/((?:async )?function closeDetail\([\s\S]*?)function randomPick\(/);
assert.ok(handlers,'detail handlers exist');
let decide,asked=0,closed=0,paused=0,progress=0,refreshed=0,saved=0,actor=null;
const events={},nodes={editDirty:{textContent:'unsaved'},player:{pause(){paused++}}};
const tags=new Set();let rendered=0;
const context={detail:{open:true,close(){this.open=false;closed++},addEventListener(name,fn){events[name]=fn}},selected:{id:1},activeTags:tags,render:()=>rendered++,page:9,pushViewState(){},
 $:id=>nodes[id],allowManagementLeave:()=>{asked++;return new Promise(resolve=>decide=resolve)},saveProgress:()=>progress++,refresh:()=>refreshed++,saveViewState:()=>saved++,openActor:name=>actor=name};
vm.createContext(context);vm.runInContext(handlers[1],context);
(async()=>{
 const canceled=context.closeDetail();
 assert.equal(context.detail.open,true,'do not close before confirmation');assert.equal(paused,0,'do not pause before confirmation');
 decide(false);assert.equal(await canceled,false);assert.equal(closed,0);assert.equal(nodes.editDirty.textContent,'unsaved');
 const accepted=context.closeDetail();decide(true);assert.equal(await accepted,true);
 assert.equal(closed,1);assert.equal(progress,1);assert.equal(paused,1);assert.equal(refreshed,1);assert.equal(saved,1);
 // An already closed dialog does not re-run lifecycle work.
 assert.equal(await context.closeDetail(),true);assert.equal(closed,1);
 context.detail.open=true;
 let prevented=false;events.cancel({preventDefault(){prevented=true}});assert.equal(prevented,true);
 decide(false);await new Promise(resolve=>setImmediate(resolve));assert.equal(context.detail.open,true);
 const cancelActor=context.openDetailActor('Synthetic Actor');assert.equal(actor,null);decide(false);
 assert.equal(await cancelActor,false);assert.equal(actor,null);assert.equal(context.detail.open,true);
 const acceptActor=context.openDetailActor('Synthetic Actor');assert.equal(actor,null);decide(true);
 assert.equal(await acceptActor,true);assert.equal(actor,'Synthetic Actor');assert.equal(context.detail.open,false);
 context.detail.open=true;context.selected={id:1};
 const stale=context.closeDetail();context.selected={id:2};decide(true);
 assert.equal(await stale,false);assert.equal(context.detail.open,true,'old confirmation cannot close another file');
 const cancelTag=context.openDetailTag('Synthetic Tag');decide(false);assert.equal(await cancelTag,false);
 assert.equal(tags.size,0);assert.equal(context.page,9);assert.equal(rendered,0);
 const acceptTag=context.openDetailTag('Synthetic Tag');decide(true);assert.equal(await acceptTag,true);
 assert.equal(tags.has('Synthetic Tag'),true);assert.equal(context.page,1);assert.equal(rendered,1);
 context.detail.open=true;
 nodes.editDirty.textContent='';const prior=asked;assert.equal(await context.closeDetail(),true);assert.equal(asked,prior);
})().catch(error=>{console.error(error);process.exitCode=1});
'''.replace('SOURCE', json.dumps(str(source)))
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / 'detail-navigation.cjs'
            target.write_text(script, encoding='utf-8')
            result = subprocess.run(['node', str(target)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
