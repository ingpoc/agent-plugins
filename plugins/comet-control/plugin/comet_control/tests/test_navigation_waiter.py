"""Event-based navigation waiter: scripting poison clears only when the NEW
document (committed after arming) completes; never on a timer (Codex batch-3 #3)."""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SW = (ROOT / "extension" / "service_worker.js").read_text()


def _extract() -> str:
    start = SW.index("const NAV_POISON_CLEAR_MAX_MS")
    end = SW.index("\n}\n", SW.index("function requireNavigation")) + 3
    return (SW[start:end].replace("NAV_POISON_CLEAR_MAX_MS = 8000", "NAV_POISON_CLEAR_MAX_MS = 120")
            .replace("NAV_COMMIT_MAX_MS = 15000", "NAV_COMMIT_MAX_MS = 120"))


HARNESS = r"""
function ev(){const l=new Set();return{addListener:f=>l.add(f),removeListener:f=>l.delete(f),fire:(...a)=>{for(const f of [...l])f(...a)},size:()=>l.size};}
let frameDoc=null;
const chrome={webNavigation:{onCommitted:ev(),onCompleted:ev(),onErrorOccurred:ev(),onReferenceFragmentUpdated:ev(),onHistoryStateUpdated:ev(),
  getFrame:async()=>({documentId:frameDoc})},tabs:{onUpdated:ev()}};
const tabScriptingPoisoned=new Set(); const invalidated=[];
function invalidateTabInjection(t){invalidated.push(t);}
function assertRequestLive(s){if(s.cancelled)throw new Error('cancelled');}
""" + "%s" + r"""
const W=chrome.webNavigation, T=chrome.tabs;
const tick=()=>new Promise(r=>setTimeout(r,5));
async function scenario(name){
  tabScriptingPoisoned.clear(); tabScriptingPoisoned.add(7); frameDoc='OLD';
  const w=armNavigationWaiter(7); const p=settleArmedNavigation(w,7,(name==='timeout'||name==='slow_commit')?60:400,{});
  await tick();
  if(name==='new_doc_complete'){
    T.onUpdated.fire(7,{status:'complete'});            // stale: before commit
    W.onCompleted.fire({tabId:7,frameId:0,documentId:'OLD'});
    W.onCommitted.fire({tabId:7,frameId:3,documentId:'IFR'}); // subframe ignored
    W.onCommitted.fire({tabId:7,frameId:0,documentId:'NEW'}); frameDoc='NEW';
    W.onCompleted.fire({tabId:7,frameId:0,documentId:'OLD'}); // other doc ignored
    await tick(); T.onUpdated.fire(7,{status:'complete'});
  } else if(name==='completed_event'){
    W.onCommitted.fire({tabId:7,frameId:0,documentId:'NEW'}); frameDoc='NEW';
    W.onCompleted.fire({tabId:7,frameId:0,documentId:'NEW'});
  } else if(name==='same_document'){
    W.onReferenceFragmentUpdated.fire({tabId:7,frameId:0,documentId:'OLD'});
  } else if(name==='superseded'){
    W.onCommitted.fire({tabId:7,frameId:0,documentId:'A'}); frameDoc='B';
    W.onCommitted.fire({tabId:7,frameId:0,documentId:'B'});
    W.onCompleted.fire({tabId:7,frameId:0,documentId:'A'});
    await tick(); W.onCompleted.fire({tabId:7,frameId:0,documentId:'B'});
  } else if(name==='aborted_then_commit'){
    W.onErrorOccurred.fire({tabId:7,frameId:0,error:'net::ERR_ABORTED'});
    W.onCommitted.fire({tabId:7,frameId:0,documentId:'R'}); frameDoc='R';
    W.onCompleted.fire({tabId:7,frameId:0,documentId:'R'});
  } else if(name==='slow_commit'){
    await new Promise(r=>setTimeout(r,90));               // after waitMs (60), inside commit max
    W.onCommitted.fire({tabId:7,frameId:0,documentId:'S'}); frameDoc='S';
  } else if(name==='error'){
    W.onErrorOccurred.fire({tabId:7,frameId:0,error:'net::ERR_CONNECTION_REFUSED'});
  }
  const nav=await p; w.dispose();
  let required='ok'; try{requireNavigation(nav,name);}catch(e){required=e.code;}
  const leaked=[W.onCommitted,W.onCompleted,W.onErrorOccurred,W.onReferenceFragmentUpdated,W.onHistoryStateUpdated,T.onUpdated].some(e=>e.size());
  return {name,complete:nav.complete,same_document:nav.same_document,error:nav.error,poison_cleared:nav.poison_cleared,poisoned:nav.poisoned,document_id:nav.document_id,leaked,required};
}
(async()=>{const out=[];for(const n of ['new_doc_complete','completed_event','same_document','superseded','error','timeout','aborted_then_commit','slow_commit'])out.push(await scenario(n));
console.log(JSON.stringify({out,invalidated}));})().catch(e=>{console.error(e);process.exit(1);});
"""


@unittest.skipUnless(shutil.which("node"), "node required")
class NavigationWaiter(unittest.TestCase):
    def test_poison_clears_only_on_new_document_complete(self):
        proc = subprocess.run(["node", "-e", HARNESS % _extract()], capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        res = {r["name"]: r for r in data["out"]}
        for r in data["out"]:
            self.assertFalse(r["leaked"], r)
        self.assertEqual((res["new_doc_complete"]["complete"], res["new_doc_complete"]["poison_cleared"],
                          res["new_doc_complete"]["document_id"]), (True, True, "NEW"))
        self.assertTrue(res["completed_event"]["poison_cleared"])
        self.assertEqual((res["superseded"]["complete"], res["superseded"]["document_id"]), (True, "B"))
        for name in ("same_document", "error", "timeout"):
            self.assertFalse(res[name]["poison_cleared"], name)
            self.assertTrue(res[name]["poisoned"], name)
        self.assertTrue(res["same_document"]["same_document"])
        self.assertEqual(res["error"]["error"], "net::ERR_CONNECTION_REFUSED")
        self.assertFalse(res["timeout"]["complete"])
        self.assertEqual(data["invalidated"], [7] * 8)
        required = {name: r["required"] for name, r in res.items()}
        self.assertEqual(required, {
            "new_doc_complete": "ok", "completed_event": "ok", "same_document": "ok", "superseded": "ok",
            "error": "NAVIGATION_FAILED", "timeout": "NAVIGATION_TIMEOUT",
            "aborted_then_commit": "ok", "slow_commit": "ok"})
        self.assertTrue(res["aborted_then_commit"]["poison_cleared"])
        self.assertEqual(res["slow_commit"]["document_id"], "S")


if __name__ == "__main__":
    unittest.main()
