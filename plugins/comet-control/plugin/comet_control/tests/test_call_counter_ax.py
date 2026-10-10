"""Per-run call ledger (#33/#94)."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SW = (ROOT / "extension" / "service_worker.js").read_text()
PARITY = (ROOT / "extension" / "parity_capabilities.js").read_text()
CTRL = ROOT.parents[1] / "skills" / "comet-control" / "scripts" / "durable_lease_controller.py"


def _controller():
    spec = importlib.util.spec_from_file_location("cc_dlc_calls", CTRL)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class CallCounterContract(unittest.TestCase):
    def test_wraps_the_three_choke_apis_once(self):
        for api in ('chrome.debugger, "sendCommand"', 'chrome.scripting, "executeScript"', 'chrome.tabs, "sendMessage"'):
            self.assertEqual(SW.count(f"wrapCountedApi({api}"), 1, api)

    def test_run_state_and_reply_carry_calls(self):
        self.assertIn("calls: newRunCallLedger(),", SW)
        self.assertIn("telemetry: { calls: state.calls },", SW)
        self.assertIn("error.failureRecord.calls = state.calls", SW)

    def test_counter_never_changes_call_arguments(self):
        body = SW[SW.index("function wrapCountedApi"):SW.index("const callCounterInstalled")]
        self.assertIn("return original.apply(this, args);", body)

    def test_counter_counts_per_tab_in_node(self):
        harness = r"""
const activeRunStates = new Set();
""" + SW[SW.index("function newRunCallLedger"):SW.index("].every(Boolean);") + len("].every(Boolean);")].replace(
            "const callCounterInstalled", "var callCounterInstalled") + r"""
"""
        node = "const chrome={debugger:{sendCommand:(t,m,p)=>m},scripting:{executeScript:(o)=>1},tabs:{sendMessage:(t,m)=>2}};" + harness + r"""
const a={tabId:1,calls:null}; const b={tabId:2,calls:null};
activeRunStates.add(a); activeRunStates.add(b); a.calls=newRunCallLedger(); b.calls=newRunCallLedger();
if (chrome.debugger.sendCommand({tabId:1},"Input.dispatchMouseEvent",{})!=="Input.dispatchMouseEvent") throw 1;
chrome.debugger.sendCommand({tabId:1},"Input.dispatchMouseEvent",{}); chrome.scripting.executeScript({target:{tabId:1}});
chrome.tabs.sendMessage(2,{}); console.log(JSON.stringify([a.calls,b.calls]));
"""
        out = subprocess.run(["node", "-e", node], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        a, b = json.loads(out.stdout)
        self.assertEqual((a["counter_installed"], a["cdp_total"], a["cdp"], a["scripting"], a["messages"]),
                         (True, 2, {"Input.dispatchMouseEvent": 2}, 1, 0))
        self.assertEqual((b["cdp_total"], b["scripting"], b["messages"]), (0, 0, 1))


class ControllerCallsLedger(unittest.TestCase):
    def test_append_and_status_total(self):
        c = _controller()
        with tempfile.TemporaryDirectory() as tmp:
            w = Path(tmp)
            ok = {"event": "run", "response": {"success": True, "telemetry": {"calls": {"cdp_total": 4, "cdp": {"Runtime.evaluate": 4},
                                                                                       "scripting": 2, "messages": 3, "counter_installed": True}}}}
            bad = {"event": "run", "response": {"success": False, "failure_record": {"calls": {"cdp_total": 1, "scripting": 0, "messages": 1}}}}
            c._append_calls(w, 1, ok)
            c._append_calls(w, 2, bad)
            c._append_calls(w, 3, {"event": "closeout", "response": {"success": True}})
            self.assertEqual(len((w / "calls.jsonl").read_text().splitlines()), 2)
            self.assertEqual(c._calls_total(w), {"runs": 2, "cdp": 5, "scripting": 2, "messages": 4, "all": 11})


if __name__ == "__main__":
    unittest.main()
