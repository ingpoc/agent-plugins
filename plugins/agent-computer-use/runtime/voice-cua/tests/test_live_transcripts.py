"""GPT-Live transcript deltas become utterances in voice.log / activity log (docs: no turn-end event)."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from voice_cua import providers  # noqa: E402


class TranscriptBurstTests(unittest.TestCase):
    def test_fragments_join_exactly_and_flush_once(self):
        got = []
        burst = providers.TranscriptBurst(got.append, gap_s=60)
        self.assertTrue(burst.add("What is"))
        self.assertFalse(burst.add(" the time?"))
        burst.flush()
        burst.flush()
        self.assertEqual(got, ["What is the time?"])

    def test_quiet_gap_flushes_on_its_own(self):
        got = []
        burst = providers.TranscriptBurst(got.append, gap_s=0.05)
        burst.add("hello")
        import time
        time.sleep(0.3)
        self.assertEqual(got, ["hello"])


class LiveInboundTranscriptTests(unittest.TestCase):
    def test_user_then_assistant_speech_is_logged(self):
        events = []
        with patch.object(providers, "log_event", lambda kind, **kw: events.append((kind, kw))), \
                patch("voice_cua.island_facade.island_publish", lambda *a, **k: None):
            live = providers.LiveProvider()
            out = live.inbound({"type": "session.input_transcript.delta", "delta": "Open Calculator", "start_ms": 0, "end_ms": 900})
            self.assertEqual(out, [{"type": "input_audio_buffer.speech_started"}])
            self.assertEqual(live.inbound({"type": "session.output_transcript.delta", "delta": "Opening it now."}), [])
            live.said.flush()
        kinds = [k for k, _ in events]
        self.assertIn("user_transcript", kinds)
        user = dict(events)["user_transcript"]
        self.assertEqual(user["chars"], len("Open Calculator"))
        self.assertEqual(dict(events)["assistant_reply"]["text"], "Opening it now.")


class LiveCloseTests(unittest.TestCase):
    def test_finalize_sends_session_close_and_waits_for_closed(self):
        live = providers.LiveProvider()
        live.outbound({"type": "session.update", "session": {"tools": []}})  # session.start
        sent = []

        def send(event):
            sent.append(event)
            live.inbound({"type": "session.closed", "usage": {"seconds": 7}})
            return True

        with patch.object(providers, "log_event", lambda *a, **k: None):
            live.finalize(send, timeout=1)
        self.assertEqual(sent, [{"type": "session.close"}])
        self.assertTrue(live.closed.is_set())
        self.assertEqual(live.voice_seconds, 7)

    def test_finalize_is_a_no_op_before_session_start(self):
        sent = []
        providers.LiveProvider().finalize(lambda e: sent.append(e) or True, timeout=0.1)
        providers.RealtimeProvider("gpt-realtime-2.1").finalize(lambda e: sent.append(e) or True, timeout=0.1)
        self.assertEqual(sent, [])


class LiveBargeInTests(unittest.TestCase):
    def test_live_barge_in_flushes_player_without_cancel_or_truncate(self):
        from voice_cua import realtime_session

        session = realtime_session.RealtimeSession.__new__(realtime_session.RealtimeSession)
        session.provider = providers.LiveProvider()
        sent = []
        session._send = sent.append

        class Player:
            def has_playback(self):
                return True

            def interrupt(self):
                return 640, "live_out_1"

        session._audio = Player()
        with patch.object(realtime_session, "log_event", lambda *a, **k: None):
            session._handle_speech_started()
        self.assertEqual(sent, [])


def _started(live, session_id="live_abc"):
    with patch.object(providers, "log_event", lambda *a, **k: None):
        live.inbound({"type": "session.started", "session": {"id": session_id, "store": True}})


class LiveSessionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.live = providers.LiveProvider()
        self.live.bind(lambda e: self.sent.append(e) or True)

    def start(self):
        (start,) = self.live.outbound({"type": "session.update", "session": {"tools": []}})
        return start

    def test_start_has_event_id_store_and_nothing_else_until_started(self):
        start = self.start()
        self.assertEqual(start["type"], "session.start")
        self.assertTrue(start["event_id"].startswith("voice_start_"))
        self.assertIs(start["session"]["store"], True)
        self.assertEqual(start["session"]["delegation"]["type"], "responses")
        self.assertEqual(self.live.outbound({"type": "input_audio_buffer.append", "audio": "AAAA"}), [])
        self.assertEqual(self.live.outbound({"type": "conversation.item.create", "item": {
            "type": "message", "role": "system", "content": [{"type": "input_text", "text": "note"}]}}), [])
        _started(self.live)
        self.assertEqual([e["type"] for e in self.sent], ["session.instructions.append", "session.input_audio.append"])
        self.assertIsNone(self.sent[0]["delegation_id"])
        self.assertEqual(self.live.outbound({"type": "input_audio_buffer.append", "audio": "BBBB"}),
                         [{"type": "session.input_audio.append", "audio": "BBBB"}])

    def test_reconnect_forks_the_stored_session_with_only_allowed_overrides(self):
        self.start()
        _started(self.live, "live_src1")
        self.live.reset()
        self.assertEqual(self.live.url(), "wss://api.openai.com/v1/live/sessions/live_src1/fork")
        start = self.start()
        self.assertEqual(set(start["session"]), {"store", "audio"})
        note = {"type": "conversation.item.create", "item": {"type": "message", "role": "system", "content": [
            {"type": "input_text", "text": "The realtime connection was re-established; earlier turns are not in this conversation."}]}}
        self.assertEqual(self.live.outbound(note), [])

    def test_failed_fork_falls_back_to_a_seeded_fresh_session(self):
        self.start()
        _started(self.live, "live_src2")
        self.live._remember("user", "Open Calculator")
        self.live._remember("assistant", "Calculator is open.")
        self.live.reset()
        self.start()
        with patch.object(providers, "log_event", lambda *a, **k: None):
            out = self.live.inbound({"type": "error", "error": {"type": "invalid_request_error",
                                                                "code": "session_not_found", "message": "no recording"}})
        self.assertEqual(out, [])
        self.assertEqual(self.live.restart_requested, "live_fork_failed")
        self.live.reset()
        self.assertEqual(self.live.url(), "wss://api.openai.com/v1/live/sessions")
        start = self.start()
        roles = [(m["role"], m["content"][0]["type"]) for m in start["session"]["input"]]
        self.assertEqual(roles, [("user", "input_text"), ("assistant", "output_text")])

    def test_running_errors_surface_but_rejected_context_commands_do_not(self):
        self.start()
        _started(self.live)
        with patch.object(providers, "log_event", lambda *a, **k: None):
            rejected = self.live.inbound({"type": "error", "error": {"code": "invalid_value", "param": "content",
                                                                     "client_event_id": "voice_instructions_3"}})
            moderation = self.live.inbound({"type": "error", "error": {"code": "content_filter", "message": "cut"}})
        self.assertEqual(rejected, [])
        self.assertEqual(len(moderation), 1)

    def test_closed_with_expired_reason_requests_a_restart(self):
        self.start()
        _started(self.live)
        with patch.object(providers, "log_event", lambda *a, **k: None):
            self.live.inbound({"type": "session.closed", "reason": "expired", "usage": {"seconds": 3600}})
        self.assertEqual(self.live.restart_requested, "live_closed_expired")
        self.assertEqual(self.live.voice_seconds, 3600)

    def test_mute_events_follow_the_docs(self):
        self.assertEqual(self.live.mute_event(True)["type"], "session.input_audio.mute")
        self.assertEqual(self.live.mute_event(False)["type"], "session.input_audio.unmute")


class WakeDetectionTests(unittest.TestCase):
    def test_has_sound(self):
        import array
        import base64
        from voice_cua.realtime_session import _has_sound

        silent = base64.b64encode(bytes(960)).decode()
        loud = base64.b64encode(array.array("h", [3000] * 480).tobytes()).decode()
        self.assertFalse(_has_sound(silent))
        self.assertTrue(_has_sound(loud))


if __name__ == "__main__":
    unittest.main()
