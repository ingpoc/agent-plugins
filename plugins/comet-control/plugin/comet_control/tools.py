"""Visible Comet control through an attested native bridge."""
from __future__ import annotations

import json
import os
import re
import socket
import sys
from pathlib import Path
from typing import Any

try:
    from comet_control_constants import get_comet_control_home as _get_comet_control_home  # noqa: F401
    from tools.registry import tool_error, tool_result
except ImportError:
    def tool_result(data: Any) -> str:  # type: ignore[misc]
        return json.dumps({"success": True, **data} if isinstance(data, dict) else {"result": data})

    def tool_error(msg: str, *, success: bool = False, **_: Any) -> str:  # type: ignore[misc]
        return json.dumps({"success": success, "error": msg})

PLUGIN_DIR = Path(__file__).resolve().parent
WIP_ROOT = PLUGIN_DIR.parents[1]
DEFAULT_TIMEOUT_SECONDS = 45
VISUAL_FOCUS_TRANSPORT_MARGIN_SECONDS = 35
MAX_ACTIONS = 20
MAX_TEXT_CHARS = 120_000
DEFAULT_LEASE_TTL_SECONDS = 30 * 60

_SOCKET_PATH = Path(os.environ.get("COMET_CONTROL_BRIDGE_SOCKET",
    str(WIP_ROOT / "run" / "comet-control.sock")))
_LEASE_TOKENS: dict[str, str] = {}


# ── Shared helpers ────────────────────────────────────────────────────────────

