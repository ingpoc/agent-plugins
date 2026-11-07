#!/usr/bin/env python3
"""Stdio MCP adapter: two tools over CUAService. Not an engine.

CUAService owns AX, cursor, settle, screenshots. This process exists only
because Cursor injects tools through MCP stdio. No start/end/verify: the
service auto-spawns; verified act returns a compact AX delta (not a full tree); failures use error_type taxonomy.

Dual-era MCP (https://modelcontextprotocol.io/specification/latest):
- Modern 2026-07-28: per-request _meta, server/discover, resultType.
- Legacy 2025-11-25 and earlier: initialize handshake + ping.
stdio writer is newline-delimited JSON-RPC (spec). Content-Length is read-only
compat for older Cursor senders.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import sys
import tempfile
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

_BACKEND: Any = CUABackend()
_CLIENT: dict[str, str] = {"name": ""}
_ACTIVITY_UNSET = object()
_ACTIVITY: Any = _ACTIVITY_UNSET
_ACTIVITY_FAILURES: dict[str, int] = {"count": 0}
_PULSED: dict[str, Any] = {"on": False, "key": "", "keys": []}


def _backend() -> CUABackend:
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = CUABackend()
    return _BACKEND


def _remember_client(params: dict[str, Any]) -> None:
    info = params.get("clientInfo")
    name = info.get("name") if isinstance(info, dict) else None
    _CLIENT["name"] = name.strip()[:48] if isinstance(name, str) else ""


def _agent_name() -> str:
    origin = os.environ.get("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "").strip()
    if origin:
        prefix = "acu_exec."
        if origin.startswith(prefix):
            origin = origin[len(prefix):].strip()
        return origin or "Agent"
    return _CLIENT["name"].strip() or "Agent"


def _activity() -> Any:
    global _ACTIVITY
    if _ACTIVITY is not _ACTIVITY_UNSET:
        return _ACTIVITY
    module: Any = None
    try:
        path = PLUGIN_ROOT / "bin" / "island_activity.py"
        spec = importlib.util.spec_from_file_location("acu_island_activity", path)
        if spec is not None and spec.loader is not None:
            loaded = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(loaded)
            module = loaded
    except Exception:
        module = None
    _ACTIVITY = module
    return module


def _target_app(arguments: Any) -> str:
    """The Mac app an act/state call operates on (name or bundle id), for the in-pill icon."""
    app = arguments.get("app") if isinstance(arguments, dict) else None
    return app.strip()[:128] if isinstance(app, str) else ""


def _activity_key(activity: Any, app: str = "") -> tuple[str, str]:
    """One row per agent process and operated app: each app clears 2 s after its own last call."""
    agent = _agent_name()
    shown = activity.normalize_agent(agent) if hasattr(activity, "normalize_agent") else agent
    key = f"{shown}:{os.getpid()}"
    return agent, f"{key}:app:{app}" if app else key


def _app_kwargs(app: str) -> dict[str, str]:
    return {"target_app": app} if app else {}


def _begin_activity(app: str = "") -> None:
    """ACU mark (and the operated app's icon) holds while an act/state call runs."""
    activity = _activity()
    if activity is None:
        return
    try:
        agent, key = _activity_key(activity, app)
        if activity.call_started("acu", agent, key=key, in_flight_s=120.0, **_app_kwargs(app)):
            _PULSED["on"] = True
            _PULSED["key"] = key
            if key not in _PULSED["keys"]:
                _PULSED["keys"].append(key)
    except Exception:
        _ACTIVITY_FAILURES["count"] += 1


def _finish_activity(app: str = "") -> None:
    """The call ended (ok, failed or raised): the mark drops 2 s after this, never later."""
    activity = _activity()
    if activity is None:
        return
    try:
        agent, key = _activity_key(activity, app)
        activity.call_finished("acu", agent, key=key, **_app_kwargs(app))
    except Exception:
        _ACTIVITY_FAILURES["count"] += 1


def _end_activity() -> None:
    if not _PULSED["on"]:
        return
    activity = _activity()
    try:
        if activity is not None:
            for key in list(_PULSED["keys"]) or [str(_PULSED["key"])]:
                activity.end("acu", str(key))
    except Exception:
        _ACTIVITY_FAILURES["count"] += 1
    finally:
        _PULSED["on"] = False
        _PULSED["key"] = ""
        _PULSED["keys"] = []


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
        "value": {"type": "string"},
        "checked": {"type": "boolean"},
    }
    predicate = _schema({
        "text": {"type": "string", "minLength": 1},
        "not_text": {"type": "string", "minLength": 1},
        "path": {"type": "string", "minLength": 1},
        "value": {
            "type": "object",
            "additionalProperties": False,
            "required": ["label"],
            "properties": {
                "label": {"type": "string", "minLength": 1},
                "equals": {"type": "string"},
                "text": {"type": "string"},
            },
        },
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
        "full_text_omitted": {"type": "boolean"},
        "failure": _schema({
            "completed_steps": {"type": "integer"},
            "failed_step": {"type": ["integer", "null"]},
        }),
    }, ["ok"])
    return [
        {
            "name": "state",
            "description": "One compact AX state for discovery or after an act miss. Prefer act deltas; at most one query/diff after a miss.",
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
                    "screenshot": {
                        "type": "boolean",
                        "description": "Attach a window JPEG only for a visual claim. Default false.",
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
            "description": "Drive one Mac app in one native plan. Exact paths use op=open with path, not Finder search; op=open with only app launches it or shows a windowless running app. Verified success returns a compact AX delta (~hundreds of chars; long text as range+excerpt); omitted IDs are stale — use labels or one state. Failures return error_type + nearby context (not a full AX dump). ok/verified only when expect matches. Screenshots attach only when pixels are the evidence (empty AX, Stage Manager thumb, or screenshot:true).",
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
                    "screenshot": {
                        "type": "boolean",
                        "description": "Force a window JPEG when the claim is visual. Default off.",
                    },
                },
                ["app"],
            ),
            "outputSchema": result_schema,
        },
        {
            "name": "sessions",
            "description": "Read-only: local agent sessions the island hides (Codex threads with title, state, model, effort, cwd, repo, branch, tokens, context %, pid/host, deep_link; idle threads; probe runs flagged probe:true; harness processes). No emails or account ids.",
            "inputSchema": _schema({"include_idle": {"type": "boolean", "default": True},
                                    "include_probes": {"type": "boolean", "default": True},
                                    "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50}}),
            "annotations": {"readOnlyHint": True, "idempotentHint": True},
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
        screenshot=arguments.get("screenshot") is True,
    )


def handle_act(arguments: dict[str, Any]) -> dict[str, Any]:
    app = str(arguments.get("app") or "").strip()
    if not app:
        return {"ok": False, "error": "app is required"}
    return _backend().act(app, arguments)


HANDLERS = {
    "sessions": lambda arguments: _backend().sessions(arguments),
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
    tracked = name in ("act", "state")
    app = _target_app(arguments) if tracked else ""
    if tracked:
        _begin_activity(app)
    try:
        payload = handler(arguments or {})
    except Exception as exc:
        payload = {"ok": False, "error": str(exc)}
    finally:
        if tracked:
            _finish_activity(app)
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
        _remember_client(params)
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

class MCPInput:
    def __init__(self, stream, *, pending: bytes = b"") -> None:
        self.stream = stream
        self.pending = bytearray(pending)
        self.eof = False

    def _fill(self) -> None:
        chunk = self.stream.read1(65536)
        if chunk:
            self.pending.extend(chunk)
        else:
            self.eof = True

    def readline(self) -> bytes:
        while b"\n" not in self.pending and not self.eof:
            self._fill()
        at = self.pending.find(b"\n")
        size = at + 1 if at >= 0 else len(self.pending)
        result = bytes(self.pending[:size])
        del self.pending[:size]
        return result

    def read(self, size: int) -> bytes:
        while len(self.pending) < size and not self.eof:
            self._fill()
        result = bytes(self.pending[:size])
        del self.pending[:size]
        return result


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


def _handoff_if_stale(message: dict[str, Any], stdin: MCPInput) -> None:
    if message.get("method") != "tools/call":
        return
    try:
        from cua_client import _CLIENT_CONTRACT
        from runtime_contract import APP, RECORD, package_identity

        installed = json.loads((APP / RECORD).read_text())
        if installed == _CLIENT_CONTRACT:
            return
        current = _backend()._cua().call("runtime_contract", retry=True)
        if not isinstance(current, dict) or current != installed or current == _CLIENT_CONTRACT:
            return
        version = current.get("version")
        if not isinstance(version, str) or not version or not isinstance(current.get("sha256"), str):
            return
        roots = [Path.home() / ".agents/plugins/agent-computer-use"]
        roots.extend(PLUGIN_ROOT.parent.glob("*"))
        for root in roots:
            try:
                manifest = json.loads((root / "plugin.json").read_text())
                script = root / "skills/macos-cua/scripts/compact_mcp.py"
                marker = script.with_name("mcp-handoff-v1")
                if manifest.get("version") != version or not script.is_file() or marker.read_text() != "1\n" or package_identity(root) != current:
                    continue
            except (OSError, ValueError, KeyError):
                continue
            with tempfile.TemporaryFile() as spool:
                spool.write(json.dumps({"message": message, "pending": base64.b64encode(stdin.pending).decode(), "client": _CLIENT}).encode())
                spool.seek(0)
                os.set_inheritable(spool.fileno(), True)
                os.execv(sys.executable, [sys.executable, "-u", str(script), "--resume-fd", str(spool.fileno())])
    except Exception:
        return


def serve() -> int:
    resumed = None
    pending = b""
    if len(sys.argv) == 3 and sys.argv[1] == "--resume-fd":
        with os.fdopen(int(sys.argv[2]), "rb") as spool:
            state = json.load(spool)
        resumed = state["message"]
        pending = base64.b64decode(state["pending"], validate=True)
        _CLIENT.update(state["client"])
    stdin = MCPInput(sys.stdin.buffer, pending=pending)
    stdout = sys.stdout.buffer
    while True:
        try:
            if resumed is not None:
                message, incoming = resumed, "resume"
                resumed = None
            else:
                message, incoming = read_message(stdin)
        except json.JSONDecodeError:
            write_message(
                stdout,
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            )
            continue
        if incoming == "eof" or message is None:
            _end_activity()
            return 0
        _handoff_if_stale(message, stdin)
        reply = handle_rpc(message)
        if reply is not None:
            write_message(stdout, reply)


if __name__ == "__main__":
    raise SystemExit(serve())
