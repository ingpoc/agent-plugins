from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from voice_cua import activity_log  # noqa: E402
from voice_cua.realtime_session import RealtimeSession  # noqa: E402


class FakeWS:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def send(self, raw: str) -> None:
        self.events.append(json.loads(raw))


class SpokenStopTests(unittest.TestCase):
    def test_detector_matches_short_stop_intents_only(self) -> None:
        for phrase in (
            "stop", "please stop", "Wait, stop", "cancel that", "never mind",
            "leave it", "stop it", "you can stop", "that's enough",
        ):
            with self.subTest(phrase=phrase):
                self.assertTrue(RealtimeSession._spoken_task_cancel(phrase))
        for phrase in (
            "don't stop", "keep going", "stop by the store and open Safari",
            "please stop by the store", "the stop button is there",
        ):
            with self.subTest(phrase=phrase):
                self.assertFalse(RealtimeSession._spoken_task_cancel(phrase))

    def test_spoken_stop_between_response_start_and_dispatch_skips_action(self) -> None:
        session = RealtimeSession(api_key="test", enable_audio=False)
        session.ws = FakeWS()
        run_id = "run-spoken-stop"
        session._active_task_run_id = run_id
        session._task_runs[run_id] = []
        rows: list[dict] = []

        with patch.object(activity_log, "_append_row", side_effect=rows.append), \
                patch("voice_cua.realtime_session.dispatch") as dispatch, \
                patch("voice_cua.realtime_session.island_publish"):
            session._dispatch_event({"type": "response.created", "response": {
                "id": "response-before-stop", "metadata": {"voice_cua_request_id": run_id},
            }})
            session._dispatch_event({"type": "conversation.item.input_audio_transcription.completed",
                                     "transcript": "Wait, stop", "item_id": "stop-transcript"})
            session._handle_function_done({"call_id": "call-after-stop", "name": "cua_act",
                                           "arguments": "{}", "response_id": "response-before-stop"})
            deadline = time.monotonic() + 2
            while session._inflight_tools and time.monotonic() < deadline:
                time.sleep(0.01)

        dispatch.assert_not_called()
        cancelled = [row for row in rows if row.get("kind") == "tool_cancelled"]
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(cancelled[0]["classification"], "user_cancelled")
        cancel = next(row for row in rows if row.get("kind") == "cancel")
        self.assertEqual(cancel["cancel_source"], "transcript")
        self.assertEqual(cancel["caused_by"][-1], next(
            row["step_ref"] for row in rows
            if row.get("kind") == "user_transcript" and row.get("text") == "Wait, stop"
        ))


if __name__ == "__main__":
    unittest.main()


def test_detector_ignores_leading_filler_words() -> None:
    from voice_cua.realtime_session import RealtimeSession
    for phrase in ("Uh, you can stop", "Um, wait, stop", "Okay, please stop.", "Hey Samantha, cancel that"):
        assert RealtimeSession._spoken_task_cancel(phrase), phrase
    assert not RealtimeSession._spoken_task_cancel("Uh, don't stop")
