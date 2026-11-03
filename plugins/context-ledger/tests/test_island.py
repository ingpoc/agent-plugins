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

    @staticmethod
    def normalize_agent(name):
        return " ".join(w[:1].upper() + w[1:] for w in str(name).replace("-", " ").split()) or "Agent"

    def _record(self, kind, args, kwargs):
        self.calls.append((kind, args, kwargs))
        if self.fail:
            raise RuntimeError("island down")
        return True

    def call_started(self, *args, **kwargs):
        return self._record("started", args, kwargs)

    def call_finished(self, *args, **kwargs):
        return self._record("finished", args, kwargs)


class IslandNoteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeActivity()
        patcher = mock.patch.object(island, "_ACTIVITY", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        island.FAILURES["count"] = 0
        os.environ.pop("CONTEXT_LEDGER_ISLAND", None)

    def test_started_and_finished_carry_the_actor(self) -> None:
        island.note_started({"actor": "  codex  "})
        island.note_finished({"actor": "codex"})
        self.assertEqual(
            self.fake.calls,
            [
                ("started", ("context-ledger", "codex"), {"key": "Codex", "in_flight_s": 60.0}),
                ("finished", ("context-ledger", "codex"), {"key": "Codex"}),
            ],
        )
        self.assertIs(island.note_call, island.note_started)

    def test_spellings_of_one_agent_share_one_row(self) -> None:
        island.note_started({"actor": "grok-bot"})
        island.note_started({"actor": "Grok Bot"})
        self.assertEqual({c[2]["key"] for c in self.fake.calls}, {"Grok Bot"})

    def test_missing_actor_falls_back_to_agent(self) -> None:
        island.note_started({})
        self.assertEqual(self.fake.calls[0][1], ("context-ledger", "agent"))
        self.assertEqual(self.fake.calls[0][2]["key"], "Agent")

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
        with mock.patch.object(island, "_ACTIVITY", FakeActivity(fail=True)):
            island.note_finished({"actor": "codex"})
        self.assertEqual(island.FAILURES["count"], 2)

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

    def test_call_starts_before_and_finishes_after_the_tool(self) -> None:
        fake = FakeActivity()
        seen: list[int] = []
        with mock.patch.object(island, "_ACTIVITY", fake):
            server._call(lambda _arguments: seen.append(len(fake.calls)) or {"value": 7}, {"actor": "codex"})
        self.assertEqual(seen, [1])
        self.assertEqual([c[0] for c in fake.calls], ["started", "finished"])

    def test_a_failing_tool_still_finishes(self) -> None:
        fake = FakeActivity()

        def boom(_arguments):
            raise RuntimeError("x")

        with mock.patch.object(island, "_ACTIVITY", fake):
            result = server._call(boom, {"actor": "codex"})
        self.assertTrue(result.is_error)
        self.assertEqual([c[0] for c in fake.calls], ["started", "finished"])


if __name__ == "__main__":
    unittest.main()
