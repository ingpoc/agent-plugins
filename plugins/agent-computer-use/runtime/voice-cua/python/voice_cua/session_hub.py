"""Active Realtime session registry — inject text while CUAService island streams."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from voice_cua.realtime_session import RealtimeSession

_lock = threading.Lock()
_active: RealtimeSession | None = None
_credential_lock = threading.Lock()


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


WAKE_WAIT_S = 12.0


def send_text_and_wait(text: str, *, timeout: float = 90.0) -> dict[str, Any]:
    with _lock:
        sess = _active
    if sess is None or sess._stop.is_set():
        return {"ok": False, "error": "no active realtime session — turn Samantha on in menu bar"}
    if not sess._session_ready.is_set():
        # A resting GPT-Live session (idle close / expiry) wakes for a typed turn; a reconnect
        # in flight gets a short grace instead of an immediate "not ready".
        dormant = getattr(sess, "_dormant", None)
        if dormant is not None and dormant.is_set():
            sess._wake.set()
        if not sess._session_ready.wait(timeout=WAKE_WAIT_S):
            return {"ok": False, "error": "realtime session not ready yet"}
    return sess.send_text_and_wait(text, timeout=timeout)


def island_control(action: str) -> bool:
    """Run an island card control on the live session (False = no session / not supported)."""
    with _lock:
        sess = _active
    if sess is None or sess._stop.is_set() or not sess._session_ready.is_set():
        return False
    try:
        return bool(sess.island_control(action))
    except Exception:  # noqa: BLE001 — a button must never crash the gateway
        return False


@contextmanager
def credential_privacy():
    """One secure-entry owner mutes capture until that transaction alone finishes."""
    if not _credential_lock.acquire(blocking=False):
        raise RuntimeError("credential_entry_busy")
    try:
        with _lock:
            session = _active
        if session is None:
            yield
            return
        previously_muted = getattr(session, "_input_muted", False)
        session._credential_capture = True
        session.provider._credential_capture = True
        try:
            session.set_input_muted(True)
            yield
        finally:
            session._credential_capture = False
            session.provider._credential_capture = False
            if not session._stop.is_set():
                session.set_input_muted(previously_muted)
    finally:
        _credential_lock.release()
