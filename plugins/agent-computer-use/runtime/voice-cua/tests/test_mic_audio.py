"""Mic path for AirPods / GPT-Live: resampling, no dropped or gated frames, local barge-in that
needs sustained speech above the adaptive floor and above Samantha's own playback echo."""

from __future__ import annotations

import base64
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
os.environ.setdefault(
    "VOICE_CUA_ACTIVITY_LOG",
    str(Path(tempfile.mkdtemp(prefix="voice-cua-test-")) / "activity.jsonl"),
)

import numpy as np  # noqa: E402

from voice_cua import audio_io, providers  # noqa: E402
from voice_cua.audio_dsp import BargeInDetector, FrameChunker, NoiseFloor, StreamResampler, pcm_rms  # noqa: E402
from voice_cua.voice_meter import pcm_levels  # noqa: E402

WIRE = 24000


def tone(freq: float, rate: int, seconds: float, amp: float = 0.3, phase0: float = 0.0) -> bytes:
    n = int(round(rate * seconds))
    t = np.arange(n) / rate
    return (np.sin(2 * np.pi * freq * t + phase0) * amp * 32767).astype(np.int16).tobytes()


def frame_at(level: float, samples: int = 480) -> bytes:
    """20 ms frame whose raw RMS is `level` (a full-scale-normalised square-ish noise)."""
    rng = np.random.default_rng(int(level * 1e6) % 2**32)
    x = rng.choice([-1.0, 1.0], size=samples) * level * 32768
    return np.clip(x, -32768, 32767).astype(np.int16).tobytes()


