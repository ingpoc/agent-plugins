"""Cursor install mcp.json: absolute launcher, or a loud failure.

Source mcp.json stays portable (`./bin/context-ledger-mcp`, cwd `./`).
Cursor resolves that relative command against the workspace and does not
expand `${PLUGIN_*}`. A marketplace refresh copies the source file back over
any dest rewrite, so doctor must refuse a dest that is still relative.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

LAUNCHER_NAME = "context-ledger-mcp"
SERVER_NAME = "context-ledger"
HOME_ENV = "CONTEXT_LEDGER_CURSOR_HOME"


def scan_home() -> Path:
    raw = os.environ.get(HOME_ENV)
    if not raw:
        return Path.home()
    path = Path(raw)
    if not path.is_dir():
        raise OSError(f"{HOME_ENV} is not a directory: {raw}")
    return path


def candidate_dests(home: Path | None = None) -> list[Path]:
    """Local install plus every marketplace cache copy that has mcp.json."""
    root = home if home is not None else scan_home()
    found: list[Path] = []
    local = root / ".cursor/plugins/local/context-ledger"
    if (local / "mcp.json").is_file():
        found.append(local)
    cache = root / ".cursor/plugins/cache"
    if not cache.is_dir():
        return found
    for market in sorted(path for path in cache.iterdir() if path.is_dir()):
        plugin = market / "context-ledger"
        if (plugin / "mcp.json").is_file():
            found.append(plugin)
        if not plugin.is_dir():
            continue
        for child in sorted(path for path in plugin.iterdir() if path.is_dir()):
            if (child / "mcp.json").is_file():
                found.append(child)
    return found


def _server(data: dict) -> dict | None:
    server = data.get("mcpServers", {}).get(SERVER_NAME)
    if isinstance(server, dict):
        return server
    return None


def inspect_dest(dest: Path) -> dict | None:
    """Return a problem object when this dest cannot be spawned, else None.

    A directory without mcp.json is not a Cursor dest.
    """
    mcp_path = dest / "mcp.json"
    if not mcp_path.is_file():
        return None
    try:
        data = json.loads(mcp_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"path": str(mcp_path), "issues": ["unreadable"], "error": str(exc)}
    if not isinstance(data, dict):
        return {"path": str(mcp_path), "issues": ["unreadable"], "error": "mcp.json is not an object"}
    server = _server(data)
    if server is None:
        return {"path": str(mcp_path), "issues": ["server_missing"]}
    issues: list[str] = []
    blob = json.dumps(server)
    if "${PLUGIN_" in blob:
        issues.append("literal_plugin_token")
    command = str(server.get("command") or "")
    if not command.startswith("/"):
        issues.append("relative_command")
    elif not Path(command).is_file():
        issues.append("missing_launcher")
    if server.get("cwd") != "./":
        issues.append("cwd")
    if not issues:
        return None
    return {
        "path": str(mcp_path),
        "issues": issues,
        "command": command,
        "cwd": server.get("cwd"),
    }


def cursor_dest_problems(dests: list[Path] | None = None, *, home: Path | None = None) -> list[dict]:
    roots = dests if dests is not None else candidate_dests(home)
    return [problem for dest in roots if (problem := inspect_dest(dest)) is not None]


def cursor_dest_failure(dests: list[Path] | None = None, *, home: Path | None = None) -> dict | None:
    try:
        problems = cursor_dest_problems(dests, home=home)
    except OSError as exc:
        return {
            "ok": False,
            "error": {
                "code": "CURSOR_DEST",
                "retryable": False,
                "message": str(exc),
            },
        }
    if not problems:
        return None
    return {
        "ok": False,
        "error": {
            "code": "CURSOR_DEST",
            "retryable": False,
            "message": (
                "Cursor dest mcp.json has a relative command or literal ${PLUGIN_*}; "
                "run python3 scripts/install_cursor_dest.py"
            ),
            "dests": problems,
        },
    }


def rewrite_dest(dest: Path, *, data_dir: Path | None = None) -> dict:
    launcher = dest / "bin" / LAUNCHER_NAME
    mcp_path = dest / "mcp.json"
    if not mcp_path.is_file():
        return {"ok": False, "error": "mcp.json missing", "path": str(mcp_path)}
    if not launcher.is_file():
        return {"ok": False, "error": "launcher missing", "path": str(launcher)}
    try:
        mode = launcher.stat().st_mode
        if not os.access(launcher, os.X_OK):
            launcher.chmod(mode | 0o111)
        if not os.access(launcher, os.X_OK):
            return {"ok": False, "error": "launcher not executable", "path": str(launcher)}
    except OSError as exc:
        return {"ok": False, "error": str(exc), "path": str(launcher)}
    try:
        data = json.loads(mcp_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc), "path": str(mcp_path)}
    if not isinstance(data, dict):
        return {"ok": False, "error": "mcp.json is not an object", "path": str(mcp_path)}
    server = _server(data)
    if server is None:
        return {"ok": False, "error": f"{SERVER_NAME} missing", "path": str(mcp_path)}
    if data_dir is None:
        env = os.environ.get("PLUGIN_DATA")
        data_dir = Path(env) if env else Path.home() / ".context-ledger"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(data_dir, 0o700)
    except OSError as exc:
        return {"ok": False, "error": str(exc), "path": str(data_dir)}
    server["command"] = str(launcher.resolve())
    server["cwd"] = "./"
    server["args"] = ["--data", str(data_dir.resolve()), "serve"]
    try:
        mcp_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": str(exc), "path": str(mcp_path)}
    problem = inspect_dest(dest)
    if problem is not None:
        return {"ok": False, "error": "rewrite left dest unusable", "path": str(mcp_path), "problem": problem}
    return {
        "ok": True,
        "path": str(mcp_path),
        "command": server["command"],
        "cwd": server["cwd"],
        "data": server["args"][1],
    }
