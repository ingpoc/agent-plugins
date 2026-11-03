#!/usr/bin/env python3
"""The ACU MCP reports its own use per calling agent. No real island sockets."""

from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "compact_mcp.py"
SPEC = importlib.util.spec_from_file_location("compact_mcp_island_under_test", SCRIPT)
assert SPEC and SPEC.loader
compact_mcp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compact_mcp)


class FakeActivity:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, tuple, dict]] = []

    def pulse(self, *args, **kwargs):
        self.calls.append(("pulse", args, kwargs))
        if self.fail:
            raise RuntimeError("island down")
        return True

    def end(self, *args, **kwargs):
        self.calls.append(("end", args, kwargs))
        if self.fail:
            raise RuntimeError("island down")
        return True


class CompactMcpIslandTests(unittest.TestCase):
    def setUp(self) -> None:
        compact_mcp._CLIENT["name"] = ""
        compact_mcp._PULSED["on"] = False
        compact_mcp._PULSED["key"] = ""
        compact_mcp._ACTIVITY_FAILURES["count"] = 0
        os.environ.pop("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", None)
        self.fake = FakeActivity()
        patcher = mock.patch.object(compact_mcp, "_ACTIVITY", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _act(self) -> dict:
        return compact_mcp.call_tool(
            "act", {"app": "Finder", "steps": [{"op": "click", "label": "Go"}]}, modern=False
        )

    def test_initialize_remembers_the_client_name(self) -> None:
        compact_mcp.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "  Cursor Agent  "}},
            }
        )
        self.assertEqual(compact_mcp._CLIENT["name"], "Cursor Agent")
        self.assertEqual(compact_mcp._agent_name(), "Cursor Agent")

    def test_act_pulses_acu_activity_for_the_agent(self) -> None:
        compact_mcp._CLIENT["name"] = "Cursor"
        patcher = mock.patch.dict(compact_mcp.HANDLERS, {"act": lambda _args: {"ok": True}})
        with patcher:
            result = self._act()
        self.assertTrue(result["structuredContent"]["ok"])
        agent = "Cursor"
        self.assertEqual(
            self.fake.calls,
            [("pulse", ("acu", agent), {"key": f"{agent}:{os.getpid()}", "hold_s": 30.0})],
        )

    def test_sessions_never_pulses(self) -> None:
        with mock.patch.dict(compact_mcp.HANDLERS, {"sessions": lambda _args: {"ok": True}}):
            compact_mcp.call_tool("sessions", {}, modern=False)
        self.assertEqual(self.fake.calls, [])

    def test_env_initiator_wins_over_the_client_name(self) -> None:
        compact_mcp._CLIENT["name"] = "Cursor"
        os.environ["CODEX_INTERNAL_ORIGINATOR_OVERRIDE"] = "acu_exec.grokbot.cursor"
        try:
            with mock.patch.dict(compact_mcp.HANDLERS, {"state": lambda _args: {"ok": True}}):
                compact_mcp.call_tool("state", {"app": "Finder"}, modern=False)
        finally:
            os.environ.pop("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", None)
        self.assertEqual(self.fake.calls[0][1], ("acu", "grokbot.cursor"))
        self.assertEqual(self.fake.calls[0][2]["hold_s"], 30.0)

    def test_eof_ends_the_row_once(self) -> None:
        with mock.patch.dict(compact_mcp.HANDLERS, {"act": lambda _args: {"ok": True}}):
            self._act()
        key = compact_mcp._PULSED["key"]

        class FakeBinary:
            def __init__(self) -> None:
                self.buffer = io.BytesIO(b"")

        with mock.patch.object(sys, "stdin", FakeBinary()), mock.patch.object(
            sys, "stdout", FakeBinary()
        ):
            self.assertEqual(compact_mcp.serve(), 0)
        self.assertEqual(self.fake.calls[-1], ("end", ("acu", key), {}))
        self.assertFalse(compact_mcp._PULSED["on"])

    def test_a_pulse_exception_never_breaks_the_tool_result(self) -> None:
        failing = FakeActivity(fail=True)
        with mock.patch.object(compact_mcp, "_ACTIVITY", failing), mock.patch.dict(
            compact_mcp.HANDLERS, {"act": lambda _args: {"ok": True, "value": 1}}
        ):
            result = self._act()
        self.assertEqual(result["structuredContent"], {"ok": True, "value": 1})
        self.assertEqual(compact_mcp._ACTIVITY_FAILURES["count"], 1)
        self.assertFalse(compact_mcp._PULSED["on"])


if __name__ == "__main__":
    unittest.main()
