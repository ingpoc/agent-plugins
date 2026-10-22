"""Server-side confirmation gate for risky actions (fails closed).

Invariant: a risky action runs only if the user approved THAT exact action
(fingerprint of tool + arguments) on the island moments before. The model's
own judgement never unlocks it:

1. A risky tool call with no fresh approval is refused, with a hint to call
   ``confirm_risky``; the action is parked as *pending*.
2. ``confirm_risky`` shows the user a server-written description of the
   pending action (not the model's prompt) and records an approval bound to
   that fingerprint for ``APPROVAL_TTL`` seconds.
3. The retried call consumes the approval (single use) right before it runs.

Any error inside the gate refuses. State is per process and cleared on
reconnect: the voice helper runs exactly one Realtime session per process, so
"per process" is "per conversation".
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from typing import Any, Callable

APPROVAL_TTL = 45.0  # approval → execution window ("immediately before")
PENDING_TTL = 120.0  # how long a refused action waits for confirm_risky
# Arguments that do not change what the action does (labels and capture only). Anything that
# changes dispatch or verification (expect, allow_unverified, wait…) is part of the approval.
_VOLATILE_KEYS = frozenset({"step_label", "risky"})

CONFIRM_HINT = "Needs the user's OK: call confirm_risky, then retry this exact call."


def fingerprint(tool: str, args: dict[str, Any] | None, *, ignore: frozenset[str] = frozenset()) -> str:
    drop = _VOLATILE_KEYS | ignore
    clean = {k: v for k, v in (args or {}).items() if k not in drop}
    blob = json.dumps([tool, clean], sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class RiskGate:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._lock = threading.Lock()
        self._confirm_lock = threading.Lock()  # one island prompt at a time
        self._clock = clock
        self._pending: dict[str, tuple[str, float]] = {}  # fp → (description, created)
        self._approved: dict[str, float] = {}  # fp → expires
        self._last_pending = ""

    def reset(self) -> None:
        with self._lock:
            self._pending.clear()
            self._approved.clear()
            self._last_pending = ""

    def _prune_locked(self, now: float) -> None:
        self._pending = {fp: v for fp, v in self._pending.items() if now - v[1] <= PENDING_TTL}
        self._approved = {fp: exp for fp, exp in self._approved.items() if exp >= now}
        if self._last_pending not in self._pending:
            self._last_pending = next(reversed(self._pending), "") if self._pending else ""

    def check(self, tool: str, args: dict[str, Any] | None, description: str) -> dict[str, Any] | None:
        """None = approved (approval consumed). Otherwise a refusal result for the model."""
        try:
            fp = fingerprint(tool, args)
            now = self._clock()
            with self._lock:
                self._prune_locked(now)
                if self._approved.pop(fp, None) is not None:
                    self._pending.pop(fp, None)
                    return None
                self._pending.pop(fp, None)
                self._pending[fp] = (str(description or "this action")[:160], now)
                self._last_pending = fp
        except Exception:  # noqa: BLE001 — fail closed
            pass
        return {
            "ok": False,
            "error": "confirmation required",
            "error_type": "needs_confirmation",
            "needs_confirmation": True,
            "retryable": True,
            "hint": CONFIRM_HINT,
        }

    def confirm(self, ask: Callable[[str, str], bool]) -> dict[str, Any]:
        """Ask the user about the newest pending action; bind approval to it."""
        with self._confirm_lock:
            return self._confirm(ask)

    def _confirm(self, ask: Callable[[str, str], bool]) -> dict[str, Any]:
        with self._lock:
            self._prune_locked(self._clock())
            fp = self._last_pending
            pending = self._pending.get(fp) if fp else None
        if not pending:
            return {
                "ok": False,
                "approved": False,
                "error": "nothing to confirm",
                "hint": "Call the risky action itself first; it asks for confirmation when needed.",
            }
        description = pending[0]
        try:
            approved = bool(ask(str(uuid.uuid4()), description))
        except Exception:  # noqa: BLE001 — fail closed
            approved = False
        with self._lock:
            self._pending.pop(fp, None)
            if self._last_pending == fp:
                self._last_pending = next(reversed(self._pending), "") if self._pending else ""
            if approved:
                self._approved[fp] = self._clock() + APPROVAL_TTL
        if approved:
            return {"ok": True, "approved": True, "hint": "Approved: retry the same call now."}
        return {"ok": True, "approved": False, "hint": "User said no: do not retry; tell the user."}


GATE = RiskGate()
