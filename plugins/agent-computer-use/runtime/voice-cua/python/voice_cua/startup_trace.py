"""Samantha voice stack startup telemetry — one timeline in activity jsonl + voice.log."""

from __future__ import annotations

import os
import sys
import time
import uuid
from typing import Any

from voice_cua.activity_log import log_event, tail_events

_timer: "StartupTimer | None" = None


def log_startup(phase: str, *, status: str = "ok", **fields: Any) -> None:
    """Append structured startup event and mirror a grep-friendly line to stderr (→ voice.log)."""
    payload = {"phase": phase, "status": status, "source": "voice_stack", **fields}
    log_event("startup", **payload)
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
        os.environ["VOICE_CUA_STARTUP_ID"] = self.session_id

    def mark(self, phase: str, *, status: str = "ok", **fields: Any) -> None:
        now = time.monotonic()
        log_startup(
            phase,
            status=status,
            session_id=self.session_id,
            elapsed_ms=int((now - self._t0) * 1000),
            step_ms=int((now - self._last) * 1000),
            **fields,
        )
        self._last = now


def begin() -> StartupTimer:
    global _timer
    _timer = StartupTimer()
    _timer.mark("stack_begin")
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
