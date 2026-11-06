"""Samantha voice stack startup telemetry — one timeline in activity jsonl + voice.log."""

from __future__ import annotations

import os
import sys
import time
import uuid
from typing import Any

from voice_cua.activity_log import log_event, tail_events

_timer: "StartupTimer | None" = None

# The session is live once audio is ready (voice) or realtime is ready (text-only). Phases after that
# are mid-session (kind=session_phase), not startup (0.2.85: response_done was logged as startup).
LIVE_PHASES = frozenset({"audio_ready", "realtime_ready"})


def log_startup(phase: str, *, status: str = "ok", kind: str = "startup", **fields: Any) -> None:
    """Append structured startup event and mirror a grep-friendly line to stderr (→ voice.log)."""
    payload = {"phase": phase, "status": status, "source": "voice_stack", **fields}
    log_event(kind, **payload)
    bits = [f"[startup] phase={phase} status={status}"]
    for key in ("session_id", "elapsed_ms", "step_ms", "detail", "error"):
        val = fields.get(key)
        if val is not None and val != "":
            bits.append(f"{key}={val}")
    print(" ".join(bits), file=sys.stderr, flush=True)


class StartupTimer:
    def __init__(self) -> None:
        self.session_id = uuid.uuid4().hex[:8]
        self._t0 = time.monotonic()
        self._last = self._t0
        self.live = False
        self._mic_failure_seen = False
        os.environ["VOICE_CUA_STARTUP_ID"] = self.session_id

    def mark(self, phase: str, *, status: str = "ok", **fields: Any) -> None:
        now = time.monotonic()
        if phase in {"mic_preflight", "audio_ready"}:
            try:
                from voice_cua.audio_io import input_device_snapshot

                fields.update(input_device_snapshot())
            except Exception:  # noqa: BLE001 — diagnostics must not break startup
                fields["input_availability"] = "unknown"
            if status in {"fail", "skip"}:
                self._mic_failure_seen = True
            fields["input_recovery"] = (
                "recovered" if phase == "audio_ready" and status == "ok" and self._mic_failure_seen
                else "not_observed"
            )
            if phase == "audio_ready" and status == "ok":
                self._mic_failure_seen = False
        # Reconnects re-run startup from the socket open; everything else after go-live is mid-session.
        if phase in {"realtime_ws_open", "stack_begin"}:
            self.live = False
        kind = "session_phase" if self.live else "startup"
        if phase in LIVE_PHASES and status == "ok":
            self.live = True
        log_startup(
            phase,
            status=status,
            kind=kind,
            session_id=self.session_id,
            elapsed_ms=int((now - self._t0) * 1000),
            step_ms=int((now - self._last) * 1000),
            **fields,
        )
        self._last = now


def _acu_version() -> str:
    try:
        from voice_cua import __version__

        return str(__version__)
    except Exception:  # noqa: BLE001 — telemetry must never stop startup
        return "unknown"


def begin() -> StartupTimer:
    global _timer
    _timer = StartupTimer()
    # Version on the first row of every session, so logs split by ACU release (historic vs current).
    _timer.mark("stack_begin", version=_acu_version())
    return _timer


def mark(phase: str, *, status: str = "ok", **fields: Any) -> None:
    global _timer
    if _timer is None:
        begin()
    assert _timer is not None
    _timer.mark(phase, status=status, **fields)


def session_id() -> str:
    if _timer is not None:
        return _timer.session_id
    return os.environ.get("VOICE_CUA_STARTUP_ID", "")


def tail_startup(*, limit: int = 80) -> list[dict[str, Any]]:
    rows = tail_events(limit=min(limit * 4, 500))
    out = [row for row in rows if row.get("kind") == "startup"]
    return out[-limit:]
