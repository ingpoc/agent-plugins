import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
import threading
import time
import unittest
from types import SimpleNamespace

from voice_cua import session_hub


def _session(ready=False):
    sess = SimpleNamespace(_session_ready=threading.Event(), _stop=threading.Event(), _dormant=None,
                           _text_only=False, _audio_enabled=True)
    sess.send_text_and_wait = lambda text, timeout, cancel_event=None: {"ok": True, "reply": "hi " + text}
    if ready:
        sess._session_ready.set()
    return sess


class StartupTextTurnTest(unittest.TestCase):
    def tearDown(self):
        session_hub._active = None

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


if __name__ == "__main__":
    unittest.main()
