"""Low-latency PCM capture/playback for OpenAI Realtime GA (24 kHz mono int16).

Design: 20 ms frames (480 samples), callback I/O, dedicated mic sender thread,
minimal playback buffer. The mic opens at the input device's native rate (AirPods in
headset mode run at 16 or 24 kHz) and is resampled to the 24 kHz declared in
session.start audio.format. GPT-Live (continuous) gets every mic frame unmodified: it is
full duplex and does its own turn detection; a local barge-in only flushes the player after
sustained speech above the adaptive floor and above the echo of Samantha's own playback.
Polite eagerness keeps the local echo guard (mic silenced while she is audible).
"""

from __future__ import annotations

import base64
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import numpy as np

try:
    from voice_cua.audio_dsp import BargeInDetector, FrameChunker, StreamResampler, pcm_rms
except ImportError:  # pragma: no cover — bundled without package root
    from audio_dsp import BargeInDetector, FrameChunker, StreamResampler, pcm_rms  # type: ignore

try:
    from voice_cua.activity_log import log_event as _log_event
except ImportError:  # pragma: no cover
    def _log_event(_kind: str, **_fields: Any) -> None:
        return None

try:
    import sounddevice as sd
except ImportError:  # pragma: no cover
    sd = None  # type: ignore

try:
    from voice_cua.voice_settings import local_mute_mode as _local_mute_mode
    from voice_cua.voice_settings import mic_gate_thresholds as _mic_gate_thresholds
except ImportError:  # pragma: no cover — bundled without package root
    def _local_mute_mode(_eagerness: str | None = None) -> str:
        return os.environ.get("VOICE_CUA_EAGERNESS", "balanced")

    def _mic_gate_thresholds(_eagerness: str | None = None) -> tuple[float, float]:
        level = os.environ.get("VOICE_CUA_EAGERNESS", "balanced").lower()
        if level == "eager":
            return 0.048, 0.034
        if level == "polite":
            return 0.075, 0.055
        return 0.062, 0.042

SAMPLE_RATE = int(os.environ.get("VOICE_CUA_SAMPLE_RATE", "24000"))
# 20 ms @ 24 kHz — low latency without excessive WS event rate (~50/s).
BLOCKSIZE = int(os.environ.get("VOICE_CUA_AUDIO_BLOCK", "480"))
FRAME_BYTES = BLOCKSIZE * 2  # int16 mono

SendFn = Callable[[dict[str, Any]], None]
MeterFn = Callable[[str, float, list[float]], None]
# Island mic meter: at most 10 updates/s, and only while the user is audible
# (plus one "idle" on release) — never a continuous waveform flood.
METER_INTERVAL_S = 0.1
# A played or queued chunk below this RMS is silence. GPT-Live streams output audio
# continuously, silence included, so "bytes queued" does not mean "Samantha is speaking".
AUDIBLE_RMS = 0.002  # about -54 dBFS
# Non-continuous gate: stay open this long after the level dips (word endings, soft consonants).
GATE_HANGOVER_FRAMES = 15  # 300 ms


def input_device_info(device: Any = None) -> dict[str, Any]:
    """Name and native sample rate of the input device PortAudio will open (default: system input)."""
    if sd is None:
        return {}
    try:
        info = sd.query_devices(device, kind="input") if device is None else sd.query_devices(device)
        return {"name": str(info.get("name") or ""), "rate": int(round(float(info.get("default_samplerate") or 0)))}
    except Exception:  # noqa: BLE001 — no device / PortAudio error
        return {}


def output_device_info() -> dict[str, Any]:
    if sd is None:
        return {}
    try:
        info = sd.query_devices(None, kind="output")
        return {"name": str(info.get("name") or ""), "rate": int(round(float(info.get("default_samplerate") or 0)))}
    except Exception:  # noqa: BLE001
        return {}


def _voice_cua_app_bundle() -> Path | None:
    exe = Path(sys.executable).resolve()
    if exe.name != "voice-cua":
        return None
    info = exe.parent.parent / "Info.plist"
    if not info.is_file():
        return None
    return exe.parent.parent.parent


def _mic_authorized() -> bool:
    """Preflight only (AVCaptureDevice.authorizationStatus via the bundled mic-preflight tool).
    Never prompts: ACU onboarding owns the Microphone request. Opening the mic while access is
    undecided would raise the prompt mid-session, so callers stop when this is False."""
    mic_bin = Path(sys.executable).resolve().parent / "mic-preflight"
    if mic_bin.is_file():
        try:
            proc = subprocess.run([str(mic_bin)], timeout=10, check=False)
            return proc.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
    # Dev checkout (no app bundle): nothing ACU-attributed to protect.
    return _voice_cua_app_bundle() is None


