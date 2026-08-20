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
import re
import socket
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPT_DIR.parents[2]
SERVICE_DIR = SCRIPT_DIR.parent / "service"
DEFAULT_MAX = 80
MODERN_VERSION = "2026-07-28"
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SUPPORTED_VERSIONS = (MODERN_VERSION,) + LEGACY_VERSIONS
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_CAPS = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"
TOOL_NAMES = ("state", "act")
INSTRUCTIONS = (
    "Thin MCP over CUAService (state + act only). Do not call start_session, "
    "verify, end_session, list_apps, or raw cua-driver. "
    "Two wall clocks: within-app batch one asserted act (steps+expect); "
    "cross-app one act per surface then switch — each extra tool is a full "
    "agent turn. Act-first when labels are known; state first only for "
    "discovery or after act miss. Never probe with state then state then act "
    "on the same app. Do not verify when act.verified is true — act already "
    "returns the settled tree. "
    "Friction or rooted inefficiency: encode an app-agnostic fast_path grader "
    "that fails the old trace, then retry; no named-app helpers; do not leave "
    "the fix in chat. "
    "Best first: overlay tip lands, then AX press (ax_timeout fail-closed). "
    "Fallback only on miss: at most one fresh state; then screenshot/PIXEL_CLICK. "
    "Never silent pixel fallback. Dispatch ok is never proof; no desktop-global click. "
    "Each batched act captures screenshot_before then screenshot_after (same tool, not a "
    "screenshot catalog). Inspect those pixels before trusting overlay/AX landing; if the "
    "before shot is the wrong window or a Stage Manager thumb, stop and correct bounds — "
    "do not add a third MCP tool. "
    "WhatsApp send/attach: $whatsapp skill, not these tools."
)
_BIDI = dict.fromkeys(
    map(
        ord,
        "\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e"
        "\u2066\u2067\u2068\u2069\ufeff",
    )
)


_DASH = str.maketrans({
    0x2010: "-", 0x2011: "-", 0x2012: "-", 0x2013: "-",
    0x2014: "-", 0x2015: "-", 0x2212: "-", 0xFE63: "-", 0xFF0D: "-",
})


def _norm_text(s: str) -> str:
    return (s or "").translate(_BIDI).translate(_DASH).strip().lower()


def expect_verified(expect: str, tree_text: str) -> bool:
    """Match expect against AXStaticText, AXTextArea, AXTextField, AXCell values — never button titles."""
    needle = _norm_text(expect)
    if not needle:
        return True
    values = [
        _norm_text(v)
        for v in re.findall(
            r'AX(?:StaticText|TextArea|TextField|Cell)[^\n]*value="([^"]*)"',
            tree_text,
        )
    ]
    values.extend(
        _norm_text(v) for v in re.findall(r'AXCell "([^"]*)"', tree_text)
    )
    for v in values:
        if not v:
            continue
        if needle == v:
            return True
        if len(needle) >= 2 and needle in v:
            return True
        if v.endswith("...") and len(v) > 8:
            stem = v[:-3]
            if stem and (needle.startswith(stem) or stem in needle):
                return True
    return False


def expect_is_new(expect: str, before_tree: str, after_tree: str) -> bool:
    """True when expect is a new value. Empty expect skips the check."""
    if not _norm_text(expect):
        return True
    return expect_verified(expect, after_tree) and not expect_verified(
        expect, before_tree
    )


