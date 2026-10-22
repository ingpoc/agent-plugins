"""expect.value is not blind past the 8th input (batch-3 #6): the expect
evidence read asks for a bounded larger input list and the targeted field."""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CS = (ROOT / "extension" / "content-scripts" / "cursor-agent.js").read_text()
SW = (ROOT / "extension" / "service_worker.js").read_text()

CS_HARNESS = r"""
const _isVisible=()=>true, _accessibleName=(e)=>e.name||'', pageRevision=1, location={href:'https://x.test/f'};
let INPUTS=[];
const document={title:'Form',querySelectorAll:(sel)=>sel==='input,textarea,select'?INPUTS:[],
  querySelector:(sel)=>{ if(sel==='[[bad') throw new SyntaxError('bad selector'); return INPUTS.find(i=>'#'+i.id===sel)||null; }};
const mk=(n)=>Array.from({length:n},(_,i)=>({name:'f'+(i+1),id:'f'+(i+1),type:'text',tagName:'INPUT',value:'value-'+(i+1)+'-'+'x'.repeat(60),getAttribute:()=>null}));
""" + "%s" + r"""
INPUTS=mk(12);
const dflt=getPageContext(['inputs'],{compact:true});
const wide=getPageContext(['inputs'],{compact:true,input_cap:100,input_value_chars:200,field:'#f12'});
const missing=getPageContext(['inputs'],{compact:true,field:'#nope'});
const bad=getPageContext(['inputs'],{compact:true,field:'[[bad'});
INPUTS[11].value='x'.repeat(9001);
const long=getPageContext(['inputs'],{compact:true,field:'#f12'});
INPUTS=mk(300);
const capped=getPageContext(['inputs'],{compact:true,input_cap:99999});
console.log(JSON.stringify({dflt:dflt.inputs.length,dfltField:'field' in dflt,dfltValLen:dflt.inputs[0].value.length,
  wide:wide.inputs.length,wideVal:wide.inputs[11].value,field:wide.field,missing:missing.field,bad:bad.field,capped:capped.inputs.length,
  longFieldLength:long.field.value.length,longListLength:long.inputs[0].value.length}));
"""

SW_HARNESS = r"""
const chrome={tabs:{get:async()=>({url:'https://x.test/f',title:'Form'})}};
const calls=[];
function codedError(c,m){const e=new Error(m);e.code=c;return e;}
function requireContentScriptResult(resp){return resp.result;}
async function sendToContentScript(tabId,action,args){calls.push({action,args});
  const opts=args[1]||{}; const n=Math.min(12,opts.input_cap||8);
  const inputs=Array.from({length:n},(_,i)=>({name:'f'+(i+1),value:'v'+(i+1)})); inputs.push({name:'f3x',value:'v3'});
  const result={url:'https://x.test/f',title:'Form',inputs};
  if(opts.field) result.field= opts.field==='#f12'?{found:true,value:'v12'}:{found:false};
  return {result};}
""" + "%s" + r"""
(async()=>{
  const state={tabId:1};
  const p1=[{kind:'value',value:'v12'}];
  const e1=await collectExpectEvidence(p1,{type:'locator'},null,state);
  const p2=[{kind:'value',value:'v12',selector:'#f12'}];
  const e2=await collectExpectEvidence(p2,{type:'fill'},null,state);
  // expect on a compact page_context action: its own result carries only 8 inputs.
  const pc={type:'page_context',url:'https://x.test/f',title:'Form',headings:['H'],
    inputs:Array.from({length:8},(_,i)=>({name:'f'+(i+1),value:'v'+(i+1)}))};
  const before=calls.length;
  const e3=await collectExpectEvidence(p1,{type:'page_context'},pc,state);
  const pcCalls=calls.length-before;
  // selector-scoped: another input holding the value must not satisfy it
  const p6=[{kind:'value',value:'v3',selector:'#f3x'}];   // a name-only lookalike must not count
  const e6=await collectExpectEvidence(p6,{type:'fill'},null,state);
  const p4=[{kind:'value',value:'v3',selector:'#nope'}];
  const e4=await collectExpectEvidence(p4,{type:'fill'},null,state);
  const p5=[{kind:'value',value:'v3'}];   // scoped by the action's selector #f12 (value v12)
  const e5=await collectExpectEvidence(p5,{type:'fill_selector',selector:'#f12'},null,state);
  console.log(JSON.stringify({miss1:unmatchedExpect(p1,e1),miss2:unmatchedExpect(p2,e2),sel:e2.selector_value,
    opts:calls.map(c=>c.args[1]), n1:e1.inputs.length, miss3:unmatchedExpect(p1,e3), pcCalls, miss4:unmatchedExpect(p4,e4), miss5:unmatchedExpect(p5,e5), miss6:unmatchedExpect(p6,e6)}));
})().catch(e=>{console.error(e);process.exit(1);});
"""


def _cs_fn() -> str:
    start = CS.index("  function getPageContext(sections, options) {")
    return CS[start:CS.index("  function flashLabel(label)")]


def _sw_fns() -> str:
    start = SW.index("const EXPECT_INPUT_CAP")
    end = SW.index("function failureScreenshotNeeded")
    collect = SW[SW.index("async function collectExpectEvidence"):SW.index("async function verifyActionExpect")]
    return SW[start:end] + collect


@unittest.skipUnless(shutil.which("node"), "node required")
class ExpectValueInputs(unittest.TestCase):
    def _node(self, src):
        proc = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_content_script_bounded_inputs_and_direct_field(self):
        r = self._node(CS_HARNESS % _cs_fn())
        self.assertEqual((r["dflt"], r["dfltField"], r["dfltValLen"]), (8, False, 40))  # agent payload unchanged
        self.assertEqual(r["wide"], 12)
        self.assertTrue(r["wideVal"].startswith("value-12-"))
        self.assertEqual(r["field"]["found"], True)
        self.assertTrue(r["field"]["value"].startswith("value-12-"))
        self.assertEqual(r["missing"], {"found": False})
        self.assertIn("bad selector", r["bad"]["error"])
        self.assertEqual(r["capped"], 200)
        self.assertEqual(r["longFieldLength"], 9001)
        self.assertEqual(r["longListLength"], 40)  # normal agent context stays compact

    def test_expect_value_past_eighth_input_passes(self):
        r = self._node(SW_HARNESS % _sw_fns())
        self.assertIsNone(r["miss1"])
        self.assertIsNone(r["miss2"])
        self.assertEqual(r["sel"], "v12")
        self.assertEqual(r["n1"], 13)  # 12 fN + the f3x lookalike
        self.assertEqual(r["opts"][0], {"compact": True, "input_cap": 100, "input_value_chars": 200})
        self.assertEqual(r["opts"][1]["field"], "#f12")
        self.assertIsNone(r["miss3"])  # page_context result does not short-circuit value checks
        self.assertEqual(r["pcCalls"], 1)
        self.assertIsNotNone(r["miss4"])  # target missing -> miss, even though input f3 holds v3
        self.assertIsNotNone(r["miss5"])  # action selector #f12 holds v12, not v3
        self.assertIsNotNone(r["miss6"])  # input NAMED f3x is not the #f3x direct read


if __name__ == "__main__":
    unittest.main()
