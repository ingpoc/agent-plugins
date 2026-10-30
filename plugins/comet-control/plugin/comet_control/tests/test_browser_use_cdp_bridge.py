#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import threading
import subprocess
from unittest.mock import Mock
import unittest
from pathlib import Path

from websockets.sync.client import connect


PLUGIN_ROOT = Path(__file__).resolve().parents[3]
BRIDGE = PLUGIN_ROOT / "skills/comet-control/scripts/browser_use_cdp_bridge.py"
PARITY = PLUGIN_ROOT / "plugin/comet_control/extension/parity_capabilities.js"


def load_bridge():
    spec = importlib.util.spec_from_file_location("browser_use_cdp_bridge", BRIDGE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BrowserUseCDPBridgeTests(unittest.TestCase):
    def test_bridge_exposes_only_the_leased_tab_and_keeps_capability_private(
        self,
    ) -> None:
        module = load_bridge()
        actions: list[dict] = []

        def run_action(action: dict) -> dict:
            actions.append(action)
            if action["type"] == "cdp_events":
                return {"cursor": 0, "events": [], "has_more": False}
            return {"method": action.get("method", "")}

        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "browser-use.env"
            bridge = module.BrowserUseCDPBridge(
                "session-a",
                run_action,
                lambda: {"url": "https://example.com", "title": "Example"},
                lambda: actions.append({"type": "hidden"}),
            )
            public = bridge.start(env_file)
            self.assertNotIn("ws://", json.dumps(public))
            self.assertEqual(os.stat(env_file).st_mode & 0o777, 0o600)
            ws_url = env_file.read_text().split("BU_CDP_WS=", 1)[1].splitlines()[0]
            with connect(ws_url) as websocket:
                websocket.send(json.dumps({"id": 1, "method": "Target.getTargets"}))
                targets = json.loads(websocket.recv())["result"]["targetInfos"]
                self.assertEqual(len(targets), 1)
                self.assertEqual(targets[0]["url"], "https://example.com")
                websocket.send(
                    json.dumps(
                        {
                            "id": 2,
                            "method": "Input.dispatchMouseEvent",
                            "params": {"type": "mousePressed", "x": 12, "y": 34},
                            "sessionId": bridge.session_id,
                        }
                    )
                )
                response = json.loads(websocket.recv())
                self.assertEqual(response["id"], 2)
                self.assertEqual(actions[-1]["method"], "Input.dispatchMouseEvent")
                websocket.send(
                    json.dumps(
                        {
                            "id": 3,
                            "method": "Target.getTargetInfo",
                            "params": {"targetId": "foreign"},
                        }
                    )
                )
                self.assertIn(
                    "outside the leased Comet tab",
                    json.loads(websocket.recv())["error"]["message"],
                )
            bridge.stop()
            self.assertIn({"type": "hidden"}, actions)

    def test_dead_client_slot_is_handed_over_while_its_call_is_in_flight(self):
        """2026-09-26 d620efc3: a killed daemon's handler held the slot until its
        blocked serialized call returned, so the respawn got 1008."""
        import socket
        from websockets.exceptions import ConnectionClosed
        from websockets.protocol import State

        module = load_bridge()
        release = threading.Event()
        entered = threading.Event()
        # Keyed by the connection object (strong ref): id() of a collected rival
        # can be reused by the successor and alias its already-set event.
        exits: dict[object, threading.Event] = {}

        class ProbeBridge(module.BrowserUseCDPBridge):
            def _handle(self, websocket):
                done = exits.setdefault(websocket, threading.Event())
                try:
                    super()._handle(websocket)
                finally:
                    done.set()

        def run_action(action: dict) -> dict:
            if action["type"] == "cdp_events":
                return {"cursor": 0, "events": [], "has_more": False}
            if action.get("method") == "Runtime.evaluate" and not release.is_set():
                entered.set()
                release.wait()  # set by the test (finally) only
            return {"method": action.get("method", "")}

        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "browser-use.env"
            bridge = ProbeBridge(
                "session-slot", run_action, lambda: {"url": "u", "title": "t"},
                lambda: None,
            )
            bridge.start(env_file)
            try:
                ws_url = env_file.read_text().split("BU_CDP_WS=", 1)[1].splitlines()[0]
                ws_url = ws_url.strip("'")
                first = connect(ws_url)
                first.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": "1"},
                                       "sessionId": bridge.session_id}))
                self.assertTrue(entered.wait(5))
                dead = bridge._active_websocket
                self.assertIsNotNone(dead)
                # A second live client is still refused.
                with connect(ws_url) as rival:
                    with self.assertRaises(ConnectionClosed) as ctx:
                        rival.recv(timeout=5)
                    self.assertIn("already has a Browser Use client", str(ctx.exception))
                # Kill the first client abruptly, as process death does: the kernel
                # sends FIN. A bare close() does not on Linux while the client's
                # reader thread is blocked in recv() on that fd, so shut it down first.
                first.socket.shutdown(socket.SHUT_RDWR)
                first.socket.close()
                # The server notices EOF via its reader thread; wait on that, fail loud.
                dead.recv_events_thread.join(timeout=5)
                self.assertFalse(dead.recv_events_thread.is_alive(), "server never saw the dead peer")
                self.assertIn(dead.state, (State.CLOSING, State.CLOSED))
                self.assertFalse(exits[dead].is_set(), "old handler must still be blocked")
                with connect(ws_url) as second:
                    second.send(json.dumps({"id": 7, "method": "Target.getTargets"}))
                    self.assertEqual(json.loads(second.recv(timeout=5))["id"], 7)
                    successor = bridge._active_websocket
                    self.assertIsNot(successor, dead)
                    release.set()
                    # The old handler finishes; it must not clear the new owner.
                    self.assertTrue(exits[dead].wait(5), "old handler never returned")
                    self.assertTrue(bridge._client_active)
                    self.assertIs(bridge._active_websocket, successor)
                    second.send(json.dumps({"id": 8, "method": "Target.getTargets"}))
                    self.assertEqual(json.loads(second.recv(timeout=5))["id"], 8)
                self.assertTrue(exits[successor].wait(5), "successor handler never returned")
                self.assertFalse(bridge._client_active)
            finally:
                release.set()  # explicit only; never auto-released mid-test
                bridge.stop()

    def test_destructive_page_commands_cannot_bypass_closeout(self):
        module = load_bridge()
        bridge = object.__new__(module.BrowserUseCDPBridge)
        bridge.run_action = Mock()
        for method in ("Page.close", "Page.crash", "Target.closeTarget"):
            with self.assertRaises(module.CDPError):
                bridge._browser_command(method, {})
        bridge.run_action.assert_not_called()

    def test_event_backlog_drains_without_poll_delays(self):
        module = load_bridge()
        bridge = object.__new__(module.BrowserUseCDPBridge)
        bridge.session_id = "test"
        stop = Mock()
        stop.is_set.side_effect = [False, False, True]
        bridge.run_action = Mock(side_effect=[
            {"cursor": 200, "events": [], "has_more": True},
            {"cursor": 201, "events": [{"method": "Page.loadEventFired"}]},
        ])
        socket = Mock()
        bridge._event_pump(socket, stop, threading.Lock(), 0)
        stop.wait.assert_called_once_with(0.1)
        self.assertEqual(bridge.run_action.call_args.args[0]["afterSequence"], 200)
        socket.send.assert_called_once()

    def test_transient_event_failure_recovers_after_extension_reload(self):
        module = load_bridge()
        bridge = object.__new__(module.BrowserUseCDPBridge)
        bridge.session_id = "test"
        stop = Mock()
        stop.is_set.side_effect = [False, False, True]
        bridge.run_action = Mock(side_effect=[RuntimeError("reload"), {"cursor": 1, "events": [], "has_more": False}])
        socket = Mock()
        bridge._event_pump(socket, stop, threading.Lock(), 0)
        socket.close.assert_not_called()
        self.assertEqual(bridge.run_action.call_count, 2)

    def test_event_overflow_closes_socket_instead_of_hanging(self):
        module = load_bridge()
        bridge = object.__new__(module.BrowserUseCDPBridge)
        bridge.session_id = "test"
        bridge.run_action = Mock(return_value={"truncated": True})
        socket = Mock()
        bridge._event_pump(socket, threading.Event(), threading.Lock(), 0)
        socket.close.assert_called_once()
        self.assertEqual(socket.close.call_args.kwargs["code"], 1011)

    def test_cursor_releases_only_on_confirmed_arrival(self):
        cursor = PARITY.parent / "content-scripts/cursor-agent.js"
        source = cursor.read_text()
        functions = source[source.index("  const GLIDE_MS"):source.index("  function pulseClick")]
        harness = r"""
const assert = require('node:assert/strict');
let cursorX=0, cursorY=0, isVisible=true, cursorPhase='idle', animCalls=0, finish=null, stuck=false, lie=false;
const cls=new Set(['comet-control-visible']);
let running=null;
const cursorEl={classList:{add:(c)=>cls.add(c),remove:(c)=>cls.delete(c)},style:{transform:'translate(0px, 0px)'},
  addEventListener(){}, removeEventListener(){},
  animate(frames,opts){animCalls++; const a={frames,opts,cancel(){if(running===a)running=null}};
    a.finished=stuck?new Promise(()=>{}):new Promise(r=>{finish=()=>{running=null;r()};setTimeout(finish,5)});
    running=a; return a;}};
const document={visibilityState:'visible'};
const requestAnimationFrame=(f)=>setTimeout(f,1);
function getComputedStyle(){ if(lie) return {transform:'matrix(1, 0, 0, 1, -100, -100)'};
  if(running){const f=running.frames[0].transform.match(/-?[\d.]+/g);return {transform:`matrix(1, 0, 0, 1, ${f[0]}, ${f[1]})`};}
  const m=cursorEl.style.transform.match(/-?[\d.]+/g);return {transform:`matrix(1, 0, 0, 1, ${m[0]}, ${m[1]})`};}
function createOverlay(){}
function hide(){cls.delete('comet-control-visible');isVisible=false;}
const getStatus=()=>({visible:isVisible});
""" + functions + r"""
(async()=>{
  assert.equal(glideDurationMs(0,24),200);
  assert.ok(Math.abs(glideDurationMs(100,32)-283.3)<1);
  assert.equal(glideDurationMs(1022,40),450);
  assert.equal(glideDurationMs(400,4),glideDurationMs(400,8));  // tiny targets floor at 8px
  const p=planGlide(0,0,400,0,32); const last=p.frames.at(-1);
  assert.deepEqual(last,[400,0]); assert.ok(p.arrivalMs<=p.duration);
  assert.ok(Math.min(...p.frames.map(f=>f[1]))<-10);  // light arc, not straight
  assert.ok(Math.min(...p.frames.map(f=>f[1]))>=-40);
  // 1) WAAPI glide: resolves only after finished + measured tip within 1px.
  let r=await moveToAndWait(400,0,900,{w:80,h:32});
  assert.equal(r.arrival.method,'waapi'); assert.equal(r.arrival.confirmed,true); assert.equal(r.arrival.error_px,0);
  assert.equal(animCalls,1); assert.equal(r.arrival.target_w,32);
  // 2) Same point again: stationary, measured, no new glide.
  r=await moveToAndWait(400,0,900); assert.equal(r.arrival.confirmed,true); assert.equal(animCalls,1);
  // 3) Animation never finishes: deadline selects hide + snap retry, never a timer "arrival".
  stuck=true; const t0=Date.now(); r=await moveToAndWait(0,300,900);
  assert.equal(r.arrival.method,'snap_retry'); assert.equal(r.arrival.confirmed,true);
  assert.ok(Date.now()-t0 < 700); stuck=false;
  // 4) Hidden document: no glide, snap + measure.
  document.visibilityState='hidden'; r=await moveToAndWait(300,300,900);
  assert.equal(r.arrival.method,'snap'); assert.equal(animCalls,2); assert.equal(r.arrival.confirmed,true);
  document.visibilityState='visible';
  // 5) Rendered tip never reaches target: report unconfirmed and stay hidden.
  lie=true; r=await moveToAndWait(10,10,900);
  assert.equal(r.arrival.confirmed,false); assert.equal(r.arrival.method,'unconfirmed'); assert.equal(isVisible,false);
  assert.ok(!cls.has('comet-control-visible'));
})().catch(e=>{console.error(e);process.exit(1)});
"""
        subprocess.run(["node", "-e", harness], check=True, capture_output=True)

    def test_extension_moves_visible_pointer_before_cdp_mouse_press(self) -> None:
        source = PARITY.read_text()
        branch = source[
            source.index('if (action.type === "browser_use_cdp")') : source.index(
                'if (action.type === "cdp_send")'
            )
        ]
        self.assertLess(
            branch.index('"moveToAndWait"'),
            branch.index("hooks.send(state.tabId, method, params)"),
        )
        self.assertIn('"pulseClick"', branch)


