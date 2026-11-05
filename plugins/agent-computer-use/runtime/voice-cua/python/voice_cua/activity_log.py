"""Structured Samantha activity log — tools, apps, errors (no secret values)."""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from voice_cua import log_rotation

_DEFAULT = Path.home() / ".cache/macos-cua/samantha-activity.jsonl"
_lock = threading.Lock()
_SECRET_KEYS = frozenset({"value", "secret", "password", "api_key", "token"})
_DIAGNOSTIC_SECRET = re.compile(
    r"(?i)([A-Z0-9_]*(?:bearer|token|password|secret|api[_-]?key|authorization|cookie)(?![A-Z0-9_]))"
    r"([\"']?\s*[=:]\s*|\s+)(?!\[redacted\])(?:\"[^\"]*\"|'[^']*'|\S+)"
)
_DIAGNOSTIC_TOKEN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|xox[abpr]-[A-Za-z0-9-]{8,}|AKIA[0-9A-Z]{12,}|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9._-]+)")
_DIAGNOSTIC_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_LEDGER_DETAIL_KEYS = frozenset({
    "ok", "status", "decision_id", "revision", "decision_ids", "decisions", "data", "matches",
    "match_count", "summary", "decision", "situation", "action", "rationale", "applicability",
    "scope", "rule", "entities", "sensitivity", "constraints", "reason", "changed_choice",
    "supersedes", "superseded_by", "source", "tags", "created_at", "updated_at", "record_type",
})
_TASK_OUTCOME_TOOLS = frozenset({"cua_act", "comet_act", "system_volume"})
_process_session_id = uuid.uuid4().hex[:8]  # fallback when startup_trace never ran


def log_path() -> Path:
    raw = os.environ.get("VOICE_CUA_ACTIVITY_LOG", "").strip()
    return Path(raw).expanduser() if raw else _DEFAULT


def current_session_id() -> str:
    """Samantha session id for every row (set by ``startup_trace.begin``).

    Read from the environment so this module never imports ``startup_trace``
    (which imports us). Falls back to one stable per-process id.
    """
    sid = os.environ.get("VOICE_CUA_STARTUP_ID", "").strip()
    return sid or _process_session_id


def _append_row(entry: dict[str, Any]) -> None:
    """Atomic append: the whole line goes out in ONE O_APPEND write(2), rotating at 50 MB (log_rotation)."""
    line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
    with _lock:
        log_rotation.append(log_path(), line.encode("utf-8"))


def log_event(kind: str, **fields: Any) -> None:
    """Append one JSON line. Safe fields only — never log secret values."""
    entry: dict[str, Any] = {"ts": time.time(), "kind": kind, "session_id": current_session_id()}
    for key, value in fields.items():
        if value is None:
            continue
        if key in _SECRET_KEYS:
            continue
        entry[key] = value
    _append_row(entry)


# response.usage → row field. Integers only; anything missing is omitted.
_USAGE_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("input_tokens", ("input_tokens",)),
    ("output_tokens", ("output_tokens",)),
    ("cached_tokens", ("input_token_details", "cached_tokens")),
    ("input_text_tokens", ("input_token_details", "text_tokens")),
    ("input_audio_tokens", ("input_token_details", "audio_tokens")),
    ("output_text_tokens", ("output_token_details", "text_tokens")),
    ("output_audio_tokens", ("output_token_details", "audio_tokens")),
    ("total_tokens", ("total_tokens",)),
    # Responses API (GPT-Live backend) spelling.
    ("cached_tokens", ("input_tokens_details", "cached_tokens")),
    ("reasoning_tokens", ("output_tokens_details", "reasoning_tokens")),
)


def _usage_int(usage: Any, path: tuple[str, ...]) -> int | None:
    node = usage
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    if isinstance(node, bool) or not isinstance(node, (int, float)):
        return None
    if node != node or node < 0:  # NaN / negative → not a count
        return None
    return int(node)


