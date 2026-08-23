"""Samantha / Realtime preferences — ~/.config/voice-cua/settings.json (metadata only)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

SETTINGS_PATH = Path(
    os.environ.get("VOICE_CUA_SETTINGS", "~/.config/voice-cua/settings.json")
).expanduser()

MODELS: list[tuple[str, str]] = [
    ("gpt-realtime-2", "Intelligent (gpt-realtime-2)"),
    ("gpt-realtime-2.1-mini", "Balanced (gpt-realtime-2.1-mini)"),
]

VOICES: list[tuple[str, str]] = [
    ("alloy", "Alloy"),
    ("ash", "Ash"),
    ("ballad", "Ballad"),
    ("coral", "Coral"),
    ("echo", "Echo"),
    ("sage", "Sage"),
    ("shimmer", "Shimmer"),
    ("verse", "Verse"),
]

# polite = wait until agent finishes speaking (local echo guard)
# balanced = semantic VAD low + client noise gate (ignore background mumble)
# eager = semantic VAD high + always listen (barge-in)
EAGERNESS: list[tuple[str, str]] = [
    ("polite", "Polite — ignore background noise"),
    ("balanced", "Balanced — clear speech only"),
    ("eager", "Eager — barge in anytime"),
]

MIC_PROFILES: list[tuple[str, str]] = [
    ("near_field", "Close mic — headset / AirPods"),
    ("far_field", "Far mic — laptop / room"),
]

DEFAULTS: dict[str, str] = {
    "realtime_model": "gpt-realtime-2",
    "realtime_voice": "alloy",
    "eagerness": "balanced",
    "mic_profile": "near_field",
}


def load_settings() -> dict[str, str]:
    data = dict(DEFAULTS)
    if SETTINGS_PATH.is_file():
        try:
            raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for key in DEFAULTS:
                    val = raw.get(key)
                    if isinstance(val, str) and val.strip():
                        data[key] = val.strip()
        except (json.JSONDecodeError, OSError):
            pass
    return data


def save_settings(data: dict[str, str]) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in data.items() if k in DEFAULTS and isinstance(v, str)})
    SETTINGS_PATH.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(SETTINGS_PATH, 0o600)
    except OSError:
        pass


def apply_env(settings: dict[str, str] | None = None) -> dict[str, str]:
    """Push settings into os.environ for child processes."""
    cfg = settings or load_settings()
    os.environ["VOICE_CUA_REALTIME_MODEL"] = cfg["realtime_model"]
    os.environ["VOICE_CUA_REALTIME_VOICE"] = cfg["realtime_voice"]
    os.environ["VOICE_CUA_EAGERNESS"] = cfg["eagerness"]
    os.environ["VOICE_CUA_MIC_PROFILE"] = cfg["mic_profile"]
    return cfg


def noise_reduction_config(mic_profile: str | None = None) -> dict[str, str]:
    """OpenAI Realtime input noise reduction — filters audio before VAD."""
    raw = (
        mic_profile
        or os.environ.get("VOICE_CUA_MIC_PROFILE")
        or load_settings()["mic_profile"]
    ).lower()
    profile = raw if raw in {"near_field", "far_field"} else "near_field"
    return {"type": profile}


def turn_detection_config(eagerness: str | None = None) -> dict[str, Any]:
    level = (eagerness or load_settings()["eagerness"]).lower()
    if level == "polite":
        return {
            "type": "server_vad",
            "threshold": 0.62,
            "prefix_padding_ms": 250,
            "silence_duration_ms": 700,
            "create_response": True,
            "interrupt_response": False,
        }
    if level == "eager":
        return {
            "type": "semantic_vad",
            "eagerness": "high",
            "create_response": True,
            "interrupt_response": True,
        }
    # balanced (default) — low semantic eagerness resists background mumble
    return {
        "type": "semantic_vad",
        "eagerness": "low",
        "create_response": True,
        "interrupt_response": True,
    }


def mic_gate_thresholds(eagerness: str | None = None) -> tuple[float, float]:
    """Client RMS open/close thresholds (hysteresis) before streaming mic to Realtime."""
    level = (eagerness or load_settings()["eagerness"]).lower()
    if level == "eager":
        return 0.048, 0.034
    if level == "polite":
        return 0.075, 0.055
    return 0.062, 0.042


def local_mute_mode(eagerness: str | None = None) -> str:
    """How aggressively local mic is muted during playback (echo guard)."""
    level = (eagerness or load_settings()["eagerness"]).lower()
    if level == "polite":
        return "full"
    if level == "eager":
        return "none"
    return "tail"
