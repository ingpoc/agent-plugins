"""Strict allowlist for metadata that may enter a prepared observability snapshot."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

SOURCES = ("codex", "cursor", "grokbot", "opencode", "t3")
_ID_SUFFIX = r"[a-z0-9][a-z0-9._-]{0,47}"
_PREFIXES = {
    "owner_ref": "owner",
    "workflow_id": "wf",
    "step_id": "step",
    "revision": "rev",
    "execution_context_id": "ctx",
    "invocation_id": "inv",
    "attempt_id": "attempt",
    "event_id": "evt",
    "check_id": "check",
    "recovery_of_invocation_id": "inv",
}
_ISSUE_IDENTITY_FIELDS = frozenset({
    "source", "owner_ref", "workflow_id", "step_id", "revision",
    "execution_context_id", "signature_version", "failure_signature",
})
_SIGNATURE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SESSION_REF = re.compile(r"sref_[0-9a-f]{32}\Z")
_FAILURE_CODES = frozenset({"missing", "wrong_revision", "malformed", "validation_failed"})


class ContractError(ValueError):
    """An input field failed the metadata contract."""


def safe_session_ref(value: Any) -> str | None:
    return value if isinstance(value, str) and _SESSION_REF.fullmatch(value) else None


def _id(value: Any, field: str) -> str:
    prefix = _PREFIXES[field]
    if not isinstance(value, str) or not re.fullmatch(rf"{prefix}:{_ID_SUFFIX}", value):
        raise ContractError("invalid_identifier")
    return value


def timestamp(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 40:
        raise ContractError("invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ContractError("invalid_timestamp") from None
    if parsed.tzinfo is None:
        raise ContractError("invalid_timestamp")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _signature(value: Any, version: Any) -> tuple[str, int]:
    if not isinstance(value, str) or not _SIGNATURE.fullmatch(value):
        raise ContractError("invalid_signature")
    if isinstance(version, bool) or not isinstance(version, int) or not 1 <= version <= 999:
        raise ContractError("invalid_signature_version")
    return value, version


def _sequence(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**63 - 1:
        raise ContractError("invalid_source_sequence")
    return value


def _duration(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 7 * 24 * 60 * 60 * 1000:
        raise ContractError("invalid_duration")
    return value


def _recovery_target(value: Any, *, event: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _ISSUE_IDENTITY_FIELDS:
        raise ContractError("invalid_recovery_target")
    target: dict[str, Any] = {
        "source": value.get("source"),
        "owner_ref": _id(value.get("owner_ref"), "owner_ref"),
        "workflow_id": _id(value.get("workflow_id"), "workflow_id"),
        "step_id": _id(value.get("step_id"), "step_id"),
        "revision": _id(value.get("revision"), "revision"),
        "execution_context_id": _id(value.get("execution_context_id"), "execution_context_id"),
    }
    if target["source"] not in SOURCES:
        raise ContractError("invalid_recovery_target")
    signature, version = _signature(value.get("failure_signature"), value.get("signature_version"))
    target.update(signature_version=version, failure_signature=signature)
    for field in ("source", "owner_ref", "workflow_id", "step_id", "execution_context_id"):
        if target[field] != event[field]:
            raise ContractError("invalid_recovery_target")
    if target["signature_version"] != event["signature_version"] or target["failure_signature"] != event["failure_signature"]:
        raise ContractError("invalid_recovery_target")
    return target


def normalize_event(raw: Any) -> dict[str, Any]:
    """Return only allowlisted fields; reject malformed identities and outcomes."""
    if not isinstance(raw, dict):
        raise ContractError("invalid_event")
    source = raw.get("source")
    if source not in SOURCES:
        raise ContractError("unknown_source")
    event_type = raw.get("event_type")
    if event_type not in ("attempt_failed", "attempt_finished", "result_check", "recovery_reported"):
        raise ContractError("unknown_event_type")

    event = {
        "source": source,
        "event_type": event_type,
        "event_id": _id(raw.get("event_id"), "event_id"),
        "event_at": timestamp(raw.get("event_at")),
        "received_at": timestamp(raw.get("received_at")),
        "owner_ref": _id(raw.get("owner_ref"), "owner_ref"),
        "workflow_id": _id(raw.get("workflow_id"), "workflow_id"),
        "step_id": _id(raw.get("step_id"), "step_id"),
        "revision": _id(raw.get("revision"), "revision"),
        "execution_context_id": _id(raw.get("execution_context_id"), "execution_context_id"),
        "invocation_id": _id(raw.get("invocation_id"), "invocation_id"),
        "attempt_id": _id(raw.get("attempt_id"), "attempt_id"),
    }
    if "duration_ms" in raw:
        event["duration_ms"] = _duration(raw["duration_ms"])
    if "recovery_target" in raw and (event_type != "result_check" or raw.get("check_result") != "pass"):
        raise ContractError("invalid_recovery_target")
    if "source_sequence" in raw:
        event["source_sequence"] = _sequence(raw["source_sequence"])

    if event_type == "attempt_failed":
        signature, version = _signature(raw.get("failure_signature"), raw.get("signature_version"))
        event.update(failure_signature=signature, signature_version=version)
    elif event_type == "attempt_finished":
        exit_code = raw.get("exit_code")
        if isinstance(exit_code, bool) or not isinstance(exit_code, int) or not -255 <= exit_code <= 255:
            raise ContractError("invalid_exit_code")
        event["exit_code"] = exit_code
        if exit_code != 0:
            signature, version = _signature(raw.get("failure_signature"), raw.get("signature_version"))
            event.update(failure_signature=signature, signature_version=version)
    elif event_type == "result_check":
        check_result = raw.get("check_result")
        if check_result not in ("pass", "fail"):
            raise ContractError("invalid_check_result")
        event.update(check_id=_id(raw.get("check_id"), "check_id"), check_result=check_result)
        if check_result == "fail":
            signature, version = _signature(raw.get("failure_signature"), raw.get("signature_version"))
            failure_code = raw.get("failure_code")
            if failure_code not in ("missing", "wrong_revision", "malformed", "validation_failed"):
                raise ContractError("invalid_failure_code")
            event.update(failure_signature=signature, signature_version=version, failure_code=failure_code)
            if "recovery_of_invocation_id" in raw:
                raise ContractError("invalid_recovery_target")
        elif "recovery_of_invocation_id" in raw:
            event["recovery_of_invocation_id"] = _id(raw["recovery_of_invocation_id"], "recovery_of_invocation_id")
            signature, version = _signature(raw.get("failure_signature"), raw.get("signature_version"))
            event.update(failure_signature=signature, signature_version=version)
            if "recovery_target" in raw:
                event["recovery_target"] = _recovery_target(raw["recovery_target"], event=event)
        elif "recovery_target" in raw:
            raise ContractError("invalid_recovery_target")
    else:
        event["recovery_of_invocation_id"] = _id(raw.get("recovery_of_invocation_id"), "recovery_of_invocation_id")
        signature, version = _signature(raw.get("failure_signature"), raw.get("signature_version"))
        event.update(failure_signature=signature, signature_version=version)
    return event


def issue_identity(event: dict[str, Any]) -> tuple[str, ...]:
    return tuple(event[key] for key in (
        "source", "owner_ref", "workflow_id", "step_id", "revision",
        "execution_context_id", "signature_version", "failure_signature",
    ))
