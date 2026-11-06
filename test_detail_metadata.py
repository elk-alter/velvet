"""Metadata saves preserve concurrent edits and only update their original view."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


class DetailMetadataTests(unittest.TestCase):
    def run_case(self, scenario):
        source=Path(__file__).with_name('index.html')
        script=r'''
const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const html=fs.readFileSync(SOURCE,'utf8'),start=html.indexOf('async function saveVideo(){'),end=html.indexOf('let playbackEpoch=',start);
let completed=false;process.on('beforeExit',()=>{if(!completed){console.error('Metadata scenario did not complete');process.exitCode=1}});
const fields=['title','code','actors','studio','tags','date','rating'];
const baseline={id:1,title:'Original',code:'DEMO-1',actors:'Original Actor',studio:'Studio',tags:'',date:'',rating:0,plot:'',censor_override:'auto',part_index:20};
const fresh={...baseline,title:'Submitted',actors:'Saved Actor',tags:'Manual tag',auto_tags:'Auto tag',tag_reasons:'{}',locked_fields:'["title","actors"]',censorship_label:'Unknown',censorship_source:'Synthetic'};
const nodes={},button={disabled:false};for(const key of fields)nodes['edit_'+key]={value:String(baseline[key])};
for(const id of ['heading','fieldLocks','autoTagDetail','actorLinks','censorEvidence'])nodes[id]={textContent:'',innerHTML:''};
nodes.edit_plot={value:''};nodes.editCensor={value:'auto'};nodes.edit_domestic={checked:false};nodes.edit_amateur={checked:false};nodes.editDirty={textContent:'unsaved'};nodes.currentReviewNote={value:'Independent quality draft'};
let post,submitted,loads=0,refreshes=0,nav=null,summaries=0;const messages=[];
const work=()=>({...fresh,_detail:{selected:fresh,active_version:{part_count:22},parts:{offset:20}},_versions:[{part_count:22,_parts:[fresh]}]});
const context={window:{MovieDetailView:{parameters:()=>({version_offset:0,part_offset:20}),show:data=>nav=data}},detail:{open:true,querySelector:()=>button},detailGeneration:7,selected:{...baseline},editBaseline:{...baseline},cardFlagFields:['domestic','amateur'],editFields:fields.map(key=>[key,key]),library:{movies:[],videos:[]},
 $:id=>nodes[id],esc:String,actorNames:value=>String(value||'').split(',').map(x=>x.trim()).filter(Boolean),toast:message=>messages.push(message),renderSelectedFilmSummary:()=>summaries++,
 api:async(path,data)=>{submitted=data;return await new Promise(resolve=>post=resolve)},refresh:async()=>{refreshes++;context.library.movies=[work()];context.library.videos=[fresh]}};
context.serverBrowse=context.window.serverBrowse={loadMovie:async(id,options)=>{loads++;if(options.accept&&!options.accept())return null;context.library.movies=[work()];context.library.videos=[fresh];return work()}};
vm.createContext(context);vm.runInContext(html.slice(start,end),context);
(async()=>{SCENARIO;completed=true})().catch(error=>{console.error(error);process.exitCode=1});
'''.replace('SOURCE',json.dumps(str(source))).replace('SCENARIO',scenario)
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'metadata-save.cjs';path.write_text(script,encoding='utf-8')
            result=subprocess.run(['node',str(path)],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_save_keeps_later_input_and_updates_authoritative_links(self):
        self.run_case(r'''
nodes.edit_title.value='Submitted';nodes.edit_actors.value='Saved Actor';nodes.edit_tags.value='Manual tag';
const pending=context.saveVideo();assert.equal(submitted.id,1);assert.equal(submitted.title,'Submitted');assert.equal(submitted.expected.title,'Original');assert.equal(submitted.expected.actors,'Original Actor');
nodes.edit_title.value='Typed while saving';post({});await pending;
assert.equal(nodes.edit_title.value,'Typed while saving');assert.ok(nodes.editDirty.textContent);
assert.equal(context.editBaseline.title,'Submitted');assert.equal(context.selected.actors,'Saved Actor');
assert.match(nodes.actorLinks.innerHTML,/Saved Actor/);assert.match(nodes.autoTagDetail.innerHTML,/Manual tag/);assert.match(nodes.autoTagDetail.innerHTML,/Auto tag/);
assert.match(nodes.fieldLocks.textContent,/actors/);assert.match(nodes.heading.textContent,/Part 20/);
assert.equal(nodes.currentReviewNote.value,'Independent quality draft');assert.equal(button.disabled,false);assert.equal(summaries,1);assert.ok(nav);
''')

    def test_stale_save_cannot_clear_a_new_editor(self):
        self.run_case(r'''
nodes.edit_title.value='Submitted';const pending=context.saveVideo();
context.selected={...baseline,id:2};context.detailGeneration++;nodes.edit_title.value='New view draft';post({});await pending;
assert.equal(nodes.edit_title.value,'New view draft');assert.equal(nodes.editDirty.textContent,'unsaved');assert.equal(loads,0);assert.equal(summaries,0);
''')

    def test_late_reload_cannot_update_a_reopened_same_file(self):
        self.run_case(r'''
nodes.edit_title.value='Submitted';context.api=async()=>({});let finish;
context.serverBrowse.loadMovie=async()=>await new Promise(resolve=>finish=resolve);
const pending=context.saveVideo();while(!finish)await new Promise(resolve=>setImmediate(resolve));
context.detailGeneration++;nodes.edit_title.value='Reopened view draft';finish(work());await pending;
assert.equal(nodes.edit_title.value,'Reopened view draft');assert.equal(context.editBaseline.title,'Original');assert.equal(nodes.editDirty.textContent,'unsaved');assert.equal(summaries,0);
''')

    def test_pending_navigation_takes_precedence_over_a_save_response(self):
        self.run_case(r'''
nodes.edit_title.value='Submitted';context.window.MovieDetailView.generation=1;
const pending=context.saveVideo();context.window.MovieDetailView.generation=2;post({});await pending;
assert.equal(loads,0,'old save cannot replace the cache needed by pending navigation');assert.equal(nodes.edit_title.value,'Submitted');assert.equal(nodes.editDirty.textContent,'unsaved');assert.equal(summaries,0);assert.equal(button.disabled,false);
''')

    def test_repeated_save_does_not_submit_later_input_without_confirmation(self):
        self.run_case(r'''
nodes.edit_title.value='Submitted';let count=0;context.api=async()=>{count++;return await new Promise(resolve=>post=resolve)};
const pending=context.saveVideo();nodes.edit_title.value='Later draft';assert.equal(await context.saveVideo(),false);assert.equal(count,1);
post({});await pending;assert.equal(nodes.edit_title.value,'Later draft');assert.ok(nodes.editDirty.textContent);assert.equal(button.disabled,false);
''')

    def test_save_and_reload_failures_keep_the_form(self):
        self.run_case(r'''
nodes.edit_title.value='Submitted';context.api=async()=>{throw Error('Synthetic save failure')};
assert.equal(await context.saveVideo(),false);assert.equal(nodes.edit_title.value,'Submitted');assert.ok(nodes.editDirty.textContent);assert.equal(button.disabled,false);
context.api=async()=>({});context.serverBrowse.loadMovie=async()=>{throw Object.assign(Error('Synthetic read failure'),{status:500})};
assert.equal(await context.saveVideo(),false);assert.equal(nodes.edit_title.value,'Submitted');assert.ok(nodes.editDirty.textContent);assert.equal(context.editBaseline.title,'Original');assert.equal(button.disabled,false);
''')

    def test_relationship_change_can_retry_without_obsolete_page_offsets(self):
        self.run_case(r'''
nodes.edit_code.value='DEMO-2';fresh.code='DEMO-2';context.api=async()=>({});let attempts=0;
context.serverBrowse.loadMovie=async(id,options)=>{attempts++;if(attempts===1)throw Object.assign(Error('Old page out of range'),{status:400});assert.equal(options.part_offset,undefined);assert.equal(options.version_offset,undefined);return work()};
assert.equal(await context.saveVideo(),true);assert.equal(attempts,2);assert.equal(nodes.edit_code.value,'DEMO-2');assert.equal(nodes.editDirty.textContent,'');assert.equal(button.disabled,false);
''')


if __name__=='__main__':
    unittest.main()
