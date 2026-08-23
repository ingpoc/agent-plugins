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
    active_apps: list[str] = field(default_factory=list)
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

    @property
    def state(self) -> IslandState:
        with self._lock:
            return IslandState(**asdict(self._state))

    def subscribe(self, fn: Callable[[IslandState], None]) -> None:
        with self._lock:
            self._listeners.append(fn)

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
    ) -> IslandState:
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


ISLAND = IslandBus()
