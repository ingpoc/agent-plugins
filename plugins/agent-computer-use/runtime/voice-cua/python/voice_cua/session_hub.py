"""Active LiveSession registry — inject text while CUAService island streams."""

from __future__ import annotations

import threading
import math
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from voice_cua.live_session import LiveSession

_lock = threading.Lock()
_active: LiveSession | None = None
_credential_lock = threading.Lock()
_turn_lock = threading.Lock()
_turns: OrderedDict[str, dict[str, Any]] = OrderedDict()
TURN_WAIT_MAX_S = 1800.0
TURN_LIFETIME_S = 1800.0
TURN_HISTORY = 20


def register(session: LiveSession) -> None:
    global _active
    with _lock:
        _active = session


def unregister(session: LiveSession) -> None:
    global _active
    with _lock:
        if _active is session:
            _active = None
    _fail_turns("session closed", session=session)


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


def _evict_turns_locked() -> None:
    while len(_turns) > TURN_HISTORY:
        old = next((key for key, turn in _turns.items() if turn["status"] != "running"), None)
        if old is None:
            break
        _turns.pop(old, None)


def _settle_turn(turn_id: str, result: dict[str, Any], *, session: LiveSession | None = None) -> None:
    with _turn_lock:
        turn = _turns.get(turn_id)
        if turn is None or turn["status"] != "running":
            return
        if session is not None and turn.get("_session") is not session:
            return
        turn["reply"] = str(result.get("reply") or "")
        turn["error"] = str(result.get("error") or "") or None
        turn["ok"] = result.get("ok") is True
        turn["status"] = "done" if turn["ok"] else "error"
        turn["elapsed_ms"] = int((time.monotonic() - turn["started_at"]) * 1000)
        turn["event"].set()
        _evict_turns_locked()


def _fail_turns(error: str, *, session: LiveSession | None = None) -> None:
    with _turn_lock:
        for turn in _turns.values():
            if turn["status"] == "running" and (session is None or turn.get("_session") is session):
                turn.update(ok=False, status="error", error=error,
                            elapsed_ms=int((time.monotonic() - turn["started_at"]) * 1000))
                turn["cancel_event"].set()
                turn["event"].set()
        _evict_turns_locked()


def fail_session_turns(session: LiveSession, error: str) -> None:
    _fail_turns(error, session=session)


def shutdown_turns() -> None:
    _fail_turns("gateway shutting down")


def _turn_payload(turn_id: str, turn: dict[str, Any], pending: dict[str, str] | None) -> dict[str, Any]:
    elapsed = turn.get("elapsed_ms")
    if elapsed is None:
        elapsed = int((time.monotonic() - turn["started_at"]) * 1000)
    return {
        "turn_id": turn_id,
        "status": turn["status"],
        "reply": turn.get("reply", ""),
        "error": turn.get("error"),
        "elapsed_ms": elapsed,
        "pending_confirm": pending,
        "ok": turn.get("ok") is True,
    }


def _pending_confirm() -> dict[str, str] | None:
    from voice_cua.island_state import ISLAND

    return ISLAND.pending_confirm()


def get_turn(turn_id: str) -> dict[str, Any] | None:
    pending = _pending_confirm()
    with _turn_lock:
        turn = _turns.get(turn_id)
        if turn is None:
            return None
        payload = _turn_payload(turn_id, turn, pending)
    payload.pop("ok", None)
    return payload


def _run_turn(turn_id: str, text: str, session: LiveSession | None,
              cancel_event: threading.Event) -> None:
    if cancel_event.is_set():
        return
    if session is None or session._stop.is_set():
        _settle_turn(turn_id, {"ok": False, "error": "no active live session — turn Samantha on in menu bar"})
        return
    try:
        if getattr(session, "_spoken_task_cancel", lambda _text: False)(text):
            result = session.send_text_and_wait(text, timeout=TURN_LIFETIME_S, cancel_event=cancel_event)
        else:
            if not session._session_ready.is_set():
                # A resting session wakes for text; a reconnect gets a short grace period.
                dormant = getattr(session, "_dormant", None)
                if dormant is not None and dormant.is_set():
                    session._wake.set()
                ready_deadline = time.monotonic() + WAKE_WAIT_S
                while not session._session_ready.is_set() and not cancel_event.is_set():
                    remaining = ready_deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    cancel_event.wait(min(0.1, remaining))
                if cancel_event.is_set():
                    return
                if not session._session_ready.is_set():
                    result = {"ok": False, "error": "live session still starting or reconnecting — try again shortly",
                              "error_type": "session_not_ready"}
                    _settle_turn(turn_id, result, session=session)
                    return
            if session._stop.is_set():
                result = {"ok": False, "error": "session closed"}
            else:
                result = session.send_text_and_wait(text, timeout=TURN_LIFETIME_S, cancel_event=cancel_event)
        _settle_turn(turn_id, result, session=session)
    except Exception as exc:  # noqa: BLE001 — a turn error must settle its poll record
        _settle_turn(turn_id, {"ok": False, "error": str(exc)}, session=session)


def start_turn(text: str, *, timeout: float = 90.0, asynchronous: bool = False) -> dict[str, Any]:
    if isinstance(timeout, bool):
        raise ValueError("timeout must be finite and greater than zero")
    try:
        timeout = float(timeout)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("timeout must be finite and greater than zero") from None
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and greater than zero")
    wait_timeout = min(timeout, TURN_WAIT_MAX_S)
    with _lock:
        session = _active
    turn_id = uuid.uuid4().hex
    turn = {"status": "running", "ok": False, "reply": "", "error": None,
            "started_at": time.monotonic(), "elapsed_ms": None, "event": threading.Event(),
            "cancel_event": threading.Event(), "_session": session}
    with _turn_lock:
        _turns[turn_id] = turn
        _evict_turns_locked()
    try:
        thread = threading.Thread(target=_run_turn, args=(turn_id, text, session, turn["cancel_event"]),
                                  name=f"typed-turn-{turn_id[:8]}", daemon=True)
        thread.start()
    except Exception as exc:  # noqa: BLE001
        _settle_turn(turn_id, {"ok": False, "error": str(exc)}, session=session)
    if asynchronous:
        return {"ok": True, "turn_id": turn_id, "status": "running"}
    if not turn["event"].wait(timeout=wait_timeout):
        return {"ok": False, "error": "still_running", "turn_id": turn_id, "status": "running"}
    with _turn_lock:
        return _turn_payload(turn_id, turn, None)


def send_text_and_wait(text: str, *, timeout: float = 90.0) -> dict[str, Any]:
    return start_turn(text, timeout=timeout)


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
