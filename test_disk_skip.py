"""Skip confirmation stays single-flight and cannot navigate a later view."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


class DiskSkipTests(unittest.TestCase):
    def test_confirmation_locks_checkbox_and_ignores_late_navigation(self):
        source=Path(__file__).with_name('disk_manager.js')
        script=r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(SOURCE,'utf8'),start=source.indexOf('async function diskConfirmSkip(');assert(start>=0);
let release,requests=[],navigations=[],done=false;
const checkbox={checked:true,disabled:false},button={disabled:false};
const context={managerRouteEpoch:1,diskSkipPlan:{id:'original',ordinal:3,digest:'fixed'},managerNavigationState:{params:{offset:50}},$:()=>checkbox,api:(path,body)=>{requests.push({path,body});return new Promise(resolve=>release=resolve)},toast(){},diskGo:(...args)=>navigations.push(args),Number};
vm.createContext(context);vm.runInContext(source.slice(start),context);
(async()=>{
 const pending=context.diskConfirmSkip(button);
 assert.equal(button.disabled,true);assert.equal(checkbox.disabled,true);
 await context.diskConfirmSkip(button);assert.equal(requests.length,1);
 context.managerRouteEpoch=2;context.diskSkipPlan={id:'later',ordinal:7,digest:'different'};
 release({skipped:2});await pending;assert.equal(navigations.length,0);
 assert.equal(requests[0].body.id,'original');assert.equal(requests[0].body.digest,'fixed');
 context.managerRouteEpoch=3;button.disabled=false;checkbox.disabled=false;
 context.api=async()=>{throw Error('range changed')};await context.diskConfirmSkip(button);
 assert.equal(button.disabled,false);assert.equal(checkbox.disabled,false);done=true;
})().catch(error=>{console.error(error);process.exitCode=1});
process.on('beforeExit',()=>{if(!done){console.error('Skip scenario did not finish');process.exitCode=1}});
'''.replace('SOURCE',json.dumps(str(source)))
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'skip-confirmation.cjs';path.write_text(script,encoding='utf-8')
            result=subprocess.run(['node',str(path)],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
