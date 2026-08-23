"""Active Realtime session registry — inject text while CUAService island streams."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from voice_cua.realtime_session import RealtimeSession

_lock = threading.Lock()
_active: RealtimeSession | None = None


def register(session: RealtimeSession) -> None:
    global _active
    with _lock:
        _active = session


def unregister(session: RealtimeSession) -> None:
    global _active
    with _lock:
        if _active is session:
            _active = None


def status() -> dict[str, Any]:
    with _lock:
        sess = _active
    if sess is None:
        return {"ok": True, "active": False, "ready": False}
    return {
        "ok": True,
        "active": True,
        "ready": sess._session_ready.is_set() and not sess._stop.is_set(),
        "text_only": sess._text_only,
        "audio": sess._audio_enabled,
    }


def send_text_and_wait(text: str, *, timeout: float = 90.0) -> dict[str, Any]:
    with _lock:
        sess = _active
    if sess is None or sess._stop.is_set():
        return {"ok": False, "error": "no active realtime session — turn Samantha on in menu bar"}
    if not sess._session_ready.is_set():
        return {"ok": False, "error": "realtime session not ready yet"}
    return sess.send_text_and_wait(text, timeout=timeout)
