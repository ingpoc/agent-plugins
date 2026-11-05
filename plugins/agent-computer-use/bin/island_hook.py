#!/usr/bin/env python3
"""Metadata-only MCP hooks for marketplace plugin activity on the ACU island."""
from __future__ import annotations

import json
import os
import re
import sys

try:
    import island_activity
except Exception:
    island_activity = None

_PLUGIN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
_MCP_TOOL = re.compile(r"^mcp__(.+?)__(.+)$")
_HARNESS_NAMES = {
    "claude-code": "Claude Code",
    "cursor": "Cursor",
    "codex": "Codex",
    "grok-build": "Grok Build",
}
_SELF_POSTING = {"acu", "agent-computer-use", "comet-control", "context-ledger", "developer-observability"}


def _harness(value: str) -> str:
    return re.sub(r"[\s_]+", "-", value.strip().lower())


def _tool_server(value: object) -> str:
    if not isinstance(value, str):
        return ""
    match = _MCP_TOOL.fullmatch(value.strip())
    return match.group(1) if match else ""


def _server(harness: str, payload: dict) -> str:
    if harness == "cursor":
        for key in ("server", "mcp_server", "mcp_server_name"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return _tool_server(payload.get("tool_name"))


def _plugin_id(server: str) -> str:
    normalized = re.sub(r"[_\s]+", "-", server.strip().lower())
    normalized = re.sub(r"^(?:plugin|user)-", "", normalized, count=1)
    return normalized if _PLUGIN_ID.fullmatch(normalized) else ""


def _session(payload: dict) -> str:
    for key in ("session_id", "sessionId"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    pid = payload.get("pid")
    if isinstance(pid, str) and pid.strip():
        return pid.strip()
    if isinstance(pid, int) and not isinstance(pid, bool):
        return str(pid)
    return str(os.getppid())


def _post(harness: str, event: str, payload: dict) -> None:
    if island_activity is None or harness not in _HARNESS_NAMES or event not in {"start", "finish"}:
        return
    plugin = _plugin_id(_server(harness, payload))
    if not plugin or plugin in _SELF_POSTING:
        return
    key = f"{harness}:{plugin}:{_session(payload)}"
    if event == "start":
        island_activity.call_started(plugin, _HARNESS_NAMES[harness], key)
    else:
        island_activity.call_finished(plugin, _HARNESS_NAMES[harness], key)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    harness = _harness(args[0]) if args else ""
    event = args[1].strip().lower() if len(args) > 1 else ""
    if harness == "cursor" and event == "start":
        try:
            sys.stdout.write('{"permission":"allow"}\n')
        except Exception:
            pass
    try:
        payload = json.load(sys.stdin)
        if isinstance(payload, dict):
            _post(harness, event, payload)
    except BaseException:
        pass
    return 0


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        pass
    raise SystemExit(0)
