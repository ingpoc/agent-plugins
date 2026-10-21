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


if __name__ == "__main__":
    unittest.main()
