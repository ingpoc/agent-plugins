"""Structured Samantha activity log — tools, apps, errors (no secret values)."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

_DEFAULT = Path.home() / ".cache/macos-cua/samantha-activity.jsonl"
_lock = threading.Lock()
_SECRET_KEYS = frozenset({"value", "secret", "password", "api_key", "token"})
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
    """Atomic append: the whole line goes out in ONE O_APPEND write(2)."""
    line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = line.encode("utf-8")
    with _lock:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)


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
    row: dict[str, Any] = {
        "kind": "usage",
        "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
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
    _append_row(usage_row(response, latency_ms=latency_ms, labels=labels))


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
    app = str(arguments.get("app") or "")
    log_event(
        "tool_start",
        tool=name,
        app=app or None,
        step=str(arguments.get("step_label") or arguments.get("label") or "") or None,
        has_expect=bool(arguments.get("expect")) or None,
        allow_unverified=arguments.get("allow_unverified") is True or None,
        plan=plan_shape(arguments, tool=name) if name in ("cua_act", "cua_state") else None,
    )


def log_tool_result(name: str, result: dict[str, Any], *, app: str = "") -> None:
    ok = bool(result.get("ok"))
    err = str(result.get("error") or "")
    error_type = str(result.get("error_type") or "")
    if not error_type:
        error_type = "user_denied" if err == "user denied" else ("act_miss" if not ok else "")
    log_event(
        "tool_result" if ok else "tool_error",
        **_current_tool_ids(),
        tool=name,
        app=app or None,
        ok=ok,
        verified=result.get("verified") if "verified" in result else None,
        dispatched=result.get("dispatched") if "dispatched" in result else None,
        completion=result.get("completion"),
        error=err[:240] if err else None,
        error_type=error_type or None,
        hint=str(result.get("hint") or "")[:160] or None,
        failed_step=(result.get("failure") or {}).get("failed_step") if isinstance(result.get("failure"), dict) else None,
        **result_size(result),
    )


def log_exception(name: str, exc: BaseException, *, app: str = "") -> None:
    log_event(
        "tool_error",
        **_current_tool_ids(),
        tool=name,
        app=app or None,
        ok=False,
        error=str(exc)[:240],
        error_type=type(exc).__name__,
        trace=traceback.format_exc()[-400:],
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
