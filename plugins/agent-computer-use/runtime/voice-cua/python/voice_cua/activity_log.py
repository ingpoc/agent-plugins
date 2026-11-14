"""Structured local activity log — task data stays useful; credentials are scrubbed."""

from __future__ import annotations

import json
import hashlib
import os
import re
import shlex
import sys
import threading
import time
import traceback
import uuid
from difflib import SequenceMatcher
from datetime import datetime
from pathlib import Path
from typing import Any

from voice_cua import log_rotation

_DEFAULT = Path.home() / ".cache/macos-cua/samantha-activity.jsonl"
_lock = threading.Lock()
_SECRET_KEYS = frozenset({
    "secret", "client_secret", "password", "api_key", "token", "access_token", "refresh_token",
    "auth_token", "authorization", "cookie", "credentials", "private_key",
})
_DIAGNOSTIC_SECRET = re.compile(
    r"(?i)([A-Z0-9_]*(?:bearer|token|password|secret|api[_-]?key|authorization|cookie|credential)(?![A-Z0-9_]))"
    r"([\"']?\s*[=:]\s*)(?!\[redacted\])(?:bearer\s+)?(?:\"[^\"]*\"|'[^']*'|[^&;,\s]+)"
)
_DIAGNOSTIC_TOKEN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|xox[abpr]-[A-Za-z0-9-]{8,}|AKIA[0-9A-Z]{12,}|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9._-]+)")
_BEARER_TOKEN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_URL_USERINFO = re.compile(r"(?i)(https?://)[^/@\s]+@")
_CREDENTIAL_TARGET = re.compile(r"(?i)(password|secret|credential|api[_ -]?key|authorization|auth token|securetextfield)")
_SECURE_ROLE = re.compile(r"(?i)\b(?:AXSecureTextField|password\s+role|role\s*[:=]\s*password)\b")
_OBSERVATION_KEYS = frozenset({"text", "page_text", "inner_text", "content", "html", "dom", "elements", "tree", "accessibility_tree"})
_SECRET_VALUE_TOOLS = frozenset({"secrets_get", "secrets_inject", "secrets_provide", "secrets_put"})
_LEDGER_DETAIL_KEYS = frozenset({
    "ok", "status", "decision_id", "revision", "decision_ids", "decisions", "data", "matches",
    "match_count", "summary", "decision", "situation", "action", "rationale", "applicability",
    "scope", "rule", "entities", "sensitivity", "constraints", "reason", "changed_choice",
    "supersedes", "superseded_by", "source", "tags", "created_at", "updated_at", "record_type",
})
_TASK_OUTCOME_TOOLS = frozenset({"cua_act", "comet_act", "system_volume", "ledger_task",
                                 "shell_run", "shell_job", "file_edit"})
_DEV_TOOLS = frozenset({"shell_run", "shell_job", "file_edit"})
_process_session_id = uuid.uuid4().hex[:8]  # fallback when startup_trace never ran
_build_label: str | None = None
_timeline_lock = threading.Lock()
_spoken_timelines: dict[str, dict[str, Any]] = {}


def spoken_timeline(session_id: str, first_audio: float, playback: float | None) -> dict[str, Any]:
    """Return observed wall-clock phases for the current spoken turn and audio burst."""
    with _timeline_lock:
        state = _spoken_timelines.get(session_id, {})
        transcript = state.get("transcript")
        if transcript is None:
            return {}
        first_tool = state.get("first_tool")
        last_tool_end = state.get("last_tool_end")
        backend_reply = state.get("backend_reply")
        tools = state.get("tools", [])
        total_tool_ms = sum(round((end - start) * 1000) for start, end, _ in tools)
        slowest = max(tools, key=lambda row: row[1] - row[0]) if tools else None

    def elapsed(start: float | None, end: float | None) -> int | None:
        return round((end - start) * 1000) if start is not None and end is not None else None

    durations = {
        "user_transcript_to_first_audio": elapsed(transcript, first_audio),
        "user_transcript_to_first_tool_start": elapsed(transcript, first_tool),
        "tool_time_total": total_tool_ms if tools else None,
        "last_tool_end_to_first_audio": elapsed(last_tool_end, first_audio)
        if last_tool_end is not None and last_tool_end <= first_audio else None,
        "backend_reply_to_first_audio": elapsed(backend_reply, first_audio)
        if backend_reply is not None and backend_reply <= first_audio else None,
        "first_audio_delta_to_playback_start": elapsed(first_audio, playback),
        "total_user_transcript_to_playback_start": elapsed(transcript, playback),
    }
    phases = {key: value for key, value in durations.items()
              if key != "total_user_transcript_to_playback_start" and value is not None}
    return {**durations,
            "slowest_tool": slowest[2] if slowest else None,
            "slowest_tool_ms": round((slowest[1] - slowest[0]) * 1000) if slowest else None,
            "dominant_phase": max(phases, key=phases.get) if phases else None,
            "over_budget": durations["total_user_transcript_to_playback_start"] is not None
                           and durations["total_user_transcript_to_playback_start"] > 4000}


