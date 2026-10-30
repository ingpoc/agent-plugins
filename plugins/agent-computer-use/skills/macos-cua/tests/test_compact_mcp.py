#!/usr/bin/env python3
"""Protocol and dispatch tests for the thin MCP facade. No live apps."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT.parents[1]
SCRIPT = ROOT / "scripts" / "compact_mcp.py"
LAUNCHER = PLUGIN_ROOT / "bin" / "agent-computer-use-mcp"
SPEC = importlib.util.spec_from_file_location("compact_mcp", SCRIPT)
compact_mcp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compact_mcp)

FIVE = ["state", "act"]
MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": compact_mcp.MODERN_VERSION,
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "0"},
}


def _send(proc: subprocess.Popen, payload: dict, framing: str = "nl") -> None:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    if framing == "lsp":
        proc.stdin.write(f"Content-Length: {len(raw)}\r\n\r\n".encode() + raw)
    else:
        proc.stdin.write(raw + b"\n")
    proc.stdin.flush()


def _recv(proc: subprocess.Popen) -> dict:
    message, incoming = compact_mcp.read_message(proc.stdout)
    if message is None:
        raise AssertionError("server closed without a response")
    if incoming != "nl":
        raise AssertionError(f"writer must be spec NDJSON, got {incoming}")
    return message


def _spawn(command: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=os.environ | {"CUA_DRIVER_RS_UPDATE_CHECK": "0", "PYTHONUNBUFFERED": "1"},
    )


def _close(proc: subprocess.Popen) -> None:
    if proc.stdin:
        proc.stdin.close()
    try:
        proc.wait(timeout=5)
    finally:
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()


class CompactMcpDispatchTests(unittest.TestCase):
    def test_action_delta_preserves_full_state_verification_and_failure_context(self):
        before = '[0] AXWindow "Fixture"\n' + '\n'.join(
            f'  [{i}] AXButton "Button {i}" [pressable] {{0,0 10x10}}'
            for i in range(1, 30)
        ) + '\n  [30] AXStaticText value="waiting"'
        after = before.replace('value="waiting"', 'value="finished"')

        class Backend(compact_mcp.CUABackend):
            def __init__(self):
                pass

            def _rpc(self, fn, *, retry=True):
                class Client:
                    def execute_plan(self, app, steps):
                        return {
                            "before": {"text": before}, "after": {"text": after},
                            "results": [{"ok": True, "method": "ax-press"}],
                        }
                return fn(Client())

        result = Backend().act("Fixture", {"label": "Button 1", "expect": "finished"})
        self.assertTrue(result["verified"])
        self.assertLess(len(result["text"]), len(after) // 2)
        self.assertIn('[30] AXStaticText value="finished"', result["text"])
        self.assertNotIn('AXButton "Button 20"', result["text"])
        failed = Backend().act("Fixture", {"label": "Button 1", "expect": "missing"})
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["error_type"], "expect_unverified")
        self.assertLess(len(failed["text"]), len(after) // 2)
        self.assertIn("reason: expect_unverified", failed["text"])
        self.assertNotIn('AXButton "Button 20"', failed["text"])
        self.assertTrue(failed.get("full_text_omitted"))

    def test_allow_unverified_without_expect_stays_compact(self):
        before = 'Window: "W"\n  [1] AXButton "A" {0,0 10x10}'
        after = 'Window: "W"\n  [1] AXButton "A" {0,0 10x10}\n  [2] AXStaticText value="x" {0,0 10x10}'

        class Backend(compact_mcp.CUABackend):
            def __init__(self):
                pass

            def _rpc(self, fn, *, retry=True):
                class Client:
                    def execute_plan(self, app, steps):
                        return {
                            "before": {"text": before},
                            "after": {"text": after},
                            "results": [{"ok": True, "method": "ax-press"}],
                        }
                return fn(Client())

        out = Backend().act("W", {"label": "A", "allow_unverified": True})
        self.assertFalse(out["ok"])
        self.assertTrue(out["dispatched"])
        self.assertEqual(out["error_type"], "allow_unverified")
        self.assertNotEqual(out["error_type"], "expect_unverified")
        self.assertTrue(out.get("full_text_omitted"))
        self.assertIn("allow_unverified:", out["text"])
        self.assertNotIn('AXStaticText value="x"', out["text"])

    def test_native_rpc_label_miss_returns_target_missing_taxonomy(self):
        class Backend(compact_mcp.CUABackend):
            def __init__(self):
                pass

            def state(self, app, **kwargs):
                return {"ok": True, "text": '[0] AXWindow "W"\n  [1] AXButton "Save"', "app": app}

            def _rpc(self, fn, *, retry=True):
                raise RuntimeError("RPC error -32602: Label not found: Nope. elementCount=2 axTrusted=true")

        out = Backend().act("W", {"label": "Nope", "expect": "x"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "target_missing")
        self.assertIn("reason: target_missing", out["text"])
        self.assertLess(len(out["text"]), 500)
        self.assertTrue(out.get("full_text_omitted"))

    def test_app_only_act_retries_cold_launch_window(self):
        calls = []

        class Backend(compact_mcp.CUABackend):
            def __init__(self):
                pass

            def _rpc(self, fn, *, retry=True):
                class Client:
                    def get_app_state(self, app, **kwargs):
                        calls.append(kwargs)
                        if len(calls) == 1:
                            raise RuntimeError(f"No window for app: {app}")
                        return {
                            "app": app,
                            "text": "ready",
                            "screenshot": {"url": "file:///tmp/ready.png"},
                        }

                return fn(Client())

        out = Backend().act("Dictionary", {"app": "Dictionary"})
        self.assertTrue(out["ok"])
        self.assertTrue(out["verified"])
        self.assertTrue(any(call.get("raiseForInput") for call in calls))

    def test_mutating_act_without_expect_fails_before_dispatch(self):
        class Backend(compact_mcp.CUABackend):
            def __init__(self):
                pass

            def _rpc(self, fn, *, retry=True):
                self.fail("must not dispatch")

        out = Backend().act("Calculator", {"label": "Equals"})
        self.assertFalse(out["ok"])
        self.assertFalse(out["dispatched"])
        self.assertEqual(out["error_type"], "verification_required")

    def test_failed_expectation_is_not_success(self):
        class Backend(compact_mcp.CUABackend):
            def __init__(self):
                pass

            def _rpc(self, fn, *, retry=True):
                class Client:
                    def get_app_state(self, app, **kwargs):
                        return {"text": '[1] AXStaticText value="Applications"'}

                    def click(self, app, **kwargs):
                        return {"ok": True, "method": "ax-press"}

                return fn(Client())

        out = Backend().act("Finder", {"label": "Apps", "expect": "Apps"})
        self.assertFalse(out["ok"])
        self.assertTrue(out["dispatched"])
        self.assertFalse(out["verified"])
        self.assertEqual(out["error_type"], "expect_unverified")

    def test_not_text_expectation_verifies_removal(self):
        before = '[1] AXStaticText value="Calculator"'
        after = '[1] AXStaticText value="Applications"'
        self.assertTrue(
            compact_mcp.expectation_is_new(
                {"not_text": "Calculator"}, before, after
            )
        )

    def test_value_expect_does_not_skip_sibling_text_and_lists_are_all_checked(self):
        state = {"elements": [
            {"element_index": 1, "role": "AXTextField", "label": "Name", "value": "Ada"},
            {"element_index": 2, "role": "AXTextField", "label": "Email", "value": "ada@example.com"},
        ]}
        before = ""
        after = '[1] AXTextField value="Ada"\n[2] AXStaticText value="saved"'
        both = {"value": {"label": "Name", "equals": "Ada"}, "text": "saved"}
        self.assertTrue(compact_mcp.expectation_is_new(both, before, after, None, state))
        self.assertFalse(compact_mcp.expectation_is_new(
            {"value": {"label": "Name", "equals": "Ada"}, "text": "missing"}, before, after, None, state
        ))
        expects = [
            {"value": {"label": "Name", "equals": "Ada"}},
            {"value": {"label": "Email", "equals": "ada@example.com"}},
        ]
        self.assertTrue(compact_mcp.expectation_is_new(expects, before, after, None, state))
        self.assertFalse(compact_mcp.expectation_is_new(expects, before, after, None, None))

    def test_focus_checkbox_and_wait_reresolve_stay_pinned(self):
        clicks = []
        fills = []

        class Backend(compact_mcp.CUABackend):
            def __init__(self):
                pass

            def _rpc(self, fn, *, retry=True):
                class Client:
                    def __init__(self):
                        self.snaps = [
                            {"text": "", "elements": []},
                            {"text": "", "elements": [
                                {"element_index": 1, "role": "AXCheckBox", "label": "Bacon", "value": 0},
                                {"element_index": 9, "role": "AXTextField", "label": "City", "value": ""},
                            ]},
                            {"text": "", "elements": [
                                {"element_index": 4, "role": "AXTextField", "label": "City", "value": ""},
                            ]},
                            {"text": '[4] AXTextField value="Pune"', "elements": [
                                {"element_index": 4, "role": "AXTextField", "label": "City", "value": "Pune"},
                            ]},
                        ]

                    def get_app_state(self, app, **kwargs):
                        if kwargs.get("raiseForInput"):
                            return {"app": app, "text": "front", "windowId": 44, "elements": []}
                        snap = self.snaps.pop(0)
                        return {"app": app, "windowId": 44, **snap}

                    def execute_plan(self, app, steps, **kwargs):
                        raise AssertionError(f"sequential plan must not execute_plan {kwargs}")

                    def click(self, app, **kwargs):
                        clicks.append(kwargs)
                        return {"ok": True, "method": "ax-press"}

                    def set_value(self, app, element, value, window_id=None):
                        fills.append((element, value, window_id))
                        return {"ok": True, "method": "ax-set-value"}

                return fn(Client())

        out = Backend().act("App", {
            "steps": [
                {"op": "focus"},
                {"wait": 0},
                {"action": "set_checked", "label": "Bacon", "checked": False},
                {"action": "set_value", "label": "City", "value": "Pune"},
            ],
            "expect": [{"value": {"label": "City", "equals": "Pune"}}],
        })
        self.assertEqual(clicks, [])
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(fills, [(4, "Pune", 44)])

    def test_set_checked_readback_refuses_when_click_ignored(self):
        """Radio/checkbox click that leaves value unchanged must not ok:true (r11c #4)."""
        clicks = []

        class Backend(compact_mcp.CUABackend):
            def __init__(self):
                pass

            def _rpc(self, fn, *, retry=True):
                class Client:
                    def get_app_state(self, app, **kwargs):
                        return {
                            "app": app,
                            "windowId": 44,
                            "text": "form",
                            "elements": [
                                {"element_index": 1, "role": "AXCheckBox", "label": "Bacon", "value": 1},
                            ],
                        }

                    def click(self, app, **kwargs):
                        clicks.append(kwargs)
                        return {"ok": True, "method": "ax-press"}

                    def execute_plan(self, app, steps, **kwargs):
                        raise AssertionError("must stay sequential")

                return fn(Client())

        out = Backend().act("App", {
            "steps": [
                {"action": "set_checked", "label": "Bacon", "checked": False},
            ],
            "expect": "x",
            "allow_unverified": True,
        })
        self.assertEqual(len(clicks), 1, clicks)
        self.assertFalse(out.get("dispatched"), out)
        blob = str(out).lower()
        self.assertIn("readback", blob)

    def test_execute_plan_keyboard_pins_observed_window(self):
        """execute_plan for key-only must pass observed windowId (r11c #3)."""
        plans = []

        class Backend(compact_mcp.CUABackend):
            def __init__(self):
                pass

            def _rpc(self, fn, *, retry=True):
                class Client:
                    def get_app_state(self, app, **kwargs):
                        return {
                            "app": app,
                            "windowId": 77,
                            "text": "doc",
                            "elements": [
                                {"element_index": 0, "role": "AXTextArea", "label": "Body", "value": "hi"},
                            ],
                        }

                    def press_key(self, app, key, window_id=None):
                        raise AssertionError("key-only should use execute_plan")

                    def execute_plan(self, app, steps, **kwargs):
                        plans.append((app, steps, kwargs))
                        return {
                            "ok": True,
                            "results": [{"ok": True, "method": "cgevent-pid"}],
                            "before": {"text": "doc"},
                            "after": {"text": "x"},
                        }

                return fn(Client())

        out = Backend().act("App", {
            "steps": [{"key": "Delete"}],
            "expect": "x",
            "allow_unverified": True,
        })
        self.assertEqual(len(plans), 1, plans)
        self.assertEqual(plans[0][2].get("window_id"), 77, plans)
        self.assertTrue(out.get("dispatched"), out)

    def test_sequential_press_key_pins_observed_window(self):
        """Sequential keyboard after set_checked must pin windowId (r11c #3)."""
        presses = []

        class Backend(compact_mcp.CUABackend):
            def __init__(self):
                pass

            def _rpc(self, fn, *, retry=True):
                class Client:
                    def get_app_state(self, app, **kwargs):
                        return {
                            "app": app,
                            "windowId": 77,
                            "text": "form",
                            "elements": [
                                {"element_index": 1, "role": "AXCheckBox", "label": "Bacon", "value": 0},
                            ],
                        }

                    def press_key(self, app, key, window_id=None):
                        presses.append((app, key, window_id))
                        return {"ok": True, "method": "cgevent-pid"}

                    def click(self, app, **kwargs):
                        return {"ok": True, "method": "ax-press"}

                    def execute_plan(self, app, steps, **kwargs):
                        raise AssertionError("set_checked forces sequential")

                return fn(Client())

        out = Backend().act("App", {
            "steps": [
                {"action": "set_checked", "label": "Bacon", "checked": False},
                {"key": "Delete"},
            ],
            "expect": "x",
            "allow_unverified": True,
        })
        self.assertEqual(presses, [("App", "Delete", 77)], presses)
        self.assertTrue(out.get("dispatched"), out)

    def test_state_dispatches_in_process_without_cli(self):

        calls = []

        class Backend:
            def state(self, app, **kwargs):
                calls.append((app, kwargs))
                return {"ok": True, "text": "7"}

        original = compact_mcp._BACKEND
        compact_mcp._BACKEND = Backend()
        try:
            payload = compact_mcp._state_payload(
                {"app": "Calculator", "query": "7", "diff": True, "max": 12}
            )
        finally:
            compact_mcp._BACKEND = original
        self.assertEqual(payload, {"ok": True, "text": "7"})
        self.assertEqual(
            calls,
            [
                (
                    "Calculator",
                    {"query": "7", "diff": True, "max_elements": 12, "screenshot": False},
                )
            ],
        )

    def test_act_dispatches_plan_in_process_without_cli(self):
        calls = []

        class Backend:
            def act(self, app, arguments):
                calls.append((app, arguments))
                return {"ok": True, "verified": True}

        arguments = {
            "app": "Calculator",
            "steps": [{"label": "7"}],
            "expect": "7",
        }
        original = compact_mcp._BACKEND
        compact_mcp._BACKEND = Backend()
        try:
            payload = compact_mcp.handle_act(arguments)
        finally:
            compact_mcp._BACKEND = original
        self.assertEqual(payload, {"ok": True, "verified": True})
        self.assertEqual(calls, [("Calculator", arguments)])

    def test_catalog_is_state_and_act_only(self):
        names = [tool["name"] for tool in compact_mcp.tool_schemas()]
        self.assertEqual(names, FIVE)
        self.assertFalse(hasattr(compact_mcp, "run_cli"))
        self.assertNotIn("handle_start_session", dir(compact_mcp))

    def test_instructions_encode_two_wall_clocks(self):
        text = compact_mcp.INSTRUCTIONS
        self.assertIn("Two wall clocks", text)
        self.assertIn("Act-first", text)
        self.assertIn("Do not call verify", text)
        self.assertIn("compact AX delta", text)
        self.assertIn("Dispatch ok is never proof", text)
        self.assertIn("no desktop-global click", text)
        self.assertIn("fails the old trace", text)
        self.assertIn("app-agnostic fast_path grader", text)
        self.assertIn("CUAService", text)
        self.assertIn("ax_timeout", text)
        self.assertIn("Fallback only on miss", text)
        self.assertIn("empty AX, Stage Manager thumb", text)
        self.assertIn("screenshot:true", text)
        act = next(tool for tool in compact_mcp.tool_schemas() if tool["name"] == "act")
        self.assertIn("one native plan", act["description"])
        self.assertIn("Exact paths", act["description"])
        self.assertIn("outputSchema", act)
        props = act["inputSchema"]["properties"]
        self.assertEqual(props["x"]["type"], "number")
        self.assertEqual(props["y"]["type"], "number")
        self.assertEqual(props["wait"]["type"], "number")
        step_props = props["steps"]["items"]["properties"]
        self.assertEqual(step_props["x"]["type"], "number")
        self.assertEqual(step_props["wait"]["type"], "number")
        self.assertIn("open", step_props["op"]["enum"])
        self.assertIn("scroll", step_props["op"]["enum"])
        self.assertEqual(step_props["path"]["type"], "string")
        self.assertEqual(step_props["value"]["type"], "string")
        self.assertEqual(step_props["checked"]["type"], "boolean")
        self.assertEqual(props["expect"]["oneOf"][1]["properties"]["value"]["type"], "object")

    def test_exact_path_uses_one_native_plan_and_verifies_result(self):
        calls = []

        class Backend(compact_mcp.CUABackend):
            def __init__(self):
                pass

            def _rpc(self, fn, *, retry=True):
                class Client:
                    def execute_plan(self, app, steps):
                        calls.append((app, steps))
                        return {
                            "ok": True,
                            "before": {"text": '[1] AXStaticText value="apps"'},
                            "after": {"text": '[1] AXStaticText value="voice-cua-agent"'},
                            "results": [{
                                "ok": True,
                                "method": "workspace-open",
                                "path": "/tmp/voice-cua-agent",
                            }],
                        }

                return fn(Client())

        out = Backend().act("Finder", {
            "steps": [{"op": "open", "path": "/tmp/voice-cua-agent"}],
            "expect": [
                {"path": "/tmp/voice-cua-agent"},
                {"text": "voice-cua-agent"},
            ],
        })
        self.assertTrue(out["ok"])
        self.assertTrue(out["verified"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1][0]["method"], "open_item")
        self.assertEqual(calls[0][1][0]["params"]["path"], "/tmp/voice-cua-agent")

    def test_act_omits_screenshots_when_expect_verified(self):
        class Backend(compact_mcp.CUABackend):
            def __init__(self):
                pass

            def _rpc(self, fn, *, retry=True):
                class Client:
                    def __init__(self):
                        self.n = 0

                    def get_app_state(self, app, **kwargs):
                        self.n += 1
                        value = "0" if self.n == 1 else "7"
                        return {
                            "text": f'[1] AXStaticText value="{value}"',
                            "screenshot": {"url": f"file:///tmp/shot{self.n}.png"},
                        }

                    def click(self, app, **kwargs):
                        return {"ok": True, "method": "ax-press"}

                return fn(Client())

        out = Backend().act(
            "Calculator", {"steps": [{"label": "7"}], "expect": "7"}
        )
        self.assertIsNone(out["screenshot_before"])
        self.assertIsNone(out["screenshot_after"])
        self.assertIsNone(out["screenshot"])
        self.assertTrue(out["verified"])

    def test_act_keeps_screenshot_for_empty_ax(self):
        fetches = []

        class Backend(compact_mcp.CUABackend):
            def __init__(self):
                pass

            def _rpc(self, fn, *, retry=True):
                class Client:
                    def get_app_state(self, app, **kwargs):
                        fetches.append(kwargs)
                        return {
                            "text": "",
                            "screenshot": {"url": "file:///tmp/empty.png"}
                            if kwargs.get("includeScreenshot")
                            else None,
                        }

                    def click(self, app, **kwargs):
                        return {"ok": False, "error": "Label not found"}

                return fn(Client())

        out = Backend().act(
            "Preview", {"steps": [{"label": "Ink"}], "expect": "signed"}
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["screenshot"]["url"], "file:///tmp/empty.png")
        self.assertTrue(any(item.get("includeScreenshot") is True for item in fetches))

    def test_act_keeps_screenshot_for_stage_manager_thumb(self):
        class Backend(compact_mcp.CUABackend):
            def __init__(self):
                pass

            def _rpc(self, fn, *, retry=True):
                class Client:
                    def __init__(self):
                        self.n = 0

                    def get_app_state(self, app, **kwargs):
                        self.n += 1
                        value = "0" if self.n == 1 else "7"
                        return {
                            "text": f'[1] AXStaticText value="{value}"',
                            "windowWidth": 30,
                            "windowHeight": 79,
                            "screenshot": {"url": "file:///tmp/thumb.png"},
                        }

                    def click(self, app, **kwargs):
                        return {"ok": True, "method": "ax-press"}

                return fn(Client())

        out = Backend().act(
            "Calculator", {"steps": [{"label": "7"}], "expect": "7"}
        )
        self.assertTrue(out["verified"])
        self.assertEqual(out["screenshot"]["url"], "file:///tmp/thumb.png")

    def test_act_stops_after_first_failed_step(self):
        clicks = []

        class Backend(compact_mcp.CUABackend):
            def __init__(self):
                pass

            def _rpc(self, fn, *, retry=True):
                class Client:
                    def get_app_state(self, app, **kwargs):
                        return {"text": '[1] AXStaticText value="0"'}

                    def click(self, app, **kwargs):
                        clicks.append(kwargs.get("label"))
                        if kwargs.get("label") == "Miss":
                            return {"ok": False, "error": "Label not found"}
                        return {"ok": True, "method": "ax-press"}

                return fn(Client())

        out = Backend().act(
            "Calculator",
            {
                "steps": [
                    {"label": "All Clear"},
                    {"label": "Miss"},
                    {"label": "Equals"},
                ],
                "expect": "0",
            },
        )
        self.assertFalse(out["ok"])
        self.assertEqual(clicks, ["All Clear", "Miss"])
        self.assertEqual(out["failure"], {"completed_steps": 1, "failed_step": 2})

    def test_act_fails_closed_on_null_click_point(self):
        class Backend(compact_mcp.CUABackend):
            def __init__(self):
                pass

            def _rpc(self, fn, *, retry=True):
                class Client:
                    def get_app_state(self, app, **kwargs):
                        return {"text": '[1] AXStaticText value="0"'}

                    def click(self, app, **kwargs):
                        return {
                            "ok": True,
                            "method": "cgevent-click",
                            "point": {"x": None, "y": None},
                        }

                return fn(Client())

        out = Backend().act("App", {"steps": [{"element": 1}], "expect": "0"})
        self.assertFalse(out["ok"])
        self.assertIn("nonfinite click point", out["text"])
        self.assertEqual(out["failure"]["failed_step"], 1)

    def test_act_set_value_by_label_and_expect_value(self):
        """compile_cua schema (action/label/value + expect.value) must dispatch fills, not clicks."""
        calls = []

        class Backend(compact_mcp.CUABackend):
            def __init__(self):
                pass

            def _rpc(self, fn, *, retry=True):
                class Client:
                    def __init__(self):
                        self.n = 0

                    def get_app_state(self, app, **kwargs):
                        self.n += 1
                        email = "ada@example.com" if self.n >= 2 else ""
                        name = "Ada Lovelace" if self.n >= 2 else ""
                        return {
                            "text": f'[1] AXTextField value="{name}"\n[2] AXTextField value="{email}"',
                            "elements": [
                                {"element_index": 1, "role": "AXTextField", "label": "Full name", "value": name},
                                {"element_index": 2, "role": "AXTextField", "label": "Email", "value": email},
                            ],
                            "screenshot": {"url": "file:///tmp/s.png"},
                        }

                    def set_value(self, app, element, value):
                        calls.append(("set_value", app, element, value))
                        return {"ok": True, "method": "ax-set-value"}

                    def click(self, app, **kwargs):
                        calls.append(("click", kwargs))
                        raise AssertionError("set_value-by-label must not fall through to click")

                return fn(Client())

        out = Backend().act(
            "System Settings",
            {
                "steps": [{"action": "set_value", "label": "Full name", "value": "Ada Lovelace"}],
                "expect": {"value": {"label": "Email", "equals": "ada@example.com"}},
            },
        )
        self.assertTrue(out.get("ok"), out)
        self.assertTrue(out.get("verified"), out)
        self.assertEqual(calls, [("set_value", "System Settings", 1, "Ada Lovelace")])
        evil = Backend().act(
            "System Settings",
            {
                "steps": [{"action": "set_value", "label": "Full name", "value": "Ada Lovelace"}],
                "expect": {"value": {"label": "Email", "equals": "ada@example.com.evil"}},
            },
        )
        self.assertFalse(evil.get("verified"))
        self.assertFalse(evil.get("ok"))

    def test_native_plan_resolves_label_fill_and_expect_uses_elements(self):
        """execute_plan fills must compile with element_index; after carries elements (r8 #2/#3)."""
        plans = []

        class Backend(compact_mcp.CUABackend):
            def __init__(self):
                pass

            def _rpc(self, fn, *, retry=True):
                class Client:
                    def get_app_state(self, app, **kwargs):
                        return {
                            "text": '[1] AXTextField "Full name"',
                            "elements": [
                                {"element_index": 1, "role": "AXTextField", "label": "Full name", "value": ""},
                                {"element_index": 2, "role": "AXTextField", "label": "Email", "value": "ada@example.com"},
                            ],
                        }

                    def execute_plan(self, app, steps):
                        plans.append(list(steps))
                        return {
                            "ok": True,
                            "before": {
                                "text": "",
                                "elements": [
                                    {"element_index": 1, "role": "AXTextField", "label": "Full name", "value": ""},
                                    {"element_index": 2, "role": "AXTextField", "label": "Email", "value": ""},
                                ],
                            },
                            "after": {
                                "text": "Ada",
                                "elements": [
                                    {"element_index": 1, "role": "AXTextField", "label": "Full name", "value": "Ada Lovelace"},
                                    {"element_index": 2, "role": "AXTextField", "label": "Email", "value": "ada@example.com"},
                                ],
                            },
                            "results": [{"ok": True, "method": "set_value"}],
                        }

                return fn(Client())

        out = Backend().act(
            "System Settings",
            {
                "steps": [{"action": "set_value", "label": "Full name", "value": "Ada Lovelace"}],
                "expect": {"value": {"label": "Full name", "equals": "Ada Lovelace"}},
            },
        )
        self.assertTrue(out.get("ok"), out)
        self.assertTrue(out.get("verified"), out)
        self.assertEqual(len(plans), 1)
        step = plans[0][0]
        self.assertEqual(step["method"], "set_value")
        self.assertEqual(step["params"].get("element_index"), 1)
        self.assertNotIn("label", step["params"])
        # Missing label fails loud before execute_plan.
        plans.clear()
        miss = Backend().act(
            "System Settings",
            {
                "steps": [{"action": "set_value", "label": "Nope", "value": "x"}],
                "expect": {"value": {"label": "Full name", "equals": "Ada Lovelace"}},
            },
        )
        self.assertFalse(miss.get("ok"))
        self.assertIn("field not found", miss.get("error", ""))
        self.assertEqual(plans, [])

    def test_act_ambiguous_duplicate_label_fails_loud(self):
        """Duplicate field labels must not silently pick the first (r9 #4)."""
        plans = []

        class Backend(compact_mcp.CUABackend):
            def __init__(self):
                pass

            def _rpc(self, fn, *, retry=True):
                class Client:
                    def get_app_state(self, app, **kwargs):
                        return {
                            "text": '[1] AXTextField "Name"\n[3] AXTextField "Name"',
                            "elements": [
                                {"element_index": 1, "role": "AXTextField", "label": "Name", "value": ""},
                                {"element_index": 3, "role": "AXTextField", "label": "Name", "value": ""},
                            ],
                        }

                    def execute_plan(self, app, steps):
                        plans.append(list(steps))
                        raise AssertionError("ambiguous label must fail before execute_plan")

                    def set_value(self, app, element, value):
                        raise AssertionError("ambiguous label must not dispatch set_value")

                return fn(Client())

        out = Backend().act(
            "App",
            {
                "steps": [{"action": "set_value", "label": "Name", "value": "Ada"}],
                "expect": {"value": {"label": "Name", "equals": "Ada"}},
            },
        )
        self.assertFalse(out.get("ok"))
        self.assertFalse(out.get("dispatched"))
        self.assertIn("ambiguous label", out.get("error", ""))
        self.assertEqual(plans, [])

    def test_act_label_fill_after_click_skips_stale_plan_snapshot(self):
        """Nav/click before a label fill must re-resolve (not one pre-plan snap) (r9 #3)."""
        plans = []
        states = []
        set_values = []

        class Backend(compact_mcp.CUABackend):
            def __init__(self):
                pass

            def _rpc(self, fn, *, retry=True):
                class Client:
                    def __init__(self):
                        self.n = 0

                    def get_app_state(self, app, **kwargs):
                        self.n += 1
                        states.append(self.n)
                        if self.n == 1:
                            # before click
                            return {
                                "text": '[1] AXButton "Next"',
                                "elements": [
                                    {"element_index": 1, "role": "AXButton", "label": "Next", "value": ""},
                                ],
                            }
                        # after click: form fields appear; Email at index 5
                        return {
                            "text": '[5] AXTextField "Email"',
                            "elements": [
                                {"element_index": 5, "role": "AXTextField", "label": "Email", "value": "ada@example.com"},
                            ],
                        }

                    def execute_plan(self, app, steps):
                        plans.append(list(steps))
                        raise AssertionError("mutation-before-fill must not use stale execute_plan compile")

                    def click(self, app, **kwargs):
                        return {"ok": True, "method": "ax-press"}

                    def set_value(self, app, element, value):
                        set_values.append((element, value))
                        return {"ok": True, "method": "ax-set-value"}

                return fn(Client())

        out = Backend().act(
            "App",
            {
                "steps": [
                    {"label": "Next"},
                    {"action": "set_value", "label": "Email", "value": "ada@example.com"},
                ],
                "expect": {"value": {"label": "Email", "equals": "ada@example.com"}},
            },
        )
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(plans, [])
        self.assertEqual(set_values, [(5, "ada@example.com")])
        self.assertGreaterEqual(len(states), 2)

    def test_act_second_label_fill_re_resolves_after_first_fill(self):
        """A fill can reorder the form; the next label fill re-resolves (r10 #2)."""
        set_values = []
        plans = []

        class Backend(compact_mcp.CUABackend):
            def __init__(self):
                pass

            def _rpc(self, fn, *, retry=True):
                class Client:
                    def get_app_state(self, app, **kwargs):
                        if not set_values:
                            els = [
                                {"element_index": 2, "role": "AXTextField", "label": "Country", "value": ""},
                                {"element_index": 3, "role": "AXTextField", "label": "City", "value": ""},
                            ]
                        else:
                            # Country fill inserted a State field; City moved to 4.
                            els = [
                                {"element_index": 2, "role": "AXTextField", "label": "Country", "value": "IN"},
                                {"element_index": 3, "role": "AXTextField", "label": "State", "value": ""},
                                {"element_index": 4, "role": "AXTextField", "label": "City",
                                 "value": "Pune" if len(set_values) > 1 else ""},
                            ]
                        return {"text": "", "elements": els}

                    def execute_plan(self, app, steps):
                        plans.append(list(steps))
                        raise AssertionError("fill-after-fill must not run a pre-compiled plan")

                    def set_value(self, app, element, value):
                        set_values.append((element, value))
                        return {"ok": True, "method": "ax-set-value"}

                return fn(Client())

        out = Backend().act(
            "App",
            {
                "steps": [
                    {"action": "set_value", "label": "Country", "value": "IN"},
                    {"action": "set_value", "label": "City", "value": "Pune"},
                ],
                "expect": {"value": {"label": "City", "equals": "Pune"}},
            },
        )
        self.assertEqual(plans, [])
        self.assertEqual(set_values, [(2, "IN"), (4, "Pune")])
        self.assertTrue(out.get("ok"), out)

    def test_act_expect_already_true_before_is_not_verified(self):
        class Backend(compact_mcp.CUABackend):
            def __init__(self):
                pass

            def _rpc(self, fn, *, retry=True):
                class Client:
                    def get_app_state(self, app, **kwargs):
                        return {
                            "text": '[16] AXTextArea value="the event horizon is a boundary"',
                            "screenshot": {"url": "file:///tmp/s.png"},
                        }

                    def set_value(self, app, element, value):
                        return {"ok": True, "method": "ax-set-value"}

                return fn(Client())

        out = Backend().act(
            "App",
            {"steps": [{"element": 35, "text": "Idea"}], "expect": "event horizon"},
        )
        self.assertFalse(out["ok"])
        self.assertTrue(out["dispatched"])
        self.assertFalse(out["verified"])

    def test_act_does_not_fetch_screenshot_when_expect_verified(self):
        calls = []

        class Backend(compact_mcp.CUABackend):
            def __init__(self):
                pass

            def _rpc(self, fn, *, retry=True):
                class Client:
                    def __init__(self):
                        self.n = 0

                    def get_app_state(self, app, **kwargs):
                        calls.append(kwargs)
                        self.n += 1
                        value = "0" if self.n == 1 else "7"
                        return {
                            "text": f'[1] AXStaticText value="{value}"',
                            "screenshot": None,
                        }

                    def click(self, app, **kwargs):
                        return {"ok": True, "method": "ax-press"}

                return fn(Client())

        out = Backend().act("App", {"steps": [{"label": "7"}], "expect": "7"})
        self.assertTrue(out["verified"])
        self.assertIsNone(out["screenshot"])
        self.assertFalse(any(item.get("includeScreenshot") is True for item in calls))

    def test_typed_failure_omits_screenshots(self):
        class Backend(compact_mcp.CUABackend):
            def __init__(self):
                pass

            def _rpc(self, fn, *, retry=True):
                class Client:
                    def get_app_state(self, app, **kwargs):
                        return {
                            "text": '[1] AXStaticText value="0"',
                            "screenshot": {"url": "file:///tmp/fail.png"},
                        }

                    def click(self, app, **kwargs):
                        return {"ok": False, "error": "Label not found"}

                return fn(Client())

        out = Backend().act(
            "Calculator", {"steps": [{"label": "Nope"}], "expect": "7"}
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "target_missing")
        self.assertIsNone(out["screenshot"])


class CompactMcpSpecTests(unittest.TestCase):
    def test_legacy_initialize_tools_list_shape(self):
        listed = compact_mcp.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            }
        )
        init = compact_mcp.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            }
        )
        ping = compact_mcp.handle_rpc({"jsonrpc": "2.0", "id": 3, "method": "ping"})
        self.assertEqual(init["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(init["result"]["capabilities"], {"tools": {"listChanged": False}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "agent-computer-use")
        self.assertIn("instructions", init["result"])
        self.assertNotIn("resultType", init["result"])
        names = [tool["name"] for tool in listed["result"]["tools"]]
        self.assertEqual(names, FIVE)
        self.assertNotIn("resultType", listed["result"])
        self.assertEqual(ping["result"], {})
        self.assertIsNone(
            compact_mcp.handle_rpc(
                {"jsonrpc": "2.0", "method": "notifications/initialized"}
            )
        )

    def test_modern_discover_and_tools_list_shape(self):
        discover = compact_mcp.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": "discover-1",
                "method": "server/discover",
                "params": {"_meta": MODERN_META},
            }
        )
        listed = compact_mcp.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {"_meta": MODERN_META},
            }
        )
        result = discover["result"]
        self.assertEqual(result["resultType"], "complete")
        self.assertEqual(result["supportedVersions"][0], compact_mcp.MODERN_VERSION)
        self.assertIn(compact_mcp.MODERN_VERSION, result["supportedVersions"])
        self.assertEqual(result["cacheScope"], "public")
        self.assertIsInstance(result["ttlMs"], int)
        self.assertEqual(result["capabilities"], {"tools": {"listChanged": False}})
        self.assertEqual(
            result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"],
            "agent-computer-use",
        )
        listed_result = listed["result"]
        self.assertEqual(listed_result["resultType"], "complete")
        self.assertEqual(listed_result["cacheScope"], "public")
        self.assertIsInstance(listed_result["ttlMs"], int)
        self.assertEqual([tool["name"] for tool in listed_result["tools"]], FIVE)
        for tool in listed_result["tools"]:
            self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_unsupported_modern_version(self):
        reply = compact_mcp.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "1900-01-01",
                        "io.modelcontextprotocol/clientCapabilities": {},
                    }
                },
            }
        )
        self.assertEqual(reply["error"]["code"], -32022)
        self.assertIn(compact_mcp.MODERN_VERSION, reply["error"]["data"]["supported"])

    def test_call_tool_includes_structured_content(self):
        original = compact_mcp.HANDLERS["act"]
        compact_mcp.HANDLERS["act"] = lambda _args: {
            "ok": True,
            "verified": True,
            "expect": "64",
        }
        try:
            result = compact_mcp.call_tool(
                "act",
                {"app": "Calculator", "label": "Equals", "expect": "64"},
                modern=True,
            )
        finally:
            compact_mcp.HANDLERS["act"] = original
        self.assertEqual(result["structuredContent"]["expect"], "64")
        self.assertIn("64", result["content"][0]["text"])
        self.assertFalse(result["isError"])
        self.assertEqual(result["resultType"], "complete")

    def test_modern_unknown_tool_is_protocol_error(self):
        reply = compact_mcp.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "list_apps", "arguments": {}, "_meta": MODERN_META},
            }
        )
        self.assertEqual(reply["error"]["code"], -32602)