POLICY = 'DOM.getDocument needs chrome.debugger, which browser policy blocks on this Comet ("Host access is restricted by policy.")'


class FakeUploadPage:
    """Python stand-in for the userScripts evaluate path used by the upload fallback."""

    def __init__(self, element: dict | None, dom_error: str | None = POLICY) -> None:
        import base64 as _b64
        import re as _re
        self._b64, self._re = _b64, _re
        self.element = element
        self.dom_error = dom_error
        self.store: dict[str, str] = {}
        self.calls: list[tuple[str, dict]] = []
        self.files: list[tuple[str, int, str]] = []

    def run_action(self, action: dict) -> dict:
        method, params = action["method"], action.get("params") or {}
        self.calls.append((method, params))
        if method.startswith("DOM."):
            if self.dom_error:
                raise RuntimeError(self.dom_error.replace("DOM.getDocument", method))
            return {"root": {"nodeId": 7}} if method == "DOM.getDocument" else {"nodeId": 9}
        expr = params["expression"]
        if expr.startswith("!!document.querySelector"):
            value = self.element is not None
        elif "not an input[type=file]" in expr:
            el = self.element
            if el is None:
                value = {"ok": False, "why": "no element"}
            elif el["tag"] != "INPUT" or el["type"] != "file":
                value = {"ok": False, "why": "not an input[type=file]"}
            else:
                value = {"ok": True, "multiple": el.get("multiple", False)}
        elif "g.__ccUpload[" in expr and "+ " in expr:
            key = json.loads(self._re.search(r"g\.__ccUpload\[(\"[^\"]+\")\] = ", expr).group(1))
            chunk = json.loads(self._re.search(r"\+ (\"[^\"]*\");", expr).group(1))
            self.store[key] = self.store.get(key, "") + chunk
            value = len(self.store[key])
        elif "new DataTransfer()" in expr:
            metas = json.loads(self._re.search(r"const metas = (\[.*?\]);", expr).group(1))
            self.files = [(m["name"], len(self._b64.b64decode(self.store.get(m["key"], ""))), m["type"]) for m in metas]
            for m in metas:
                self.store.pop(m["key"], None)
            value = {"ok": True, "count": len(self.files), "names": [f[0] for f in self.files], "sizes": [f[1] for f in self.files]}
        else:
            value = True
        return {"result": {"type": type(value).__name__, "value": value}}


