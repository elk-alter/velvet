"""File-review drafts and navigation cancellation, with no media or browser."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


class ReviewNavigationTests(unittest.TestCase):
    def run_node(self, script):
        source=Path(__file__).with_name('file_review.js')
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'review-client.cjs'
            path.write_text(script.replace('SOURCE',json.dumps(str(source))),encoding='utf-8')
            result=subprocess.run(['node',str(path)],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_cancel_and_late_response_preserve_quality_editor(self):
        self.run_node(r'''
const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
let completed=false;process.on('beforeExit',()=>{if(!completed){console.error('Review scenario did not complete');process.exitCode=1}});
const source=fs.readFileSync(SOURCE,'utf8'),start=source.indexOf('const reviewOpenVideo=openVideo;'),end=start+source.slice(start).search(/(?:async )?function goManageFromFileReview/);
let count=0,pending=[];
const context={openVideo:()=>new Promise(resolve=>pending.push(resolve)),document:{querySelectorAll:()=>[]},stopCardPreview(){},selected:{id:1},detail:{open:true},renderFileReviewDetail(){count++}};
vm.createContext(context);vm.runInContext(source.slice(start,end),context);
(async()=>{
 const cancelled=context.openVideo(2,true);pending.shift()(false);
 assert.equal(await cancelled,false);assert.equal(count,0,'cancel cannot replace the editor');
 const stale=context.openVideo(2,true),current=context.openVideo(3,true);
 pending[1](true);assert.equal(await current,true);assert.equal(count,1);
 pending[0](false);assert.equal(await stale,false);assert.equal(count,1,'late canceled read cannot replace current editor');
 completed=true;
})().catch(error=>{console.error(error);process.exitCode=1});
''')

    def test_quality_draft_survives_refresh_and_resets_only_for_other_file(self):
        self.run_node(r'''
const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const source=fs.readFileSync(SOURCE,'utf8'),start=source.indexOf('let reviewEditorBaseline'),end=source.indexOf('const reviewOpenVideo',start);
assert(start>=0&&end>start,'review editor state exists');
const records={1:{note:'Saved note',decision:'open',status:'unchecked',issues:[]},2:{note:'Other file',decision:'ignored',status:'done',issues:[]}},nodes={},events={};
const context={selected:{id:1},detail:{addEventListener(name,fn){events[name]=fn}},fileReview:id=>records[id],reviewLabels:{},esc:String,renderAutomaticFindings(){},$:id=>nodes[id]};
context.reviewDetail={addEventListener(){},set innerHTML(html){nodes.currentReviewNote={value:records[context.selected.id].note};nodes.currentReviewDecision={value:records[context.selected.id].decision};nodes.currentReviewDirty={textContent:''}}};
vm.createContext(context);vm.runInContext(source.slice(start,end),context);
context.renderFileReviewDetail();assert.equal(context.hasFileReviewChanges(),false);
nodes.currentReviewNote.value='Unsaved note';nodes.currentReviewDecision.value='accepted';assert.equal(context.hasFileReviewChanges(),true);
context.renderFileReviewDetail();assert.equal(nodes.currentReviewNote.value,'Unsaved note');assert.equal(nodes.currentReviewDecision.value,'accepted');assert.equal(context.hasFileReviewChanges(),true);
// A successful save is reflected by the authoritative review; another edit
// made while saving stays pending on the next refresh.
records[1]={...records[1],note:'Unsaved note',decision:'accepted'};nodes.currentReviewNote.value='Edited while saving';
context.renderFileReviewDetail();assert.equal(nodes.currentReviewNote.value,'Edited while saving');assert.equal(context.hasFileReviewChanges(),true);
records[1]={...records[1],note:'Edited while saving'};context.renderFileReviewDetail();assert.equal(context.hasFileReviewChanges(),false);
context.selected={id:2};context.renderFileReviewDetail();assert.equal(nodes.currentReviewNote.value,'Other file');assert.equal(context.hasFileReviewChanges(),false);
nodes.currentReviewNote.value='Discarded';events.close();context.renderFileReviewDetail();assert.equal(nodes.currentReviewNote.value,'Other file','discarded draft must not reappear on reopen');
''')


    def test_exclusion_cancel_failure_and_late_response_do_not_discard_another_editor(self):
        self.run_node(r'''
const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const source=fs.readFileSync(SOURCE,'utf8'),start=source.indexOf('async function confirmDirectoryExclusion');
let completed=false;process.on('beforeExit',()=>{if(!completed){console.error('Exclusion scenario did not complete');process.exitCode=1}});
let posts=0,closed=0,cleared=0,prompts=0,approved=false,fail=false,previewReply=null;
const node={textContent:'unsaved metadata'};
const context={exclusionPending:false,detail:{open:true},detailGeneration:1,selected:{id:1},detailHasChanges:()=>true,$:()=>node,
 discardFileReviewDraft:()=>cleared++,closeDetail:async()=>{closed++;context.detail.open=false},refresh:async()=>{},
 api:async(path)=>{if(path==='/api/exclusion-preview')return previewReply?await previewReply:{path:'C:/Synthetic/Film',files:3,movies:1,digest:'fixed'};posts++;if(fail)throw Error('synthetic failure')},
 requestDirectoryExclusion:async(preview,dirty,apply)=>{prompts++;assert.equal(dirty,true);if(!approved)return false;return await apply()!==false}};
vm.createContext(context);vm.runInContext(source.slice(start),context);
(async()=>{
 assert.equal(await context.confirmDirectoryExclusion({id:1}),false);assert.equal(posts,0);assert.equal(closed,0);assert.equal(cleared,0);assert.equal(node.textContent,'unsaved metadata');
 approved=true;fail=true;await assert.rejects(context.confirmDirectoryExclusion({id:1}),/synthetic failure/);assert.equal(closed,0);assert.equal(cleared,0);
 fail=false;assert.equal(await context.confirmDirectoryExclusion({id:1}),true);assert.equal(closed,1);assert.equal(cleared,1);assert.equal(node.textContent,'');
 context.detail.open=true;node.textContent='new draft';context.selected={id:1};let resolvePreview;
 previewReply=new Promise(resolve=>resolvePreview=resolve);const prior=prompts,stale=context.confirmDirectoryExclusion({id:1});
 context.selected={id:2};context.detailGeneration++;resolvePreview({path:'C:/Synthetic/Film',files:3,movies:1,digest:'fixed'});
 assert.equal(await stale,false);assert.equal(prompts,prior);assert.equal(closed,1);assert.equal(node.textContent,'new draft');
 previewReply=null;context.selected={id:1};let finishPost;
 context.api=async(path)=>path==='/api/exclusion-preview'?{path:'C:/Synthetic/Film',files:3,movies:1,digest:'fixed'}:await new Promise(resolve=>finishPost=resolve);
 const late=context.confirmDirectoryExclusion({id:1});while(!finishPost)await new Promise(resolve=>setImmediate(resolve));
 context.selected={id:2};context.detailGeneration++;finishPost({});assert.equal(await late,true);assert.equal(closed,1);assert.equal(cleared,1);assert.equal(node.textContent,'new draft');
 completed=true;
})().catch(error=>{console.error(error);process.exitCode=1});
''')

    def test_exclusion_dialog_blocks_repeat_submission_and_escape_during_apply(self):
        self.run_node(r'''
const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const source=fs.readFileSync(SOURCE,'utf8'),start=source.indexOf('function requestDirectoryExclusion'),end=source.indexOf('let exclusionPending',start);
let completed=false;process.on('beforeExit',()=>{if(!completed){console.error('Exclusion dialog did not complete');process.exitCode=1}});
let dialog,applied=0,finish;
function create(){const events={},buttons=[{},{}],status={},form={addEventListener(name,fn){events['form:'+name]=fn}};return {events,buttons,status,setAttribute(){},addEventListener(name,fn){events[name]=fn},querySelector(selector){return selector==='form'?form:status},querySelectorAll(){return buttons},showModal(){},remove(){this.removed=true},close(value){this.returnValue=value;events.close()}}}
const context={esc:String,document:{body:{append(){}},createElement(){return dialog=create()}}};
vm.createContext(context);vm.runInContext(source.slice(start,end),context);
(async()=>{
 const pending=context.requestDirectoryExclusion({path:'Synthetic',files:2,movies:1},true,()=>{applied++;return new Promise(resolve=>finish=resolve)});
 const event={submitter:{value:'exclude'},preventDefault(){}};const submit=dialog.events['form:submit'](event);
 await dialog.events['form:submit'](event);assert.equal(applied,1);assert.ok(dialog.buttons.every(button=>button.disabled));assert.equal(dialog.status.textContent,'正在应用排除…');
 let prevented=false;dialog.events.cancel({preventDefault(){prevented=true}});assert.equal(prevented,true);
 finish(true);await submit;assert.equal(await pending,true);assert.equal(dialog.removed,true);
 const failure=context.requestDirectoryExclusion({path:'Synthetic',files:2,movies:1},false,async()=>{throw Error('synthetic failure')});const caught=assert.rejects(failure,/synthetic failure/);
 await dialog.events['form:submit'](event);await caught;assert.equal(dialog.removed,true);
 const cancel=context.requestDirectoryExclusion({path:'Synthetic',files:2,movies:1},false,()=>{throw Error('must not apply')});prevented=false;dialog.events.cancel({preventDefault(){prevented=true}});assert.equal(prevented,false);dialog.close('cancel');assert.equal(await cancel,false);
 completed=true;
})().catch(error=>{console.error(error);process.exitCode=1});
''')


if __name__=='__main__':
    unittest.main()
