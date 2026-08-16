#!/usr/bin/env python3
"""Thin stdio MCP facade over macos-cua CLI. Five tools. No raw cua-driver catalog.

Dual-era MCP (https://modelcontextprotocol.io/specification/latest):
- Modern 2026-07-28: per-request _meta, server/discover, resultType.
- Legacy 2025-11-25 and earlier: initialize handshake + ping.
stdio writer is newline-delimited JSON-RPC (spec). Content-Length is read-only
compat for older Cursor/cua-driver senders.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPT_DIR.parents[2]
MACOS_CUA = SCRIPT_DIR / "macos-cua.py"
WORKFLOW = SCRIPT_DIR / "workflow.py"
DEFAULT_MAX = 80
STATE_TIMEOUT = 30
ACT_TIMEOUT = 30
RUN_TIMEOUT = 60
MODERN_VERSION = "2026-07-28"
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SUPPORTED_VERSIONS = (MODERN_VERSION,) + LEGACY_VERSIONS
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_CAPS = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"
AX_ACTIONS = frozenset(
    {
        "open",
        "show_menu",
        "confirm",
        "cancel",
        "pick",
        "press",
        "increment",
        "decrement",
        "raise",
        "zoom",
    }
)
TOOL_NAMES = ("start_session", "state", "act", "verify", "end_session")
INSTRUCTIONS = (
    "Thin macos-cua MCP over cua-driver. Use start_session, state, act, verify, "
    "end_session only. Never list_apps or raw cua-driver MCP (54 tools). "
    "state is compact/query/diff, include_screenshot false. act is AX+background "
    "with a visible operator glide; follow compact effect/escalation "
    "(px|foreground|page) in structuredContent and never silent pixel "
    "fallback. CLI macos-cua.py "
    "state/run is the default AX batch. bin/cua-driver-mcp is diagnostic-only."
)

_SESSION: dict[str, Any] = {}


def plugin_version() -> str:
    try:
        return str(json.loads((PLUGIN_ROOT / "plugin.json").read_text())["version"])
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return "0.2.8"


def server_info() -> dict[str, str]:
    return {"name": "agent-computer-use", "version": plugin_version()}


def resolve_driver() -> str | None:
    for candidate in (
        os.environ.get("CUA_DRIVER_BIN"),
        os.environ.get("CUA_DRIVER"),
        shutil.which("cua-driver"),
        str(Path.home() / ".local/bin/cua-driver"),
    ):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    return None


def state_argv(
    app: str,
    *,
    query: str | None = None,
    diff: bool = False,
    max_elements: int = DEFAULT_MAX,
) -> list[str]:
    cmd = [
        sys.executable,
        str(MACOS_CUA),
        "state",
        app,
        "--compact",
        "--no-screenshot",
        "--max",
        str(max_elements),
    ]
    if query:
        cmd.extend(["--query", query])
    if diff:
        cmd.append("--diff")
    return cmd


def act_argv(arguments: dict[str, Any]) -> tuple[list[str], int, dict[str, Any] | None]:
    app = str(arguments.get("app") or "").strip()
    if not app:
        return [], ACT_TIMEOUT, {"ok": False, "error": "app is required"}
    plan = arguments.get("plan")
    text = arguments.get("text")
    label = arguments.get("label")
    element = arguments.get("element")
    action = str(arguments.get("action") or "").strip()
    expect = arguments.get("expect")
    if plan is not None:
        if not isinstance(plan, dict):
            return [], RUN_TIMEOUT, {"ok": False, "error": "plan must be an object"}
        payload = dict(plan)
        if expect is not None and "expect" not in payload:
            payload["expect"] = {"text": expect} if isinstance(expect, str) else expect
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        try:
            json.dump(payload, handle)
            handle.close()
            return (
                [sys.executable, str(MACOS_CUA), "run", app, f"@{handle.name}"],
                RUN_TIMEOUT,
                {"_temp": handle.name},
            )
        except Exception:
            os.unlink(handle.name)
            raise
    if text is not None:
        typed = str(text)
        if label:
            return (
                [sys.executable, str(MACOS_CUA), "type-label", app, str(label), typed],
                ACT_TIMEOUT,
                None,
            )
        cmd = [sys.executable, str(MACOS_CUA), "type-text", app, typed]
        if element is not None:
            cmd.extend(["--element", str(int(element))])
        return cmd, ACT_TIMEOUT, None
    if action and action not in {"click", "press"} and action in AX_ACTIONS:
        cmd = [sys.executable, str(MACOS_CUA), "perform-action", app, action]
        if element is not None:
            cmd.extend(["--element", str(int(element))])
        elif label:
            cmd.extend(["--label", str(label)])
        else:
            return [], ACT_TIMEOUT, {"ok": False, "error": "perform-action needs label or element"}
        return cmd, ACT_TIMEOUT, None
    if label:
        return (
            [sys.executable, str(MACOS_CUA), "click-label-pointer", app, str(label)],
            ACT_TIMEOUT,
            None,
        )
    if element is not None:
        return (
            [sys.executable, str(MACOS_CUA), "click", app, str(int(element))],
            ACT_TIMEOUT,
            None,
        )
    return [], ACT_TIMEOUT, {
        "ok": False,
        "error": "act needs label, element, text, or plan",
    }


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
    }
    if required:
        schema["required"] = required
    return schema


def tool_schemas() -> list[dict[str, Any]]:
    return [
        {
            "name": "start_session",
            "description": "Mint a cheap session id. Optional driver start_session. No preflight.",
            "inputSchema": _schema(
                {
                    "session": {"type": "string"},
                    "preflight": {"type": "boolean", "default": False},
                }
            ),
        },
        {
            "name": "state",
            "description": "Compact AX state. Always --compact --no-screenshot --max. Prefer query/diff.",
            "inputSchema": _schema(
                {
                    "app": {"type": "string"},
                    "query": {"type": "string"},
                    "diff": {"type": "boolean"},
                    "max": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "default": DEFAULT_MAX,
                    },
                },
                ["app"],
            ),
        },
        {
            "name": "act",
            "description": "Glide then AX. Label/index click, type, perform-action, or a small asserted plan.",
            "inputSchema": _schema(
                {
                    "app": {"type": "string"},
                    "label": {"type": "string"},
                    "element": {"type": "integer"},
                    "action": {"type": "string"},
                    "text": {"type": "string"},
                    "plan": {"type": "object"},
                    "expect": {"type": ["string", "object"]},
                },
                ["app"],
            ),
        },
        {
            "name": "verify",
            "description": "Fail-closed re-read. ok false when degraded or expect misses.",
            "inputSchema": _schema(
                {"app": {"type": "string"}, "expect": {"type": "string"}},
                ["app", "expect"],
            ),
        },
        {
            "name": "end_session",
            "description": "workflow closeout; driver end_session if this process started one.",
            "inputSchema": _schema({"session": {"type": "string"}}),
        },
    ]


def run_cli(argv: list[str], timeout: float) -> dict[str, Any]:
    env = os.environ.copy()
    env["CUA_DRIVER_RS_UPDATE_CHECK"] = "0"
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timeout after {timeout}s"}
    stdout = (result.stdout or "").strip()
    try:
        payload: Any = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError:
        payload = {"ok": result.returncode == 0, "raw": stdout[:4000]}
    if not isinstance(payload, dict):
        return {"ok": result.returncode == 0, "result": payload}
    if result.returncode != 0:
        payload = dict(payload)
        payload.setdefault("ok", False)
        err = (result.stderr or "").strip()
        if err and "error" not in payload:
            payload["error"] = err[:1000]
    return payload


def call_driver(tool: str, params: dict[str, Any], timeout: float = 10) -> dict[str, Any]:
    driver = resolve_driver()
    if not driver:
        return {"ok": False, "error": "cua-driver unavailable"}
    return run_cli([driver, "call", tool, json.dumps(params)], timeout)


def _state_payload(arguments: dict[str, Any]) -> dict[str, Any]:
    app = str(arguments.get("app") or "").strip()
    if not app:
        return {"ok": False, "error": "app is required"}
    max_elements = int(arguments.get("max") or DEFAULT_MAX)
    max_elements = max(1, min(max_elements, 200))
    query = arguments.get("query")
    return run_cli(
        state_argv(
            app,
            query=str(query) if query else None,
            diff=bool(arguments.get("diff")),
            max_elements=max_elements,
        ),
        STATE_TIMEOUT,
    )


def _haystack(payload: dict[str, Any]) -> str:
    return "\n".join(
        str(payload[key])
        for key in ("text", "tree_markdown")
        if payload.get(key)
    )


def handle_start_session(arguments: dict[str, Any]) -> dict[str, Any]:
    session = str(arguments.get("session") or "").strip() or f"acu-{secrets.token_hex(6)}"
    os.environ["MACOS_CUA_SESSION"] = session
    driver = call_driver("start_session", {"session": session}, timeout=8)
    driver_started = "error" not in driver and driver.get("ok") is not False
    operator = run_cli(
        [sys.executable, str(MACOS_CUA), "operator", "start"],
        15,
    )
    preflight = None
    if arguments.get("preflight"):
        preflight = run_cli([sys.executable, str(WORKFLOW), "preflight"], 25)
    _SESSION.update(
        {
            "session": session,
            "driver_started": driver_started,
            "operator_ok": operator.get("ok") is True,
        }
    )
    out = {
        "ok": True,
        "session": session,
        "driver": driver,
        "operator": operator,
        "preflight": False,
    }
    if preflight is not None:
        out["preflight"] = preflight
    return out


def handle_act(arguments: dict[str, Any]) -> dict[str, Any]:
    argv, timeout, extra = act_argv(arguments)
    temp = (extra or {}).get("_temp")
    if extra and not argv:
        return extra
    try:
        return run_cli(argv, timeout)
    finally:
        if temp:
            Path(temp).unlink(missing_ok=True)


def handle_verify(arguments: dict[str, Any]) -> dict[str, Any]:
    expect = str(arguments.get("expect") or "")
    if not expect:
        return {"ok": False, "error": "expect is required"}
    payload = _state_payload({"app": arguments.get("app"), "query": expect})
    text = _haystack(payload)
    matched = bool(expect) and expect in text
    degraded = bool(payload.get("degraded"))
    return {
        "ok": matched
        and not degraded
        and payload.get("ok") is not False
        and "error" not in payload,
        "expect": expect,
        "matched": matched,
        "degraded": degraded,
        "app": payload.get("app") or arguments.get("app"),
        "text": text if matched else text[:800],
        "state_error": payload.get("error"),
        "effect": payload.get("effect"),
        "escalation": payload.get("escalation"),
    }


def handle_end_session(arguments: dict[str, Any]) -> dict[str, Any]:
    session = str(arguments.get("session") or _SESSION.get("session") or "").strip()
    driver = None
    if session and _SESSION.get("driver_started"):
        driver = call_driver("end_session", {"session": session}, timeout=8)
    closeout = run_cli([sys.executable, str(WORKFLOW), "closeout"], 20)
    _SESSION.clear()
    return {
        "ok": closeout.get("success") is True or closeout.get("ok") is True,
        "session": session or None,
        "closeout": closeout,
        "driver": driver,
    }


HANDLERS = {
    "start_session": handle_start_session,
    "state": _state_payload,
    "act": handle_act,
    "verify": handle_verify,
    "end_session": handle_end_session,
}


def _params_meta(params: dict[str, Any]) -> dict[str, Any]:
    meta = params.get("_meta")
    return meta if isinstance(meta, dict) else {}


def _rpc_error(rpc_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": rpc_id, "error": error}


def _rpc_result(rpc_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def initialize_result(params: dict[str, Any]) -> dict[str, Any]:
    requested = str(params.get("protocolVersion") or "")
    version = requested if requested in LEGACY_VERSIONS else LEGACY_VERSIONS[0]
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": server_info(),
        "instructions": INSTRUCTIONS,
    }


def discover_result() -> dict[str, Any]:
    return {
        "resultType": "complete",
        "supportedVersions": list(SUPPORTED_VERSIONS),
        "capabilities": {"tools": {"listChanged": False}},
        "instructions": INSTRUCTIONS,
        "ttlMs": 3_600_000,
        "cacheScope": "public",
        "_meta": {META_SERVER_INFO: server_info()},
    }


def tools_list_result(*, modern: bool) -> dict[str, Any]:
    result: dict[str, Any] = {"tools": tool_schemas()}
    if modern:
        result.update(
            {
                "resultType": "complete",
                "ttlMs": 3_600_000,
                "cacheScope": "public",
                "_meta": {META_SERVER_INFO: server_info()},
            }
        )
    return result


def call_tool(name: str, arguments: dict[str, Any] | None, *, modern: bool) -> dict[str, Any]:
    handler = HANDLERS.get(name)
    if handler is None:
        raise KeyError(name)
    try:
        payload = handler(arguments or {})
    except Exception as exc:
        payload = {"ok": False, "error": str(exc)}
    failed = isinstance(payload, dict) and (
        payload.get("ok") is False or payload.get("error")
    )
    if name == "verify":
        failed = not bool(payload.get("ok"))
    text = json.dumps(payload, default=str, separators=(",", ":"))
    result: dict[str, Any] = {
        "content": [{"type": "text", "text": text}],
        "isError": bool(failed),
    }
    if isinstance(payload, dict):
        result["structuredContent"] = payload
    if modern:
        result["resultType"] = "complete"
        result["_meta"] = {META_SERVER_INFO: server_info()}
    return result


def _modern_guard(params: dict[str, Any], rpc_id: Any) -> dict[str, Any] | None:
    meta = _params_meta(params)
    if META_VERSION not in meta:
        return _rpc_error(rpc_id, -32602, f"Invalid params: missing _meta.{META_VERSION}")
    if META_CLIENT_CAPS not in meta:
        return _rpc_error(rpc_id, -32602, f"Invalid params: missing _meta.{META_CLIENT_CAPS}")
    requested = str(meta.get(META_VERSION) or "")
    if requested != MODERN_VERSION:
        return _rpc_error(
            rpc_id,
            -32022,
            "Unsupported protocol version",
            {"supported": list(SUPPORTED_VERSIONS), "requested": requested},
        )
    return None


def handle_rpc(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    rpc_id = message.get("id")
    params = message.get("params")
    if not isinstance(params, dict):
        params = {}
    if method in {
        None,
        "notifications/initialized",
        "initialized",
        "notifications/cancelled",
    }:
        return None
    if rpc_id is None:
        return None
    modern = META_VERSION in _params_meta(params)
    if method == "initialize":
        return _rpc_result(rpc_id, initialize_result(params))
    if modern:
        guard = _modern_guard(params, rpc_id)
        if guard is not None:
            return guard
        if method == "server/discover":
            return _rpc_result(rpc_id, discover_result())
        if method == "tools/list":
            return _rpc_result(rpc_id, tools_list_result(modern=True))
        if method == "tools/call":
            name = str(params.get("name") or "")
            if name not in HANDLERS:
                return _rpc_error(rpc_id, -32602, f"Unknown tool: {name}")
            return _rpc_result(
                rpc_id, call_tool(name, params.get("arguments") or {}, modern=True)
            )
        return _rpc_error(rpc_id, -32601, f"Method not found: {method}")
    if method == "ping":
        return _rpc_result(rpc_id, {})
    if method == "tools/list":
        return _rpc_result(rpc_id, tools_list_result(modern=False))
    if method == "tools/call":
        name = str(params.get("name") or "")
        if name not in HANDLERS:
            return _rpc_error(rpc_id, -32602, f"Unknown tool: {name}")
        return _rpc_result(
            rpc_id, call_tool(name, params.get("arguments") or {}, modern=False)
        )
    if method == "server/discover":
        return _rpc_error(rpc_id, -32602, f"Invalid params: missing _meta.{META_VERSION}")
    return _rpc_error(rpc_id, -32601, f"Method not found: {method}")


def read_message(stream) -> tuple[dict[str, Any] | None, str]:
    first = stream.readline()
    if not first:
        return None, "eof"
    if first.lower().startswith(b"content-length:"):
        length = int(first.split(b":", 1)[1].strip())
        while True:
            line = stream.readline()
            if line in (b"\r\n", b"\n", b""):
                break
        body = stream.read(length)
        return json.loads(body), "lsp"
    line = first.strip()
    if not line:
        return read_message(stream)
    return json.loads(line), "nl"


def write_message(stream, payload: dict[str, Any]) -> None:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode()
    if b"\n" in raw:
        raise ValueError("stdio MCP messages must not contain embedded newlines")
    stream.write(raw + b"\n")
    stream.flush()


def serve() -> int:
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    while True:
        try:
            message, incoming = read_message(stdin)
        except json.JSONDecodeError:
            write_message(
                stdout,
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            )
            continue
        if incoming == "eof" or message is None:
            return 0
        reply = handle_rpc(message)
        if reply is not None:
            write_message(stdout, reply)


if __name__ == "__main__":
    raise SystemExit(serve())
