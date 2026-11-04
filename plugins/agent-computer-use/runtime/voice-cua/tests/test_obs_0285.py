"""0.2.85: Comet foreground verification, comet_call target ids, final usage row, session_phase labels."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault(
    "VOICE_CUA_ACTIVITY_LOG",
    str(Path(tempfile.mkdtemp(prefix="voice-cua-test-")) / "activity.jsonl"),
)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from voice_cua import activity_log, startup_trace, tools  # noqa: E402
from test_phase1_reliability import make_session  # noqa: E402


def _rows(kind: str) -> list[dict]:
    path = activity_log.log_path()
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in rows if r.get("kind") == kind]


class CometBeginTests(unittest.TestCase):
    def _run(self, args: dict, *, frontmost: list):
        backend = MagicMock()
        backend.focus.return_value = {"ok": True}
        backend.frontmost_app.side_effect = frontmost
        published: list[tuple] = []
        events: list[tuple] = []
        with patch.object(tools, "comet_begin", return_value={"ok": True}), \
                patch.object(tools, "comet_is_active", return_value=False), \
                patch.object(tools, "comet_tab_id", return_value="abc123def456"), \
                patch.object(tools, "get_backend", return_value=backend), \
                patch.object(tools, "FRONT_VERIFY_S", 0.2), \
                patch.object(tools.time, "sleep"), \
                patch.object(tools, "island_publish", side_effect=lambda *a, **k: published.append((a, k))), \
                patch.object(tools, "log_event", side_effect=lambda kind, **k: events.append((kind, k))):
            result = tools._comet_begin(args)
        return result, published, events, backend

    def test_comet_is_never_a_separate_island_app(self) -> None:
        _, published, _, _ = self._run({"url": "https://www.netflix.com/browse?q=secret"}, frontmost=[])
        self.assertTrue(published)
        self.assertTrue(all(k.get("app") == "" for _, k in published), published)

    def test_comet_call_logs_opaque_target_and_tab_ids_only(self) -> None:
        _, _, events, _ = self._run({"url": "https://www.netflix.com/browse?q=secret"}, frontmost=[])
        call = next(k for kind, k in events if kind == "comet_call")
        self.assertRegex(call["target_id"], r"^[0-9a-f]{12}$")
        self.assertEqual(call["tab_id"], "abc123def456")
        self.assertNotIn("secret", json.dumps(call))
        self.assertEqual(tools.comet_target_id("x"), tools.comet_target_id("x"), "stable for duplicate detection")

    def test_foreground_is_claimed_only_when_verified(self) -> None:
        result, published, events, backend = self._run(
            {"url": "https://netflix.com", "foreground": True}, frontmost=["Finder", "Comet"])
        backend.focus.assert_called_once_with("Comet")
        self.assertTrue(result["ok"])
        self.assertTrue(result["frontmost"])
        self.assertEqual(published[-1][1]["title"], "Comet in front")
        self.assertTrue(next(k for kind, k in events if kind == "comet_foreground")["ok"])

    def test_foreground_failure_is_reported_not_claimed(self) -> None:
        result, published, _, _ = self._run(
            {"url": "https://netflix.com", "foreground": True}, frontmost=["Finder"] * 50)
        self.assertFalse(result["ok"])
        self.assertFalse(result["frontmost"])
        self.assertEqual(result["error_type"], "comet_not_frontmost")
        self.assertEqual(published[-1][0][0], "error")
        from voice_cua.island_copy import human_error
        self.assertEqual(human_error(published[-1][1]["title"]), "Couldn't bring Comet to front")

    def test_without_foreground_comet_is_not_raised(self) -> None:
        result, _, _, backend = self._run({"url": "https://netflix.com"}, frontmost=[])
        backend.focus.assert_not_called()
        self.assertNotIn("frontmost", result)


class FinalUsageTests(unittest.TestCase):
    def test_one_final_usage_row_with_numeric_totals(self) -> None:
        session_id = activity_log.current_session_id()
        activity_log._usage_totals.pop(session_id, None)
        before = len(_rows("usage"))
        activity_log.log_usage({"id": "r1", "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}})
        activity_log.log_usage({"id": "r2", "usage": {"input_tokens": 4, "output_tokens": 1, "total_tokens": 5}})
        session = make_session()
        session._log_final_usage()
        session._log_final_usage()
        rows = _rows("usage")[before:]
        finals = [r for r in rows if r.get("final")]
        self.assertEqual(len(finals), 1)
        final = finals[0]
        self.assertIsInstance(final["ts"], float)
        self.assertEqual(final["responses"], 2)
        self.assertEqual(final["total_tokens"], 20)
        self.assertEqual(final["input_tokens"], 14)


class SessionPhaseTests(unittest.TestCase):
    def test_phases_after_go_live_are_not_startup(self) -> None:
        kinds: list[tuple[str, str]] = []
        with patch.dict(os.environ, {}), patch.object(startup_trace, "print", create=True), \
                patch.object(startup_trace, "log_event", side_effect=lambda kind, **k: kinds.append((kind, k["phase"]))):
            timer = startup_trace.StartupTimer()
            for phase in ("stack_begin", "realtime_ws_open", "audio_ready", "response_done", "realtime_session_updated",
                          "realtime_ws_open", "realtime_ready", "response_done"):
                timer.mark(phase)
        self.assertEqual(kinds, [
            ("startup", "stack_begin"), ("startup", "realtime_ws_open"), ("startup", "audio_ready"),
            ("session_phase", "response_done"), ("session_phase", "realtime_session_updated"),
            ("startup", "realtime_ws_open"), ("startup", "realtime_ready"), ("session_phase", "response_done"),
        ])


if __name__ == "__main__":
    unittest.main()
