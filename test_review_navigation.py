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
            result=subprocess.run(['node',str(path)],capture_output=True,text=True)
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


if __name__=='__main__':
    unittest.main()
