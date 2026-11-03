"""Report each ledger call on the ACU Dynamic Island as plugin activity.

One row per calling agent while the ledger is in use. Loading is lazy and every
failure is swallowed: an island outage must never break a ledger call.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any

DISABLED_VALUES = frozenset({"0", "false", "off", "no"})
_UNSET = object()
_ACTIVITY: Any = _UNSET
FAILURES: dict[str, int] = {"count": 0}


def _path() -> Path:
    env = os.environ.get("CONTEXT_LEDGER_ISLAND_ACTIVITY")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".agents/plugins/agent-computer-use/bin/island_activity.py"


def enabled() -> bool:
    return os.environ.get("CONTEXT_LEDGER_ISLAND", "").strip().lower() not in DISABLED_VALUES


def activity() -> Any:
    global _ACTIVITY
    if _ACTIVITY is not _UNSET:
        return _ACTIVITY
    module: Any = None
    try:
        spec = importlib.util.spec_from_file_location("context_ledger_island_activity", _path())
        if spec is not None and spec.loader is not None:
            loaded = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(loaded)
            module = loaded
    except Exception:
        module = None
    _ACTIVITY = module
    return module


def note_call(arguments: dict) -> None:
    """Pulse one context-ledger row for the calling actor. Never raises."""
    if not enabled():
        return
    try:
        actor = arguments.get("actor") if isinstance(arguments, dict) else None
        actor = actor.strip() if isinstance(actor, str) else ""
        actor = actor or "agent"
        module = activity()
        if module is None:
            return
        module.pulse("context-ledger", actor, key=actor, hold_s=10.0)
    except Exception:
        FAILURES["count"] += 1
