#!/usr/bin/env python3
"""Repair Cursor fallbacks for Agent Computer Use without copying the plugin."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil


PLUGIN_ROOT = Path(__file__).resolve().parents[3]
# Retired: a hand-synced copy Cursor kept loading as a stale local plugin.
# Cursor now installs this plugin natively from the marketplace and resolves
# ${CURSOR_PLUGIN_ROOT} in .cursor-plugin/mcp.json, so nothing is copied.
STALE_LOCAL_PLUGIN = Path.home() / ".cursor/plugins/local/agent-computer-use"
LAUNCHER_NAME = "agent-computer-use-mcp"
SERVER_NAME = "agent-computer-use"


def _retire_stale_local(stale: Path | None = None, trash: Path | None = None) -> dict:
    """Move the stale hand-synced Cursor copy to the Trash (never delete)."""
    stale = stale or STALE_LOCAL_PLUGIN
    trash = trash or (Path.home() / ".Trash")
    if not (stale.exists() or stale.is_symlink()):
        return {"ok": True, "path": str(stale), "trashed": None}
    trash.mkdir(parents=True, exist_ok=True)
    destination = trash / stale.name
    suffix = 1
    while destination.exists() or destination.is_symlink():
        destination = trash / f"{stale.name} {suffix}"
        suffix += 1
    try:
        shutil.move(str(stale), str(destination))
    except (OSError, shutil.Error) as error:
        return {"ok": False, "path": str(stale), "error": str(error)}
    return {"ok": True, "path": str(stale), "trashed": str(destination)}


def sync_cursor_plugin(*, user_mcp: bool = False, plugin_root: Path | None = None) -> dict:
    """Point bare-name fallbacks at this plugin root; never copy into Cursor dirs."""
    root = (plugin_root or PLUGIN_ROOT).resolve()
    launcher = root / "bin" / LAUNCHER_NAME
    stale = _retire_stale_local()
    path_mcp = _install_path_launcher(launcher) if launcher.is_file() else {"ok": False, "error": "launcher missing", "path": str(launcher)}
    user_mcp_result = (
        _install_cursor_user_mcp(launcher)
        if user_mcp and launcher.is_file()
        else _remove_cursor_user_mcp()
    )
    ok = stale.get("ok", False) and path_mcp.get("ok", False) and user_mcp_result.get("ok", False)
    return {
        "ok": ok,
        "source": str(root),
        "stale_local_plugin": stale,
        "path_launcher": path_mcp,
        "cursor_user_mcp": user_mcp_result,
        "next": "Install or refresh agent-computer-use from the Cursor marketplace; Cursor resolves ${CURSOR_PLUGIN_ROOT} natively.",
    }


def _install_path_launcher(launcher: Path) -> dict:
    """Bare-name fallback when Cursor reads source mcp.json instead of dest."""
    directory = Path.home() / ".local/bin"
    directory.mkdir(parents=True, exist_ok=True)
    link = directory / LAUNCHER_NAME
    if link.exists() or link.is_symlink():
        if link.is_symlink() or link.is_file():
            link.unlink()
        else:
            return {"ok": False, "error": f"refusing to replace {link}", "path": str(link)}
    link.symlink_to(launcher.resolve())
    return {"ok": True, "path": str(link), "target": str(launcher.resolve())}


def _remove_cursor_user_mcp(path: Path | None = None) -> dict:
    """Drop the standalone user MCP so Customize shows the plugin only."""
    path = path or (Path.home() / ".cursor/mcp.json")
    if not path.is_file():
        return {"ok": True, "path": str(path), "removed": False}
    try:
        loaded = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {"ok": False, "error": "cursor mcp.json is not JSON", "path": str(path)}
    if not isinstance(loaded, dict):
        return {"ok": True, "path": str(path), "removed": False}
    servers = loaded.setdefault("mcpServers", {})
    if not isinstance(servers, dict) or SERVER_NAME not in servers:
        return {"ok": True, "path": str(path), "removed": False}
    del servers[SERVER_NAME]
    path.write_text(json.dumps(loaded, indent=2) + "\n")
    return {"ok": True, "path": str(path), "removed": True}


def _install_cursor_user_mcp(launcher: Path) -> dict:
    """Optional fallback only when plugin spawn is proven broken."""
    path = Path.home() / ".cursor/mcp.json"
    data = {"mcpServers": {}}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text())
        except json.JSONDecodeError:
            return {"ok": False, "error": "cursor mcp.json is not JSON", "path": str(path)}
        if isinstance(loaded, dict):
            data = loaded
            data.setdefault("mcpServers", {})
    data["mcpServers"][SERVER_NAME] = {
        "command": str(launcher.resolve()),
    }
    path.write_text(json.dumps(data, indent=2) + "\n")
    return {"ok": True, "path": str(path), "command": str(launcher.resolve())}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "harness",
        choices=["cursor-plugin"],
    )
    parser.add_argument(
        "--user-mcp",
        action="store_true",
        help="Re-add ~/.cursor/mcp.json only if plugin MCP spawn is proven broken",
    )
    args = parser.parse_args()
    payload = {
        "harness": "cursor-plugin",
        **sync_cursor_plugin(user_mcp=args.user_mcp),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
