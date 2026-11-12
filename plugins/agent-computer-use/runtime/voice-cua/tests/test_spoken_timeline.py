from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from voice_cua import activity_log  # noqa: E402


class SpokenTimelineTests(unittest.TestCase):
    def test_absent_transcript_does_not_add_live_fields(self) -> None:
        self.assertEqual(activity_log.spoken_timeline("no-transcript", 2.0, 3.0), {})

    def test_observed_phases_and_new_transcript_reset(self) -> None:
        clock = iter([1.0, 2.0, 4.0, 6.0, 8.0])
        with (patch.object(activity_log.time, "monotonic", side_effect=lambda: next(clock)),
              patch.object(activity_log.time, "time", return_value=100.0),
              patch.object(activity_log, "current_session_id", return_value="timeline-test"),
              patch.object(activity_log, "build_label", return_value="test"),
              patch.object(activity_log, "_append_row")):
            activity_log.log_event("user_transcript")
            activity_log.log_event("tool_start", tool="comet_act")
            activity_log.log_event("tool_result", tool="comet_act")
            activity_log.log_event("assistant_reply", source="backend")
            fields = activity_log.spoken_timeline("timeline-test", 8.0, 9.0)
            activity_log.log_event("user_transcript")
            reset = activity_log.spoken_timeline("timeline-test", 10.0, 11.0)
        self.assertEqual(fields["user_transcript_to_first_audio"], 7000)
        self.assertEqual(fields["user_transcript_to_first_tool_start"], 1000)
        self.assertEqual(fields["tool_time_total"], 2000)
        self.assertEqual(fields["slowest_tool"], "comet_act")
        self.assertEqual(fields["last_tool_end_to_first_audio"], 4000)
        self.assertEqual(fields["backend_reply_to_first_audio"], 2000)
        self.assertEqual(fields["first_audio_delta_to_playback_start"], 1000)
        self.assertEqual(fields["total_user_transcript_to_playback_start"], 8000)
        self.assertEqual(fields["dominant_phase"], "user_transcript_to_first_audio")
        self.assertTrue(fields["over_budget"])
        self.assertIsNone(reset["user_transcript_to_first_tool_start"])
        self.assertIsNone(reset["tool_time_total"])


if __name__ == "__main__":
    unittest.main()
