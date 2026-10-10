"""Cursor install MCP: package override, or an absolute launcher for old dests.

Portable root mcp.json stays `./bin/context-ledger-mcp` with cwd `./`.
Cursor resolves that relative command against the workspace and does not
expand `${PLUGIN_ROOT}` or `${PLUGIN_DATA}`. It does expand
`${CURSOR_PLUGIN_ROOT}`. When `dest/.cursor-plugin/plugin.json` sets
`mcpServers` to a relative path, that file is the Cursor-effective config:
healthy iff command is CURSOR_PLUGIN_COMMAND, cwd is in CURSOR_PLUGIN_CWDS,
and the server has no `${PLUGIN_` token. `rewrite_dest` leaves a healthy
override untouched (`mode` `cursor_override`) and fails loud on a broken one.
Dests with no override are still rewritten to the absolute launcher so
pre-override cache copies can spawn.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

LAUNCHER_NAME = "context-ledger-mcp"
SERVER_NAME = "context-ledger"
HOME_ENV = "CONTEXT_LEDGER_CURSOR_HOME"
CURSOR_PLUGIN_COMMAND = "${CURSOR_PLUGIN_ROOT}/bin/context-ledger-mcp"
CURSOR_PLUGIN_CWDS = frozenset({"./", "${CURSOR_PLUGIN_ROOT}"})


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


def _plugin_override_ref(dest: Path) -> tuple[str, Path | None, str]:
    """How this dest declares a Cursor package override.

    status is `none` (legacy root mcp.json), `ok` (relative path exists),
    `missing` (relative path does not exist), or `bad` (unreadable or not
    a relative path). A bad or missing declaration is not a legacy dest.
    """
    manifest = dest / ".cursor-plugin" / "plugin.json"
    if not manifest.is_file():
        return "none", None, ""
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return "bad", manifest, str(exc)
    if not isinstance(data, dict):
        return "bad", manifest, "plugin.json is not an object"
    if "mcpServers" not in data:
        return "none", None, ""
    ref = data.get("mcpServers")
    if not isinstance(ref, str) or not ref or ref.startswith("/") or "${" in ref:
        return "bad", manifest, "mcpServers must be a relative path"
    target = dest / ref
    if not target.is_file():
        return "missing", target, ""
    return "ok", target, ""


def inspect_dest(dest: Path) -> dict | None:
    """Return a problem object when this dest cannot be spawned, else None.

    A directory without mcp.json and without a Cursor override is not a dest.
    A declared override is inspected instead of the portable root mcp.json.
    """
    status, path, detail = _plugin_override_ref(dest)
    if status == "bad":
        return {"path": str(path), "issues": ["unreadable"], "error": detail}
    if status == "missing":
        return {"path": str(path), "issues": ["override_missing"]}
    mcp_path = path if status == "ok" else dest / "mcp.json"
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
    cwd = server.get("cwd")
    if command == CURSOR_PLUGIN_COMMAND:
        if cwd not in CURSOR_PLUGIN_CWDS:
            issues.append("cwd")
    else:
        if not command.startswith("/"):
            issues.append("relative_command")
        elif not Path(command).is_file():
            issues.append("missing_launcher")
        if cwd != "./":
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
    status, path, _detail = _plugin_override_ref(dest)
    if status != "none":
        problem = inspect_dest(dest)
        if problem is not None:
            return {
                "ok": False,
                "error": "cursor override unusable",
                "path": str(path),
                "problem": problem,
            }
        return {"ok": True, "mode": "cursor_override", "path": str(path)}
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
