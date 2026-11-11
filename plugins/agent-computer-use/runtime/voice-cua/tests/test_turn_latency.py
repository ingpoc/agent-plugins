from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from voice_cua import realtime_session  # noqa: E402
from voice_cua.realtime_session import _turn_latency_fields  # noqa: E402


class TurnLatencyTests(unittest.TestCase):
    def test_phase_math_dominant_phase_and_budget(self):
        clock = iter([7.0])
        with patch.object(realtime_session.time, "monotonic", side_effect=lambda: next(clock)):
            turn = {"speech_end": 1.0, "response_created": 2.0, "first_tool_start": 2.5,
                    "tool_time_total_ms": 4100, "tool_count": 1, "slowest_tool": "comet_act",
                    "slowest_tool_ms": 4100, "last_tool_end": 6.6, "first_audio_delta": 6.8}
            fields = _turn_latency_fields(turn, realtime_session.time.monotonic())
        self.assertEqual(fields["speech_end_to_response_created"], 1000)
        self.assertEqual(fields["response_created_to_first_tool_start"], 500)
        self.assertEqual(fields["tool_time_total"], 4100)
        self.assertEqual(fields["slowest_tool"], "comet_act")
        self.assertEqual(fields["slowest_tool_ms"], 4100)
        self.assertEqual(fields["last_tool_end_to_first_audio_delta"], 200)
        self.assertEqual(fields["first_audio_delta_to_playback_start"], 200)
        self.assertEqual(fields["total_speech_end_to_playback_start"], 6000)
        self.assertEqual(fields["dominant_phase"], "tool_time_total")
        self.assertTrue(fields["over_budget"])

    def test_missing_phases_are_null_and_no_tool_is_not_zero(self):
        fields = _turn_latency_fields({}, None)
        self.assertIsNone(fields["speech_end_to_response_created"])
        self.assertIsNone(fields["response_created_to_first_tool_start"])
        self.assertIsNone(fields["tool_time_total"])
        self.assertIsNone(fields["last_tool_end_to_first_audio_delta"])
        self.assertIsNone(fields["first_audio_delta_to_playback_start"])
        self.assertIsNone(fields["total_speech_end_to_playback_start"])
        self.assertIsNone(fields["slowest_tool"])
        self.assertIsNone(fields["slowest_tool_ms"])
        self.assertIsNone(fields["dominant_phase"])
        self.assertFalse(fields["over_budget"])


if __name__ == "__main__":
    unittest.main()
