"""Bridge to CUAService via plugin cua_client + compact_mcp act/state semantics."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

_PACKAGED_PLUGIN_SCRIPTS = (
    Path(__file__).resolve().parents[4] / "skills" / "macos-cua" / "scripts"
)

# Last-resort fallback: the installed plugin copy. Used when the frozen helper
# (PyInstaller) lacks bundled scripts — parents[4] of a frozen module points
# inside the .app, not at a plugin root.
INSTALLED_PLUGIN_SCRIPTS = Path(
    "~/.agents/plugins/agent-computer-use/skills/macos-cua/scripts"
)
_PATHS_LOGGED: set[str] = set()


def _scripts_candidates() -> list[Path]:
    """Ordered search: env > _MEIPASS (frozen) > repo layout (dev) > installed plugin."""
    candidates: list[Path] = []
    env = os.environ.get("VOICE_CUA_MACOS_CUA_SCRIPTS", "").strip()
    if env:
        candidates.append(Path(env).expanduser())
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass))
    else:
        candidates.append(_PACKAGED_PLUGIN_SCRIPTS)
    candidates.append(INSTALLED_PLUGIN_SCRIPTS.expanduser())
    return candidates


def _plugin_scripts_dir() -> Path:
    """Locate macos-cua/scripts (compact_mcp.py) for dev and PyInstaller bundles."""
    for root in _scripts_candidates():
        if (root / "compact_mcp.py").is_file():
            return root
    raise ModuleNotFoundError(
        "compact_mcp scripts not found; set VOICE_CUA_MACOS_CUA_SCRIPTS to macos-cua/scripts"
    )


def _ensure_paths() -> None:
    root = _plugin_scripts_dir()
    s = str(root)
    if s not in sys.path:
        sys.path.insert(0, s)
    service = root.parent / "service"
    if service.is_dir():
        ss = str(service)
        if ss not in sys.path:
            sys.path.insert(0, ss)
    if s in _PATHS_LOGGED:
        return
    _PATHS_LOGGED.add(s)
    # Once per process: prove where compact_mcp resolved (safe fields only).
    try:
        from voice_cua.activity_log import log_event

        log_event(
            "startup",
            phase="cua_scripts_dir",
            status="ok",
            scripts_dir=s,
            frozen=bool(getattr(sys, "frozen", False)),
        )
    except Exception:
        pass
    print(f"[cua_bridge] compact_mcp scripts dir: {s}", file=sys.stderr, flush=True)


def get_backend():
    _ensure_paths()
    import compact_mcp  # noqa: WPS433

    return compact_mcp._backend()


def get_tool_schemas() -> dict[str, dict[str, Any]]:
    """Return canonical CUA MCP schemas without starting CUAService."""
    _ensure_paths()
    import compact_mcp  # noqa: WPS433

    return {item["name"]: item for item in compact_mcp.tool_schemas()}


def hide_agent_cursor() -> None:
    """Dismiss Hermes pointer when a response cycle is fully complete."""
    try:
        get_backend().hide_agent_cursor()
    except Exception:
        pass


def redact_for_model(result: dict[str, Any]) -> dict[str, Any]:
    """Strip screenshots and huge trees before returning to Realtime."""
    out = {k: v for k, v in result.items() if k not in {
        "screenshot", "screenshot_before", "screenshot_after"
    }}
    text = out.get("text")
    if isinstance(text, str) and len(text) > 12_000:
        out["text"] = text[:12_000] + "\n…truncated…"
    return out
