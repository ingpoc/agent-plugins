"""Observability-driven fixes: one island error per failed tool, confirm requests are not
tool errors, and a Mac with no microphone fails the mic preflight fast as an environment skip."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

# Keep test events out of the real ~/.cache/macos-cua activity log and settings.
os.environ["VOICE_CUA_SETTINGS"] = str(Path(tempfile.mkdtemp(prefix="voice-cua-test-settings-")) / "settings.json")
os.environ.setdefault("VOICE_CUA_ACTIVITY_LOG", str(Path(tempfile.mkdtemp(prefix="voice-cua-test-")) / "activity.jsonl"))
_STATE_DIR = tempfile.TemporaryDirectory(prefix="voice-cua-test-state-")
os.environ.setdefault("VOICE_CUA_COMET_STATE", str(Path(_STATE_DIR.name) / "lease.json"))

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from voice_cua import audio_io, island_facade  # noqa: E402
from voice_cua.activity_log import log_tool_result  # noqa: E402
from voice_cua.island_facade import errors_published, summarize_result  # noqa: E402
from voice_cua.island_state import ISLAND  # noqa: E402
from voice_cua.realtime_session import RealtimeSession  # noqa: E402


class FakeWS:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def send(self, raw: str) -> None:
        self.events.append(json.loads(raw))


def _error_calls(mock: Mock) -> list:
    return [c for c in mock.call_args_list if c.args and c.args[0] == "error"]


class OneIslandErrorPerFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        env = patch.dict(os.environ, {"VOICE_CUA_REMOTE_ISLAND": ""})
        env.start()
        self.addCleanup(env.stop)

    def _run(self, dispatch) -> Mock:
        session = RealtimeSession(api_key="test", enable_audio=False)
        session.ws = FakeWS()
        with (
            patch.object(ISLAND, "publish") as publish,
            patch("voice_cua.realtime_session.dispatch", side_effect=dispatch),
        ):
            session._run_tool("call_1", "cua_act", '{"app":"Calculator"}')
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and session._inflight_tools:
                time.sleep(0.01)
        self.assertEqual(session._inflight_tools, 0, "tool worker never finished")
        return publish

    def test_tool_that_published_its_error_gets_no_second_failed_card(self) -> None:
        def dispatch(_name, _args):
            island_facade.island_publish("error", title="expect_unverified", app="Calculator")
            return {"ok": False, "error_type": "expect_unverified"}

        publish = self._run(dispatch)
        self.assertEqual(len(_error_calls(publish)), 1, publish.call_args_list)

    def test_silent_tool_failure_still_shows_the_summary_error(self) -> None:
        publish = self._run(lambda _n, _a: {"ok": False, "error": "boom"})
        errors = _error_calls(publish)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].kwargs.get("title"), "Failed")

    def test_error_counter_is_per_thread_and_monotonic(self) -> None:
        with patch.object(ISLAND, "publish"):
            before = errors_published()
            island_facade.island_publish("error", title="x")
            island_facade.island_publish("done", title="y")
        self.assertEqual(errors_published(), before + 1)


class ConfirmRequestIsNotAFailureTests(unittest.TestCase):
    RESULT = {"ok": False, "error": "confirmation required", "error_type": "needs_confirmation",
              "needs_confirmation": True}

    def test_summary_is_needs_your_ok(self) -> None:
        kind, title, _detail = summarize_result("cua_act", dict(self.RESULT), fallback="delete file")
        self.assertEqual((kind, title), ("acting", "Needs your OK"))

    def test_logged_as_needs_confirmation_not_tool_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "activity.jsonl"
            with patch.dict(os.environ, {"VOICE_CUA_ACTIVITY_LOG": str(log)}):
                log_tool_result("cua_act", dict(self.RESULT), app="Finder")
            rows = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual([r["kind"] for r in rows], ["tool_needs_confirmation"])

    def test_real_failure_still_logged_as_tool_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "activity.jsonl"
            with patch.dict(os.environ, {"VOICE_CUA_ACTIVITY_LOG": str(log)}):
                log_tool_result("cua_act", {"ok": False, "error_type": "target_missing"}, app="Finder")
            rows = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual([r["kind"] for r in rows], ["tool_error"])


class NoMicFailsFastTests(unittest.TestCase):
    def test_no_input_device_returns_code_without_tcc_or_stream(self) -> None:
        fake_sd = Mock()
        fake_sd.query_devices.side_effect = RuntimeError("Error querying device -1")
        with (
            patch.dict(os.environ, {"VOICE_CUA_NO_AUDIO": ""}),
            patch.object(audio_io, "sd", fake_sd),
            patch.object(audio_io, "_mic_authorized") as tcc,
        ):
            self.assertEqual(audio_io.mic_preflight(), audio_io.MIC_NO_INPUT_DEVICE)
        tcc.assert_not_called()
        fake_sd.InputStream.assert_not_called()

    def test_start_audio_marks_skip_once_and_goes_text_only(self) -> None:
        session = RealtimeSession(api_key="test", enable_audio=True)
        with (
            patch("voice_cua.audio_io.audio_available", return_value=True),
            patch("voice_cua.audio_io.wait_mic_preflight", return_value=audio_io.MIC_NO_INPUT_DEVICE),
            patch("voice_cua.realtime_session.island_publish") as publish,
            patch("voice_cua.realtime_session.mark") as mark,
        ):
            session._start_audio()
            session._start_audio()  # reconnect path: no second preflight row
        self.assertFalse(session._audio_enabled)
        rows = [c for c in mark.call_args_list if c.args and c.args[0] == "mic_preflight"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].kwargs.get("status"), "skip")
        self.assertEqual(rows[0].kwargs.get("cause"), "no_input_device")
        self.assertEqual(publish.call_count, 1)


class StackBeginCarriesVersionTests(unittest.TestCase):
    def test_stack_begin_row_has_acu_version(self) -> None:
        from voice_cua import __version__, startup_trace

        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "activity.jsonl"
            with patch.dict(os.environ, {"VOICE_CUA_ACTIVITY_LOG": str(log), "VOICE_CUA_STARTUP_ID": ""}):
                with patch.object(startup_trace, "_timer", None):
                    startup_trace.begin()
            rows = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(rows[0]["phase"], "stack_begin")
        self.assertEqual(rows[0]["version"], __version__)


if __name__ == "__main__":
    unittest.main()