def audio_available() -> bool:
    return sd is not None


def mic_preflight() -> int:
    """Open/close mic once so macOS TCC runs when Samantha turns on, not mid-session."""
    if os.environ.get("VOICE_CUA_NO_AUDIO", "").strip().lower() in {"1", "true", "yes"}:
        return 0
    if sys.platform == "darwin" and not _mic_authorized():
        print("mic preflight failed: microphone needs access (ACU onboarding › Microphone)", file=sys.stderr, flush=True)
        return 1
    if sd is None:
        print("mic preflight: sounddevice unavailable", file=sys.stderr, flush=True)
        return 1
    stream = None
    try:
        stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=BLOCKSIZE,
        )
        stream.start()
        stream.stop()
        stream.close()
        print("mic preflight ok", flush=True)
        return 0
    except Exception as exc:  # pragma: no cover — hardware/TCC dependent
        print(f"mic preflight failed: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass


_preflight_event: threading.Event | None = None
_preflight_result = 0
_preflight_lock = threading.Lock()


def reset_mic_preflight_async() -> None:
    global _preflight_event, _preflight_result
    with _preflight_lock:
        _preflight_event = threading.Event()
        _preflight_result = 0


def start_mic_preflight_async() -> None:
    """Run mic TCC preflight on a background thread (overlaps Realtime WS connect)."""
    reset_mic_preflight_async()

    def _run() -> None:
        global _preflight_result
        _preflight_result = mic_preflight()
        if _preflight_event is not None:
            _preflight_event.set()

    threading.Thread(target=_run, name="mic-preflight", daemon=True).start()


def wait_mic_preflight(*, timeout: float = 30.0) -> int:
    """Block until async preflight completes (sync fallback if not started)."""
    if _preflight_event is None:
        return mic_preflight()
    if not _preflight_event.wait(timeout=timeout):
        return 1
    return _preflight_result


class MicStreamer:
    """Capture mic PCM and emit input_audio_buffer.append events."""

    def __init__(
        self,
        send: SendFn,
        *,
        sample_rate: int = SAMPLE_RATE,
        blocksize: int = BLOCKSIZE,
        mute_while: Callable[[], bool] | None = None,
        meter: MeterFn | None = None,
        continuous: bool = False,
        on_barge_in: Callable[[dict[str, Any]], None] | None = None,
        playback_level: Callable[[], float] | None = None,
    ) -> None:
        # continuous: send silence instead of nothing while muted (GPT-Live expects a continuous,
        # ordered input stream and keeps listening through silence).
        self._continuous = continuous
        self._on_barge_in = on_barge_in
        self._playback_level = playback_level or (lambda: 0.0)
        self.barge = BargeInDetector()
        self.last_speech_at: float | None = None
        self.device_name = ""
        self.device_rate = 0
        self.stream_rate = sample_rate
        self._resampler: StreamResampler | None = None
        self._chunker = FrameChunker(blocksize * 2)
        self.frames_sent = 0
        self.overflows = 0
        self._send = send
        self._meter = meter
        self._meter_last = 0.0
        self._meter_side = "idle"
        self._sender_lock = threading.Lock()
        self.send_errors = 0
        self._sample_rate = sample_rate
        self._blocksize = blocksize
        self._mute_while = mute_while or (lambda: False)
        self._q: queue.SimpleQueue[bytes] = queue.SimpleQueue()
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._stream: Any = None
        self._sender: threading.Thread | None = None

    def sender_alive(self) -> bool:
        return self._sender is not None and self._sender.is_alive()

    def ensure_sender(self) -> bool:
        """Restart a dead sender thread (returns True when a restart happened)."""
        with self._sender_lock:
            if self._stop.is_set() or self.sender_alive():
                return False
            self._spawn_sender()
            return True

    def _start_sender(self) -> None:
        with self._sender_lock:
            if not self.sender_alive():
                self._spawn_sender()

    def _spawn_sender(self) -> None:
        self._sender = threading.Thread(target=self._sender_loop, name="mic-sender", daemon=True)
        self._sender.start()

    def _emit_meter(self, side: str, level: float, bars: list[float]) -> None:
        if self._meter is None:
            return
        import time as _time

        now = _time.monotonic()
        if side == self._meter_side and now - self._meter_last < METER_INTERVAL_S:
            return
        if side == "idle" and self._meter_side == "idle":
            return  # silence → already idle; nothing to push
        self._meter_last = now
        self._meter_side = side
        try:
            self._meter(side, level, bars)
        except Exception:  # noqa: BLE001 — meter is cosmetic
            pass

    def start(self) -> None:
        if sd is None:
            raise RuntimeError("pip install sounddevice numpy")
        self._start_sender()
        dev = input_device_info()
        self.device_name = str(dev.get("name") or "")
        self.device_rate = int(dev.get("rate") or 0)
        native = self.device_rate if 8000 <= self.device_rate <= 192000 else self._sample_rate
        try:
            self._open_input(native)
        except Exception:  # noqa: BLE001 — native rate refused: let PortAudio convert to the wire rate
            if native == self._sample_rate:
                raise
            self._open_input(self._sample_rate)
        self._stream.start()

    def _open_input(self, rate: int) -> None:
        self._stream = sd.InputStream(
            samplerate=rate,
            channels=1,
            dtype="int16",
            blocksize=max(1, rate * self._blocksize // self._sample_rate),  # 20 ms at that rate
            callback=self._input_callback,
        )
        self.set_stream_rate(rate)

    def set_stream_rate(self, rate: int) -> None:
        """Capture rate of the open stream; frames are resampled to the wire rate when it differs."""
        self.stream_rate = int(rate)
        self._resampler = None if rate == self._sample_rate else StreamResampler(rate, self._sample_rate)

    def device_summary(self) -> dict[str, Any]:
        return {
            "input": self.device_name or None,
            "input_rate": self.device_rate or None,
            "stream_rate": self.stream_rate,
            "wire_rate": self._sample_rate,
            "resampled": self._resampler is not None,
        }

    def _input_callback(self, indata: Any, _frames: int, _time: Any, status: Any) -> None:
        if self._stop.is_set():
            return
        if status and getattr(status, "input_overflow", False):
            self.overflows += 1
        # Every captured block is queued; muting is decided in the sender, so nothing is lost
        # here and the resampler sees a continuous stream.
        self._q.put(bytes(indata))

    def _wire_frames(self, chunk: bytes) -> list[bytes]:
        """Captured block → 20 ms frames at the wire rate."""
        if self._resampler is not None:
            chunk = self._resampler.process(chunk)
        return self._chunker.push(chunk)

    def _sender_loop(self) -> None:
        from voice_cua.voice_meter import pcm_levels

        self._gate_open = False
        self._gate_hold = 0
        self._gate_thresholds = _mic_gate_thresholds()
        self._overflows_logged = 0
        while not self._stop.is_set():
            try:
                block = self._q.get(timeout=0.05)
            except queue.Empty:
                self._gate_open = False
                continue
            for frame in self._wire_frames(block):
                self._process_frame(frame, pcm_levels)
            if self.overflows > self._overflows_logged:
                if self._overflows_logged == 0 or self.overflows - self._overflows_logged >= 50:
                    _log_event("mic_overflow", overflows=self.overflows)
                    self._overflows_logged = self.overflows

    def _process_frame(self, frame: bytes, pcm_levels: Callable[[bytes], tuple[float, list[float]]]) -> None:
        """One 20 ms wire frame: mute, local barge-in, meter, send."""
        muted = self._paused.is_set() or self._mute_while()
        if muted:
            self._gate_open = False
            self._gate_hold = 0
            if not self._continuous:
                return
            self._emit_meter("idle", 0.0, [0.0] * 8)
            self._send_frame(b"\x00" * len(frame))
            return

        if self._continuous:
            # GPT-Live: every frame goes out as captured (no client gate); the server does turn
            # detection. Locally, sustained speech over Samantha's audible playback flushes the player.
            raw = pcm_rms(frame)
            playback = float(self._playback_level() or 0.0)
            if self.barge.is_speech(raw, playback):
                self.last_speech_at = time.monotonic()
            hit = self.barge.feed(raw, playback)
            if hit is not None and playback > 0 and self._on_barge_in is not None:
                try:
                    self._on_barge_in(hit)
                except Exception:  # noqa: BLE001 — barge-in is best effort; never stop the mic
                    pass
            if self.barge.in_speech:
                rms, bars = pcm_levels(frame)
                self._emit_meter("user", rms, list(bars))
            else:
                self._emit_meter("idle", 0.0, [0.0] * 8)
            self._send_frame(frame)
            return

        rms, bars = pcm_levels(frame)
        gate_open_rms, gate_close_rms = self._gate_thresholds
        if rms >= gate_open_rms:
            self._gate_open = True
            self._gate_hold = GATE_HANGOVER_FRAMES
        elif rms < gate_close_rms:
            if self._gate_hold > 0:
                self._gate_hold -= 1   # hangover: keep word endings and soft consonants
            else:
                self._gate_open = False
        if self._gate_open:
            self._emit_meter("user", rms, list(bars))
        else:
            self._emit_meter("idle", 0.0, [0.0] * 8)
        self._send_frame(frame if self._gate_open else b"\x00" * len(frame))

    def _send_frame(self, payload: bytes) -> None:
        try:
            self._send({
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(payload).decode("ascii"),
            })
            self.frames_sent += 1
        except Exception:  # noqa: BLE001 — socket closed mid-reconnect: drop frame, keep streaming
            self.send_errors += 1

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def stop(self) -> None:
        self._stop.set()
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


class SpeakerPlayer:
    """Play response.output_audio.delta PCM with minimal buffering."""

    def __init__(self, *, sample_rate: int = SAMPLE_RATE, blocksize: int = BLOCKSIZE) -> None:
        self._sample_rate = sample_rate
        self._blocksize = blocksize
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._stream: Any = None
        # Frames actually consumed by the output callback since last clear —
        # the basis for conversation.item.truncate audio_end_ms on barge-in.
        self._played_samples = 0
        # Played-sample position where the current reply item's audio begins.
        # truncate needs ms *within that item*; a running total across replies
        # overshoots ("Audio content of 1850ms is already shorter than 8640ms").
        self._item_start_samples = 0
        # Samples pushed since clear, and the pushed-sample index where the last audible chunk
        # ends. Audible audio is still ahead while _played_samples < _audible_end.
        self._pushed_samples = 0
        self._audible_end = 0
        self._timing_marks: list[tuple[int, float, float, str]] = []
        self.timings: queue.SimpleQueue[dict[str, Any]] = queue.SimpleQueue()
        self._recent_levels: list[float] = []  # RMS of the last few played blocks (echo reference)

    def has_audio(self) -> bool:
        """Audible speech is queued (silence frames GPT-Live streams between replies do not count)."""
        with self._lock:
            return self._audible_end > self._played_samples

    def has_substantial_audio(self, *, min_bytes: int = 4800) -> bool:
        """~100 ms of audible PCM still queued — used for tail-only local mute."""
        with self._lock:
            return (self._audible_end - self._played_samples) * 2 >= min_bytes

    def playback_level(self) -> float:
        """Peak RMS of the last ~100 ms actually played (0 when silent): the echo reference."""
        with self._lock:
            level = max(self._recent_levels, default=0.0)
        return level if level >= AUDIBLE_RMS else 0.0

    def start(self) -> None:
        if sd is None:
            raise RuntimeError("pip install sounddevice numpy")
        self._stream = sd.OutputStream(
            samplerate=self._sample_rate,
            channels=1,
            dtype="int16",
            blocksize=self._blocksize,
            callback=self._output_callback,
        )
        self._stream.start()

    def _output_callback(self, outdata: Any, frames: int, _time: Any, _status: Any) -> None:
        need = frames * 2
        with self._lock:
            consumed = min(frames, len(self._buffer) // 2)
            while self._timing_marks and self._timing_marks[0][0] < self._played_samples + consumed:
                sample, speech_at, received_at, item_id = self._timing_marks.pop(0)
                dac_delay = max(0.0, float(getattr(_time, "outputBufferDacTime", 0)) - float(getattr(_time, "currentTime", 0)))
                played_at = time.monotonic() + dac_delay + (sample - self._played_samples) / self._sample_rate
                self.timings.put({"item_id": item_id, "speech_to_received_ms": round((received_at - speech_at) * 1000),
                                  "received_to_playback_ms": round((played_at - received_at) * 1000),
                                  "speech_to_playback_ms": round((played_at - speech_at) * 1000),
                                  "speech_end_source": "local_threshold", "playback_source": "scheduled_dac"})
            if len(self._buffer) >= need:
                chunk = self._buffer[:need]
                del self._buffer[:need]
                self._played_samples += frames
            else:
                chunk = bytes(self._buffer)
                self._buffer.clear()
                # Count only real samples; zero-padding is silence, not playback.
                self._played_samples += len(chunk) // 2
                chunk += b"\x00" * (need - len(chunk))
            self._recent_levels.append(pcm_rms(bytes(chunk)))
            del self._recent_levels[:-5]
        outdata[:] = np.frombuffer(chunk, dtype=np.int16).reshape(-1, 1)

    def played_ms(self) -> int:
        """Milliseconds of the current item's PCM actually played (24 kHz → /24)."""
        with self._lock:
            played = max(0, self._played_samples - self._item_start_samples)
            return played * 1000 // self._sample_rate

    def mark_item_start(self) -> None:
        """A new reply item begins after everything already queued plays out."""
        with self._lock:
            self._item_start_samples = self._played_samples + len(self._buffer) // 2

    def push_b64(self, audio_b64: str, *, speech_at: float | None = None, item_id: str = "") -> bool:
        if not audio_b64:
            return False
        received_at = time.monotonic()
        pcm = base64.b64decode(audio_b64)
        audible = pcm_rms(pcm[: len(pcm) // 2 * 2]) >= AUDIBLE_RMS
        with self._lock:
            if audible and speech_at is not None:
                self._timing_marks.append((self._pushed_samples, speech_at, received_at, item_id))
            self._buffer.extend(pcm)
            self._pushed_samples += len(pcm) // 2
            if audible:
                self._audible_end = self._pushed_samples

        return audible

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()
            self._played_samples = 0
            self._item_start_samples = 0
            self._pushed_samples = 0
            self._audible_end = 0
            self._recent_levels.clear()
            self._timing_marks.clear()

    def stop(self) -> None:
        self._stop.set()
        self.clear()
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


class RealtimeAudio:
    """Mic + speaker pair for a Realtime session."""

    def __init__(
        self,
        send: SendFn,
        *,
        meter: MeterFn | None = None,
        continuous: bool = False,
        on_barge_in: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._speaker = SpeakerPlayer()
        self._timed_speech_at: float | None = None
        mode = _local_mute_mode()
        if continuous and mode != "full":
            # GPT-Live is full duplex: muting the mic while she talks dropped the user's words
            # (and with continuous silence output it muted almost all the time).
            mode = "none"
        self.mute_mode = mode
        if mode == "none":
            mute_fn: Callable[[], bool] = lambda: False
        elif mode == "tail":
            mute_fn = self._speaker.has_substantial_audio
        else:
            mute_fn = self._speaker.has_audio
        self._mic = MicStreamer(
            send,
            mute_while=mute_fn,
            meter=meter,
            continuous=continuous,
            on_barge_in=on_barge_in,
            playback_level=self._speaker.playback_level,
        )
        self._started = False
        self._current_item_id = ""
        self._item_lock = threading.Lock()

    def start(self) -> None:
        if self._started:
            return
        self._speaker.start()
        self._mic.start()
        self._started = True

    def on_output_delta(self, audio_b64: str, *, item_id: str = "") -> None:
        if item_id:
            with self._item_lock:
                if item_id != self._current_item_id:
                    # Reset the played-audio counter per reply item (truncate is per item).
                    self._speaker.mark_item_start()
                self._current_item_id = item_id
        speech_at = self._mic.last_speech_at
        pending = speech_at if speech_at != self._timed_speech_at else None
        if self._speaker.push_b64(audio_b64, speech_at=pending, item_id=item_id) and pending is not None:
            self._timed_speech_at = pending

    def flush_timings(self) -> None:
        while True:
            try:
                row = self._speaker.timings.get_nowait()
            except queue.Empty:
                return
            _log_event("voice_latency", **row)

    def device_summary(self) -> dict[str, Any]:
        out = dict(self._mic.device_summary())
        dev = output_device_info()
        out["output"] = dev.get("name") or None
        out["output_rate"] = dev.get("rate") or None
        out["mute_mode"] = self.mute_mode
        return out

    def mic_stats(self) -> dict[str, Any]:
        return {"frames_sent": self._mic.frames_sent, "send_errors": self._mic.send_errors,
                "overflows": self._mic.overflows}

    @property
    def current_item_id(self) -> str:
        with self._item_lock:
            return self._current_item_id

    def interrupt(self) -> tuple[int, str]:
        """Stop playback; return (played_ms, current_item_id) for truncate."""
        audio_end_ms = self._speaker.played_ms()
        self._speaker.clear()
        return audio_end_ms, self.current_item_id

    def clear_playback(self) -> None:
        """Drop buffered output and reset played tracking (reconnect path)."""
        self._speaker.clear()

    def mic_alive(self) -> bool:
        return self._started and self._mic.sender_alive()

    def ensure_mic(self) -> bool:
        """Restart the mic sender thread if it died; True when restarted."""
        if not self._started:
            return False
        return self._mic.ensure_sender()

    def pause_input(self) -> None:
        self._mic.pause()

    def resume_input(self) -> None:
        self._mic.resume()

    def has_playback(self) -> bool:
        return self._speaker.has_audio()

    def stop(self) -> None:
        self._mic.stop()
        self._speaker.stop()
        self._started = False
