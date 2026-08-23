"""Low-latency PCM capture/playback for OpenAI Realtime GA (24 kHz mono int16).

Design: 20 ms frames (480 samples), callback I/O, dedicated mic sender thread,
minimal playback buffer. Local mute during playback depends on eagerness
(polite = full echo guard; eager = always listen for barge-in).
"""

from __future__ import annotations

import base64
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable

import numpy as np

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


def _voice_cua_app_bundle() -> Path | None:
    exe = Path(sys.executable).resolve()
    if exe.name != "voice-cua":
        return None
    info = exe.parent.parent / "Info.plist"
    if not info.is_file():
        return None
    return exe.parent.parent.parent


def _request_macos_mic_access() -> bool:
    """AVFoundation TCC prompt — required before PortAudio; LSUIElement apps may not prompt otherwise."""
    mic_bin = Path(sys.executable).resolve().parent / "mic-preflight"
    if mic_bin.is_file():
        try:
            proc = subprocess.run([str(mic_bin)], timeout=120, check=False)
            return proc.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
    if "--preflight-mic" in sys.argv:
        return True
    bundle = _voice_cua_app_bundle()
    if bundle is None:
        return True
    try:
        proc = subprocess.run(
            ["/usr/bin/open", "-W", "-n", str(bundle), "--args", "--preflight-mic"],
            timeout=120,
            check=False,
        )
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def audio_available() -> bool:
    return sd is not None


def mic_preflight() -> int:
    """Open/close mic once so macOS TCC runs when Samantha turns on, not mid-session."""
    if os.environ.get("VOICE_CUA_NO_AUDIO", "").strip().lower() in {"1", "true", "yes"}:
        return 0
    if sys.platform == "darwin" and not _request_macos_mic_access():
        print("mic preflight failed: microphone access not granted", file=sys.stderr, flush=True)
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
    ) -> None:
        self._send = send
        self._sample_rate = sample_rate
        self._blocksize = blocksize
        self._mute_while = mute_while or (lambda: False)
        self._q: queue.SimpleQueue[bytes] = queue.SimpleQueue()
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._stream: Any = None
        self._sender: threading.Thread | None = None

    def start(self) -> None:
        if sd is None:
            raise RuntimeError("pip install sounddevice numpy")
        self._sender = threading.Thread(target=self._sender_loop, name="mic-sender", daemon=True)
        self._sender.start()
        self._stream = sd.InputStream(
            samplerate=self._sample_rate,
            channels=1,
            dtype="int16",
            blocksize=self._blocksize,
            callback=self._input_callback,
        )
        self._stream.start()

    def _input_callback(self, indata: Any, _frames: int, _time: Any, _status: Any) -> None:
        if self._stop.is_set() or self._paused.is_set() or self._mute_while():
            return
        self._q.put(bytes(indata))

    def _sender_loop(self) -> None:
        from voice_cua.voice_meter import pcm_levels

        gate_open = False
        gate_open_rms, gate_close_rms = _mic_gate_thresholds()
        while not self._stop.is_set():
            try:
                chunk = self._q.get(timeout=0.05)
            except queue.Empty:
                gate_open = False
                continue
            if self._paused.is_set() or self._mute_while():
                gate_open = False
                continue

            rms, _bars = pcm_levels(chunk)
            if rms >= gate_open_rms:
                gate_open = True
            elif rms < gate_close_rms:
                gate_open = False

            payload = chunk if gate_open else b"\x00" * len(chunk)
            self._send({
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(payload).decode("ascii"),
            })

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

    def has_audio(self) -> bool:
        with self._lock:
            return len(self._buffer) > 0

    def has_substantial_audio(self, *, min_bytes: int = 4800) -> bool:
        """~100 ms of queued PCM — used for tail-only local mute."""
        with self._lock:
            return len(self._buffer) >= min_bytes

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
            if len(self._buffer) >= need:
                chunk = self._buffer[:need]
                del self._buffer[:need]
            else:
                chunk = bytes(self._buffer)
                self._buffer.clear()
                chunk += b"\x00" * (need - len(chunk))
        outdata[:] = np.frombuffer(chunk, dtype=np.int16).reshape(-1, 1)

    def push_b64(self, audio_b64: str) -> None:
        if not audio_b64:
            return
        with self._lock:
            self._buffer.extend(base64.b64decode(audio_b64))

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()

    def stop(self) -> None:
        self._stop.set()
        self.clear()
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


class RealtimeAudio:
    """Mic + speaker pair for a Realtime session."""

    def __init__(self, send: SendFn) -> None:
        self._speaker = SpeakerPlayer()
        mode = _local_mute_mode()
        if mode == "none":
            mute_fn: Callable[[], bool] = lambda: False
        elif mode == "tail":
            mute_fn = self._speaker.has_substantial_audio
        else:
            mute_fn = self._speaker.has_audio
        self._mic = MicStreamer(send, mute_while=mute_fn)
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._speaker.start()
        self._mic.start()
        self._started = True

    def on_output_delta(self, audio_b64: str) -> None:
        self._speaker.push_b64(audio_b64)

    def interrupt(self) -> None:
        self._speaker.clear()

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
