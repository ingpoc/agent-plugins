"""The ledger reports each call as plugin activity. No real island sockets."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from context_ledger import island, server  # noqa: E402


class FakeActivity:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, tuple, dict]] = []

    def pulse(self, *args, **kwargs):
        self.calls.append(("pulse", args, kwargs))
        if self.fail:
            raise RuntimeError("island down")
        return True


class IslandNoteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeActivity()
        patcher = mock.patch.object(island, "_ACTIVITY", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        island.FAILURES["count"] = 0
        os.environ.pop("CONTEXT_LEDGER_ISLAND", None)

    def test_pulse_carries_the_actor(self) -> None:
        island.note_call({"actor": "  codex  "})
        self.assertEqual(
            self.fake.calls,
            [("pulse", ("context-ledger", "codex"), {"key": "codex", "hold_s": 10.0})],
        )

    def test_missing_actor_falls_back_to_agent(self) -> None:
        island.note_call({})
        self.assertEqual(self.fake.calls[0][1], ("context-ledger", "agent"))
        self.assertEqual(self.fake.calls[0][2]["key"], "agent")

    def test_disabled_environment_skips_the_signal(self) -> None:
        for value in ("0", "false", "OFF", " no "):
            with self.subTest(value=value), mock.patch.dict(
                os.environ, {"CONTEXT_LEDGER_ISLAND": value}
            ):
                island.note_call({"actor": "codex"})
        self.assertEqual(self.fake.calls, [])

    def test_pulse_errors_are_counted_not_raised(self) -> None:
        with mock.patch.object(island, "_ACTIVITY", FakeActivity(fail=True)):
            island.note_call({"actor": "codex"})
        self.assertEqual(island.FAILURES["count"], 1)

    def test_missing_module_is_a_silent_no_op(self) -> None:
        with mock.patch.object(island, "_ACTIVITY", None):
            island.note_call({"actor": "codex"})
        self.assertEqual(self.fake.calls, [])

    def test_module_loads_from_the_environment_override(self) -> None:
        tmp = Path(__file__).resolve().parent / "_island_fake_module.py"
        try:
            tmp.write_text("MARKER = 'fake'\n", encoding="utf-8")
            with mock.patch.dict(
                os.environ, {"CONTEXT_LEDGER_ISLAND_ACTIVITY": str(tmp)}
            ), mock.patch.object(island, "_ACTIVITY", island._UNSET):
                module = island.activity()
            self.assertEqual(module.MARKER, "fake")
        finally:
            tmp.unlink(missing_ok=True)


class ServerCallTests(unittest.TestCase):
    def test_call_still_returns_the_tool_result_when_the_island_fails(self) -> None:
        with mock.patch.object(island, "_ACTIVITY", FakeActivity(fail=True)):
            result = server._call(lambda _arguments: {"value": 7}, {"actor": "codex"})
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content, {"ok": True, "data": {"value": 7}})

    def test_call_pulses_before_running_the_tool(self) -> None:
        fake = FakeActivity()
        with mock.patch.object(island, "_ACTIVITY", fake):
            server._call(lambda _arguments: {"value": 7}, {"actor": "codex"})
        self.assertEqual(fake.calls[0][1], ("context-ledger", "codex"))


if __name__ == "__main__":
    unittest.main()
