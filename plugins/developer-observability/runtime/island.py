"""Report each developer-observability tool call on the ACU Dynamic Island as plugin activity.

The island draws the developer-observability mark OUTSIDE RIGHT while a call runs (Guru, 2026-11-04 13:33 IST)
and adds nothing inside the pill (dev-obs controls nothing). One row per calling harness, held for the call and
dropped 2 s after it ends, through ACU's shared bin/island_activity.py. Loading is lazy and every failure is
swallowed: an island outage must never break an observability call.
"""

from __future__ import annotations

import functools
import importlib.util
import os
from pathlib import Path
from typing import Any, Callable

PLUGIN = "developer-observability"
DISABLED_VALUES = frozenset({"0", "false", "off", "no"})
_UNSET = object()
_ACTIVITY: Any = _UNSET
FAILURES: dict[str, int] = {"count": 0}


def _path() -> Path:
    env = os.environ.get("DEVELOPER_OBSERVABILITY_ISLAND_ACTIVITY")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".agents/plugins/agent-computer-use/bin/island_activity.py"


def enabled() -> bool:
    return os.environ.get("DEVELOPER_OBSERVABILITY_ISLAND", "").strip().lower() not in DISABLED_VALUES


def activity() -> Any:
    global _ACTIVITY
    if _ACTIVITY is not _UNSET:
        return _ACTIVITY
    module: Any = None
    try:
        spec = importlib.util.spec_from_file_location("developer_observability_island_activity", _path())
        if spec is not None and spec.loader is not None:
            loaded = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(loaded)
            module = loaded
    except Exception:
        module = None
    _ACTIVITY = module
    return module


def _note(harness: str, finished: bool) -> None:
    if not enabled():
        return
    try:
        module = activity()
        if module is None:
            return
        key = module.normalize_agent(harness)
        if finished:
            module.call_finished(PLUGIN, harness, key=key)
        else:
            module.call_started(PLUGIN, harness, key=key, in_flight_s=300.0)
    except Exception:
        FAILURES["count"] += 1


def tracked(harness: Callable[[], str]) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator: hold the mark while the tool runs; `harness()` names the caller (MCP clientInfo)."""

    def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def inner(*args: Any, **kwargs: Any) -> Any:
            try:
                who = harness() or "agent"
            except Exception:
                who = "agent"
            _note(who, finished=False)
            try:
                return fn(*args, **kwargs)
            finally:
                _note(who, finished=True)

        return inner

    return wrap