def build_label() -> str:
    """One identity per process: the installed version plus the signed app's package digest (OBS-06)."""
    global _build_label
    if _build_label is None:
        try:
            from voice_cua import build_label as installed_build

            _build_label = installed_build()
        except Exception:  # noqa: BLE001 — telemetry must never break a tool call
            _build_label = ""
    return _build_label or "unknown"


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
    line = json.dumps(_redact_secrets(entry), ensure_ascii=False, default=str) + "\n"
    with _lock:
        log_rotation.append(log_path(), line.encode("utf-8"))


def log_event(kind: str, **fields: Any) -> None:
    """Append one local JSON line, preserving task data and scrubbing credentials."""
    now = time.monotonic()
    session_id = current_session_id()
    with _timeline_lock:
        state = _spoken_timelines.setdefault(session_id, {"tools": []})
        if kind == "user_transcript":
            state.clear()
            state.update(transcript=now, tools=[])
        elif kind == "tool_start":
            state.setdefault("pending", []).append((now, str(fields.get("tool") or "")))
            state.setdefault("first_tool", now)
        elif kind == "live_delegation":
            state.setdefault("first_tool", now)
        elif kind in ("tool_result", "tool_error"):
            tool = str(fields.get("tool") or "")
            pending = state.setdefault("pending", [])
            match = next((i for i in range(len(pending) - 1, -1, -1) if pending[i][1] == tool), None)
            if match is not None:
                started, _ = pending.pop(match)
                state.setdefault("tools", []).append((started, now, tool))
                state["last_tool_end"] = now
        elif kind == "assistant_reply" and fields.get("source") == "backend":
            state["backend_reply"] = now
    entry: dict[str, Any] = {"ts": time.time(), "kind": kind, "session_id": session_id,
                             "build": build_label()}
    for key, value in fields.items():
        if value is None:
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


_LABEL_KEYS = ("provider", "model", "live_model", "tier", "delegation_id", "run_id")


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
        "step_ref": new_step_ref(),
        "ts": now,
        "ts_iso": datetime.fromtimestamp(now).astimezone().isoformat(timespec="milliseconds"),
        "session_id": current_session_id(),
        "build": build_label(),
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
    row: dict[str, Any] = {"kind": "usage", "step_ref": new_step_ref(), "ts": now,
                           "ts_iso": datetime.fromtimestamp(now).astimezone().isoformat(timespec="milliseconds"),
                           "session_id": session, "build": build_label(), "final": True,
                           "reason": reason[:40], **totals}
    for key, value in extra.items():
        if value is not None:
            row[key] = value
    _append_row(row)


# Per-thread tool-call context: live_session runs each tool in its own
# thread, so tool_result rows can carry call_id/response_id without changing
# the dispatch() signature.
_tool_ctx = threading.local()


class tool_context:  # noqa: N801 — used as a context manager
    def __init__(self, *, call_id: str = "", response_id: str = "", run_id: str = "",
                 step_ref: str = "", caused_by: list[str] | tuple[str, ...] = (),
                 retry_of_step_ref: str = "", retry_of_run_ref: str = "",
                 cancellation_state: str = "", typed_turn_id: str = "") -> None:
        self._ids = {"call_id": call_id or None, "response_id": response_id or None}
        if isinstance(run_id, str) and re.fullmatch(r"[0-9a-f]{8,64}", run_id):
            self._ids["run_id"] = run_id
            self._ids["run_ref_status"] = "linked"
        else:
            self._ids["run_ref_status"] = "unknown"
            self._ids["run_ref_gap"] = "tool_context_missing_run_id"
        self._ids["step_ref"] = step_ref or new_step_ref()
        self._typed_turn_id = typed_turn_id
        refs = [ref for ref in caused_by if isinstance(ref, str) and ref]
        if refs:
            self._ids["caused_by"] = list(dict.fromkeys(refs))
        for key, value in (("retry_of_step_ref", retry_of_step_ref),
                           ("retry_of_run_ref", retry_of_run_ref),
                           ("cancellation_state", cancellation_state)):
            if value:
                self._ids[key] = value
        self._prev: dict[str, Any] | None = None
        self._prev_typed_turn_id = ""

    def __enter__(self) -> "tool_context":
        self._prev = getattr(_tool_ctx, "ids", None)
        self._prev_typed_turn_id = getattr(_tool_ctx, "typed_turn_id", "")
        _tool_ctx.ids = self._ids
        _tool_ctx.typed_turn_id = self._typed_turn_id or self._prev_typed_turn_id
        return self

    def __exit__(self, *_exc: object) -> None:
        _tool_ctx.ids = self._prev
        _tool_ctx.typed_turn_id = self._prev_typed_turn_id


