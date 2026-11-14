"""Island / notch session state bus — metadata only, never secrets."""

from __future__ import annotations

import threading
import time
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Literal

IslandKind = Literal[
    "idle",
    "listening",
    "thinking",
    "speaking",
    "acting",
    "driving",
    "confirm",
    "secrets",
    "done",
    "reconnecting",
    "error",
]


@dataclass
class IslandState:
    kind: IslandKind = "idle"
    title: str = ""
    detail: str = ""
    app: str = ""
    step: str = ""
    confirm_id: str = ""
    confirm_prompt: str = ""
    active_apps: list[str] = field(default_factory=list)  # oldest → newest
    expanded: bool = False
    step_index: int = 0
    step_total: int = 0
    agents: list[dict] = field(default_factory=list)  # other agents (multi-agent circle)
    agent_approval: dict | None = None  # takes over the pill until answered
    voice_side: str = "idle"  # idle | user | assistant
    voice_level: float = 0.0
    voice_levels: list[float] = field(default_factory=lambda: [0.0] * 8)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class IslandBus:
    """Thread-safe publisher for notch UI and gateway SSE."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state = IslandState()
        self._listeners: list[Callable[[IslandState], None]] = []
        self._confirms: dict[str, threading.Event] = {}
        self._confirm_results: dict[str, bool] = {}
        self._confirm_turn_ids: dict[str, str] = {}
        self._seen_confirm_ids: set[str] = set()
        self._agent_decisions: dict[str, bool] = {}

    @property
    def state(self) -> IslandState:
        with self._lock:
            return IslandState(**asdict(self._state))

    def subscribe(self, fn: Callable[[IslandState], None]) -> None:
        with self._lock:
            self._listeners.append(fn)

    def unsubscribe(self, fn: Callable[[IslandState], None]) -> None:
        with self._lock:
            if fn in self._listeners:
                self._listeners.remove(fn)

    def listener_count(self) -> int:
        with self._lock:
            return len(self._listeners)

    def publish(
        self,
        kind: IslandKind,
        *,
        title: str = "",
        detail: str = "",
        app: str = "",
        step: str = "",
        confirm_id: str = "",
        confirm_prompt: str = "",
        active_apps: list[str] | None = None,
        expanded: bool = False,
        step_index: int = 0,
        step_total: int = 0,
        agents: list[dict] | None = None,
        agent_approval: dict | None = None,
    ) -> IslandState | None:
        # Copy contract (see island_copy): compact title ≤ 28, overflow → detail,
        # errors → human copy with the raw text logged only; benign errors dropped.
        from voice_cua.island_copy import compact, human_error

        if kind == "error":
            raw = " · ".join(x for x in (title, detail) if x)
            copy = human_error(raw, target=app)
            try:
                from voice_cua.activity_log import log_event

                log_event("island_error_raw", raw=raw[:300], shown=copy or "")
            except Exception:  # noqa: BLE001
                pass
            if copy is None:
                return None
            title, detail = copy, ""
        else:
            title, detail = compact(title, detail)
        with self._lock:
            prev_apps = list(self._state.active_apps)
            apps = list(active_apps) if active_apps is not None else prev_apps
            if kind == "idle":
                apps = []
            self._state = IslandState(
                kind=kind,
                title=title,
                detail=detail,
                app=app,
                step=step,
                confirm_id=confirm_id,
                confirm_prompt=confirm_prompt,
                active_apps=apps,
                expanded=bool(expanded),
                step_index=int(step_index or 0),
                step_total=int(step_total or 0),
                agents=list(agents) if agents is not None else list(self._state.agents),
                agent_approval=(agent_approval or None) if agent_approval is not None else self._state.agent_approval,
                voice_side=self._state.voice_side,
                voice_level=self._state.voice_level,
                voice_levels=list(self._state.voice_levels),
                updated_at=time.time(),
            )
            snap = IslandState(**asdict(self._state))
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(snap)
            except Exception:
                pass
        return snap

    def set_voice_meter(
        self,
        side: str,
        level: float,
        levels: list[float] | None = None,
    ) -> IslandState:
        """High-frequency waveform updates for SSE (metadata only)."""
        with self._lock:
            bars = list(levels) if levels is not None else list(self._state.voice_levels)
            if len(bars) < 8:
                bars = bars + [0.0] * (8 - len(bars))
            elif len(bars) > 8:
                bars = bars[:8]
            self._state.voice_side = side
            self._state.voice_level = float(level)
            self._state.voice_levels = bars
            self._state.updated_at = time.time()
            snap = IslandState(**asdict(self._state))
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(snap)
            except Exception:
                pass
        return snap

    def pending_confirm(self, turn_id: str | None = None) -> dict[str, str] | None:
        with self._lock:
            confirm_id = self._state.confirm_id
            if not confirm_id or confirm_id not in self._confirms:
                return None
            origin_turn_id = self._confirm_turn_ids.get(confirm_id, "")
            if turn_id is not None and origin_turn_id != turn_id:
                return None
            return {"confirm_id": confirm_id, "prompt": self._state.confirm_prompt}

    def request_confirm(self, confirm_id: str, prompt: str, timeout: float = 120.0,
                        *, origin_turn_id: str | None = None) -> str:
        """Block for an explicit answer and distinguish denial, timeout, and transport failure."""
        if (not isinstance(confirm_id, str) or not confirm_id or isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))):
            return "error"
        try:
            timeout = float(timeout)
        except (TypeError, ValueError, OverflowError):
            return "error"
        if not math.isfinite(timeout) or timeout <= 0:
            return "error"
        if origin_turn_id is not None and (not isinstance(origin_turn_id, str) or not origin_turn_id):
            return "error"
        event = threading.Event()
        with self._lock:
            if confirm_id in self._seen_confirm_ids:
                return "error"
            self._seen_confirm_ids.add(confirm_id)
            self._confirms[confirm_id] = event
            if origin_turn_id:
                self._confirm_turn_ids[confirm_id] = origin_turn_id
        self.publish(
            "confirm",
            title="Confirm",
            detail=prompt,
            confirm_id=confirm_id,
            confirm_prompt=prompt,
        )
        answered = event.wait(timeout=timeout)
        with self._lock:
            # Resolve at the deadline wins if the response acquired the lock first.
            answered = answered or confirm_id in self._confirm_results
            result = self._confirm_results.pop(confirm_id, False) if answered else False
            self._confirms.pop(confirm_id, None)
            self._confirm_turn_ids.pop(confirm_id, None)
            timed_out = not answered
            stale = timed_out and getattr(self._state, "confirm_id", None) == confirm_id
        if stale:
            # Timed out unanswered: take the card down (default deny) instead of leaving Allow/Deny up.
            self.publish("done", title="Not confirmed", detail="No answer, so nothing was done")
        if timed_out:
            return "timed_out"
        return "approved" if result else "denied"

    def resolve_confirm(self, confirm_id: str, approved: bool, *, typed_authorized: bool = False) -> bool:
        if not isinstance(confirm_id, str) or type(approved) is not bool:
            return False
        with self._lock:
            event = self._confirms.get(confirm_id)
            if event is None or confirm_id in self._confirm_results:
                return False
            if self._confirm_turn_ids.get(confirm_id) and not typed_authorized:
                return False
            self._confirm_results[confirm_id] = approved
            self._confirms.pop(confirm_id, None)
            event.set()
            return True


    def resolve_agent_approval(self, approval_id: str, approved: bool) -> bool:
        """Stub resolver for agent approvals (no agent API yet): clear + record."""
        with self._lock:
            current = self._state.agent_approval or {}
            if not current or (approval_id and str(current.get("id") or "") != approval_id):
                return False
            self._state.agent_approval = None
            self._agent_decisions[str(current.get("id") or "")] = bool(approved)
        self.publish(self._state.kind, title=self._state.title, detail=self._state.detail,
                     app=self._state.app, active_apps=list(self._state.active_apps))
        return True


ISLAND = IslandBus()

