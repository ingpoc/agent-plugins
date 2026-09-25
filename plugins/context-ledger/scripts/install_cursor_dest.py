#!/usr/bin/env python3
"""Rewrite Cursor dest mcp.json to an absolute launcher (Agent Plugins → native).

Source mcp.json stays portable (`./bin/context-ledger-mcp`). Cursor's MCP host
often resolves relative commands against the workspace and lacks shell PATH for
bare `python`; ACU uses the same dest absolutize step.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_NAME = "context-ledger-mcp"
SERVER_NAME = "context-ledger"


def _candidate_dests() -> list[Path]:
    home = Path.home()
    out: list[Path] = [
        home / ".cursor/plugins/local/context-ledger",
    ]
    cache = home / ".cursor/plugins/cache/ingpoc-agent-plugins/context-ledger"
    if cache.is_dir():
        out.extend(sorted(p for p in cache.iterdir() if p.is_dir()))
    return out


def _rewrite(dest: Path) -> dict:
    launcher = dest / "bin" / LAUNCHER_NAME
    mcp_path = dest / "mcp.json"
    if not mcp_path.is_file():
        return {"ok": False, "error": "mcp.json missing", "path": str(mcp_path)}
    if not launcher.is_file():
        return {"ok": False, "error": "launcher missing", "path": str(launcher)}
    try:
        data = json.loads(mcp_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc), "path": str(mcp_path)}
    server = data.get("mcpServers", {}).get(SERVER_NAME)
    if not isinstance(server, dict):
        return {"ok": False, "error": f"{SERVER_NAME} missing", "path": str(mcp_path)}
    # Absolute command; keep cwd as ./ so Cursor does not ignore dest (ACU lesson).
    # Cursor does not expand ${PLUGIN_DATA} — bake absolute data (default ~/.context-ledger).
    data_dir = Path(os.environ.get("PLUGIN_DATA") or (Path.home() / ".context-ledger"))
    data_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(data_dir, 0o700)
    except OSError:
        pass
    server["command"] = str(launcher.resolve())
    server["cwd"] = "./"
    server["args"] = ["--data", str(data_dir.resolve()), "serve"]
    mcp_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {
        "ok": True,
        "path": str(mcp_path),
        "command": server["command"],
        "cwd": server["cwd"],
        "data": server["args"][1],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dest",
        type=Path,
        action="append",
        help="Cursor plugin install root (repeatable). Default: discover local+cache.",
    )
    args = parser.parse_args(argv)
    dests = args.dest or _candidate_dests()
    if not dests:
        print(json.dumps({"ok": False, "error": "no Cursor dest found"}))
        return 1
    results = []
    failed = False
    for dest in dests:
        result = _rewrite(dest.resolve())
        results.append(result)
        failed = failed or not result.get("ok")
    print(json.dumps({"ok": not failed, "results": results}, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
