"""Actual async form handlers must preserve a later page or later input."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


class ReconnectClientTests(unittest.TestCase):
    def test_confirm_single_flight_failure_and_later_page_are_isolated(self):
        self.run_case(r'''
vm.runInContext(source.slice(source.indexOf('async function diskConfirmReconnect(')),context);
context.diskReconnectState={device:{id:'old'},preview:{root:'synthetic',previous_identity:'volume',digest:'fixed'}};
const pending=context.diskConfirmReconnect(nodes.button);
assert.equal(nodes.button.disabled,true);await context.diskConfirmReconnect(nodes.button);assert.equal(requests.length,1);
context.managerRouteEpoch=2;context.diskReconnectState={device:{id:'later'},preview:{digest:'later'}};
release({ok:true});await pending;assert.equal(navigations.length,0);assert.equal(context.managerDirty,true);
assert.equal(requests[0].body.id,'old');assert.equal(requests[0].body.digest,'fixed');
nodes.button.disabled=false;context.api=async()=>{throw Error('scope changed')};await context.diskConfirmReconnect(nodes.button);
assert.equal(context.diskReconnectState.preview,null);assert.equal(nodes.button.disabled,false);assert.match(nodes.diskReconnectReview.innerHTML,/scope changed/);
''')

    def test_register_response_does_not_discard_newer_input(self):
        self.run_case(r'''
vm.runInContext(source.slice(source.indexOf('async function diskRegister(){'),source.indexOf('async function diskScan(')),context);
const pending=context.diskRegister();nodes.diskRoot.value='Later synthetic root';release({ok:true});await pending;
assert.equal(navigations.length,0);assert.equal(context.managerDirty,true);assert.equal(nodes.diskRoot.value,'Later synthetic root');
assert.equal(nodes.button.disabled,false);assert.equal(requests[0].body.path,'Original synthetic root');
''')

    def run_case(self,body):
        path=Path(__file__).with_name('disk_manager.js')
        script=r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(SOURCE,'utf8');let release,requests=[],navigations=[],finished=false;
const nodes={button:{disabled:false},diskRoot:{value:'Original synthetic root'},diskAlias:{value:'Synthetic'},diskNewLocation:{checked:false},diskReconnectConfirmed:{checked:true},diskReconnectReview:{innerHTML:''}};
const context={managerRouteEpoch:1,managerDirty:true,$:id=>id==='diskRegisterRun'?nodes.button:nodes[id],toast(){},esc:String,diskLockReconnect:locked=>nodes.button.disabled=locked,diskReconnectCanConfirm(){},diskGo:(...args)=>navigations.push(args),api:(path,body)=>{requests.push({path,body});return new Promise(resolve=>release=resolve)}};
vm.createContext(context);
(async()=>{BODY;finished=true})().catch(error=>{console.error(error);process.exitCode=1});
process.on('beforeExit',()=>{if(!finished){console.error('Form scenario did not complete');process.exitCode=1}});
'''.replace('SOURCE',json.dumps(str(path))).replace('BODY',body)
        with tempfile.TemporaryDirectory() as temporary:
            target=Path(temporary)/'reconnect-form.cjs';target.write_text(script,encoding='utf-8')
            result=subprocess.run(['node',str(target)],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
