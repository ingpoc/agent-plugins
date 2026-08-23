#!/usr/bin/env python3
"""Stdio MCP adapter: two tools over CUAService. Not an engine.

CUAService owns AX, cursor, settle, screenshots. This process exists only
because Cursor injects tools through MCP stdio. No start/end/verify: the
service auto-spawns and act returns the settled tree.

Dual-era MCP (https://modelcontextprotocol.io/specification/latest):
- Modern 2026-07-28: per-request _meta, server/discover, resultType.
- Legacy 2025-11-25 and earlier: initialize handshake + ping.
stdio writer is newline-delimited JSON-RPC (spec). Content-Length is read-only
compat for older Cursor senders.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from compact_backend import (
    CUABackend,
    DEFAULT_MAX,
    INSTRUCTIONS,
    LEGACY_VERSIONS,
    META_CLIENT_CAPS,
    META_SERVER_INFO,
    META_VERSION,
    MODERN_VERSION,
    PLUGIN_ROOT,
    SUPPORTED_VERSIONS,
    expect_is_new,
    expect_verified,
    expectation_is_new,
)

_BACKEND: Any = None


def _backend() -> CUABackend:
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = CUABackend()
    return _BACKEND


def plugin_version() -> str:
    try:
        return str(json.loads((PLUGIN_ROOT / "plugin.json").read_text())["version"])
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return "0.2.8"


def server_info() -> dict[str, str]:
    return {"name": "agent-computer-use", "version": plugin_version()}


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
    operations = [
        "click", "focus", "open", "reveal", "key", "type_text",
        "set_value", "scroll", "drag", "select_text", "secondary_action",
    ]
    step_properties = {
        "op": {"type": "string", "enum": operations},
        "label": {"type": "string"},
        "element": {"type": "integer"},
        "action": {"type": "string"},
        "text": {"type": "string"},
        "key": {"type": "string"},
        "path": {"type": "string"},
        "url": {"type": "string"},
        "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
        "pages": {"type": "integer", "minimum": 1, "maximum": 20},
        "x": {"type": "number"},
        "y": {"type": "number"},
        "from_x": {"type": "number"},
        "from_y": {"type": "number"},
        "to_x": {"type": "number"},
        "to_y": {"type": "number"},
        "prefix": {"type": "string"},
        "suffix": {"type": "string"},
        "selection_type": {"type": "string"},
        "wait": {"type": "number", "minimum": 0, "maximum": 45},
    }
    predicate = _schema({
        "text": {"type": "string", "minLength": 1},
        "not_text": {"type": "string", "minLength": 1},
        "path": {"type": "string", "minLength": 1},
    })
    predicate["minProperties"] = 1
    expect_schema = {
        "oneOf": [
            {"type": "string", "minLength": 1},
            predicate,
            {
                "type": "array",
                "minItems": 1,
                "items": {"oneOf": [{"type": "string", "minLength": 1}, predicate]},
            },
        ],
        "description": "Required postcondition: settled AX text change and/or exact native path result.",
    }
    result_schema = _schema({
        "ok": {"type": "boolean"},
        "verified": {"type": "boolean"},
        "dispatched": {"type": "boolean"},
        "completion": {"type": "string", "enum": ["verified", "unverified"]},
        "text": {"type": "string"},
        "method": {"type": ["string", "null"]},
        "screenshot_before": {"type": ["object", "null"]},
        "screenshot_after": {"type": ["object", "null"]},
        "screenshot": {"type": ["object", "null"]},
        "results": {"type": "array", "items": {"type": "object"}},
        "expect": expect_schema,
        "error": {"type": ["string", "null"]},
        "error_type": {"type": ["string", "null"]},
    }, ["ok"])
    return [
        {
            "name": "state",
            "description": "One compact AX state. Use the current tree, then at most one query/diff after a miss.",
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
            "outputSchema": _schema({
                "ok": {"type": "boolean"},
                "app": {"type": "string"},
                "text": {"type": "string"},
                "elementCount": {"type": ["integer", "null"]},
                "screenshot": {"type": ["object", "null"]},
                "pid": {"type": ["integer", "null"]},
                "error": {"type": "string"},
            }, ["ok"]),
            "annotations": {"readOnlyHint": True, "idempotentHint": True},
        },
        {
            "name": "act",
            "description": "Drive one Mac app in one native plan. Exact paths use op=open with path, not Finder search. Returns before/after settled state and ok only when expect verifies.",
            "inputSchema": _schema(
                {
                    "app": {"type": "string"},
                    **step_properties,
                    "steps": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 50,
                        "items": _schema(step_properties),
                    },
                    "expect": expect_schema,
                    "allow_unverified": {
                        "type": "boolean",
                        "description": "Dispatch only when no AX postcondition is representable; never report completion.",
                    },
                },
                ["app"],
            ),
            "outputSchema": result_schema,
        },
    ]


def _state_payload(arguments: dict[str, Any]) -> dict[str, Any]:
    app = str(arguments.get("app") or "").strip()
    if not app:
        return {"ok": False, "error": "app is required"}
    max_elements = int(arguments.get("max") or DEFAULT_MAX)
    max_elements = max(1, min(max_elements, 200))
    query = arguments.get("query")
    return _backend().state(
        app,
        query=str(query) if query else None,
        diff=bool(arguments.get("diff")),
        max_elements=max_elements,
    )


def handle_act(arguments: dict[str, Any]) -> dict[str, Any]:
    app = str(arguments.get("app") or "").strip()
    if not app:
        return {"ok": False, "error": "app is required"}
    return _backend().act(app, arguments)


HANDLERS = {
    "state": _state_payload,
    "act": handle_act,
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