class CUABackend:
    """Lazy JSON-RPC client. Tests replace compact_mcp._BACKEND."""

    def __init__(self) -> None:
        if str(SERVICE_DIR) not in sys.path:
            sys.path.insert(0, str(SERVICE_DIR))
        from cua_client import CUAClient  # noqa: WPS433

        self._client = CUAClient()

    def _cua(self):
        if self._client._sock is None:
            self._client.connect()
        return self._client

    def _reset(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
        self._client.connect()

    def _rpc(self, fn):
        try:
            return fn(self._cua())
        except (ConnectionError, OSError, TimeoutError, socket.timeout):
            self._reset()
            return fn(self._cua())

    def state(self, app: str, **kwargs: Any) -> dict[str, Any]:
        max_elements = int(kwargs.get("max_elements") or DEFAULT_MAX)
        disable_diff = not bool(kwargs.get("diff"))

        def _call(client):
            return client.get_app_state(
                app, disableDiff=disable_diff, maxElements=max_elements
            )

        result = self._rpc(_call)
        if not isinstance(result, dict) or not result.get("text"):
            self._reset()
            result = _call(self._cua())
        if not isinstance(result, dict):
            return {"ok": False, "error": "empty CUAService state", "app": app}
        text = str(result.get("text") or "")
        query = kwargs.get("query")
        if query:
            lines = [ln for ln in text.splitlines() if str(query) in ln]
            text = "\n".join(lines) if lines else text
        return {
            "ok": True,
            "app": result.get("app") or app,
            "text": text,
            "elementCount": result.get("elementCount"),
            "screenshot": result.get("screenshot"),
            "pid": result.get("pid"),
        }

    def act(self, app: str, arguments: dict[str, Any]) -> dict[str, Any]:
        def _run(client):
            steps = arguments.get("steps")
            if not isinstance(steps, list) or not steps:
                steps = [arguments]
            before = client.get_app_state(app, disableDiff=True)
            if isinstance(before, dict) and not before.get("screenshot"):
                before = client.get_app_state(app, disableDiff=True)
            before_text = str(before.get("text") or "") if isinstance(before, dict) else ""
            results: list[dict[str, Any]] = []
            after_new = False
            for step in steps:
                if not isinstance(step, dict):
                    results.append({"ok": False, "error": "step must be an object"})
                    break
                item = self._normalize_step_result(
                    self._one(client, app, step, after_new_document=after_new)
                )
                results.append(item)
                if item.get("ok") is not True:
                    break
                key = str(step.get("key") or "").lower().replace(" ", "")
                if key in {"cmd+n", "command+n", "cmd+t", "command+t"}:
                    after_new = True
                elif step.get("wait") is not None:
                    pass
                else:
                    after_new = False
            after = client.get_app_state(app, disableDiff=True)
            if isinstance(after, dict) and not after.get("screenshot"):
                after = client.get_app_state(app, disableDiff=True)
            text = str(after.get("text") or "") if isinstance(after, dict) else ""
            expect_raw = arguments.get("expect")
            expect = expect_raw if isinstance(expect_raw, str) else ""
            ok = all(item.get("ok") is True for item in results) and bool(results)
            verified = bool(ok) and expect_is_new(expect, before_text, text)
            last = results[-1] if results else {}
            shot_before = before.get("screenshot") if isinstance(before, dict) else None
            shot_after = after.get("screenshot") if isinstance(after, dict) else None
            return {
                "ok": ok,
                "verified": verified,
                "method": last.get("method"),
                "text": text,
                "screenshot_before": shot_before,
                "screenshot_after": shot_after,
                "screenshot": shot_after,
                "results": results,
                "expect": expect or None,
            }

        return self._rpc(_run)

    def _one(
        self, client: Any, app: str, step: dict[str, Any], after_new_document: bool = False
    ) -> dict[str, Any]:
        try:
            return self._dispatch(client, app, step, after_new_document)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _dispatch(
        self,
        client: Any,
        app: str,
        step: dict[str, Any],
        after_new_document: bool = False,
    ) -> dict[str, Any]:
        if step.get("wait") is not None:
            seconds = min(max(float(step["wait"]), 0.0), 45.0)
            time.sleep(seconds)
            return {"ok": True, "method": "wait", "wait": seconds}
        key = step.get("key")
        if key:
            return client.press_key(app, str(key))
        text = step.get("text")
        element = step.get("element")
        if text is not None and element is not None and step.get("label") is None:
            return client.set_value(app, int(element), str(text))
        if text is not None and step.get("label") is None and element is None:
            return client.type_text(
                app, str(text), after_new_document=after_new_document
            )
        action = step.get("action")
        element = step.get("element")
        if action and element is not None:
            return client.call(
                "perform_secondary_action",
                {"app": app, "element_index": int(element), "action": str(action)},
            )
        kwargs: dict[str, Any] = {}
        if step.get("label"):
            kwargs["label"] = str(step["label"])
        if element is not None:
            kwargs["element_index"] = int(element)
        if step.get("x") is not None and step.get("y") is not None:
            kwargs["x"] = float(step["x"])
            kwargs["y"] = float(step["y"])
        if not kwargs:
            return {"ok": False, "error": "act needs label, element, text, key, or x/y"}
        return client.click(app, **kwargs)

    def _normalize_step_result(self, result: Any) -> dict[str, Any]:
        if not isinstance(result, dict):
            return {"ok": False, "error": "step result must be an object"}
        if result.get("method") != "cgevent-click":
            return result
        point = result.get("point")
        if not isinstance(point, dict):
            return {**result, "ok": False, "error": "nonfinite click point"}
        if point.get("x") is None or point.get("y") is None:
            return {**result, "ok": False, "error": "nonfinite click point"}
        return result


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
        },
        {
            "name": "act",
            "description": "Best first: overlay tip lands, then AX press. Batch steps in one call. Returns screenshot_before, screenshot_after, and settled tree (verified when expect matches). Landing check is those two shots, not a screenshot tool. Fallback after one fresh state.",
            "inputSchema": _schema(
                {
                    "app": {"type": "string"},
                    "label": {"type": "string"},
                    "element": {"type": "integer"},
                    "action": {"type": "string"},
                    "text": {"type": "string"},
                    "key": {"type": "string"},
                    "x": {"type": "number"},
                    "y": {"type": "number"},
                    "wait": {"type": "number"},
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "element": {"type": "integer"},
                                "action": {"type": "string"},
                                "text": {"type": "string"},
                                "key": {"type": "string"},
                                "x": {"type": "number"},
                                "y": {"type": "number"},
                                "wait": {"type": "number"},
                            },
                        },
                    },
                    "expect": {"type": ["string", "object"]},
                },
                ["app"],
            ),
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