class UploadFallbackTests(unittest.TestCase):
    def make(self, page: FakeUploadPage):
        module = load_bridge()
        bridge = module.BrowserUseCDPBridge("upload", page.run_action, lambda: {"url": "http://t/"}, lambda: None)
        self.addCleanup(bridge.server.socket.close)
        return module, bridge

    def upload(self, bridge, selector: str, files: list) -> dict:
        doc = bridge._browser_command("DOM.getDocument", {"depth": -1})
        nid = bridge._browser_command("DOM.querySelector", {"nodeId": doc["root"]["nodeId"], "selector": selector})["nodeId"]
        if not nid:
            raise RuntimeError(f"no element for {selector}")
        return bridge._browser_command("DOM.setFileInputFiles", {"files": files, "nodeId": nid})

    def tmpfile(self, size: int, name: str = "cover.png") -> Path:
        directory = Path(tempfile.mkdtemp())
        path = directory / name
        path.write_bytes(os.urandom(size))
        return path

    def test_policy_block_uses_shim_and_chunks_bytes(self) -> None:
        page = FakeUploadPage({"tag": "INPUT", "type": "file"})
        module, bridge = self.make(page)
        module.UPLOAD_CHUNK_CHARS = 1000
        path = self.tmpfile(5000)
        self.assertEqual(self.upload(bridge, "input[type=file]", [str(path)]), {})
        self.assertEqual(page.files, [("cover.png", 5000, "image/png")])
        staged = [c for c in page.calls if "g.__ccUpload[" in c[1].get("expression", "") and "+ " in c[1]["expression"]]
        self.assertGreater(len(staged), 5)
        self.assertEqual(page.store, {})

    def test_missing_element_returns_zero_node(self) -> None:
        page = FakeUploadPage(None)
        _module, bridge = self.make(page)
        bridge._browser_command("DOM.getDocument", {})
        self.assertEqual(bridge._browser_command("DOM.querySelector", {"nodeId": 1, "selector": "#x"}), {"nodeId": 0})

    def test_non_file_input_and_bad_path_fail_clearly(self) -> None:
        module, bridge = self.make(FakeUploadPage({"tag": "DIV", "type": ""}))
        with self.assertRaisesRegex(module.CDPError, "userScripts DataTransfer.*not an input"):
            self.upload(bridge, "#d", [str(self.tmpfile(10))])
        module, bridge = self.make(FakeUploadPage({"tag": "INPUT", "type": "file"}))
        with self.assertRaisesRegex(module.CDPError, "absolute path"):
            self.upload(bridge, "#f", ["relative.png"])
        with self.assertRaisesRegex(module.CDPError, "multiple"):
            self.upload(bridge, "#f", [str(self.tmpfile(5)), str(self.tmpfile(5, "b.png"))])

    def test_other_dom_errors_reraise_and_debugger_path_forwards(self) -> None:
        _module, bridge = self.make(FakeUploadPage({"tag": "INPUT", "type": "file"}, dom_error="Node not found"))
        with self.assertRaisesRegex(RuntimeError, "Node not found"):
            bridge._browser_command("DOM.getDocument", {})
        page = FakeUploadPage({"tag": "INPUT", "type": "file"}, dom_error=None)
        _module, bridge = self.make(page)
        self.upload(bridge, "#f", ["/x.png"])
        self.assertEqual([c[0] for c in page.calls], ["DOM.getDocument", "DOM.querySelector", "DOM.setFileInputFiles"])

    def test_backend_node_id_gets_clear_policy_error(self) -> None:
        module, bridge = self.make(FakeUploadPage({"tag": "INPUT", "type": "file"}))
        with self.assertRaisesRegex(module.CDPError, "not backendNodeId"):
            bridge._browser_command("DOM.setFileInputFiles", {"files": ["/x.png"], "backendNodeId": 4})


if __name__ == "__main__":
    unittest.main()
