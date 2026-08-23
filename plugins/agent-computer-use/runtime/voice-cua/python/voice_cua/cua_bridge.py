"""Bridge to CUAService via plugin cua_client + compact_mcp act/state semantics."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

_PACKAGED_PLUGIN_SCRIPTS = (
    Path(__file__).resolve().parents[4] / "skills" / "macos-cua" / "scripts"
)


def _plugin_scripts_dir() -> Path:
    """Locate macos-cua/scripts (compact_mcp.py) for dev and PyInstaller bundles."""
    candidates: list[Path] = []
    env = os.environ.get("VOICE_CUA_MACOS_CUA_SCRIPTS", "").strip()
    if env:
        candidates.append(Path(env).expanduser())
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass))
    candidates.append(_PACKAGED_PLUGIN_SCRIPTS)
    for root in candidates:
        if not root:
            continue
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
