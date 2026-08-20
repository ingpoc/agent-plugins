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
                    {"query": "7", "diff": True, "max_elements": 12},
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
        self.assertIn("Do not verify when act.verified", text)
        self.assertIn("Dispatch ok is never proof", text)
        self.assertIn("no desktop-global click", text)
        self.assertIn("fails the old trace", text)
        self.assertIn("app-agnostic fast_path grader", text)
        self.assertIn("CUAService", text)
        self.assertIn("ax_timeout", text)
        self.assertIn("Fallback only on miss", text)
        self.assertIn("screenshot_before", text)
        self.assertIn("screenshot_after", text)
        act = next(tool for tool in compact_mcp.tool_schemas() if tool["name"] == "act")
        self.assertIn("Best first", act["description"])
        self.assertIn("screenshot_before", act["description"])
        self.assertIn("Fallback", act["description"])
        props = act["inputSchema"]["properties"]
        self.assertEqual(props["x"]["type"], "number")
        self.assertEqual(props["y"]["type"], "number")
        self.assertEqual(props["wait"]["type"], "number")
        step_props = props["steps"]["items"]["properties"]
        self.assertEqual(step_props["x"]["type"], "number")
        self.assertEqual(step_props["wait"]["type"], "number")

    def test_act_returns_before_and_after_screenshots(self):
        class Backend(compact_mcp.CUABackend):
            def __init__(self):
                pass

            def _rpc(self, fn):
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
        self.assertEqual(out["screenshot_before"]["url"], "file:///tmp/shot1.png")
        self.assertEqual(out["screenshot_after"]["url"], "file:///tmp/shot2.png")
        self.assertEqual(out["screenshot"], out["screenshot_after"])
        self.assertTrue(out["verified"])

    def test_act_stops_after_first_failed_step(self):
        clicks = []

        class Backend(compact_mcp.CUABackend):
            def __init__(self):
                pass

            def _rpc(self, fn):
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
        self.assertEqual(len(out["results"]), 2)

    def test_act_fails_closed_on_null_click_point(self):
        class Backend(compact_mcp.CUABackend):
            def __init__(self):
                pass

            def _rpc(self, fn):
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
        self.assertEqual(out["results"][0].get("error"), "nonfinite click point")

    def test_act_expect_already_true_before_is_not_verified(self):
        class Backend(compact_mcp.CUABackend):
            def __init__(self):
                pass

            def _rpc(self, fn):
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
        self.assertTrue(out["ok"])
        self.assertFalse(out["verified"])

    def test_act_retries_missing_screenshot(self):
        class Backend(compact_mcp.CUABackend):
            def __init__(self):
                pass

            def _rpc(self, fn):
                class Client:
                    def __init__(self):
                        self.n = 0

                    def get_app_state(self, app, **kwargs):
                        self.n += 1
                        shot = None if self.n == 1 else {"url": f"file:///tmp/r{self.n}.png"}
                        value = "0" if self.n <= 2 else "7"
                        return {
                            "text": f'[1] AXStaticText value="{value}"',
                            "screenshot": shot,
                        }

                    def click(self, app, **kwargs):
                        return {"ok": True, "method": "ax-press"}

                return fn(Client())

        out = Backend().act("App", {"steps": [{"label": "7"}], "expect": "7"})
        self.assertEqual(out["screenshot_before"]["url"], "file:///tmp/r2.png")
        self.assertTrue(out["verified"])


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

    def test_operator_button_titles_do_not_verify(self):
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
        self.assertTrue(
            compact_mcp.expect_verified(
                "battle-long-line-one battle-long-line-two battle-long-line-three",
                tree,
            )
        )
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
