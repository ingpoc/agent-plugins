#!/usr/bin/env python3
"""Install this plugin skill as the single macos-cua owner for other harnesses."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess


SKILL_DIR = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = SKILL_DIR.parents[1]
CURSOR_PLUGIN = Path.home() / ".cursor/plugins/local/agent-computer-use"
HARNESS_SKILL_DIRS = {
    "cursor": Path.home() / ".cursor/skills",
    "agents": Path.home() / ".agents/skills",
    "claude": Path.home() / ".claude/skills",
    "codex": Path.home() / ".codex/skills",
}


def _is_macos_cua_tree(path: Path) -> bool:
    return (path / "scripts/macos-cua.py").is_file() and (path / "SKILL.md").is_file()


def install_link(skills_dir: Path, *, replace_copy: bool = False) -> dict:
    destination = skills_dir / "macos-cua"
    skills_dir.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        resolved = destination.resolve()
        if resolved == SKILL_DIR:
            return {
                "ok": True,
                "changed": False,
                "destination": str(destination),
                "source": str(SKILL_DIR),
            }
        return {
            "ok": False,
            "error": f"refusing to replace symlink to {resolved}",
            "destination": str(destination),
        }
    if destination.exists():
        if replace_copy and _is_macos_cua_tree(destination):
            shutil.rmtree(destination)
        else:
            return {
                "ok": False,
                "error": "refusing to replace an existing skill directory",
                "destination": str(destination),
            }
    destination.symlink_to(SKILL_DIR, target_is_directory=True)
    return {
        "ok": True,
        "changed": True,
        "destination": str(destination),
        "source": str(SKILL_DIR),
    }


def sync_cursor_plugin(*, user_mcp: bool = False) -> dict:
    dest = CURSOR_PLUGIN
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "rsync",
        "-a",
        "--delete",
        "--exclude",
        ".DS_Store",
        "--exclude",
        ".ruff_cache",
        f"{PLUGIN_ROOT}/",
        f"{dest}/",
    ]
    subprocess.run(cmd, check=True)
    launcher = dest / "bin" / "agent-computer-use-mcp"
    plugin_mcp = _rewrite_cursor_plugin_mcp(dest, launcher)
    path_mcp = _install_path_launcher(launcher)
    user_mcp = (
        _install_cursor_user_mcp(launcher)
        if user_mcp
        else _remove_cursor_user_mcp()
    )
    return {
        "ok": True,
        "changed": True,
        "destination": str(dest),
        "source": str(PLUGIN_ROOT),
        "cursor_plugin_mcp": plugin_mcp,
        "path_launcher": path_mcp,
        "cursor_user_mcp": user_mcp,
    }


def _rewrite_cursor_plugin_mcp(dest: Path, launcher: Path) -> dict:
    """Cursor resolves plugin-relative ./ against the workspace, not plugin root.

    Source mcp.json uses the bare name agent-computer-use-mcp (Agent Plugins
    1.0.0). Dest command is the absolute launcher. cwd stays ./ so the file
    still matches the Agent Plugins cwd pattern — an absolute cwd made Cursor
    ignore dest and keep spawning {workspace}/bin/….
    """
    path = dest / "mcp.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc), "path": str(path)}
    server = data.get("mcpServers", {}).get("agent-computer-use")
    if not isinstance(server, dict):
        return {"ok": False, "error": "agent-computer-use missing", "path": str(path)}
    server["command"] = str(launcher)
    server["cwd"] = "./"
    path.write_text(json.dumps(data, indent=2) + "\n")
    return {"ok": True, "path": str(path), "command": str(launcher)}


def _install_path_launcher(launcher: Path) -> dict:
    """Bare-name fallback when Cursor reads source mcp.json instead of dest."""
    directory = Path.home() / ".local/bin"
    directory.mkdir(parents=True, exist_ok=True)
    link = directory / "agent-computer-use-mcp"
    if link.exists() or link.is_symlink():
        if link.is_symlink() or link.is_file():
            link.unlink()
        else:
            return {"ok": False, "error": f"refusing to replace {link}", "path": str(link)}
    link.symlink_to(launcher)
    return {"ok": True, "path": str(link), "target": str(launcher)}


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
    if not isinstance(servers, dict) or "agent-computer-use" not in servers:
        return {"ok": True, "path": str(path), "removed": False}
    del servers["agent-computer-use"]
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
    data["mcpServers"]["agent-computer-use"] = {
        "command": str(launcher),
        "env": {
            "CUA_DRIVER_PERMISSION_MODE": "standard",
            "CUA_DRIVER_RS_UPDATE_CHECK": "0",
        },
    }
    path.write_text(json.dumps(data, indent=2) + "\n")
    return {"ok": True, "path": str(path), "command": str(launcher)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "harness",
        choices=sorted(HARNESS_SKILL_DIRS) + ["cursor-plugin", "all"],
    )
    parser.add_argument(
        "--replace-copy",
        action="store_true",
        help="Replace a copied macos-cua skill directory with a symlink",
    )
    parser.add_argument(
        "--user-mcp",
        action="store_true",
        help="Re-add ~/.cursor/mcp.json only if plugin MCP spawn is proven broken",
    )
    args = parser.parse_args()
    results = []
    if args.harness in {"cursor-plugin", "all"}:
        results.append(
            {
                "harness": "cursor-plugin",
                **sync_cursor_plugin(user_mcp=args.user_mcp),
            }
        )
    targets = (
        HARNESS_SKILL_DIRS
        if args.harness == "all"
        else {args.harness: HARNESS_SKILL_DIRS[args.harness]}
        if args.harness in HARNESS_SKILL_DIRS
        else {}
    )
    for name, skills_dir in targets.items():
        item = install_link(skills_dir, replace_copy=args.replace_copy)
        item["harness"] = name
        results.append(item)
    payload = results[0] if len(results) == 1 else {"ok": all(r.get("ok") for r in results), "results": results}
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if (payload.get("ok") if "ok" in payload else all(r.get("ok") for r in results)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