def _current_tool_ids() -> dict[str, Any]:
    return dict(getattr(_tool_ctx, "ids", None) or {})


def current_tool_ids() -> dict[str, Any]:
    """Current step identity for sibling ACU emitters such as retry tracking."""
    return _current_tool_ids()


def current_typed_turn_id() -> str:
    return getattr(_tool_ctx, "typed_turn_id", "")


def update_tool_context(**fields: Any) -> None:
    """Attach a relation discovered during dispatch to every row for this step."""
    ids = getattr(_tool_ctx, "ids", None)
    if isinstance(ids, dict):
        ids.update({key: value for key, value in fields.items() if value is not None and value != ""})


def new_step_ref() -> str:
    """Opaque UUID for one producer step; separate attempts always get separate refs."""
    return uuid.uuid4().hex


def _redact_secrets(value: Any, *, key: str = "") -> Any:
    normalized_key = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
    if (normalized_key in _SECRET_KEYS or normalized_key.endswith(("secret", "password", "token"))
            or normalized_key in {"apikey", "bearer"}):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(k): _redact_secrets(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_secrets(item) for item in value]
    if isinstance(value, str):
        value = _DIAGNOSTIC_SECRET.sub(lambda match: match.group(1) + match.group(2) + "[redacted]", value)
        value = _BEARER_TOKEN.sub("Bearer [redacted]", value)
        value = _URL_USERINFO.sub(r"\1[redacted]@", value)
        return _DIAGNOSTIC_TOKEN.sub("[redacted]", value)
    return value


def _redact_tool_credentials(value: Any, tool: str, *, in_credential_target: bool = False) -> Any:
    if isinstance(value, dict):
        target = " ".join(str(value.get(key) or "") for key in ("label", "role", "field", "name", "target"))
        is_credential_target = (in_credential_target or bool(_CREDENTIAL_TARGET.search(target))
                                or value.get("secret") is True or value.get("is_secret") is True)
        out: dict[str, Any] = {}
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]+", "_", str(key).casefold()).strip("_")
            if ((tool in _SECRET_VALUE_TOOLS and normalized in {"value", "secret_value"})
                    or (is_credential_target and normalized in {"text", "value", "expect"})):
                out[str(key)] = "[redacted]"
            else:
                out[str(key)] = _redact_tool_credentials(item, tool,
                                                        in_credential_target=is_credential_target)
        return out
    if isinstance(value, list):
        return [_redact_tool_credentials(item, tool, in_credential_target=in_credential_target)
                for item in value]
    return value


def _has_credential_target(value: Any) -> bool:
    if isinstance(value, dict):
        target = " ".join(str(value.get(key) or "") for key in ("label", "role", "field", "name", "target"))
        return (bool(_CREDENTIAL_TARGET.search(target)) or value.get("secret") is True
                or value.get("is_secret") is True
                or any(_has_credential_target(item) for item in value.values()))
    if isinstance(value, list):
        return any(_has_credential_target(item) for item in value)
    return False