class ResamplerTests(unittest.TestCase):
    def _blocks(self, pcm: bytes, block_samples: int) -> list[bytes]:
        step = block_samples * 2
        return [pcm[i:i + step] for i in range(0, len(pcm), step)]

    def test_16k_to_24k_length_is_exact_over_the_stream(self):
        rs = StreamResampler(16000, WIRE)
        src = tone(440, 16000, 2.0)
        out = b"".join(rs.process(b) for b in self._blocks(src, 320))  # 20 ms AirPods HFP blocks
        expected = len(src) // 2 * WIRE // 16000
        self.assertLessEqual(abs(len(out) // 2 - expected), 1)

    def test_16k_to_24k_preserves_the_waveform(self):
        rs = StreamResampler(16000, WIRE)
        src = tone(440, 16000, 0.5)
        out = np.frombuffer(b"".join(rs.process(b) for b in self._blocks(src, 320)), dtype=np.int16)
        ideal = np.frombuffer(tone(440, WIRE, len(out) / WIRE), dtype=np.int16)[: len(out)]
        err = np.abs(out.astype(float) - ideal.astype(float))
        self.assertLess(err[10:].max() / 32767, 0.01)  # linear interpolation of a 440 Hz tone

    def test_block_edges_match_one_shot(self):
        src = tone(1000, 16000, 0.3)
        whole = StreamResampler(16000, WIRE).process(src)
        rs = StreamResampler(16000, WIRE)
        pieces = b"".join(rs.process(b) for b in self._blocks(src, 137))  # odd block size
        n = min(len(whole), len(pieces))
        a = np.frombuffer(whole[:n], dtype=np.int16).astype(int)
        b = np.frombuffer(pieces[:n], dtype=np.int16).astype(int)
        self.assertLessEqual(np.abs(a - b).max(), 1)

    def test_48k_downsample_filters_alias(self):
        rs = StreamResampler(48000, WIRE)
        passband = np.frombuffer(rs.process(tone(1000, 48000, 0.5)), dtype=np.int16)
        rs2 = StreamResampler(48000, WIRE)
        alias = np.frombuffer(rs2.process(tone(15000, 48000, 0.5)), dtype=np.int16)  # above 12 kHz
        self.assertGreater(pcm_rms(passband[100:]), 0.15)
        self.assertLess(pcm_rms(alias[100:]), pcm_rms(passband[100:]) / 10)  # >20 dB down
        self.assertLessEqual(abs(len(passband) - 12000), 1)

    def test_same_rate_is_passthrough(self):
        rs = StreamResampler(WIRE, WIRE)
        src = tone(300, WIRE, 0.02)
        self.assertEqual(rs.process(src), src)

    def test_chunker_recuts_to_wire_frames(self):
        ch = FrameChunker(960)
        frames = ch.push(b"\x01\x00" * 700) + ch.push(b"\x01\x00" * 260)
        self.assertEqual([len(f) for f in frames], [960, 960])


class _Sent(list):
    def __call__(self, event):
        self.append(event)

    def pcm(self) -> list[bytes]:
        return [base64.b64decode(e["audio"]) for e in self]


class MicFrameTests(unittest.TestCase):
    def _mic(self, *, continuous=True, rate=WIRE, **kw):
        sent = _Sent()
        mic = audio_io.MicStreamer(sent, continuous=continuous, **kw)
        mic.set_stream_rate(rate)
        mic._gate_open, mic._gate_hold = False, 0
        mic._gate_thresholds = (0.062, 0.042)
        return mic, sent

    def _feed_blocks(self, mic, pcm: bytes, block_samples: int):
        for i in range(0, len(pcm), block_samples * 2):
            for frame in mic._wire_frames(pcm[i:i + block_samples * 2]):
                mic._process_frame(frame, pcm_levels)

    def test_airpods_16k_capture_is_resampled_to_24k_frames_with_none_dropped(self):
        mic, sent = self._mic(rate=16000)
        self._feed_blocks(mic, tone(300, 16000, 1.0, amp=0.02), 320)
        self.assertGreaterEqual(len(sent), 49)
        self.assertTrue(all(len(p) == 960 for p in sent.pcm()))
        self.assertEqual(mic.device_summary()["resampled"], True)
        self.assertEqual(mic.device_summary()["wire_rate"], WIRE)

    def test_live_sends_quiet_speech_unmodified(self):
        """The old client gate zeroed frames under RMS 0.062 (scaled): soft AirPods speech vanished."""
        mic, sent = self._mic()
        quiet = tone(220, WIRE, 0.02, amp=0.015)
        mic._process_frame(quiet, pcm_levels)
        self.assertEqual(sent.pcm()[0], quiet)

    def test_live_is_not_muted_while_samantha_talks(self):
        with patch.object(audio_io, "_local_mute_mode", lambda *_: "tail"):
            audio = audio_io.RealtimeAudio(lambda _e: None, continuous=True)
        self.assertEqual(audio.mute_mode, "none")
        with patch.object(audio_io, "_local_mute_mode", lambda *_: "full"):
            polite = audio_io.RealtimeAudio(lambda _e: None, continuous=True)
        self.assertEqual(polite.mute_mode, "full")  # polite keeps the echo guard on purpose

    def test_muted_live_mic_still_streams_silence(self):
        mic, sent = self._mic(mute_while=lambda: True)
        mic._process_frame(tone(220, WIRE, 0.02), pcm_levels)
        self.assertEqual(sent.pcm(), [b"\x00" * 960])

    def test_non_live_gate_keeps_word_endings(self):
        mic, sent = self._mic(continuous=False)
        loud = frame_at(0.05)   # scaled RMS 0.12: opens the gate
        soft = frame_at(0.01)   # scaled 0.024: under the close threshold
        mic._process_frame(loud, pcm_levels)
        for _ in range(5):
            mic._process_frame(soft, pcm_levels)
        self.assertTrue(all(p != b"\x00" * 960 for p in sent.pcm()))  # hangover kept them
        for _ in range(audio_io.GATE_HANGOVER_FRAMES + 2):
            mic._process_frame(soft, pcm_levels)
        self.assertEqual(sent.pcm()[-1], b"\x00" * 960)

    def test_callback_queues_every_block_and_counts_overflow(self):
        mic, _ = self._mic()

        class Status:
            input_overflow = True

            def __bool__(self):
                return True

        mic._input_callback(np.zeros((480, 1), dtype=np.int16), 480, None, Status())
        mic._input_callback(np.zeros((480, 1), dtype=np.int16), 480, None, None)
        self.assertEqual(mic._q.qsize(), 2)
        self.assertEqual(mic.overflows, 1)


class LocalBargeInTests(unittest.TestCase):
    def _run(self, det, levels, playback=0.0):
        hits = []
        for lv in levels:
            hit = det.feed(lv, playback)
            if hit:
                hits.append(hit)
        return hits

    def test_blips_do_not_barge_in(self):
        det = BargeInDetector()
        self._run(det, [0.001] * 50)
        for _ in range(5):  # 120 ms coughs / clicks, the 0-420 ms blips in the log
            self.assertEqual(self._run(det, [0.1] * 6 + [0.001] * 10), [])

    def test_sustained_speech_triggers_once_after_300ms(self):
        det = BargeInDetector()
        self._run(det, [0.001] * 50)
        hits = []
        for i in range(40):
            hit = det.feed(0.05)
            if hit:
                hits.append((i, hit))
        self.assertEqual(len(hits), 1)
        index, info = hits[0]
        self.assertGreaterEqual((index + 1) * 20, 300)
        self.assertGreaterEqual(info["speech_ms"], 300)

    def test_short_dips_inside_a_word_still_count(self):
        det = BargeInDetector()
        self._run(det, [0.001] * 50)
        pattern = ([0.05] * 5 + [0.001] * 2) * 4
        self.assertEqual(len(self._run(det, pattern)), 1)

    def test_retriggers_only_after_quiet(self):
        det = BargeInDetector()
        self._run(det, [0.001] * 50)
        self.assertEqual(len(self._run(det, [0.05] * 30)), 1)
        self.assertEqual(len(self._run(det, [0.001] * 10 + [0.05] * 30)), 0)  # 200 ms pause: same turn
        self.assertEqual(len(self._run(det, [0.001] * 40 + [0.05] * 30)), 1)

    def test_steady_room_noise_raises_the_floor(self):
        det = BargeInDetector()
        hits = self._run(det, [0.012] * 6 + [0.002] * 2)  # quick noisy start, no trigger
        self.assertEqual(hits, [])
        floor = NoiseFloor()
        for _ in range(500):
            floor.update(0.02, speech=False)
        self.assertGreater(floor.level, 0.015)

    def test_own_playback_echo_is_ignored(self):
        det = BargeInDetector()
        self._run(det, [0.001] * 50)
        playback = 0.1
        echo = [0.03] * 200  # 4 s of AirPods / room echo at 0.3x her level
        self.assertEqual(self._run(det, echo, playback=playback), [])

    def test_user_over_playback_still_barges_in(self):
        det = BargeInDetector()
        self._run(det, [0.001] * 50)
        self._run(det, [0.01] * 100, playback=0.1)  # learns a low echo coupling
        hits = self._run(det, [0.2] * 30, playback=0.1)
        self.assertEqual(len(hits), 1)
        self.assertGreater(hits[0]["playback_rms"], 0)


class SpeakerAudibleTests(unittest.TestCase):
    def test_streamed_silence_is_not_playback(self):
        player = audio_io.SpeakerPlayer()
        player.push_b64(base64.b64encode(b"\x00\x00" * 4800).decode())
        self.assertFalse(player.has_audio())
        self.assertFalse(player.has_substantial_audio())

    def test_speech_is_playback_until_played(self):
        player = audio_io.SpeakerPlayer()
        speech = tone(300, WIRE, 0.04)
        player.push_b64(base64.b64encode(speech).decode())
        player.push_b64(base64.b64encode(b"\x00\x00" * 4800).decode())  # trailing silence
        self.assertTrue(player.has_audio())
        out = np.zeros((480, 1), dtype=np.int16)
        player._output_callback(out, 480, None, None)
        self.assertTrue(player.has_audio())
        self.assertGreater(player.playback_level(), 0.1)
        player._output_callback(out, 480, None, None)
        self.assertFalse(player.has_audio())  # only silence left in the queue


class SessionBargeInTests(unittest.TestCase):
    def _session(self, playing=True):
        from voice_cua import realtime_session

        session = realtime_session.RealtimeSession.__new__(realtime_session.RealtimeSession)
        session.provider = providers.LiveProvider()
        session._send = lambda _e: True

        class Player:
            interrupts = 0

            def has_playback(self):
                return playing

            def interrupt(self):
                Player.interrupts += 1
                return 900, "live_out_1"

        session._audio = Player()
        return session, Player, realtime_session

    def test_server_transcript_delta_does_not_flush_live_playback(self):
        session, player, rs = self._session()
        with patch.object(rs, "island_publish", lambda *a, **k: None), \
                patch.object(rs, "log_event", lambda *a, **k: None):
            session._dispatch_event({"type": "input_audio_buffer.speech_started"})
        self.assertEqual(player.interrupts, 0)

    def test_local_vad_flushes_and_logs(self):
        session, player, rs = self._session()
        events = []
        with patch.object(rs, "island_publish", lambda *a, **k: None), \
                patch.object(rs, "log_event", lambda kind, **kw: events.append((kind, kw))):
            session._on_local_barge_in({"speech_ms": 320, "rms": 0.05, "floor": 0.002})
        self.assertEqual(player.interrupts, 1)
        self.assertEqual(events[0][0], "barge_in_local")
        self.assertEqual(events[0][1]["source"], "local_vad")
        self.assertEqual(events[0][1]["speech_ms"], 320)

    def test_local_vad_without_playback_is_a_noop(self):
        session, player, rs = self._session(playing=False)
        with patch.object(rs, "log_event", lambda *a, **k: None):
            session._on_local_barge_in({"speech_ms": 320})
        self.assertEqual(player.interrupts, 0)

    def test_device_summary_logged_fields(self):
        session, _player, _rs = self._session()

        class Audio:
            def device_summary(self):
                return {"input": "Guru’s AirPods Pro", "input_rate": 24000, "stream_rate": 24000,
                        "wire_rate": 24000, "resampled": False, "output": None}

        session._audio = Audio()
        info = session._audio_devices()
        self.assertEqual(info["input"], "Guru’s AirPods Pro")
        self.assertNotIn("output", info)


class TranscriptLoggingTests(unittest.TestCase):
    def test_live_user_transcript_logs_text_locally(self):
        events = []
        with patch.object(providers, "log_event", lambda kind, **kw: events.append((kind, kw))), \
                patch("voice_cua.island_facade.island_publish", lambda *a, **k: None):
            live = providers.LiveProvider()
            live._on_heard("Open Sound settings")
        user = dict(events)["user_transcript"]
        self.assertEqual(user["text"], "Open Sound settings")
        self.assertEqual(user["chars"], len("Open Sound settings"))


class DeviceInfoTests(unittest.TestCase):
    def test_mic_opens_at_device_native_rate(self):
        opened = []

        class FakeStream:
            def __init__(self, **kw):
                opened.append(kw)

            def start(self):
                pass

        fake_sd = type("SD", (), {"InputStream": FakeStream})
        with patch.object(audio_io, "sd", fake_sd), \
                patch.object(audio_io, "input_device_info", lambda *_: {"name": "AirPods", "rate": 16000}):
            mic = audio_io.MicStreamer(lambda _e: None, continuous=True)
            mic.start()
            mic._stop.set()
        self.assertEqual(opened[0]["samplerate"], 16000)
        self.assertEqual(opened[0]["blocksize"], 320)
        self.assertEqual(mic.device_summary(), {"input": "AirPods", "input_rate": 16000,
                                                "stream_rate": 16000, "wire_rate": WIRE, "resampled": True})

    def test_falls_back_to_wire_rate_when_native_refused(self):
        opened = []

        class FakeStream:
            def __init__(self, **kw):
                if kw["samplerate"] != WIRE:
                    raise RuntimeError("Invalid sample rate")
                opened.append(kw)

            def start(self):
                pass

        fake_sd = type("SD", (), {"InputStream": FakeStream})
        with patch.object(audio_io, "sd", fake_sd), \
                patch.object(audio_io, "input_device_info", lambda *_: {"name": "Odd", "rate": 44100}):
            mic = audio_io.MicStreamer(lambda _e: None, continuous=True)
            mic.start()
            mic._stop.set()
        self.assertEqual(opened[0]["samplerate"], WIRE)
        self.assertFalse(mic.device_summary()["resampled"])




class VoiceLatencyTests(unittest.TestCase):
    def test_silence_does_not_start_timing_and_queued_audio_delays_playback(self):
        from types import SimpleNamespace
        player = audio_io.SpeakerPlayer()
        silence = base64.b64encode(bytes(960)).decode()
        speech = base64.b64encode(frame_at(0.2)).decode()
        with patch.object(audio_io.time, "monotonic", return_value=10.0):
            self.assertFalse(player.push_b64(silence, speech_at=9.0))
            self.assertTrue(player.push_b64(speech, speech_at=9.0, item_id="reply"))
        out = np.empty((480, 1), dtype=np.int16)
        clock = SimpleNamespace(outputBufferDacTime=2.03, currentTime=2.0)
        with patch.object(audio_io.time, "monotonic", return_value=10.02):
            player._output_callback(out, 480, clock, None)
            self.assertTrue(player.timings.empty())
            player._output_callback(out, 480, clock, None)
        row = player.timings.get_nowait()
        self.assertEqual(row["speech_to_received_ms"], 1000)
        self.assertEqual(row["received_to_playback_ms"], 50)
        self.assertEqual(row["speech_to_playback_ms"], 1050)
        self.assertEqual(row["item_id"], "reply")
        self.assertTrue(player.timings.empty())

    def test_interruption_discards_unplayed_timing(self):
        player = audio_io.SpeakerPlayer()
        player.push_b64(base64.b64encode(frame_at(0.2)).decode(), speech_at=1.0)
        player.clear()
        player._output_callback(np.empty((480, 1), dtype=np.int16), 480, None, None)
        self.assertTrue(player.timings.empty())


if __name__ == "__main__":
    unittest.main()
