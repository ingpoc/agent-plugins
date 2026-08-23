"""Island publish/confirm — local bus in gateway, HTTP remote in realtime."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from voice_cua.island_state import ISLAND, IslandKind

DEFAULT_GATEWAY = "http://127.0.0.1:8765"
_ACTIVE_APPS: set[str] = set()


def use_remote() -> bool:
    return os.environ.get("VOICE_CUA_REMOTE_ISLAND", "").strip().lower() in {"1", "true", "yes"}


def _gateway_url() -> str:
    return os.environ.get("VOICE_CUA_GATEWAY", DEFAULT_GATEWAY).rstrip("/")


def _post(path: str, body: dict, *, timeout: float) -> dict | None:
    req = urllib.request.Request(
        f"{_gateway_url()}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None


def island_voice_meter(
    side: str,
    level: float,
    levels: list[float] | None = None,
    *,
    min_interval: float = 0.045,
) -> None:
    """Push live waveform bars to island SSE (throttled)."""
    import time

    key = "_meter_ts"
    now = time.monotonic()
    last = getattr(island_voice_meter, key, 0.0)
    if now - last < min_interval and side == getattr(island_voice_meter, "_meter_side", ""):
        return
    setattr(island_voice_meter, key, now)
    setattr(island_voice_meter, "_meter_side", side)
    if use_remote():
        _post(
            "/api/island/voice",
            {"voice_side": side, "voice_level": level, "voice_levels": levels or []},
            timeout=1.5,
        )
        return
    ISLAND.set_voice_meter(side, level, levels)


def island_publish(
    kind: IslandKind,
    *,
    title: str = "",
    detail: str = "",
    app: str = "",
    step: str = "",
    confirm_id: str = "",
    confirm_prompt: str = "",
    active_apps: list[str] | None = None,
) -> None:
    global _ACTIVE_APPS
    if kind == "idle":
        _ACTIVE_APPS.clear()
    elif app.strip():
        _ACTIVE_APPS.add(app.strip())
    apps = active_apps if active_apps is not None else sorted(_ACTIVE_APPS)
    body = {
        "kind": kind,
        "title": title,
        "detail": detail,
        "app": app,
        "step": step,
        "confirm_id": confirm_id,
        "confirm_prompt": confirm_prompt,
        "active_apps": apps,
    }
    if use_remote():
        _post("/api/island/publish", body, timeout=3)
        return
    ISLAND.publish(
        kind,
        title=title,
        detail=detail,
        app=app,
        step=step,
        confirm_id=confirm_id,
        confirm_prompt=confirm_prompt,
        active_apps=apps,
    )


def island_confirm(confirm_id: str, prompt: str, *, timeout: float = 120.0) -> bool:
    if use_remote():
        data = _post(
            "/api/confirm/request",
            {"confirm_id": confirm_id, "prompt": prompt, "timeout": timeout},
            timeout=timeout + 5,
        )
        return bool(data and data.get("approved"))
    return ISLAND.request_confirm(confirm_id, prompt, timeout=timeout)