def _call_ids(response: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for item in response.get("output") or []:
        if isinstance(item, dict) and item.get("type") == "function_call":
            call_id = item.get("call_id")
            if isinstance(call_id, str) and call_id:
                out.append(call_id[:64])
    return out


_LABEL_KEYS = ("provider", "model", "live_model", "tier", "delegation_id")


def usage_row(
    response: dict[str, Any] | None,
    *,
    latency_ms: int | None = None,
    labels: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the ``usage`` row for one ``response.done`` (numbers, ids and model labels only).

    ``labels``: provider / model / live_model / tier / delegation_id (short strings only).
    GPT-Live backend rows carry the backend model + tier; live voice minutes are logged
    separately as ``live_usage`` rows (voice_seconds).
    """
    response = response if isinstance(response, dict) else {}
    usage = response.get("usage")
    now = time.time()
    # Numeric epoch ts like every other row (0.2.85); the readable local time rides along as ts_iso.
    row: dict[str, Any] = {
        "kind": "usage",
        "ts": now,
        "ts_iso": datetime.fromtimestamp(now).astimezone().isoformat(timespec="milliseconds"),
        "session_id": current_session_id(),
    }
    for key in _LABEL_KEYS:
        value = (labels or {}).get(key)
        if isinstance(value, str) and value:
            row[key] = value[:64]
    response_id = response.get("id")
    if isinstance(response_id, str) and response_id:
        row["response_id"] = response_id[:64]
    call_ids = _call_ids(response)
    if call_ids:
        row["call_id"] = call_ids[0]
        if len(call_ids) > 1:
            row["call_ids"] = call_ids
    if isinstance(latency_ms, int) and not isinstance(latency_ms, bool) and latency_ms >= 0:
        row["latency_ms"] = latency_ms
    for field, path in _USAGE_FIELDS:
        value = _usage_int(usage, path)
        if value is not None and field not in row:
            row[field] = value
    return row


def log_usage(
    response: dict[str, Any] | None,
    *,
    latency_ms: int | None = None,
    labels: dict[str, Any] | None = None,
) -> None:
    """Append one ``usage`` row (token counts + ids only — never text or transcripts)."""
    row = usage_row(response, latency_ms=latency_ms, labels=labels)
    with _usage_lock:
        totals = _usage_totals.setdefault(row["session_id"], {"responses": 0})
        totals["responses"] += 1
        for field in _TOTAL_FIELDS:
            if isinstance(row.get(field), int):
                totals[field] = totals.get(field, 0) + row[field]
    _append_row(row)


_TOTAL_FIELDS = ("input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens", "total_tokens")
_usage_lock = threading.Lock()
_usage_totals: dict[str, dict[str, int]] = {}


def log_usage_final(*, reason: str = "session_close", **extra: Any) -> None:
    """One closing ``usage`` row per session: final=True plus the session's token totals (numbers only)."""
    session = current_session_id()
    with _usage_lock:
        totals = dict(_usage_totals.pop(session, {"responses": 0}))
    now = time.time()
    row: dict[str, Any] = {"kind": "usage", "ts": now,
                           "ts_iso": datetime.fromtimestamp(now).astimezone().isoformat(timespec="milliseconds"),
                           "session_id": session, "final": True, "reason": reason[:40], **totals}
    for key, value in extra.items():
        if value is not None and key not in _SECRET_KEYS:
            row[key] = value
    _append_row(row)


# Per-thread tool-call context: realtime_session runs each tool in its own
# thread, so tool_result rows can carry call_id/response_id without changing
# the dispatch() signature.
_tool_ctx = threading.local()


class tool_context:  # noqa: N801 — used as a context manager
    def __init__(self, *, call_id: str = "", response_id: str = "") -> None:
        self._ids = {"call_id": call_id or None, "response_id": response_id or None}
        self._prev: dict[str, Any] | None = None

    def __enter__(self) -> "tool_context":
        self._prev = getattr(_tool_ctx, "ids", None)
        _tool_ctx.ids = self._ids
        return self

    def __exit__(self, *_exc: object) -> None:
        _tool_ctx.ids = self._prev


def _current_tool_ids() -> dict[str, Any]:
    return dict(getattr(_tool_ctx, "ids", None) or {})


def _diagnostic_text(value: Any, *, limit: int = 4096) -> str:
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, default=str)
    value = value.replace(str(Path.home()), "~")
    value = _DIAGNOSTIC_SECRET.sub(lambda match: match.group(1) + match.group(2) + "[redacted]", value)
    value = _DIAGNOSTIC_TOKEN.sub("[redacted]", value)
    value = _DIAGNOSTIC_EMAIL.sub("[email]", value)
    if len(value) > limit:
        half = limit // 2
        value = value[:half] + "\n…[diagnostic truncated]…\n" + value[-half:]
    return value


def _ledger_detail_value(value: Any, depth: int = 0) -> Any:
    if depth > 5:
        return "[depth limit]"
    if isinstance(value, dict):
        return {key: _ledger_detail_value(item, depth + 1) for key, item in value.items()
                if key in _LEDGER_DETAIL_KEYS and key not in _SECRET_KEYS}
    if isinstance(value, list):
        return [_ledger_detail_value(item, depth + 1) for item in value[:3]]
    if isinstance(value, str):
        return _diagnostic_text(value, limit=500)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return None


def _ledger_result_detail(result: dict[str, Any]) -> str | None:
    detail = _ledger_detail_value(result)
    if not isinstance(detail, dict) or not detail:
        return None
    return _diagnostic_text(json.dumps(detail, ensure_ascii=False, separators=(",", ":")), limit=4096)


def _tool_error_details(name: str, result: dict[str, Any], error_type: str = "") -> str | None:
    fields = [f"tool: {name}"]
    if error_type:
        fields.append(f"error_type: {error_type}")
    for key in ("error", "message", "detail", "trace", "traceback", "stack"):
        value = result.get(key)
        if isinstance(value, str) and value:
            fields.append(f"{key}: {value}")
        elif isinstance(value, dict):
            for subkey in ("type", "code", "message", "detail", "trace", "stack"):
                detail = value.get(subkey)
                if isinstance(detail, str) and detail:
                    fields.append(f"{key}.{subkey}: {detail}")
    failure = result.get("failure")
    if isinstance(failure, dict):
        for key in ("failed_step", "message", "detail", "trace"):
            value = failure.get(key)
            if isinstance(value, str) and value:
                fields.append(f"failure.{key}: {value}")
    return _diagnostic_text("\n".join(fields)) if len(fields) > 1 else None


_encoder: Any = False  # False = not probed yet; None = tiktoken unavailable


def _result_tokens(text: str) -> int | None:
    """o200k token count only when tiktoken already has o200k loaded in-process.

    Never installs, imports-on-demand or downloads BPE data: logging must stay
    cheap and offline. Otherwise callers get ``result_chars`` only.
    """
    global _encoder
    if _encoder is False or _encoder is None:
        mod = sys.modules.get("tiktoken.registry")
        found = getattr(mod, "ENCODINGS", {}).get("o200k_base") if mod is not None else None
        _encoder = found if found is not None else None
    if _encoder is None:
        return None
    try:
        return len(_encoder.encode(text))
    except Exception:  # noqa: BLE001
        return None


def result_size(result: Any) -> dict[str, int]:
    """Size of the tool output as the model sees it (json.dumps, like function_call_output)."""
    try:
        text = json.dumps(result)
    except Exception:  # noqa: BLE001
        text = json.dumps(result, default=str)
    out = {"result_chars": len(text)}
    tokens = _result_tokens(text)
    if tokens is not None:
        out["result_tokens"] = tokens
    return out


_TYPED_KEYS = ("text", "value", "query", "url", "path", "prefix", "suffix")


def plan_shape(arguments: dict[str, Any], *, tool: str = "cua_act") -> list[str]:
    """Audit view of a cua_act/cua_state call: op + target per step, typed content as a length.

    ``op:key cmd+n`` / ``op:type_text text=27ch`` / ``op:click label=Save``. UI labels and key
    chords are kept (they are what an auditor needs to spot a wrong target); anything the
    user could have dictated (text/value/query/url/path) is reduced to its length.
    """
    steps = arguments.get("steps") if isinstance(arguments.get("steps"), list) else [arguments]
    out: list[str] = []
    for step in steps[:12]:
        if not isinstance(step, dict):
            continue
        bits = [f"op:{step.get('op') or ('state' if tool == 'cua_state' else '?')}"]
        if step.get("key"):
            bits.append(str(step["key"])[:24])
        if step.get("label"):
            bits.append(f"label={str(step['label'])[:40]}")
        if step.get("element") is not None:
            bits.append(f"el={step['element']}")
        for key in _TYPED_KEYS:
            if step.get(key):
                bits.append(f"{key}={len(str(step[key]))}ch")
        out.append(" ".join(bits))
    return out


def log_tool_start(name: str, arguments: dict[str, Any]) -> None:
    app = str(arguments.get("app") or "") if name != "ledger_task" else ""
    operation = arguments.get("operation") if name == "ledger_task" else None
    ledger_operation = operation if operation in ("lookup", "capture", "recall", "preference") else "unknown"
    log_event(
        "tool_start",
        **_current_tool_ids(),
        tool=name,
        app=app or None,
        step=(str(arguments.get("step_label") or arguments.get("label") or "") or None) if name != "ledger_task" else None,
        ledger_operation=ledger_operation if name == "ledger_task" else None,
        has_expect=bool(arguments.get("expect")) or None,
        allow_unverified=arguments.get("allow_unverified") is True or None,
        plan=plan_shape(arguments, tool=name) if name in ("cua_act", "cua_state") else None,
    )


def log_tool_result(name: str, result: dict[str, Any], *, app: str = "", operation: Any = None) -> None:
    if result.get("cancelled") is True and result.get("error_type") == "user_cancelled":
        log_event("tool_cancelled", **_current_tool_ids(), tool=name, classification="user_cancelled")
        return
    ok = bool(result.get("ok"))
    err = str(result.get("error") or "")
    error_type = str(result.get("error_type") or "")
    if not ok and (result.get("needs_confirmation") or error_type.startswith("needs_confirm")):
        # Risk gate asked the user first: an expected step, not a failed tool.
        log_event("tool_needs_confirmation", **_current_tool_ids(), tool=name, app=(app or None) if name != "ledger_task" else None,
                  classification="needs_confirmation")
        return
    if not error_type:
        error_type = "user_denied" if err == "user denied" else ("act_miss" if not ok else "")
    ledger = name == "ledger_task"
    category = None
    if ledger and not ok:
        category = ({"ledger_invalid_argument": "invalid_argument", "ledger_unavailable": "unavailable",
                     "ledger_outcome_unknown": "outcome_unknown"}.get(error_type, "other"))
        if category == "invalid_argument" and "find needs query words" in err:
            category = "invalid_query_terms"
    diagnostic = _tool_error_details(name, result, error_type) if not ok else None
    decisions = result.get("decisions")
    decision_count = min(len(decisions), 3) if isinstance(decisions, list) else (1 if result.get("decision_id") else 0)
    ledger_status = result.get("status")
    ledger_status = ledger_status if isinstance(ledger_status, str) and ledger_status in {"found", "none", "saved", "skipped", "cancelled"} else "other"
    log_event(
        "tool_result" if ok else "tool_error",
        **_current_tool_ids(),
        tool=name,
        app=(app or None) if not ledger else None,
        ledger_operation=(operation if operation in ("lookup", "capture", "recall", "preference") else "unknown") if ledger else None,
        ledger_error_category=category,
        ok=ok,
        verified=result.get("verified") if not ledger and "verified" in result else None,
        dispatched=result.get("dispatched") if not ledger and "dispatched" in result else None,
        completion=result.get("completion") if not ledger else None,
        error=_diagnostic_text(err, limit=240) if err and not ledger else None,
        error_type=(error_type or None) if not ledger else (error_type if error_type in {"ledger_invalid_argument", "ledger_unavailable", "ledger_outcome_unknown"} else ("other" if not ok else None)),
        hint=(_diagnostic_text(str(result.get("hint") or ""), limit=160) or None) if not ledger else None,
        failed_step=(result.get("failure") or {}).get("failed_step") if not ledger and isinstance(result.get("failure"), dict) else None,
        error_details=diagnostic,
        ledger_result_status=ledger_status if ledger else None,
        ledger_decision_count=decision_count if ledger else None,
        ledger_result_detail=_ledger_result_detail(result) if ledger else None,
        **result_size(result),
    )


def log_exception(name: str, exc: BaseException, *, app: str = "") -> None:
    details = _diagnostic_text(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
    log_event(
        "tool_error",
        **_current_tool_ids(),
        tool=name,
        app=(app or None) if name != "ledger_task" else None,
        ok=False,
        error=_diagnostic_text(str(exc), limit=240) if name != "ledger_task" else None,
        error_type=type(exc).__name__,
        error_details=details,
    )


def tail_events(*, limit: int = 80) -> list[dict[str, Any]]:
    path = log_path()
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    out: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def log_task_outcome(*, check_id: str, outcome: str, evidence_class: str, run_id: str,
                     response_id: str = "", tools: list[str] | tuple[str, ...] = ()) -> None:
    """Emit only externally verified check outcomes, never inferred model completion."""
    import re
    if (len(check_id) > 48 or not re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", check_id)
            or outcome not in {"verified_success", "verified_incomplete", "unverified", "cancelled"}
            or evidence_class not in {"app_readback", "browser_inventory", "local_login", "combined"}
            or not re.fullmatch(r"[0-9a-f]{8,64}", run_id)
            or not isinstance(response_id, str)
            or (response_id and (len(response_id) > 160 or not re.fullmatch(r"[A-Za-z0-9_-]+", response_id)))
            or not isinstance(tools, (list, tuple))
            or any(tool not in _TASK_OUTCOME_TOOLS for tool in tools)):
        raise ValueError("invalid task outcome metadata")
    log_event("task_outcome", check_id=check_id, outcome=outcome,
              evidence_class=evidence_class, run_id=run_id, response_id=response_id or None,
              tools=sorted(set(tools)) or None)
