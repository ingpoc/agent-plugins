#!/usr/bin/env python3
"""op=open with only an app: launch / reopen a windowless app instead of failing blind."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("compact_mcp", ROOT / "scripts" / "compact_mcp.py")
compact_mcp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compact_mcp)
import compact_backend  # noqa: E402

WINDOW = '[0] AXWindow "Calendar" {0,0 800x600}\n  [1] AXButton "Today" [pressable] {10,10 40x20}'


class FakeBackend(compact_mcp.CUABackend):
    def __init__(self, states):
        self.states = list(states)
        self.plans = []

    def _rpc(self, fn, *, retry=True):
        return fn(self._cua())

    def _cua(self):
        backend = self

        class Client:
            def get_app_state(self, app, **_kw):
                text, count = backend.states.pop(0) if len(backend.states) > 1 else backend.states[0]
                return {"app": app, "text": text, "elementCount": count, "pid": 42}

            def execute_plan(self, app, steps):
                backend.plans.append(steps)
                return {"before": {"text": WINDOW}, "after": {"text": WINDOW + "\n  [2] AXStaticText value=\"done\""},
                        "results": [{"ok": True, "method": "ax-press"}]}

        return Client()

    def _reset(self):
        pass


def _ok(*_a, **_k):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


class OpenAppTests(unittest.TestCase):
    def test_step_detection(self):
        self.assertTrue(compact_backend.is_app_open_step({"op": "open"}))
        self.assertFalse(compact_backend.is_app_open_step({"op": "open", "path": "/tmp/x"}))
        self.assertFalse(compact_backend.is_app_open_step({"op": "open", "url": "https://x"}))
        self.assertFalse(compact_backend.is_app_open_step({"op": "click", "label": "Open"}))

    def test_empty_state_carries_a_hint(self):
        out = FakeBackend([("", 0)]).state("Calendar")
        self.assertTrue(out["ok"])
        self.assertIn("op=open", out["hint"])

    def test_open_reopens_windowless_app_and_returns_state(self):
        backend = FakeBackend([("", 0), (WINDOW, 2)])
        with patch.object(compact_backend.subprocess, "run", side_effect=_ok) as run:
            out = backend.act("Calendar", {"op": "open"})
        self.assertEqual(run.call_args.args[0], ["/usr/bin/open", "-a", "Calendar"])
        self.assertTrue(out["ok"])
        self.assertTrue(out["verified"])
        self.assertIn("Today", out["text"])  # saves the follow-up cua_state

    def test_open_then_remaining_steps_run_in_one_call(self):
        backend = FakeBackend([(WINDOW, 2)])
        with patch.object(compact_backend.subprocess, "run", side_effect=_ok):
            out = backend.act("Calendar", {"steps": [{"op": "open"}, {"op": "click", "label": "Today"}], "expect": "done"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(len(backend.plans), 1)
        self.assertNotIn("open", [s.get("method") for s in backend.plans[0]])

    def test_missing_app_fails_once_with_reason(self):
        def missing(*_a, **_k):
            return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="Unable to find application named 'Final Cut Pro'")
        with patch.object(compact_backend.subprocess, "run", side_effect=missing):
            out = FakeBackend([("", 0)]).act("Final Cut Pro", {"op": "open"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "app_not_found")
        self.assertFalse(out["retryable"])
        self.assertIn("Unable to find application", out["error"])


if __name__ == "__main__":
    unittest.main()
