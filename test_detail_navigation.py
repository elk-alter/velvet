"""Exercise the actual inline detail departure handlers without browser/media."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


class DetailNavigationTests(unittest.TestCase):
    def test_play_starts_loading_and_stale_metadata_does_not_seek_another_file(self):
        source=Path(__file__).with_name('index.html')
        script=r'''
const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const html=fs.readFileSync(SOURCE,'utf8'),start=html.indexOf('function internalPlay(){'),end=html.indexOf('let lastSave=0',start);
let plays=0;const player={src:'',currentTime:0,duration:10,getAttribute(){return this.src},play(){plays++;return Promise.resolve()}};
const context={$:()=>player,detail:{open:true},playbackEpoch:0,selected:{id:1,position:8,duration:10,watched:false},toast(){}};
vm.createContext(context);vm.runInContext(html.slice(start,end),context);
context.internalPlay();assert.equal(plays,1,'play must request loading with preload=none');assert.equal(player.src,'/media/1');
const oldMetadata=player.onloadedmetadata;context.selected={id:2,position:4,duration:10,watched:false};oldMetadata();assert.equal(player.currentTime,0,'stale metadata cannot seek another selected file');
context.internalPlay();player.onloadedmetadata();assert.equal(player.currentTime,4);
context.selected={id:2,position:999,duration:10,watched:false};context.internalPlay();player.onloadedmetadata();assert.equal(player.currentTime,9);
player.ended=true;context.selected={id:2,position:9,duration:10,watched:false};context.internalPlay();player.onloadedmetadata();assert.equal(player.currentTime,0,'replay starts at zero even before cached watched state updates');
context.selected={id:2,position:8,duration:10,watched:true};context.internalPlay();player.onloadedmetadata();assert.equal(player.currentTime,0);
'''.replace('SOURCE',json.dumps(str(source)))
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'play-start.cjs';path.write_text(script,encoding='utf-8')
            result=subprocess.run(['node',str(path)],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_ended_continuation_rejects_stale_progress_refresh_and_replay(self):
        source=Path(__file__).with_name('index.html')
        script=r'''
const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const html=fs.readFileSync(SOURCE,'utf8'),start=html.indexOf("$('player').onended=async"),end=html.indexOf('function saveProgress(',start);
let completed=false;process.on('beforeExit',()=>{if(!completed){console.error('Continuation did not complete');process.exitCode=1}});
let open=0,play=0,refresh=0,resolveProgress,resolveRefresh;
const player={src:'/media/1',ended:true,getAttribute(){return this.src}};
const work=id=>({_versions:[{_parts:[{id}]}],_detail:{next:{id:id+20,missing:false}}});
const context={$:()=>player,detail:{open:true},playbackEpoch:1,selected:{id:1},library:{movies:[work(1),work(2)]},
 saveProgress:()=>new Promise(resolve=>resolveProgress=resolve),refresh:async()=>refresh++,openVideo:async id=>{open++;context.selected={id};return true},internalPlay:()=>play++,toast(){}};
vm.createContext(context);vm.runInContext(html.slice(start,end),context);
(async()=>{
 let pending=player.onended();context.selected={id:2};resolveProgress();await pending;assert.equal(open,0);assert.equal(refresh,0);
 context.selected={id:1};context.saveProgress=async()=>{};context.refresh=()=>new Promise(resolve=>resolveRefresh=resolve);
 pending=player.onended();while(!resolveRefresh)await new Promise(resolve=>setImmediate(resolve));context.selected={id:2};resolveRefresh();await pending;assert.equal(open,0);
 context.selected={id:1};context.refresh=async()=>{context.playbackEpoch++};await player.onended();assert.equal(open,0,'replay of same file invalidates old ended callback');
 context.refresh=async()=>{};await player.onended();assert.equal(open,1);assert.equal(play,1,'next can be outside the loaded part page');
 context.selected={id:1};context.openVideo=async()=>false;await player.onended();assert.equal(play,1,'canceled switch cannot start playing');
 context.library.movies[0]._detail.next.missing=true;await player.onended();assert.equal(play,1);
 completed=true;
})().catch(error=>{console.error(error);process.exitCode=1});
'''.replace('SOURCE',json.dumps(str(source)))
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'continuation.cjs';path.write_text(script,encoding='utf-8')
            result=subprocess.run(['node',str(path)],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_cancel_discard_escape_actor_and_stale_confirmation(self):
        source = Path(__file__).with_name('index.html')
        script = r'''
const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
let completed=false;process.on('beforeExit',()=>{if(!completed){console.error('Navigation scenario did not complete');process.exitCode=1}});
const html=fs.readFileSync(SOURCE,'utf8');
const handlers=html.match(/((?:async )?function closeDetail\([\s\S]*?)function randomPick\(/);
assert.ok(handlers,'detail handlers exist');
let decide,asked=0,closed=0,paused=0,progress=0,refreshed=0,saved=0,actor=null,reviewDirty=false;
const events={},nodes={editDirty:{textContent:'unsaved'},player:{pause(){paused++}}};
const tags=new Set();let rendered=0;
const context={detail:{open:true,close(){this.open=false;closed++},addEventListener(name,fn){events[name]=fn}},selected:{id:1},activeTags:tags,render:()=>rendered++,page:9,pushViewState(){},
 $:id=>nodes[id],hasFileReviewChanges:()=>reviewDirty,discardFileReviewDraft:()=>reviewDirty=false,confirm:()=>true,api:async()=>({}),allowManagementLeave:()=>{asked++;return new Promise(resolve=>decide=resolve)},saveProgress:()=>progress++,refresh:()=>refreshed++,saveViewState:()=>saved++,openActor:name=>actor=name};
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
 nodes.editDirty.textContent='';reviewDirty=true;const qualityCancel=context.closeDetail();decide(false);assert.equal(await qualityCancel,false);assert.equal(context.detail.open,true);
 reviewDirty=false;const prior=asked;assert.equal(await context.closeDetail(),true);assert.equal(asked,prior);
 context.detail.open=true;reviewDirty=true;
 const questions=asked,forgot=context.forget();await Promise.resolve();assert.equal(asked,questions,'removed record cannot offer to keep editing');await forgot;assert.equal(context.detail.open,false);
 context.detail.open=true;context.selected={id:1};reviewDirty=true;let remove;
 context.api=()=>new Promise(resolve=>remove=resolve);
 const staleRemoval=context.forget();context.selected={id:2};remove({});await Promise.resolve();
 assert.equal(asked,questions,'stale removal cannot prompt for another file');await staleRemoval;assert.equal(context.detail.open,true);assert.equal(reviewDirty,true);
 completed=true;
})().catch(error=>{console.error(error);process.exitCode=1});
'''.replace('SOURCE', json.dumps(str(source)))
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / 'detail-navigation.cjs'
            target.write_text(script, encoding='utf-8')
            result = subprocess.run(['node', str(target)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
