"""Island / notch session state bus — metadata only, never secrets."""

from __future__ import annotations

import threading
import time
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

    def request_confirm(self, confirm_id: str, prompt: str, timeout: float = 120.0) -> bool:
        """Block until island/voice confirms. Default deny on timeout."""
        event = threading.Event()
        with self._lock:
            self._confirms[confirm_id] = event
            self._confirm_results.pop(confirm_id, None)
        self.publish(
            "confirm",
            title="Confirm",
            detail=prompt,
            confirm_id=confirm_id,
            confirm_prompt=prompt,
        )
        ok = event.wait(timeout=timeout)
        with self._lock:
            result = self._confirm_results.pop(confirm_id, False) if ok else False
            self._confirms.pop(confirm_id, None)
        return bool(result)

    def resolve_confirm(self, confirm_id: str, approved: bool) -> bool:
        with self._lock:
            self._confirm_results[confirm_id] = approved
            event = self._confirms.get(confirm_id)
        if event:
            event.set()
            return True
        return False


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

