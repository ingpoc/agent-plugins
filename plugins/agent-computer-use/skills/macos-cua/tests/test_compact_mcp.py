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

FIVE = ["start_session", "state", "act", "verify", "end_session"]
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

        class Runtime:
            def state(self, app, **kwargs):
                calls.append((app, kwargs))
                return {"ok": True, "text": "7"}

        original_runtime = compact_mcp._RUNTIME
        compact_mcp._RUNTIME = Runtime()
        try:
            payload = compact_mcp._state_payload(
                {"app": "Calculator", "query": "7", "diff": True, "max": 12}
            )
        finally:
            compact_mcp._RUNTIME = original_runtime
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

        class Runtime:
            def act(self, app, arguments, actions):
                calls.append((app, arguments, actions))
                return {"ok": True, "verified": True}

        arguments = {
            "app": "Calculator",
            "plan": {"actions": [{"action": "click", "label": "7"}]},
        }
        original_runtime = compact_mcp._RUNTIME
        compact_mcp._RUNTIME = Runtime()
        try:
            payload = compact_mcp.handle_act(arguments)
        finally:
            compact_mcp._RUNTIME = original_runtime
        self.assertEqual(payload, {"ok": True, "verified": True})
        self.assertEqual(calls, [("Calculator", arguments, compact_mcp.AX_ACTIONS)])

    def test_lifecycle_dispatches_in_process_without_cli(self):
        class Runtime:
            def start_driver(self, session):
                return {"ok": True, "session": session}

            def ensure_operator(self):
                return {"ok": True}

            def end_driver(self, session):
                return {"ok": True, "session": session}

            def closeout(self):
                return {"success": True}

        original_runtime = compact_mcp._RUNTIME
        compact_mcp._RUNTIME = Runtime()
        compact_mcp._SESSION.clear()
        try:
            started = compact_mcp.handle_start_session({"session": "test-session"})
            ended = compact_mcp.handle_end_session({})
        finally:
            compact_mcp._SESSION.clear()
            compact_mcp._RUNTIME = original_runtime
        self.assertTrue(started["ok"])
        self.assertNotIn("preflight", started)
        self.assertTrue(ended["ok"])
        self.assertEqual(ended["session"], "test-session")

    def test_start_session_schema_has_no_preflight(self):
        start = next(
            tool for tool in compact_mcp.tool_schemas() if tool["name"] == "start_session"
        )
        self.assertEqual(set(start["inputSchema"]["properties"]), {"session"})
        self.assertFalse(hasattr(compact_mcp, "run_cli"))

    def test_instructions_encode_two_wall_clocks(self):
        text = compact_mcp.INSTRUCTIONS
        self.assertIn("Two wall clocks", text)
        self.assertIn("Act-first", text)
        self.assertIn("Do not verify when act.verified", text)
        self.assertIn("Dispatch ok is never proof", text)
        self.assertIn("no desktop-global click", text)
        self.assertIn("fails the old trace", text)
        self.assertIn("app-agnostic fast_path grader", text)
        self.assertIn("Preflight once at start", text)
        self.assertIn("closeout once at end", text)
        self.assertIn("ax_timeout", text)
        self.assertIn("Fallback only on miss", text)
        act = next(tool for tool in compact_mcp.tool_schemas() if tool["name"] == "act")
        self.assertIn("Best first", act["description"])
        self.assertIn("Fallback", act["description"])


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
        original = compact_mcp.HANDLERS["verify"]
        compact_mcp.HANDLERS["verify"] = lambda _args: {
            "ok": True,
            "matched": True,
            "expect": "64",
            "escalation": {"recommended": "px"},
        }
        try:
            result = compact_mcp.call_tool(
                "verify",
                {"app": "Calculator", "expect": "64"},
                modern=True,
            )
        finally:
            compact_mcp.HANDLERS["verify"] = original
        self.assertEqual(result["structuredContent"]["expect"], "64")
        self.assertEqual(result["structuredContent"]["escalation"]["recommended"], "px")
        self.assertIn("64", result["content"][0]["text"])
        self.assertFalse(result["isError"])
        self.assertEqual(result["resultType"], "complete")

    def test_verify_rejects_degraded_state(self):
        original = compact_mcp._state_payload
        compact_mcp._state_payload = lambda _args: {
            "ok": True,
            "degraded": True,
            "text": "64",
            "app": "Calculator",
        }
        try:
            payload = compact_mcp.handle_verify({"app": "Calculator", "expect": "64"})
        finally:
            compact_mcp._state_payload = original
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["degraded"])
        self.assertTrue(payload["matched"])

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
    def test_script_legacy_initialize_lists_five_tools(self):
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

    def test_launcher_modern_discover_lists_five_tools(self):
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


if __name__ == "__main__":
    unittest.main()
