"""Structured Samantha activity log — tools, apps, errors (no secret values)."""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
from pathlib import Path
from typing import Any

_DEFAULT = Path.home() / ".cache/macos-cua/samantha-activity.jsonl"
_lock = threading.Lock()


def log_path() -> Path:
    raw = os.environ.get("VOICE_CUA_ACTIVITY_LOG", "").strip()
    return Path(raw).expanduser() if raw else _DEFAULT


def log_event(kind: str, **fields: Any) -> None:
    """Append one JSON line. Safe fields only — never log secret values."""
    entry: dict[str, Any] = {"ts": time.time(), "kind": kind}
    for key, value in fields.items():
        if value is None:
            continue
        if key in {"value", "secret", "password", "api_key", "token"}:
            continue
        entry[key] = value
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


def log_tool_start(name: str, arguments: dict[str, Any]) -> None:
    app = str(arguments.get("app") or "")
    log_event(
        "tool_start",
        tool=name,
        app=app or None,
        step=str(arguments.get("step_label") or arguments.get("label") or "") or None,
        has_expect=bool(arguments.get("expect")) or None,
        allow_unverified=arguments.get("allow_unverified") is True or None,
    )


def log_tool_result(name: str, result: dict[str, Any], *, app: str = "") -> None:
    ok = bool(result.get("ok"))
    err = str(result.get("error") or "")
    error_type = str(result.get("error_type") or "")
    if not error_type:
        error_type = "user_denied" if err == "user denied" else ("act_miss" if not ok else "")
    log_event(
        "tool_result" if ok else "tool_error",
        tool=name,
        app=app or None,
        ok=ok,
        verified=result.get("verified") if "verified" in result else None,
        dispatched=result.get("dispatched") if "dispatched" in result else None,
        completion=result.get("completion"),
        error=err[:240] if err else None,
        error_type=error_type or None,
    )


def log_exception(name: str, exc: BaseException, *, app: str = "") -> None:
    log_event(
        "tool_error",
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
