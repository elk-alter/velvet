"""Selection state survives paging, freezes exclusions and restores source filters."""
import subprocess
import unittest
from pathlib import Path


class DiskSelectionTests(unittest.TestCase):
    def test_cross_page_all_except_and_context_reset(self):
        program=r"""
const vm=require('vm'),fs=require('fs'),assert=require('assert');let navigation,notices=[];
const fields={diskPhysicalScope:{},diskPhysicalPlan:{}};
const context={window:{},document:{getElementById:id=>fields[id],querySelectorAll:()=>[]},diskBytes:value=>String(value),esc:value=>String(value),toast:message=>notices.push(message),goManage:(route,params)=>navigation={route,params}};
vm.createContext(context);vm.runInContext(fs.readFileSync('disk_selection.js','utf8'),context);
const selection=context.window.DiskFileSelection;
const first={snapshot:'fixed-one',total:3,bytes:60,criteria:{device:'all'},items:[{query_ordinal:0,size:10},{query_ordinal:1,size:20}]};
selection.load(first,{search:'demo',offset:0});selection.page();
assert.strictEqual(selection.payload().selected.length,2);
selection.load({...first,items:[{query_ordinal:2,size:30}]},{search:'demo',offset:50});selection.page();
assert.strictEqual(selection.payload().selected.length,3);
selection.everything();selection.toggle({checked:false,dataset:{ordinal:'1',bytes:'20'}});
assert.strictEqual(selection.payload().selected.exclude[0],1);
selection.plan();assert.strictEqual(navigation.route,'organization-plan');assert.strictEqual(navigation.params.selection_device,'all');
selection.back(navigation.params);assert.strictEqual(navigation.params.source,'all');assert.strictEqual(navigation.params.search,'demo');assert.strictEqual(navigation.params.offset,50);
selection.load({...first,snapshot:'fixed-two'},{search:'other'});
assert.throws(()=>selection.payload('fixed-one'));assert.throws(()=>selection.payload());assert.strictEqual(fields.diskPhysicalPlan.disabled,true);assert.strictEqual(notices.length,1);
selection.everything();selection.clear();assert.throws(()=>selection.payload());
"""
        result=subprocess.run(['node','-e',program],cwd=Path(__file__).parent,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