def _target_labels(value: Any) -> set[str]:
    labels: set[str] = set()
    if isinstance(value, dict):
        for key in ("label", "name", "title", "target"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                labels.add(item.strip().casefold())
        for key in ("steps", "step", "target", "expect", "expected_target"):
            if key in value:
                labels.update(_target_labels(value[key]))
    elif isinstance(value, list):
        for item in value:
            labels.update(_target_labels(item))
    return labels


def _observed_secure_target(arguments: dict[str, Any], result: dict[str, Any]) -> bool:
    labels = _target_labels(arguments)
    if not labels:
        return False
    return any(_SECURE_ROLE.search(line) and any(label in line.casefold() for label in labels)
               for text in _observation_texts(result) for line in text.splitlines())


def _typed_text_length(value: Any) -> int:
    return sum(len(item) for item in value) if isinstance(value, list) else len(value)


def _typed_text_security(arguments: dict[str, Any], result: dict[str, Any]) -> str:
    if _has_credential_target(arguments) or _observed_secure_target(arguments, result):
        return "secure"
    role = result.get("focused_role")
    subrole = result.get("focused_subrole")
    if isinstance(role, str) and role.strip():
        if _SECURE_ROLE.search(f"{role} {subrole or ''}"):
            return "secure"
        if isinstance(subrole, str) and subrole.strip():
            return "non_secure"
        if role in {"AXTextArea", "AXSearchField", "AXComboBox"}:
            return "non_secure"
    return "unknown"


def _redact_typed_values(value: Any) -> Any:
    if isinstance(value, dict):
        action = value.get("op") or value.get("action") or value.get("type")
        if isinstance(action, str) and action in {"type_text", "type", "fill", "set_value"}:
            return {key: ("[redacted]" if key in {"text", "typed_text", "value"}
                          else _redact_typed_values(item)) for key, item in value.items()}
        return {key: _redact_typed_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_typed_values(item) for item in value]
    return value


def _observation_facts(text: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Keep target-relevant lines and counts; never persist a full AX/page dump."""
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    label_terms: list[str] = []
    text_terms: list[str] = []
    for key in ("query", "text", "expect", "selector"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            text_terms.append(value.strip())
    if isinstance(arguments.get("label"), str) and arguments["label"].strip():
        label_terms.append(arguments["label"].strip())
    steps = arguments.get("steps")
    actions = arguments.get("actions")
    for step in (steps if isinstance(steps, list) else []) + (actions if isinstance(actions, list) else []):
        if isinstance(step, dict):
            if isinstance(step.get("label"), str) and step["label"].strip():
                label_terms.append(step["label"].strip())
            text_terms.extend(str(step[key]).strip() for key in ("query", "text", "expect", "selector")
                              if isinstance(step.get(key), str) and step[key].strip())
    label_terms = list(dict.fromkeys(label_terms))
    text_terms = list(dict.fromkeys(text_terms))
    matches: list[str] = []
    unmatched: list[tuple[float, str]] = []
    for line in lines[1:]:
        labels = re.findall(r'["\']([^"\']{1,120})["\']', line)
        exact_label = any(label.casefold() == term.casefold()
                          for label in labels for term in label_terms)
        exact_text = any(term.casefold() in line.casefold() for term in text_terms)
        if exact_label or exact_text:
            matches.append(line)
            continue
        score = max((SequenceMatcher(None, term.casefold(), label.casefold()).ratio()
                     for term in label_terms for label in labels), default=0.0)
        if score >= 0.55 and not any(re.fullmatch(re.escape(term) + r"\d+", label, re.I)
                                     for term in label_terms for label in labels):
            unmatched.append((score, line))
    near = [line for _, line in sorted(unmatched, reverse=True)[:4]]
    candidates = [] if _has_credential_target(arguments) else (matches[:4] or near[:4])
    window = lines[0][:160] if lines and re.search(r"(?i)(?:\bAXWindow\b|^Window:|^Window\s)", lines[0]) else None
    return {
        "state": "available" if lines else "empty",
        "window": window,
        "line_count": len(lines),
        "match_count": len(matches),
        "near_miss_count": len(unmatched),
        "candidates": [line[:200] for line in candidates],
    }


def _observation_texts(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key.casefold() in _OBSERVATION_KEYS:
                if isinstance(item, str) and item:
                    out.append(item)
                elif isinstance(item, list):
                    out.extend(str(row) for row in item if isinstance(row, str) and row)
                else:
                    out.extend(_observation_texts(item))
            else:
                out.extend(_observation_texts(item))
    elif isinstance(value, list):
        for item in value:
            out.extend(_observation_texts(item))
    return out


def _strip_observation_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _strip_observation_payload(item) for key, item in value.items()
                if key.casefold() not in _OBSERVATION_KEYS}
    if isinstance(value, list):
        return [_strip_observation_payload(item) for item in value]
    return value


def _reported_actual_url(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("actual_url", "final_url", "page_url", "current_url", "url"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
                return candidate
        for item in value.values():
            found = _reported_actual_url(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _reported_actual_url(item)
            if found:
                return found
    return ""


def _diagnostic_text(value: Any, *, limit: int = 4096) -> str:
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, default=str)
    value = _redact_secrets(value)
    if len(value) > limit:
        half = limit // 2
        value = value[:half] + "\n…[diagnostic truncated]…\n" + value[-half:]
    return value


def _ledger_detail_value(value: Any, depth: int = 0, *, truncated: list[bool]) -> Any:
    if depth > 5:
        truncated[0] = True
        return "[depth limit]"
    if isinstance(value, dict):
        return {key: _ledger_detail_value(item, depth + 1, truncated=truncated)
                for key, item in value.items()
                if key in _LEDGER_DETAIL_KEYS and key not in _SECRET_KEYS}
    if isinstance(value, list):
        if len(value) > 3:
            truncated[0] = True
        return [_ledger_detail_value(item, depth + 1, truncated=truncated) for item in value[:3]]
    if isinstance(value, str):
        if len(value) > 500:
            truncated[0] = True
        return _diagnostic_text(value, limit=500)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return None


def _ledger_result_detail(result: dict[str, Any]) -> tuple[str | None, bool]:
    truncated = [False]
    detail = _ledger_detail_value(result, truncated=truncated)
    if not isinstance(detail, dict) or not detail:
        return None, truncated[0]
    serialized = json.dumps(detail, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) > 4096:
        truncated[0] = True
    return _diagnostic_text(serialized, limit=4096), truncated[0]


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


def _typed_text(arguments: dict[str, Any]) -> str | list[str] | None:
    values: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            action = value.get("op") or value.get("action") or value.get("type")
            if isinstance(action, str) and action in {"type_text", "type", "fill", "set_value"}:
                text = value.get("typed_text", value.get("text", value.get("value")))
                if isinstance(text, str):
                    values.append(text)
            elif isinstance(value.get("typed_text"), str):
                values.append(value["typed_text"])
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(arguments)
    values = list(dict.fromkeys(values))
    return values[0] if len(values) == 1 else values or None


def _without_typed_values(value: Any) -> Any:
    if isinstance(value, dict):
        action = value.get("op") or value.get("action") or value.get("type")
        if isinstance(action, str) and action in {"type_text", "type", "fill", "set_value"}:
            return {key: _without_typed_values(item) for key, item in value.items()
                    if key not in {"text", "typed_text", "value", "expect"}}
        return {key: _without_typed_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_without_typed_values(item) for item in value]
    return value


def _target_summary(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    label = next((value[key] for key in ("label", "name", "title", "text", "target")
                  if isinstance(value.get(key), str) and value[key]), None)
    role = value.get("role") if isinstance(value.get("role"), str) else None
    if label and role:
        return f"{label} ({role})"
    return label or role or value.get("method") or value.get("op")


def _task_fields(name: str, arguments: dict[str, Any], result: dict[str, Any] | None = None,
                 ledger_error_category: str | None = None) -> dict[str, Any]:
    result = result or {}
    fields: dict[str, Any] = {}
    if name == "ledger_task":
        terms = arguments.get("query_terms")
        if not isinstance(terms, (list, tuple)):
            terms = [arguments.get(key) for key in ("need", "situation")]
        terms = [term for term in terms if isinstance(term, str) and term]
        if terms:
            fields["query_terms"] = terms
        invalid_terms = result.get("invalid_query_terms") or result.get("invalid_terms")
        if not invalid_terms and ledger_error_category == "invalid_query_terms":
            invalid_terms = terms
        if isinstance(invalid_terms, (list, tuple)):
            fields["invalid_query_terms"] = list(invalid_terms)
    else:
        failure = result.get("failure") if isinstance(result.get("failure"), dict) else {}
        observation = result.get("observation") if isinstance(result.get("observation"), dict) else {}
        if not observation and isinstance(failure.get("observation"), dict):
            observation = failure["observation"]
        expected = result.get("expected_target") or failure.get("expected_target") or arguments.get("expected_target")
        if not expected:
            expected = arguments.get("target") or arguments.get("label")
        if not expected and isinstance(failure.get("failed_step"), int):
            steps = arguments.get("steps")
            index = failure["failed_step"] - 1
            if isinstance(steps, list) and 0 <= index < len(steps):
                expected = steps[index]
        found = result.get("found_target") or failure.get("found_target")
        for key, value in (("expected_target", _target_summary(expected)),
                           ("found_target", _target_summary(found))):
            if value is not None:
                fields[key] = value
        for key in ("match_count", "near_miss_count"):
            value = result.get(key, observation.get(key))
            if value is not None:
                fields[key] = value
        typed = _typed_text(arguments)
        if typed is not None:
            security = _typed_text_security(arguments, result)
            if security == "secure":
                fields.update(typed_text="[redacted]", typed_text_redacted_reason="secure_target",
                              typed_text_length=_typed_text_length(typed))
            elif security == "unknown":
                fields.update(typed_text="[redacted]", typed_text_redacted_reason="security_unknown",
                              typed_text_length=_typed_text_length(typed))
            else:
                fields["typed_text"] = typed
    if name.startswith("comet_"):
        lease = result.get("lease")
        if lease is not None:
            fields["lease"] = lease
        actual_url = _reported_actual_url(result)
        if actual_url:
            fields["url"] = actual_url
        elif name == "comet_begin" and result.get("ok") is True:
            fields["url_gap"] = "comet_begin_did_not_report_final_url"
    return fields


def _dev_tool_metadata(name: str, arguments: dict[str, Any], result: dict[str, Any] | None = None,
                       *, duration_ms: int | None = None) -> dict[str, Any]:
    has_result = result is not None
    result = result or {}
    fields: dict[str, Any] = {"ok": result.get("ok") is True} if has_result else {}
    op = arguments.get("op") or arguments.get("action")
    if isinstance(op, str):
        fields["op"] = op
    path = arguments.get("path") or result.get("log")
    if isinstance(path, str) and path:
        fields["path_basename"] = Path(path).name
    command = arguments.get("command")
    if isinstance(command, str):
        try:
            head = shlex.split(command, comments=False, posix=True)[0]
        except (ValueError, IndexError):
            head = command.strip().split(maxsplit=1)[0] if command.strip() else ""
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", head):
            head = head.split("=", 1)[0] + "=[redacted]"
        fields["command_head"] = Path(head).name[:80]
        fields["command_hash"] = hashlib.sha256(command.encode("utf-8", errors="replace")).hexdigest()
    elif isinstance(result.get("command_hash"), str):
        fields["command_hash"] = result["command_hash"]
        fields["command_head"] = str(result.get("command_head") or "")[:80]
    if "exit_code" in result and (type(result.get("exit_code")) is int or result.get("exit_code") is None):
        fields["exit_code"] = result.get("exit_code")
    duration = duration_ms if duration_ms is not None else result.get("duration_ms")
    if type(duration) is int and duration >= 0:
        fields["duration_ms"] = duration
    for key in ("truncated", "timed_out"):
        if key in result:
            fields[key] = result.get(key) is True
    return fields


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


def log_tool_start(name: str, arguments: dict[str, Any]) -> None:
    if name in _DEV_TOOLS:
        log_event("tool_start", **_current_tool_ids(), tool=name,
                  arguments=_dev_tool_metadata(name, arguments))
        return
    arguments = _redact_tool_credentials(arguments, name)
    start_arguments = _without_typed_values(arguments)
    app = str(arguments.get("app") or "") if name != "ledger_task" else ""
    operation = arguments.get("operation") if name == "ledger_task" else None
    ledger_operation = operation if operation in ("lookup", "capture", "recall", "preference") else "unknown"
    scope = arguments.get("scope")
    scope = scope.strip().casefold() if isinstance(scope, str) else ""
    scope_kind = ("missing" if not scope else "fleet" if scope == "fleet" else
                  "browser" if scope in ("comet", "chrome", "tool:comet", "tool:chrome") else
                  "tool" if scope.startswith("tool:") or ":" not in scope else
                  "project" if scope.startswith("project:") else "invalid")
    log_event(
        "tool_start",
        **_current_tool_ids(),
        tool=name,
        app=app or ("Comet" if name.startswith("comet_") else None),
        arguments=start_arguments,
        step=(str(arguments.get("step_label") or arguments.get("label") or "") or None) if name != "ledger_task" else None,
        ledger_operation=ledger_operation if name == "ledger_task" else None,
        ledger_scope_kind=scope_kind if name == "ledger_task" else None,
        ledger_rule_supplied=bool(arguments.get("rule")) if name == "ledger_task" else None,
        has_expect=bool(arguments.get("expect")) or None,
        allow_unverified=arguments.get("allow_unverified") is True or None,
        **_task_fields(name, start_arguments),
    )


def log_tool_result(name: str, result: dict[str, Any], *, app: str = "", operation: Any = None,
                    duration_ms: int | None = None,
                    arguments: dict[str, Any] | None = None) -> None:
    """One row per finished tool call; ``duration_ms`` is the wall time the model waited (OBS-03/OBS-14)."""
    arguments = arguments or {}
    if name in _DEV_TOOLS:
        metadata = _dev_tool_metadata(name, arguments, result, duration_ms=duration_ms)
        log_event("tool_result" if result.get("ok") is True else "tool_error",
                  **_current_tool_ids(), tool=name, **metadata)
        return
    typed_text = _typed_text(arguments)
    typed_security = _typed_text_security(arguments, result) if typed_text is not None else None
    typed_redaction_fields = {}
    if typed_text is not None and typed_security in {"secure", "unknown"}:
        typed_redaction_fields = {
            "typed_text": "[redacted]",
            "typed_text_redacted_reason": "secure_target" if typed_security == "secure" else "security_unknown",
            "typed_text_length": _typed_text_length(typed_text),
        }
    failure = result.get("failure") if isinstance(result.get("failure"), dict) else {}
    observation = result.get("observation") if isinstance(result.get("observation"), dict) else {}
    credential_target = any(_has_credential_target(value) for value in (
        arguments, result.get("found_target"), failure.get("found_target"),
        observation.get("target"), failure.get("observation"),
    )) or typed_security == "secure"
    sensitive_texts = typed_text if typed_security in {"secure", "unknown"} else None
    result = _redact_tool_credentials(result, name, in_credential_target=credential_target)
    arguments = _redact_tool_credentials(arguments, name, in_credential_target=credential_target)
    if typed_security in {"secure", "unknown"}:
        result = _redact_typed_values(result)
        arguments = _redact_typed_values(arguments)
    secrets = sensitive_texts if isinstance(sensitive_texts, list) else [sensitive_texts]
    secrets = [value for value in secrets if isinstance(value, str) and value]
    if secrets:
        def redact_known(value: Any) -> Any:
            if isinstance(value, dict):
                return {key: redact_known(item) for key, item in value.items()}
            if isinstance(value, list):
                return [redact_known(item) for item in value]
            if isinstance(value, str):
                for secret_text in secrets:
                    value = value.replace(secret_text, "[redacted]")
            return value
        result = redact_known(result)
    if result.get("cancelled") is True and result.get("error_type") == "user_cancelled":
        task_fields = _task_fields(name, arguments, result)
        task_fields.update(typed_redaction_fields)
        log_event("tool_cancelled", **_current_tool_ids(), tool=name, classification="user_cancelled",
                  duration_ms=duration_ms, result=result, arguments=arguments,
                  **task_fields)
        return
    ok = bool(result.get("ok"))
    err = str(result.get("error") or "")
    error_type = str(result.get("error_type") or "")
    if not ok and (result.get("needs_confirmation") or error_type.startswith("needs_confirm")):
        # Risk gate asked the user first: an expected step, not a failed tool.
        task_fields = _task_fields(name, arguments, result)
        task_fields.update(typed_redaction_fields)
        log_event("tool_needs_confirmation", **_current_tool_ids(), tool=name, app=(app or None) if name != "ledger_task" else None,
                  classification="needs_confirmation", result=result, arguments=arguments,
                  **task_fields)
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
    data = result.get("data")
    matches = data.get("matches") if isinstance(data, dict) else None
    if not isinstance(decisions, list) and isinstance(matches, list):
        decisions = matches
    decision_count = min(len(decisions), 3) if isinstance(decisions, list) else (1 if result.get("decision_id") else 0)
    ledger_status = result.get("status")
    ledger_records = decisions if isinstance(decisions, list) else matches
    if ledger and ok and isinstance(ledger_records, list):
        if operation == "lookup" and result.get("has_more") is not True:
            if not ledger_records:
                ledger_status = "none"
            elif ledger_status in (None, "unknown"):
                ledger_status = "found"
        elif ledger_status is None:
            ledger_status = "found" if ledger_records else "none"
    ledger_status = ledger_status if isinstance(ledger_status, str) and ledger_status in {"found", "none", "saved", "skipped", "cancelled", "needs_disambiguation"} else "other"
    ledger_detail, ledger_truncated = _ledger_result_detail(result) if ledger else (None, None)
    logged_result = dict(result)
    if name in {"cua_state", "cua_act", "comet_act"}:
        observed = _observation_texts(logged_result)
        if observed:
            logged_result = _strip_observation_payload(logged_result)
            logged_result["observation"] = _observation_facts("\n".join(observed), arguments or {})
    if name == "comet_begin" and result.get("ok") is True:
        logged_result.pop("actual_url", None)
        logged_result.pop("actual_url_gap", None)
    task_fields = _task_fields(name, arguments, result, ledger_error_category=category)
    task_fields.update(typed_redaction_fields)
    ledger_decision_id = arguments.get("ledger_decision_id") if ok and not ledger else None
    if isinstance(ledger_decision_id, str) and _LEDGER_ID.fullmatch(ledger_decision_id):
        task_fields["selected_id"] = ledger_decision_id
    else:
        ledger_decision_id = None
    event_fields = {
        **_current_tool_ids(),
        "tool": name,
        "app": app or None,
        "ledger_operation": (operation if operation in ("lookup", "capture", "recall", "preference") else "unknown") if ledger else None,
        "ledger_error_category": category,
        "ok": ok,
        "verified": result.get("verified") if not ledger and "verified" in result else None,
        "dispatched": result.get("dispatched") if not ledger and "dispatched" in result else None,
        "completion": result.get("completion") if not ledger else None,
        "error": _diagnostic_text(err, limit=240) if err and not ledger else None,
        "error_type": (error_type or None) if not ledger else (error_type if error_type in {"ledger_invalid_argument", "ledger_unavailable", "ledger_outcome_unknown"} else ("other" if not ok else None)),
        "hint": (_diagnostic_text(str(result.get("hint") or ""), limit=160) or None) if not ledger else None,
        "failed_step": (result.get("failure") or {}).get("failed_step") if not ledger and isinstance(result.get("failure"), dict) else None,
        "error_details": diagnostic,
        "ledger_result_status": ledger_status if ledger else None,
        "ledger_decision_count": decision_count if ledger else None,
        "ledger_result_detail": ledger_detail,
        "ledger_detail_truncated": ledger_truncated,
        "duration_ms": duration_ms,
        "result": logged_result,
        "arguments": arguments,
        **result_size(result),
        **task_fields,
    }
    try:
        log_event("tool_result" if ok else "tool_error", **event_fields)
    except Exception:
        # Logging must never turn a completed typing action into a failed tool call.
        pass
    if ledger_decision_id:
        step_action = {key: arguments[key] for key in ("steps", "actions", "op", "type", "label", "target")
                       if key in arguments}
        log_event("ledger_use", **_current_tool_ids(), decision_id=ledger_decision_id,
                  action={"tool": name, **step_action})
    if ledger and ok:
        _log_ledger_trail(result, operation=operation, status=ledger_status, decisions=decisions)
    elif ledger and category == "invalid_query_terms":
        log_event("ledger_select", **_current_tool_ids(), ledger_operation=operation,
                  ledger_selection_status="none", ledger_selected_count=0)


_LEDGER_ID = re.compile(r"[A-Za-z0-9_-]{6,64}")


def _log_ledger_trail(result: dict[str, Any], *, operation: Any, status: str,
                      decisions: Any) -> None:
    """OBS-12 compact decision trail; full credential-scrubbed records live on the tool result row."""
    rule = result.get("rule") if isinstance(result.get("rule"), str) else None
    rule = rule if isinstance(rule, str) and re.fullmatch(r"[a-z0-9_]{1,64}", rule) else None
    if operation in ("lookup", "recall", "preference") and status == "found" and isinstance(decisions, list):
        ids = [row.get("decision_id") for row in decisions if isinstance(row, dict)]
        ids = [value for value in ids if isinstance(value, str) and _LEDGER_ID.fullmatch(value)]
        if ids:
            log_event("ledger_select", **_current_tool_ids(), ledger_operation=operation, ledger_rule=rule,
                      ledger_selection_status="available",
                      ledger_selected_count=len(ids))
    elif operation in ("lookup", "recall", "preference") and status == "none":
        log_event("ledger_select", **_current_tool_ids(), ledger_operation=operation, ledger_rule=rule,
                  ledger_selection_status="none", ledger_selected_count=0)
    if status == "saved" and isinstance(result.get("decision_id"), str) and _LEDGER_ID.fullmatch(result["decision_id"]):
        supersedes = result.get("supersedes")
        log_event("ledger_save_receipt", **_current_tool_ids(),
                  decision_id=result["decision_id"], revision=result.get("revision"),
                  supersedes=supersedes if isinstance(supersedes, str) and _LEDGER_ID.fullmatch(supersedes) else None)
        if isinstance(supersedes, str) and _LEDGER_ID.fullmatch(supersedes):
            log_event("ledger_supersede", **_current_tool_ids(),
                      decision_id=result["decision_id"], supersedes=supersedes)
        for prior in result.get("superseded_ids", []) if isinstance(result.get("superseded_ids"), list) else []:
            if isinstance(prior, str) and _LEDGER_ID.fullmatch(prior) and prior != supersedes:
                log_event("ledger_supersede", **_current_tool_ids(),
                          decision_id=result["decision_id"], supersedes=prior)


def log_exception(name: str, exc: BaseException, *, app: str = "", duration_ms: int | None = None) -> None:
    details = _diagnostic_text(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
    log_event(
        "tool_error",
        **_current_tool_ids(),
        tool=name,
        app=app or None,
        ok=False,
        error=_diagnostic_text(str(exc), limit=240) if name != "ledger_task" else None,
        error_type=type(exc).__name__,
        error_details=details,
        duration_ms=duration_ms,
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


def log_task_outcome(*, check_id: str, outcome: str, evidence_class: str | None, run_id: str,
                     response_id: str = "", tools: list[str] | tuple[str, ...] = (),
                     step_ref: str = "", caused_by: list[str] | tuple[str, ...] = (),
                     expected_outcome: str = "unknown", duration_ms: int | None = None) -> None:
    """Emit check outcomes with an explicit evidence class."""
    import re
    if (len(check_id) > 48 or not re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", check_id)
            or outcome not in {"verified_success", "verified_incomplete", "unverified", "cancelled", "unknown"}
            or evidence_class not in {None, "app_readback", "browser_inventory", "local_login", "combined", "tool_reported"}
            or (evidence_class == "tool_reported" and outcome not in {"verified_success", "unverified", "cancelled"})
            or not re.fullmatch(r"[0-9a-f]{8,64}", run_id)
            or not isinstance(response_id, str)
            or (response_id and (len(response_id) > 160 or not re.fullmatch(r"[A-Za-z0-9_-]+", response_id)))
            or not isinstance(tools, (list, tuple))
            or any(tool not in _TASK_OUTCOME_TOOLS for tool in tools)
            or not isinstance(expected_outcome, str)
            or (duration_ms is not None and (type(duration_ms) is not int or duration_ms < 0))):
        raise ValueError("invalid task outcome metadata")
    log_event("task_outcome", check_id=check_id, outcome=outcome,
              expected_outcome=expected_outcome, evidence_class=evidence_class, run_id=run_id,
              response_id=response_id or None, duration_ms=duration_ms,
              step_ref=step_ref or new_step_ref(), caused_by=list(caused_by) or None,
              tools=sorted(set(tools)) or None)
