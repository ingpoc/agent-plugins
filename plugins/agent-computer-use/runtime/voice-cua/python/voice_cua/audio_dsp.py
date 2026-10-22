"""Pure mic DSP for Samantha: streaming resampler, adaptive noise floor, local barge-in.

No audio hardware here, so every piece is unit-testable. Levels are raw RMS of int16 PCM
normalised to 0-1 (full-scale sine is about 0.707).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

FRAME_MS = 20


def pcm_rms(pcm: bytes | np.ndarray) -> float:
    """Raw RMS (0-1) of int16 mono PCM."""
    samples = np.frombuffer(pcm, dtype=np.int16) if isinstance(pcm, (bytes, bytearray, memoryview)) else pcm
    if samples.size == 0:
        return 0.0
    x = samples.astype(np.float64)
    return float(np.sqrt(np.mean(x * x)) / 32768.0)


def _lowpass_taps(cutoff: float, taps: int = 33) -> np.ndarray:
    """Windowed-sinc low-pass; cutoff is a fraction of the input sample rate (0-0.5)."""
    n = np.arange(taps) - (taps - 1) / 2.0
    h = 2.0 * cutoff * np.sinc(2.0 * cutoff * n) * np.blackman(taps)
    return h / np.sum(h)


class StreamResampler:
    """Block-by-block int16 mono resampler with carried state, so block edges never click.

    Linear interpolation; when downsampling, a 33-tap anti-alias low-pass runs first.
    Output length tracks in_len * out_rate / in_rate exactly over the stream (no drift).
    """

    def __init__(self, in_rate: int, out_rate: int) -> None:
        if in_rate <= 0 or out_rate <= 0:
            raise ValueError("sample rates must be positive")
        self.in_rate = int(in_rate)
        self.out_rate = int(out_rate)
        self._step = self.in_rate / self.out_rate
        self._pos = 0.0               # next output position, in input samples, relative to _prev
        self._prev: float | None = None
        self._taps = _lowpass_taps(0.45 * self.out_rate / self.in_rate) if self.in_rate > self.out_rate else None
        self._fir_state = np.zeros(0 if self._taps is None else len(self._taps) - 1)

    @property
    def passthrough(self) -> bool:
        return self.in_rate == self.out_rate

    def process(self, pcm: bytes) -> bytes:
        samples = np.frombuffer(pcm, dtype=np.int16)
        if self.passthrough or samples.size == 0:
            return bytes(pcm[: samples.size * 2])
        x = samples.astype(np.float64)
        if self._taps is not None:
            joined = np.concatenate([self._fir_state, x])
            x = np.convolve(joined, self._taps, mode="valid")
            self._fir_state = joined[-(len(self._taps) - 1):]
        if self._prev is not None:
            x = np.concatenate([[self._prev], x])
        last = len(x) - 1
        if last < 0:
            return b""
        count = int(np.floor((last - self._pos) / self._step)) + 1 if self._pos <= last else 0
        if count > 0:
            t = self._pos + self._step * np.arange(count)
            out = np.interp(t, np.arange(len(x)), x)
            self._pos = t[-1] + self._step - last
        else:
            out = np.zeros(0)
            self._pos -= last
        self._prev = float(x[-1])
        return np.clip(np.round(out), -32768, 32767).astype(np.int16).tobytes()


class FrameChunker:
    """Re-cut a byte stream into fixed-size frames (the wire stays 20 ms @ 24 kHz)."""

    def __init__(self, frame_bytes: int) -> None:
        self.frame_bytes = frame_bytes
        self._buf = bytearray()

    def push(self, data: bytes) -> list[bytes]:
        self._buf.extend(data)
        out = []
        while len(self._buf) >= self.frame_bytes:
            out.append(bytes(self._buf[: self.frame_bytes]))
            del self._buf[: self.frame_bytes]
        return out


class NoiseFloor:
    """Adaptive mic noise floor: falls fast to quiet frames, rises slowly (about 2 s)."""

    def __init__(self, initial: float = 0.003, *, lo: float = 0.0003, hi: float = 0.05) -> None:
        self.level = initial
        self._lo, self._hi = lo, hi

    def update(self, rms: float, *, speech: bool) -> float:
        if rms < self.level:
            self.level += (rms - self.level) * 0.2
        elif not speech:
            self.level += (rms - self.level) * 0.01
        self.level = min(self._hi, max(self._lo, self.level))
        return self.level


@dataclass
class BargeInConfig:
    min_speech_ms: int = 320        # sustained speech before a local barge-in
    max_gap_frames: int = 3         # tolerate 60 ms dips inside a word
    release_ms: int = 600           # quiet needed before a new utterance can trigger again
    floor_ratio: float = 3.16       # +10 dB over the noise floor
    abs_min_rms: float = 0.008      # about -42 dBFS: never trigger on very soft noise
    echo_margin: float = 2.0        # mic must be 6 dB over the expected echo of playback
    echo_gain_initial: float = 0.5  # conservative until the coupling is measured


@dataclass
class BargeInDetector:
    """Local barge-in for full-duplex GPT-Live: only sustained speech above the adaptive floor,
    and above the expected echo of Samantha's own playback, counts. One trigger per utterance."""

    config: BargeInConfig = field(default_factory=BargeInConfig)
    floor: NoiseFloor = field(default_factory=NoiseFloor)
    echo_gain: float = -1.0
    speech_ms: int = 0
    _gap: int = 0
    _quiet_ms: int = 0
    _latched: bool = False
    last_threshold: float = 0.0

    def __post_init__(self) -> None:
        if self.echo_gain < 0:
            self.echo_gain = self.config.echo_gain_initial

    def threshold(self, playback_rms: float) -> float:
        cfg = self.config
        base = max(self.floor.level * cfg.floor_ratio, cfg.abs_min_rms)
        if playback_rms > 0:
            base = max(base, self.echo_gain * playback_rms * cfg.echo_margin)
        return base

    def is_speech(self, rms: float, playback_rms: float = 0.0) -> bool:
        return rms >= self.threshold(playback_rms)

    def feed(self, rms: float, playback_rms: float = 0.0) -> dict[str, Any] | None:
        """One 20 ms frame. Returns trigger info once per sustained utterance, else None."""
        cfg = self.config
        thr = self.threshold(playback_rms)
        self.last_threshold = thr
        speech = rms >= thr
        if playback_rms > 0:
            ratio = min(1.0, rms / playback_rms)
            rate = 0.02 if ratio > self.echo_gain else 0.05
            self.echo_gain = min(1.0, max(0.01, self.echo_gain + (ratio - self.echo_gain) * rate))
        self.floor.update(rms, speech=speech)
        if speech:
            self.speech_ms += FRAME_MS
            self._gap = 0
            self._quiet_ms = 0
        else:
            self._gap += 1
            self._quiet_ms += FRAME_MS
            if self._gap > cfg.max_gap_frames:
                self.speech_ms = 0
            if self._quiet_ms >= cfg.release_ms:
                self._latched = False
        if speech and not self._latched and self.speech_ms >= cfg.min_speech_ms:
            self._latched = True
            return {
                "speech_ms": self.speech_ms,
                "rms": round(rms, 4),
                "floor": round(self.floor.level, 4),
                "threshold": round(thr, 4),
                "playback_rms": round(playback_rms, 4),
                "echo_gain": round(self.echo_gain, 3),
            }
        return None

    @property
    def in_speech(self) -> bool:
        return self.speech_ms > 0
