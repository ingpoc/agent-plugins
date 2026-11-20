import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from voice_cua import auth, live_session, session_hub


def _session(ready=False):
    sess = SimpleNamespace(_session_ready=threading.Event(), _stop=threading.Event(), _dormant=None,
                           _text_only=False, _audio_enabled=True)
    sess.send_text_and_wait = lambda text, timeout, cancel_event=None, typed_turn_id="": {
        "ok": True, "reply": "hi " + text,
    }
    if ready:
        sess._session_ready.set()
    return sess


class StartupTextTurnTest(unittest.TestCase):
    def tearDown(self):
        session_hub._active = None
        clear_failure = getattr(session_hub, "clear_startup_failure", None)
        if clear_failure:
            clear_failure()

    def test_text_turn_during_startup_waits_for_ready(self):
        sess = _session()
        session_hub.register(sess)
        self.assertEqual(session_hub.status()["ready"], False)
        threading.Timer(0.2, sess._session_ready.set).start()
        started = time.monotonic()
        result = session_hub.send_text_and_wait("there")
        self.assertEqual((result["ok"], result["status"], result["reply"]), (True, "done", "hi there"))
        self.assertTrue(result["turn_id"])
        self.assertLess(time.monotonic() - started, 5)

    def test_status_reports_setup_reason_until_session_is_ready(self):
        sess = _session()
        sess._session_error_reason = "Realtime open failed (RuntimeError)."
        session_hub.register(sess)
        status = session_hub.status()
        self.assertFalse(status["ready"])
        self.assertEqual(status["reason"], "setup_error: Realtime open failed (RuntimeError).")
        sess._session_ready.set()
        self.assertIsNone(session_hub.status()["reason"])

    def test_status_reports_helper_missing_failure(self):
        session_hub.report_startup_failure("helper_missing", "Bundled voice helper is missing.")
        self.assertEqual(session_hub.status()["reason"], "helper_missing: Bundled voice helper is missing.")

    def test_status_reports_mic_denied_failure(self):
        session_hub.report_startup_failure("mic_denied", "Microphone access was not granted.")
        self.assertEqual(session_hub.status()["reason"], "mic_denied: Microphone access was not granted.")

    def test_status_reports_key_missing_failure(self):
        session_hub.report_startup_failure("key_missing", "Add the Grok Voice API key to Keychain.")
        self.assertEqual(session_hub.status()["reason"], "key_missing: Add the Grok Voice API key to Keychain.")

    def test_status_reports_setup_error_failure(self):
        session_hub.report_startup_failure("setup_error", "Setup could not complete (RuntimeError).")
        self.assertEqual(session_hub.status()["reason"], "setup_error: Setup could not complete (RuntimeError).")

    def test_status_clears_failure_after_successful_start(self):
        session_hub.report_startup_failure("setup_error", "Previous attempt failed.")
        session_hub.register(_session(ready=True))
        self.assertIsNone(session_hub.status()["reason"])

    def test_status_keeps_setup_failure_after_session_unregisters(self):
        sess = _session()
        sess._session_error_reason = "Realtime open failed (RuntimeError)."
        session_hub.register(sess)
        session_hub.unregister(sess)
        self.assertEqual(session_hub.status()["reason"], "setup_error: Realtime open failed (RuntimeError).")

    def test_never_ready_reports_typed_error(self):
        session_hub.register(_session())
        old, session_hub.WAKE_WAIT_S = session_hub.WAKE_WAIT_S, 0.1
        try:
            result = session_hub.send_text_and_wait("x")
        finally:
            session_hub.WAKE_WAIT_S = old
        self.assertEqual((result["ok"], result["status"]), (False, "error"))
        self.assertIn("still starting", result["error"])

    def test_no_session_still_fails_fast(self):
        started = time.monotonic()
        result = session_hub.send_text_and_wait("x")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["turn_id"])
        self.assertLess(time.monotonic() - started, 1)


class StartupFailureStatusTest(unittest.TestCase):
    def setUp(self):
        session_hub.clear_startup_failure()

    def tearDown(self):
        session_hub._active = None
        session_hub.clear_startup_failure()

    def test_missing_key_startup_reaches_session_status(self):
        with patch.object(live_session, "LiveSession", side_effect=auth.MissingApiKey("missing")), \
                patch.object(live_session, "island_publish"), patch.object(live_session, "log_event"):
            result = live_session.main([])

        self.assertEqual(result, live_session.EXIT_NEEDS_API_KEY)
        self.assertEqual(session_hub.status()["reason"], "key_missing: Grok Voice: add xai-api to Keychain")

    def test_setup_error_startup_reaches_session_status(self):
        with patch.object(live_session, "LiveSession", side_effect=RuntimeError("private exception detail")), \
                patch.object(live_session, "island_publish"), patch.object(live_session, "log_event"):
            result = live_session.main([])

        self.assertEqual(result, 1)
        self.assertEqual(
            session_hub.status()["reason"],
            "setup_error: RuntimeError; voice session setup did not complete.",
        )

    def test_unsupported_gpt_realtime_setting_fails_start_and_reaches_status(self):
        import json
        import os
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            settings.write_text(json.dumps({"live_model": "gpt-realtime-unknown"}))
            with patch.dict(os.environ, {"VOICE_CUA_SETTINGS": str(settings), "VOICE_CUA_LIVE_MODEL": ""}), \
                    patch.object(live_session.LiveSession, "run"), \
                    patch("voice_cua.providers.LiveProvider.resolve_key", return_value="offline-test-key"), \
                    patch.object(live_session, "island_publish") as publish, \
                    patch.object(live_session, "log_event"):
                result = live_session.main([])

        self.assertEqual(result, 1)
        self.assertEqual(
            session_hub.status()["reason"],
            "setup_error: Voice model unavailable: gpt-realtime-unknown",
        )
        publish.assert_called_once_with(
            "error",
            title="Voice model unavailable",
            detail="setup_error: Voice model unavailable: gpt-realtime-unknown",
        )


if __name__ == "__main__":
    unittest.main()
