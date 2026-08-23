"""PCM → RMS + bar levels for live island waveform."""

from __future__ import annotations

import struct

BAR_COUNT = 8


def flat_levels(*, bars: int = BAR_COUNT) -> list[float]:
    return [0.0] * bars


def pcm_levels(pcm: bytes, *, bars: int = BAR_COUNT) -> tuple[float, list[float]]:
    """Return normalized RMS (0–1) and per-bar levels from int16 mono PCM."""
    if len(pcm) < 2:
        return 0.0, flat_levels(bars=bars)
    count = len(pcm) // 2
    samples = struct.unpack(f"<{count}h", pcm[: count * 2])
    if not samples:
        return 0.0, flat_levels(bars=bars)
    peak = max(abs(s) for s in samples) or 1
    rms = (sum(s * s for s in samples) / len(samples)) ** 0.5 / 32768.0
    rms = min(1.0, rms * 2.4)
    chunk = max(1, len(samples) // bars)
    levels: list[float] = []
    for i in range(bars):
        seg = samples[i * chunk : (i + 1) * chunk]
        if not seg:
            levels.append(0.0)
            continue
        seg_rms = (sum(s * s for s in seg) / len(seg)) ** 0.5 / 32768.0
        levels.append(min(1.0, seg_rms * 2.8))
    # Normalize bars relative to peak so quiet speech still moves.
    max_bar = max(levels) or 0.0
    if max_bar > 0.04:
        scale = min(1.0, peak / 32768.0 * 3.0) / max_bar
        levels = [min(1.0, v * scale) for v in levels]
    return rms, levels