class CompactMcpProtocolTests(unittest.TestCase):
    def test_script_legacy_initialize_lists_two_tools(self):
        proc = _spawn([sys.executable, "-u", str(SCRIPT)])
        try:
            _send(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0"},
                    },
                },
            )
            init = _recv(proc)
            _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            listed = _recv(proc)
            _send(proc, {"jsonrpc": "2.0", "id": 3, "method": "ping"})
            ping = _recv(proc)
        finally:
            _close(proc)
        self.assertEqual(init["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(init["result"]["serverInfo"]["name"], "agent-computer-use")
        self.assertEqual(init["result"]["capabilities"]["tools"], {"listChanged": False})
        self.assertEqual([tool["name"] for tool in listed["result"]["tools"]], FIVE)
        self.assertEqual(ping["result"], {})

    def test_launcher_reads_content_length_writes_ndjson(self):
        self.assertTrue(os.access(LAUNCHER, os.X_OK))
        proc = _spawn([str(LAUNCHER)])
        try:
            _send(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0"},
                    },
                },
                framing="lsp",
            )
            init = _recv(proc)
            _send(
                proc,
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                framing="lsp",
            )
            listed = _recv(proc)
        finally:
            _close(proc)
        self.assertEqual(init["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual([tool["name"] for tool in listed["result"]["tools"]], FIVE)

    def test_launcher_modern_discover_lists_two_tools(self):
        proc = _spawn([str(LAUNCHER)])
        try:
            _send(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": "discover-1",
                    "method": "server/discover",
                    "params": {"_meta": MODERN_META},
                },
            )
            discover = _recv(proc)
            _send(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/list",
                    "params": {"_meta": MODERN_META},
                },
            )
            listed = _recv(proc)
        finally:
            _close(proc)
        self.assertEqual(discover["result"]["resultType"], "complete")
        self.assertEqual(discover["result"]["supportedVersions"][0], "2026-07-28")
        self.assertEqual(listed["result"]["resultType"], "complete")
        self.assertEqual([tool["name"] for tool in listed["result"]["tools"]], FIVE)


