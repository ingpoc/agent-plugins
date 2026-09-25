#!/usr/bin/env python3
"""Install the Agent Computer Use plugin into Cursor."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess


PLUGIN_ROOT = Path(__file__).resolve().parents[3]
CURSOR_PLUGIN = Path.home() / ".cursor/plugins/local/agent-computer-use"
LAUNCHER_NAME = "agent-computer-use-mcp"
SERVER_NAME = "agent-computer-use"


def _candidate_cursor_dests() -> list[Path]:
    """Local install + marketplace cache copies Cursor may spawn from."""
    home = Path.home()
    out: list[Path] = [CURSOR_PLUGIN]
    cache = home / ".cursor/plugins/cache/ingpoc-agent-plugins/agent-computer-use"
    if cache.is_dir():
        out.extend(sorted(p for p in cache.iterdir() if p.is_dir()))
    return out


def _rsync_to(dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "rsync",
        "-a",
        "--delete",
        "--delete-excluded",
        "--exclude",
        ".DS_Store",
        "--exclude",
        ".ruff_cache",
        "--exclude",
        ".build",
        "--exclude",
        "__pycache__",
        "--exclude",
        ".pytest_cache",
        f"{PLUGIN_ROOT}/",
        f"{dest}/",
    ]
    subprocess.run(cmd, check=True)
    launcher = dest / "bin" / LAUNCHER_NAME
    if launcher.is_file():
        launcher.chmod(launcher.stat().st_mode | 0o111)


def sync_cursor_plugin(*, user_mcp: bool = False, rewrite_only: bool = False) -> dict:
    dests = _candidate_cursor_dests()
    if not rewrite_only:
        # Primary install tree; also refresh cache trees so marketplace spawn stays current.
        for dest in dests:
            _rsync_to(dest)

    rewritten = []
    for dest in dests:
        launcher = dest / "bin" / LAUNCHER_NAME
        rewritten.append(_rewrite_cursor_plugin_mcp(dest, launcher.resolve() if launcher.is_file() else launcher))

    primary = CURSOR_PLUGIN
    launcher = primary / "bin" / LAUNCHER_NAME
    path_mcp = _install_path_launcher(launcher) if launcher.is_file() else {"ok": False, "error": "launcher missing"}
    user_mcp_result = (
        _install_cursor_user_mcp(launcher)
        if user_mcp and launcher.is_file()
        else _remove_cursor_user_mcp()
    )
    ok = all(r.get("ok") for r in rewritten) and path_mcp.get("ok", False)
    return {
        "ok": ok,
        "changed": True,
        "destination": str(primary),
        "source": str(PLUGIN_ROOT),
        "cursor_plugin_mcp": rewritten,
        "path_launcher": path_mcp,
        "cursor_user_mcp": user_mcp_result,
    }


def _rewrite_cursor_plugin_mcp(dest: Path, launcher: Path) -> dict:
    """Cursor resolves plugin-relative ./ against the workspace, not plugin root.

    Source mcp.json uses ./bin/agent-computer-use-mcp (Agent Plugins portable).
    Dest command is the absolute launcher. cwd stays ./ so the file still
    matches the Agent Plugins cwd pattern — an absolute cwd made Cursor ignore
    dest and keep spawning {workspace}/bin/….
    """
    path = dest / "mcp.json"
    if not path.is_file():
        return {"ok": False, "error": "mcp.json missing", "path": str(path)}
    if not launcher.is_file():
        return {"ok": False, "error": "launcher missing", "path": str(launcher)}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc), "path": str(path)}
    server = data.get("mcpServers", {}).get(SERVER_NAME)
    if not isinstance(server, dict):
        return {"ok": False, "error": f"{SERVER_NAME} missing", "path": str(path)}
    server["command"] = str(launcher.resolve())
    server["cwd"] = "./"
    path.write_text(json.dumps(data, indent=2) + "\n")
    return {"ok": True, "path": str(path), "command": str(launcher.resolve())}


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
    parser.add_argument(
        "--rewrite-only",
        action="store_true",
        help="Absolutize local+cache mcp.json without rsync (after marketplace refresh)",
    )
    args = parser.parse_args()
    payload = {
        "harness": "cursor-plugin",
        **sync_cursor_plugin(user_mcp=args.user_mcp, rewrite_only=args.rewrite_only),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
