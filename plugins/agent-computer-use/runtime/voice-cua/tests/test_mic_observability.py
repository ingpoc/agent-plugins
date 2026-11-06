import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from voice_cua import audio_io, startup_trace


class MicrophoneEvidenceTests(unittest.TestCase):
    def test_snapshot_distinguishes_missing_input_and_query_failure(self):
        device = SimpleNamespace(default=SimpleNamespace(device=(-1, 1)))
        with patch.object(audio_io, "sd", device):
            self.assertEqual(audio_io.input_device_snapshot()["input_availability"], "unavailable")
        device.default.device = (0, 1)
        device.query_devices = lambda _index: (_ for _ in ()).throw(RuntimeError("query failed"))
        with patch.object(audio_io, "sd", device):
            self.assertEqual(audio_io.input_device_snapshot()["input_availability"], "unknown")
        device.query_devices = lambda _index: {"name": "Mic", "max_input_channels": 1, "default_samplerate": 48000}
        with patch.object(audio_io, "sd", device):
            self.assertEqual(audio_io.input_device_snapshot(), {
                "input_availability": "available", "input_selection": "system_default_snapshot",
                "input_device": "Mic", "input_rate": 48000,
            })

    def test_recovery_requires_success_after_failure_in_same_timer(self):
        with patch.object(startup_trace, "log_startup") as log, patch.object(
            audio_io, "input_device_snapshot", return_value={"input_availability": "unknown"}
        ):
            timer = startup_trace.StartupTimer()
            timer.mark("audio_ready", status="ok")
            self.assertEqual(log.call_args.kwargs["input_recovery"], "not_observed")
            timer.mark("mic_preflight", status="skip")
            timer.mark("audio_ready", status="fail")
            self.assertEqual(log.call_args.kwargs["input_recovery"], "not_observed")
            timer.mark("audio_ready", status="ok")
            self.assertEqual(log.call_args.kwargs["input_recovery"], "recovered")
            startup_trace.StartupTimer().mark("audio_ready", status="ok")
            self.assertEqual(log.call_args.kwargs["input_recovery"], "not_observed")


if __name__ == "__main__":
    unittest.main()
