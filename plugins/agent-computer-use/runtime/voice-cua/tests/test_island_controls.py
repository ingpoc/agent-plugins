"""Island card controls → live Realtime session (stop speaking / cancel / retry / replay)."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault(
    "VOICE_CUA_ACTIVITY_LOG",
    str(Path(tempfile.mkdtemp(prefix="voice-cua-test-")) / "activity.jsonl"),
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from voice_cua import session_hub
from voice_cua.realtime_session import RealtimeSession


class _FakeAudio:
    def __init__(self) -> None:
        self.interrupted = 0

    def has_playback(self) -> bool:
        return True

    def interrupt(self):
        self.interrupted += 1
        return 1200, "item-1"


def _session() -> RealtimeSession:
    s = RealtimeSession.__new__(RealtimeSession)
    s._stop = threading.Event()
    s._session_ready = threading.Event()
    s._session_ready.set()
    s._audio = _FakeAudio()
    s._response_lock = threading.Lock()
    s._response_condition = threading.Condition()
    s._current_response_id = "resp-1"
    s._active_response_ids = {"resp-1"}
    s._cancelled_response_ids = set()
    s._last_user_text = ""
    s.sent = []
    s._send = lambda ev: s.sent.append(ev) or True
    s._interrupt_response_enabled = lambda: True
    return s


class IslandControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pub = patch("voice_cua.realtime_session.island_publish").start()
        self.addCleanup(patch.stopall)

    def test_stop_speaking_cancels_active_response_and_truncates(self) -> None:
        s = _session()
        self.assertTrue(s.island_control("stopSpeaking"))
        types = [e["type"] for e in s.sent]
        self.assertIn("response.cancel", types)
        self.assertIn("conversation.item.truncate", types)
        self.assertIn("resp-1", s._cancelled_response_ids)
        self.pub.assert_called_with("listening", title="Listening", detail="")

    def test_stop_on_live_appends_a_stop_instruction(self) -> None:
        from voice_cua.providers import LiveProvider

        s = _session()
        s.provider = LiveProvider()
        self.assertTrue(s.island_control("stopTask"))
        notes = [e for e in s.sent if e["type"] == "conversation.item.create"]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["item"]["role"], "system")
        self.assertIn("Stop", notes[0]["item"]["content"][0]["text"])
        s.provider._ready = True
        (wire,) = s.provider.outbound(notes[0])
        self.assertEqual(wire["type"], "session.instructions.append")

    def test_retry_resends_last_utterance_or_reports_unhandled(self) -> None:
        s = _session()
        s.send_text = lambda text, request_id="": s.sent.append({"type": "text", "text": text})
        self.assertFalse(s.island_control("retry"))
        s._last_user_text = "what's 12 times 7"
        self.assertTrue(s.island_control("retry"))
        self.assertEqual(s.sent[-1], {"type": "text", "text": "what's 12 times 7"})

    def test_unknown_action_is_not_handled(self) -> None:
        self.assertFalse(_session().island_control("stopListening"))

    def test_session_hub_without_session_is_unhandled(self) -> None:
        session_hub.unregister(session_hub._active) if session_hub._active else None
        self.assertFalse(session_hub.island_control("cancel"))

    def test_session_hub_routes_to_registered_session(self) -> None:
        s = _session()
        session_hub.register(s)
        try:
            self.assertTrue(session_hub.island_control("cancel"))
        finally:
            session_hub.unregister(s)


if __name__ == "__main__":
    unittest.main()
