"""Samantha's plugin activity on the Dynamic Island, through ACU's shared island_activity path.

Same three-zone language as every other agent (Guru, 2026-11-03 13:10 IST): Samantha's circle on
the left, the controlled target in the ring (Mac for cua_* tools, Comet for comet_* tools), the
plugin mark on the right (Agent Computer Use / Comet Control), dropped 2 s after her last call.
Every failure is swallowed: the island must never break a tool call.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any

AGENT = "Samantha"
KEY = "samantha"
DISABLED_VALUES = frozenset({"0", "false", "off", "no"})
_UNSET = object()
_ACTIVITY: Any = _UNSET
FAILURES: dict[str, int] = {"count": 0}


def plugin_for_tool(name: str) -> str | None:
    """cua_* drives the Mac through ACU; comet_* drives Comet through Comet Control."""
    if name.startswith("cua_"):
        return "acu"
    if name.startswith("comet_"):
        return "comet-control"
    return None


def _candidates() -> list[Path]:
    env = os.environ.get("VOICE_CUA_ISLAND_ACTIVITY")
    if env:
        return [Path(env).expanduser()]
    here = Path(__file__).resolve()
    found = [parent / "bin" / "island_activity.py" for parent in here.parents]
    return found + [Path.home() / ".agents/plugins/agent-computer-use/bin/island_activity.py"]


def activity() -> Any:
    global _ACTIVITY
    if _ACTIVITY is not _UNSET:
        return _ACTIVITY
    module: Any = None
    for path in _candidates():
        if not path.is_file():
            continue
        try:
            spec = importlib.util.spec_from_file_location("voice_cua_island_activity", path)
            if spec is not None and spec.loader is not None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                module = loaded
                break
        except Exception:
            module = None
    _ACTIVITY = module
    return module


def enabled() -> bool:
    return os.environ.get("VOICE_CUA_PLUGIN_ISLAND", "").strip().lower() not in DISABLED_VALUES


def target_app(name: str, arguments: Any) -> str:
    """The Mac app a cua_* call operates on, drawn inside the pill like any agent's (13:27 IST)."""
    if plugin_for_tool(name) != "acu" or not isinstance(arguments, dict):
        return ""
    app = arguments.get("app")
    return app.strip()[:128] if isinstance(app, str) else ""


def _note(name: str, finished: bool, arguments: Any = None) -> None:
    plugin = plugin_for_tool(name)
    if plugin is None or not enabled():
        return
    try:
        module = activity()
        if module is None:
            return
        app = target_app(name, arguments)
        key = f"{KEY}:{plugin}"  # one row: her current app rides target_app, never a history
        extra = {"target_app": app} if app else {}
        if finished:
            module.call_finished(plugin, AGENT, key, **extra)
        else:
            module.call_started(plugin, AGENT, key, in_flight_s=120.0, **extra)
    except Exception:
        FAILURES["count"] += 1


def tool_started(name: str, arguments: Any = None) -> None:
    _note(name, finished=False, arguments=arguments)


def tool_finished(name: str, arguments: Any = None) -> None:
    _note(name, finished=True, arguments=arguments)


def island_app(name: str, app: str) -> str:
    """Comet is the centre target now, not a separate app circle on the right."""
    return "" if plugin_for_tool(name) == "comet-control" else app