class ExpectVerifiedTests(unittest.TestCase):
    def test_keypad_zero_does_not_false_green_display(self):
        tree = (
            '[0] AXWindow "Calculator"\n'
            '[1] AXStaticText value="3×"\n'
            '[2] AXButton "0" [pressable]\n'
            '[3] AXButton "1" [pressable]\n'
        )
        self.assertFalse(compact_mcp.expect_verified("0", tree))
        self.assertTrue(compact_mcp.expect_verified("3×", tree))

    def test_empty_expect_is_verified(self):
        self.assertTrue(compact_mcp.expect_verified("", "[1] AXButton \"0\""))

    def test_unicode_hyphen_matches_ascii_wifi(self):
        tree = '[1] AXStaticText value="Wi\u2011Fi"'
        self.assertTrue(compact_mcp.expect_verified("Wi-Fi", tree))

    def test_bidi_mark_in_static_text(self):
        tree = '[1] AXStaticText value="\u200eChats"'
        self.assertTrue(compact_mcp.expect_verified("Chats", tree))

    def test_unrelated_button_titles_do_not_verify(self):
        tree = (
            '[0] AXWindow "Calculator"\n'
            '[16] AXButton "All Clear" [pressable]\n'
            '[30] AXButton "Add" [pressable]\n'
            '[34] AXButton "Equals" [pressable]\n'
            '[36] AXStaticText value="4"\n'
        )
        self.assertTrue(compact_mcp.expect_verified("4", tree))
        self.assertFalse(compact_mcp.expect_verified("Add", tree))
        self.assertFalse(compact_mcp.expect_verified("Equals", tree))
        self.assertFalse(compact_mcp.expect_verified("All Clear", tree))

    def test_wrong_display_value_is_not_verified(self):
        tree = '[1] AXStaticText value="2+2"\n[2] AXStaticText value="4"\n'
        self.assertFalse(compact_mcp.expect_verified("5", tree))
        self.assertFalse(compact_mcp.expect_verified("10", tree))

    def test_needle_zero_does_not_substring_ten(self):
        tree = '[1] AXStaticText value="10"'
        self.assertFalse(compact_mcp.expect_verified("0", tree))
        self.assertTrue(compact_mcp.expect_verified("10", tree))

    def test_table_cell_value_verifies(self):
        tree = (
            '[31] AXCell "The crew logged swell, wind, and visibility before the evening run."\n'
            '[11] AXButton "Table" [pressable]\n'
        )
        self.assertTrue(compact_mcp.expect_verified("crew logged swell", tree))
        self.assertFalse(compact_mcp.expect_verified("Table", tree))
        tree = '[18] AXTextArea value="battle-long-line-one battle-long-line-two battle-long-lin..."'
        self.assertFalse(
            compact_mcp.expect_verified(
                "battle-long-line-one battle-long-line-two battle-long-line-three",
                tree,
            )
        )
        self.assertTrue(compact_mcp.expect_verified("battle-long-line-one", tree))
        self.assertFalse(compact_mcp.expect_verified("unseen prefix battle-long-line-one battle-long-line-two battle-long-lin", tree))
        tree = (
            '[0] AXWindow "Untitled"\n'
            '[17] AXTextArea value="batch-cross-app"\n'
            '[2] AXButton "Open" [pressable]\n'
        )
        self.assertTrue(compact_mcp.expect_verified("batch-cross-app", tree))
        self.assertFalse(compact_mcp.expect_verified("Open", tree))

    def test_expect_is_new_rejects_needle_already_in_before_tree(self):
        before = '[16] AXTextArea value="the event horizon is a boundary"\n'
        after = (
            before + '[38] AXCell "Event horizon"\n'
        )
        self.assertTrue(compact_mcp.expect_verified("event horizon", before))
        self.assertFalse(
            compact_mcp.expect_is_new("event horizon", before, after)
        )
        self.assertTrue(
            compact_mcp.expect_is_new("Glowing ring of gas", before, after + '[42] AXCell "Glowing ring of gas"\n')
        )


if __name__ == "__main__":
    unittest.main()
