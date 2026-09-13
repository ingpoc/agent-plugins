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

    def test_cursor_waits_only_for_remaining_travel(self):
        cursor = PARITY.parent / "content-scripts/cursor-agent.js"
        source = cursor.read_text()
        functions = source[source.index("  const GLIDE_MS"):source.index("  function pulseClick")]
        harness = """
const assert = require('node:assert/strict');
let now=0, waits=[], cursorX=0, cursorY=0, isVisible=true, cursorPhase='idle';
let cursorEl={classList:{add(){},remove(){}},style:{}};
const performance={now:()=>now};
const setTimeout=(fn,ms)=>{waits.push(ms);return 1};
const clearTimeout=()=>{};
const getStatus=()=>({});
""" + functions + """
moveToAndWait(100,100); assert.equal(waits.at(-1),360);
now=200; moveToAndWait(100,100); assert.equal(waits.at(-1),160);
now=400; moveToAndWait(100,100); assert.equal(waits.at(-1),0);
moveToAndWait(200,100); assert.equal(waits.at(-1),360);
isVisible=false; moveToAndWait(200,100); assert.equal(waits.at(-1),360);
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


if __name__ == "__main__":
    unittest.main()