def _coerce_positive_int(raw: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(raw)
    except Exception:
        value = default
    return max(minimum, min(maximum, value))


def _normalise_action(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    t = str(raw.get("type") or "").strip()
    if not t:
        return None
    action = dict(raw)
    action["type"] = t
    return action


def _normalise_actions(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("actions must be a list")
    return [a for item in raw[:MAX_ACTIONS] if (a := _normalise_action(item)) is not None]


def _safe_identity(raw: Any, *, fallback: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._:@/-]+", "-", str(raw or "").strip()).strip("-._")
    return (value or fallback)[:96]


def _is_private_key(key: Any) -> bool:
    return str(key).replace("_", "").lower() == "leasetoken"


def _redact_private(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _redact_private(item)
            for key, item in value.items()
            if not _is_private_key(key)
        }
    if isinstance(value, list):
        return [_redact_private(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_private(item) for item in value]
    if isinstance(value, str):
        redacted = value
        for token in _LEASE_TOKENS.values():
            if token:
                redacted = redacted.replace(token, "[redacted]")
        return redacted
    return value


def _agent_identity(args: dict[str, Any], task_id: str | None) -> tuple[str, str, str]:
    """Return stable machine id, human label, and Comet window label.

    Explicit agent/session ids work across all harnesses. Codex/Comet Control callers that
    provide a task id get isolation without having to invent another identifier.
    Lease operations deliberately have no process-wide fallback: two agents can
    share one plugin process, so a PID is not a safe ownership identity.
    """
    explicit_session = str(args.get("session_id") or "").strip()
    explicit_agent = str(args.get("agent_id") or "").strip()
    explicit_name = str(args.get("session_name") or "").strip()
    raw_id = explicit_session or explicit_agent or task_id or os.environ.get("COMET_CONTROL_AGENT_ID")
    if not raw_id and explicit_name and explicit_name != "Comet Control":
        raw_id = explicit_name
    agent_id = _safe_identity(raw_id, fallback="")
    label = str(args.get("agent_label") or os.environ.get("COMET_CONTROL_AGENT_LABEL") or "").strip()
    label = (label or explicit_name or agent_id or "Comet Control Agent")[:80]
    group = explicit_name if explicit_name and explicit_name != "Comet Control" else f"Comet Control · {label}"
    return agent_id, label, group[:80]


# ── Extension socket path ─────────────────────────────────────────────────────

def _socket_reachable(*, timeout: float = 2.0) -> bool:
    if not _SOCKET_PATH.exists():
        return False
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(str(_SOCKET_PATH))
        s.close()
        return True
    except OSError:
        return False


def _call_socket(payload: dict[str, Any], *, timeout_seconds: int) -> dict[str, Any]:
    """Raw socket call — no retry logic."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    # The extension owns the operation deadline. Allow the broker a small
    # envelope to materialize screenshots and frame the response.
    # The broker may wait up to 30 seconds for a disjoint macOS CUA focus
    # slice, then gives the extension the full requested browser deadline.
    client.settimeout(timeout_seconds + VISUAL_FOCUS_TRANSPORT_MARGIN_SECONDS)
    with client:
        client.connect(str(_SOCKET_PATH))
        client.sendall(json.dumps(payload, ensure_ascii=False).encode())
        chunks: list[bytes] = []
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    raw = b"".join(chunks).decode()
    return json.loads(raw) if raw else {
        "success": False, "error_code": "EMPTY_RESPONSE",
        "error": "Extension bridge returned no output",
    }


def _run_extension_bridge(request: dict[str, Any], *, timeout_seconds: int) -> dict[str, Any]:
    request_action = str(request.get("action") or "run")
    payload: dict[str, Any] = {
        "type": request_action,
        "timeoutSeconds": timeout_seconds,
    }
    if request_action == "run":
        payload.update({
            "actions": request.get("actions", []),
            "sessionName": request.get("sessionName"),
            "taskId": request.get("taskId"),
            "maxTextChars": request.get("maxTextChars", 20_000),
        })
    for key in (
        "sessionId", "leaseToken", "sessionName", "taskId", "agentId", "agentLabel", "url",
        "isolation", "ttlSeconds", "keepWindow", "filter", "limit", "query",
        "queries", "startTime", "endTime",
    ):
        if request.get(key) is not None:
            payload[key] = request[key]

    if not _SOCKET_PATH.exists():
        return {
            "success": False,
            "error_code": "SOCKET_DOWN",
            "error": f"Comet Control runtime is down; run {WIP_ROOT / 'scripts' / 'launch-wip-comet.sh'}",
            "socket": str(_SOCKET_PATH),
        }
    try:
        return _call_socket(payload, timeout_seconds=timeout_seconds)
    except socket.timeout:
        return {
            "success": False,
            "error_code": "BRIDGE_TIMEOUT",
            "error": f"Comet Control request exceeded {timeout_seconds}s",
        }
    except OSError as error:
        return {
            "success": False,
            "error_code": "SOCKET_DOWN",
            "error": f"Comet Control bridge is unreachable: {error}",
            "socket": str(_SOCKET_PATH),
        }


def _extension_health(*, timeout_seconds: int) -> dict[str, Any]:
    reachable = _socket_reachable()
    result: dict[str, Any] = {
        "success": True,
        "bridge": "extension",
        "socket": str(_SOCKET_PATH),
        "preflight_ok": reachable,
        "ready": False,
    }
    if not reachable:
        result["error_code"] = "SOCKET_DOWN"
        return result
    broker = _run_extension_bridge({"action": "broker_status"}, timeout_seconds=timeout_seconds)
    if not broker.get("success") or not (broker.get("broker") or {}).get("runtime_verified"):
        result["error_code"] = broker.get("error_code", "RUNTIME_OWNER_UNVERIFIED")
        result["error"] = broker.get("error", "Comet Control broker runtime is not attested")
        return result
    status = _run_extension_bridge({"action": "status"}, timeout_seconds=timeout_seconds)
    result["ready"] = bool(status.get("success"))
    result["runtime"] = broker.get("broker", {})
    result["active_agent_sessions"] = status.get("active_agent_sessions", 0)
    result["cua_claim"] = status.get("cua_claim")
    result["operator_paused"] = status.get("paused", False)
    result["protocol_version"] = status.get("protocol_version")
    result["extension_version"] = status.get("extension_version")
    result["extension_build_sha256"] = status.get("extension_build_sha256")
    result["capabilities"] = status.get("capabilities", [])
    if not status.get("success"):
        result["error_code"] = status.get("error_code", "BRIDGE_ERROR")
        result["error"] = status.get("error", "Bridge status failed")
    return result


def _check_comet_control_available() -> bool:
    """Cheap registry probe; full diagnostics remain an explicit tool action."""
    return _socket_reachable()


# ── Diagnostics (filesystem/manifest checks, bridge-independent) ──────────────

def _diagnostics() -> dict[str, Any]:
    try:
        sys.path.insert(0, str(PLUGIN_DIR))
        from diagnostics import run_diagnostics  # type: ignore[import]
        return run_diagnostics()
    except Exception as exc:
        reachable = _socket_reachable()
        return {
            "success": True,
            "preflight_ok": reachable,
            "blocking_checks": [] if reachable else ["bridge_socket_reachable"],
            "diagnostics_degraded": str(exc),
        }


def _install_info() -> dict[str, Any]:
    return {
        "success": True,
        "bridge": "extension",
        "socket": str(_SOCKET_PATH),
        "steps": [
            f"1. Start the Comet-only broker: {WIP_ROOT / 'scripts' / 'ensure-wip-broker.sh'} start",
            f"2. Launch the logged-in runtime: {WIP_ROOT / 'scripts' / 'launch-wip-comet.sh'}",
            f"3. Load unpacked once from {WIP_ROOT / 'plugin' / 'comet_control' / 'extension'} only in Comet.",
            "4. Require broker status, then session_preflight before every run.",
        ],
    }


# ── Tool schema ───────────────────────────────────────────────────────────────

COMET_CONTROL_BROWSER_SCHEMA = {
    "name": "comet_control_browser",
    "description": (
        "Control the logged-in Comet browser via the isolated extension bridge. "
        "Requires Comet Control installed only in Comet. "
        "Use for browser testing, authenticated tasks, screenshots, UI verification, "
        "and any interaction with authenticated browser state. Each agent must call "
        "preflight once, operate with run, then call closeout."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "install_info", "preflight", "diagnose", "health", "status",
                    "sessions", "user_tabs", "history", "run", "closeout",
                ],
                "description": (
                    "status/health: check if extension bridge is reachable. "
                    "diagnose: detailed global health. preflight: acquire an isolated agent window. "
                    "sessions: list active agent leases. closeout: release this agent's lease/window. "
                    "user_tabs: read-only list of visible top-level tabs. "
                    "history: run one focused browser-history lookup. "
                    "install_info: setup instructions. "
                    "run: execute browser actions."
                ),
                "default": "run",
            },
            "url": {
                "type": "string",
                "description": "Navigate to this URL before executing actions.",
            },
            "actions": {
                "type": "array",
                "description": (
                    "Ordered browser actions. Batch everything into one call. "
                    "Supported types: "
                    "page_context (compact overview ~1KB), "
                    "snapshot (interactive elements with selectors), "
                    "text (full visible text), "
                    "screenshot, "
                    "zoom {x0, y0, x1, y1, quality?} (region-specific JPEG — use instead of full screenshot when you only care about part of the page), "
                    "goto {url, waitMs?, reload?}, "
                    "back / forward / reload_page {waitMs?, bypassCache?}, "
                    "wait {ms}, "
                    "wait_for_selector {selector, timeout?} (poll until element present — use after goto instead of fixed wait), "
                    "wait_for_url_change {from_url?, timeout?} (poll until URL changes — use after form submit / login redirect), "
                    "click_text {text}, "
                    "click_selector {selector}, "
                    "fill_selector {selector, value, append?}, "
                    "locator {locator:{by:css|text|role|label|placeholder|testid,...}, operation} (semantic and frame-scoped interaction), "
                    "evaluate {expression} (runtime-enforced read-only), "
                    "console_tail {limit?, levels?, filter?, clear?} (filtered console detail; defaults to errors/warns), "
                    "network_watch {clear?} (opt in before the navigation/action being diagnosed), "
                    "network_summary (compact counts + last error), "
                    "network_errors {limit?, kinds?, filter?, clear?} (details only after summary), "
                    "cdp_send {method, params?}; cdp_events {afterSequence?, methods?, limit?, timeoutMs?}, "
                    "dialog_get; dialog_handle {accept?, dismiss?, promptText?}, "
                    "clipboard_read_text; clipboard_write_text {text}; clipboard_read {includeData?}; clipboard_write {items}, "
                    "upload_files {selector?, path|paths}; download_click/download_media {locator,...}, "
                    "page_assets_list {limit?, includeInlineSvg?}; page_assets_bundle {inventoryId, assetIds?|kinds?}, "
                    "viewport_set {width,height,deviceScaleFactor?,mobile?}; viewport_reset, "
                    "cursor_move {x,y}, cursor_type {text}, cursor_key {key, modifiers?}, "
                    "cursor_scroll {deltaX?, deltaY?}, cursor_drag {x,y,duration?}, "
                    "cursor_click, cursor_double_click, cursor_right_click, cursor_hide, "
                    "close_tab."
                ),
                "items": {"type": "object"},
            },
            "timeout_seconds": {
                "type": "integer",
                "description": "Max wall time per bridge call.",
                "default": DEFAULT_TIMEOUT_SECONDS,
            },
            "max_text_chars": {
                "type": "integer",
                "description": "Max characters for text/snapshot actions.",
                "default": 20000,
            },
            "session_name": {
                "type": "string",
                "description": (
                    "Human-visible Comet window label. Machine ownership uses session_id, "
                    "not this display value."
                ),
            },
            "session_id": {
                "type": "string",
                "description": "Stable unique machine identity for this agent/browser session.",
            },
            "agent_id": {
                "type": "string",
                "description": "Cross-harness agent identity; used as session_id when omitted.",
            },
            "agent_label": {
                "type": "string",
                "description": "Short operator-facing name shown below this agent's cursor.",
            },
            "ttl_seconds": {
                "type": "integer",
                "description": "Idle lease lifetime before crash cleanup (30-3600 seconds).",
                "default": DEFAULT_LEASE_TTL_SECONDS,
            },
            "filter": {
                "type": "string",
                "description": "Optional title/URL filter for user_tabs.",
            },
            "query": {
                "type": "string",
                "description": "Focused text query for history.",
            },
            "queries": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Small set of focused history terms.",
            },
            "limit": {
                "type": "integer",
                "description": "Bounded result count for user_tabs or history.",
            },
            "start_time": {
                "type": "number",
                "description": "History lower bound as Unix milliseconds.",
            },
            "end_time": {
                "type": "number",
                "description": "History upper bound as Unix milliseconds.",
            },
        },
        "additionalProperties": False,
    },
}


# ── Handler ───────────────────────────────────────────────────────────────────

def _handle_comet_control_browser(args: dict, task_id: str | None = None, **_: Any) -> str:
    args = args or {}
    action = str(args.get("action") or "run").strip().lower()
    agent_id, agent_label, session_name = _agent_identity(args, task_id)
    timeout_seconds = _coerce_positive_int(
        args.get("timeout_seconds"),
        default=DEFAULT_TIMEOUT_SECONDS, minimum=5, maximum=180,
    )
    if action in {"preflight", "run", "closeout"} and not agent_id:
        return tool_error(
            "Leased browser work requires a stable task_id, session_id, or agent_id",
            success=False,
            error_code="AGENT_ID_REQUIRED",
        )

    # ── Static actions (no bridge needed) ─────────────────────────────────────
    if action == "install_info":
        return tool_result(_install_info())

    if action == "diagnose":
        diag = _diagnostics()
        ext = _extension_health(timeout_seconds=timeout_seconds)
        return tool_result({
            "extension": ext,
            "diagnostics": diag,
            "ready": ext.get("ready", False),
        })

    if action == "preflight":
        diag = _diagnostics()
        ext = _extension_health(timeout_seconds=timeout_seconds)
        if not ext.get("ready"):
            return tool_error(
                ext.get("error") or "Comet Control bridge is not ready",
                success=False,
                error_code=ext.get("error_code", "BRIDGE_ERROR"),
            )
        request = {
            "action": "session_preflight",
            "sessionId": agent_id,
            "agentId": agent_id,
            "agentLabel": agent_label,
            "sessionName": session_name,
            "leaseToken": _LEASE_TOKENS.get(agent_id),
            "url": str(args.get("url") or "").strip() or None,
            "isolation": "window",
            "ttlSeconds": _coerce_positive_int(
                args.get("ttl_seconds"),
                default=DEFAULT_LEASE_TTL_SECONDS, minimum=30, maximum=3600,
            ),
        }
        session = _run_extension_bridge(request, timeout_seconds=timeout_seconds)
        token = str(session.pop("lease_token", "") or "")
        if token:
            # A retryable preflight failure can retain a partial exact target.
            # Preserve its private cleanup capability without exposing it in
            # the tool result so the same agent can retry preflight/closeout.
            _LEASE_TOKENS[agent_id] = token
        if not session.get("success"):
            return tool_error(
                session.get("error") or "Agent browser preflight failed",
                success=False,
                error_code=session.get("error_code", "PREFLIGHT_ERROR"),
            )
        return tool_result(_redact_private({
            "extension": ext,
            "diagnostics": diag,
            "ready": True,
            "session": session,
        }))

    # ── Health / status ────────────────────────────────────────────────────────
    if action in ("health", "status"):
        if args.get("session_id") or args.get("agent_id"):
            return tool_result(_redact_private(_run_extension_bridge({
                "action": "sessions",
                "sessionId": agent_id,
            }, timeout_seconds=timeout_seconds)))
        return tool_result(_extension_health(timeout_seconds=timeout_seconds))

    if action == "sessions":
        return tool_result(_redact_private(_run_extension_bridge({
            "action": "sessions",
            "sessionId": str(args.get("session_id") or "").strip() or None,
        }, timeout_seconds=timeout_seconds)))

    if action == "user_tabs":
        return tool_result(_redact_private(_run_extension_bridge({
            "action": "user_tabs",
            "filter": str(args.get("filter") or "").strip() or None,
            "limit": _coerce_positive_int(args.get("limit"), default=100, minimum=1, maximum=500),
        }, timeout_seconds=timeout_seconds)))

    if action == "history":
        return tool_result(_redact_private(_run_extension_bridge({
            "action": "history",
            "query": str(args.get("query") or "").strip() or None,
            "queries": args.get("queries") or None,
            "limit": _coerce_positive_int(args.get("limit"), default=50, minimum=1, maximum=500),
            "startTime": args.get("start_time"),
            "endTime": args.get("end_time"),
        }, timeout_seconds=timeout_seconds)))

    if action == "closeout":
        result = _run_extension_bridge({
            "action": "session_closeout",
            "sessionId": agent_id,
            "leaseToken": _LEASE_TOKENS.get(agent_id),
            "keepWindow": False,
        }, timeout_seconds=timeout_seconds)
        if result.get("success"):
            public_result = _redact_private(result)
            _LEASE_TOKENS.pop(agent_id, None)
            return tool_result(public_result)
        return tool_error(
            result.get("error") or "Agent browser closeout failed",
            success=False,
            error_code=result.get("error_code", "CLOSEOUT_ERROR"),
        )

    # ── Run ────────────────────────────────────────────────────────────────────
    if action != "run":
        return tool_error(f"Unknown action: {action!r}", success=False)

    try:
        actions = _normalise_actions(args.get("actions"))
    except ValueError as e:
        return tool_error(str(e), success=False)
    if not actions:
        actions = [{"type": "page_context"}]

    url = str(args.get("url") or "").strip()
    if url:
        actions.insert(0, {"type": "goto", "url": url})
    request: dict[str, Any] = {
        "action": "run",
        "actions": actions,
        "sessionName": session_name,
        "taskId": task_id or "",
        "agentId": agent_id,
        "agentLabel": agent_label,
        "maxTextChars": _coerce_positive_int(
            args.get("max_text_chars"), default=20_000, minimum=1_000, maximum=MAX_TEXT_CHARS,
        ),
    }
    lease_token = str(_LEASE_TOKENS.get(agent_id) or "")
    if not lease_token:
        return tool_error(
            "WIP Comet Control run requires preflight for this session before any browser action",
            success=False,
            error_code="LEASE_REQUIRED",
        )
    request["sessionId"] = agent_id
    request["leaseToken"] = lease_token
    result = _run_extension_bridge(request, timeout_seconds=timeout_seconds)

    if not result.get("success"):
        return tool_error(
            result.get("error") or "Bridge call failed",
            success=False,
            error_code=result.get("error_code", "BRIDGE_ERROR"),
        )
    return tool_result(_redact_private(result))
