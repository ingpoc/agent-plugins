import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from voice_cua import audio_io


def _device(name="Built-in Mic", channels=1):
    return {"name": name, "max_input_channels": channels, "default_samplerate": 48000}


def test_failure_class_mapping():
    cases = {
        "no input device": "no_input_device",
        "Error querying device -1": "no_input_device",
        "Device or resource busy": "device_busy",
        "microphone needs access": "permission_denied",
        "PortAudio query failed": "query_error",
        "unexpected failure": "other",
    }
    for error, expected in cases.items():
        assert audio_io.mic_failure_class(error) == expected


def test_transient_failure_retries_and_logs_recovery():
    logs = []
    opens = []

    class Stream:
        def start(self):
            if not opens:
                opens.append("failed")
                raise RuntimeError("PortAudio query failed")
            opens.append("ok")

        def stop(self):
            pass

        def close(self):
            pass

    fake_sd = SimpleNamespace(
        default=SimpleNamespace(device=(0, -1)),
        query_devices=lambda *args, **kwargs: _device() if kwargs.get("kind") == "input" else [_device()],
        InputStream=lambda **kwargs: Stream(),
    )
    with (
        patch.object(audio_io, "sd", fake_sd),
        patch.object(audio_io, "_log_event", side_effect=lambda kind, **fields: logs.append((kind, fields))),
        patch.object(audio_io.time, "sleep") as sleep,
        patch.object(audio_io.sys, "platform", "linux"),
        patch.dict("os.environ", {"VOICE_CUA_NO_AUDIO": ""}),
    ):
        assert audio_io.mic_preflight() == 0

    assert len(logs) == 2
    assert all(kind == "mic_preflight" for kind, _ in logs)
    first, second = (fields for _, fields in logs)
    assert first["attempt"] == 1 and first["failure_class"] == "query_error"
    assert first["recovery"] == "retried" and first["retry_succeeded"] is True
    assert second["attempt"] == 2 and second["ok"] is True
    assert first["input_devices"] == [{"name": "Built-in Mic", "index": 0, "default": True}]
    assert first["default_input_device"] == "Built-in Mic"
    assert isinstance(first["duration_ms"], int)
    sleep.assert_called_once_with(0.5)


def test_missing_input_does_not_retry():
    logs = []
    fake_sd = SimpleNamespace(default=SimpleNamespace(device=(-1, -1)), query_devices=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Error querying device -1")))
    with (
        patch.object(audio_io, "sd", fake_sd),
        patch.object(audio_io, "_log_event", side_effect=lambda kind, **fields: logs.append((kind, fields))),
        patch.object(audio_io.time, "sleep") as sleep,
        patch.dict("os.environ", {"VOICE_CUA_NO_AUDIO": ""}),
    ):
        assert audio_io.mic_preflight() == audio_io.MIC_NO_INPUT_DEVICE
    assert len(logs) == 1
    assert logs[0][1]["failure_class"] == "no_input_device"
    assert logs[0][1]["error"] == "Error querying device -1"
    assert logs[0][1]["recovery"] == "none"
    sleep.assert_not_called()
