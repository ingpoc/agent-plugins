"""Closed request/event schemas, limits, and validation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal, TypedDict

SCHEMA_VERSION = 1
MAX_REQUEST_LINE = 64 * 1024
MAX_EVENT = 12 * 1024
MAX_BODY = 4 * 1024
MAX_FIND_RESPONSE = 4 * 1024
MAX_FIND_BYTES = 1200  # v2 find budget: top-3 compact hits plus linked precedents
RELATED_CAP = 2
BROWSE_OFFSET_MAX = 20_000
MAX_GET_RESPONSE = 8 * 1024
MAX_NESTING = 12
MAX_EVENTS = 10_000
MAX_DB_BYTES = 64 * 1024 * 1024
MAX_COMBINED_ROWS = 20_000
MAX_IMPORT_BYTES = 128 * 1024 * 1024
MAX_IMPORT_LINES = 20_001
MAX_TOMBSTONE_BYTES = 512
FTS_TOKEN_MAX = 64
FTS_TOKEN_LIMIT = 12
CANDIDATE_CAP = 100
MAX_CONFLICTS = 32
SCOPE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
APPLICABILITY_SCOPE_RE = re.compile(
    r"^(?:fleet|project:[a-z0-9][a-z0-9._-]{0,63}|tool:[a-z0-9][a-z0-9._-]{0,63})$"
)
TOOL_SCOPE_IDS = frozenset(
    {"comet", "cursor-cli", "codex-cli", "git", "python", "render", "kite"}
)
STATUS_WORDS = frozenset(
    "pending success failed merged blocked passing wip shipped inconclusive abandoned".split()
)
VERSION_RE = re.compile(r"\b\d+\.\d+(?:\.\d+)?\b")
SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b", re.I)
PR_ID_RE = re.compile(r"\b(?:pr|pull request|project)\s*#?\s*\d+\b|\bpull/\d+\b", re.I)
MODEL_RE = re.compile(
    r"\b(?:gpt-\d[\w.-]*|chatgpt|claude[\w.-]*|grok[\w.-]*|gemini[\w.-]*|opus[\w.-]*|sonnet[\w.-]*|haiku[\w.-]*|composer[\w.-]*|fable[\w.-]*)\b",
    re.I,
)
TIME_BOUND_RE = re.compile(
    r"\b(?:until|temporary|temporarily|for now|today|this week|this sprint|expires|deadline)\b"
    r"|\bafter \d+ days?\b|\bby \d{4}-\d{2}-\d{2}\b",
    re.I,
)
TRACKER_MESSAGE = "This belongs in the tracker, not the ledger."
RULE_CUES = frozenset({"must", "never", "always", "only", "because", "rule", "when"})
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

ERROR_CODES = (
    "INVALID_ARGUMENT",
    "UNAVAILABLE",
    "REVISION_CONFLICT",
    "IDEMPOTENCY_CONFLICT",
    "INVALID_TRANSITION",
    "LIMIT_EXCEEDED",
    "BUSY",
    "MAINTENANCE",
    "UPGRADE_REQUIRED",
    "CORRUPT_LEDGER",
    "INTERNAL",
)
RETRYABLE = {"BUSY", "MAINTENANCE", "UNAVAILABLE"}

KIND = ("decision", "observation")
SENSITIVITY = ("model_safe", "local_only")
EVIDENCE_STATE = ("unverified", "reported_verified", "invalid", "missing")
SUMMARY_EVIDENCE = ("none", "unverified", "reported_verified", "invalid")
APPROVAL_STATE = ("none", "self_reported", "owner_attested")
OUTCOME_STATUS = (
    "unknown",
    "pending",
    "success",
    "failed",
    "inconclusive",
    "abandoned",
)
EVENT_TYPE = (
    "created",
    "outcome",
    "corrected",
    "superseded",
    "conflict_opened",
    "conflict_resolved",
    "attested",
    "applied",
    "archived",
)
CHANNEL = ("agent", "owner_cli")
LIFECYCLE = ("active", "superseded", "archived")
APPEND_TYPES = (
    "outcome",
    "corrected",
    "superseded",
    "conflict_opened",
    "conflict_resolved",
    "applied",
)


class EvidenceInput(TypedDict):
    ref: str
    revision: str | None
    sha256: str | None
    state: Literal["unverified", "reported_verified", "invalid", "missing"]


class ApprovalInput(TypedDict):
    state: Literal["none", "self_reported", "owner_attested"]
    authority_ref: str | None


class McpApprovalInput(TypedDict):
    state: Literal["none", "self_reported"]
    authority_ref: str | None


class BodyInput(TypedDict):
    kind: Literal["decision", "observation"]
    summary: str
    situation: str
    action: str
    rationale: str
    constraints: list[str]
    applicability: str
    entities: list[str]
    evidence: list[EvidenceInput]
    sensitivity: Literal["model_safe", "local_only"]
    approval: ApprovalInput


class McpBodyInput(TypedDict):
    kind: Literal["decision", "observation"]
    summary: str
    situation: str
    action: str
    rationale: str
    constraints: list[str]
    applicability: str
    entities: list[str]
    evidence: list[EvidenceInput]
    sensitivity: Literal["model_safe", "local_only"]
    approval: McpApprovalInput


class OutcomeInput(TypedDict):
    status: Literal["unknown", "pending", "success", "failed", "inconclusive", "abandoned"]
    note: str
    evidence: list[EvidenceInput]


class CorrectedPayload(TypedDict):
    body: BodyInput
    reason: str


class McpCorrectedPayload(TypedDict):
    body: McpBodyInput
    reason: str


class SupersededPayload(TypedDict):
    replacement_id: str
    reason: str


class ConflictOpenedPayload(TypedDict):
    other_id: str
    reason: str


class ConflictResolvedPayload(TypedDict):
    other_id: str
    opened_event_id: str
    reason: str


class AppliedPayload(TypedDict):
    task_id: str
    decision_ids: list[str]


AppendPayload = (
    OutcomeInput
    | CorrectedPayload
    | SupersededPayload
    | ConflictOpenedPayload
    | ConflictResolvedPayload
    | AppliedPayload
)
McpAppendPayload = (
    OutcomeInput
    | McpCorrectedPayload
    | SupersededPayload
    | ConflictOpenedPayload
    | ConflictResolvedPayload
    | AppliedPayload
)
AppendEventType = Literal[
    "outcome",
    "corrected",
    "superseded",
    "conflict_opened",
    "conflict_resolved",
    "applied",
]


class LedgerError(Exception):
    def __init__(
        self, code: str, retryable: bool | None = None, message: str | None = None
    ) -> None:
        if code not in ERROR_CODES:
            code = "INTERNAL"
        self.code = code
        self.retryable = RETRYABLE.__contains__(code) if retryable is None else retryable
        self.message = message
        super().__init__(code)

    def as_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "retryable": self.retryable}
        if self.message:
            error["message"] = self.message
        return {"ok": False, "error": error}


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def now_ts(clock=None) -> str:
    from datetime import datetime, timezone

    dt = datetime.now(timezone.utc) if clock is None else clock()
    ms = int(dt.microsecond / 1000)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{ms:03d}Z"


def new_uuid() -> str:
    return str(uuid.uuid4())


def parse_json(raw: str | bytes, *, max_bytes: int | None = None) -> Any:
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LedgerError("INVALID_ARGUMENT") from exc
    else:
        text = raw
    if max_bytes is not None and len(text.encode("utf-8")) > max_bytes:
        raise LedgerError("LIMIT_EXCEEDED")
    if "\x00" in text:
        raise LedgerError("INVALID_ARGUMENT")

    def no_dup(pairs: list[tuple[Any, Any]]) -> dict[str, Any]:
        seen: set[str] = set()
        out: dict[str, Any] = {}
        for key, val in pairs:
            if key in seen:
                raise LedgerError("INVALID_ARGUMENT")
            seen.add(key)
            out[key] = val
        return out

    try:
        value = json.loads(
            text,
            object_pairs_hook=no_dup,
            parse_constant=lambda _c: (_ for _ in ()).throw(LedgerError("INVALID_ARGUMENT")),
            strict=True,
        )
    except LedgerError:
        raise
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise LedgerError("INVALID_ARGUMENT") from exc
    _check_tree(value, 0)
    return value


def _check_tree(value: Any, depth: int) -> None:
    if depth > MAX_NESTING:
        raise LedgerError("INVALID_ARGUMENT")
    if isinstance(value, str):
        if "\x00" in value:
            raise LedgerError("INVALID_ARGUMENT")
        return
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        raise LedgerError("INVALID_ARGUMENT")
    if isinstance(value, dict):
        for val in value.values():
            _check_tree(val, depth + 1)
        return
    if isinstance(value, list):
        for val in value:
            _check_tree(val, depth + 1)


def require_keys(obj: Any, required: set[str], optional: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise LedgerError("INVALID_ARGUMENT")
    extra = set(obj) - required - (optional or set())
    if extra or required - set(obj):
        raise LedgerError("INVALID_ARGUMENT")
    return obj


def require_str(value: Any, max_len: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > max_len:
        raise LedgerError("INVALID_ARGUMENT")
    if "\x00" in value:
        raise LedgerError("INVALID_ARGUMENT")
    if not allow_empty and value == "":
        raise LedgerError("INVALID_ARGUMENT")
    return value


def require_enum(value: Any, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        raise LedgerError("INVALID_ARGUMENT")
    return value  # type: ignore[return-value]


def require_uuid(value: Any) -> str:
    text = require_str(value, 36)
    if not UUID_RE.fullmatch(text):
        raise LedgerError("INVALID_ARGUMENT")
    return text


def require_scope(value: Any) -> str:
    text = require_str(value, 64)
    if not SCOPE_RE.fullmatch(text):
        raise LedgerError("INVALID_ARGUMENT")
    return text


def parse_applicability_scope(value: Any) -> str:
    if not isinstance(value, str) or not APPLICABILITY_SCOPE_RE.fullmatch(value):
        raise LedgerError(
            "INVALID_ARGUMENT",
            message="scope must be fleet, project:<id>, or tool:<id>; nothing was saved",
        )
    return value


def allowed_scopes(binding: dict[str, Any]) -> set[str]:
    """Fleet sits above the caller's project and the tools that caller touches."""
    allowed = {"fleet"}
    project = binding.get("project")
    if project:
        allowed.add(f"project:{project}")
    for tool in binding.get("tools") or []:
        allowed.add(f"tool:{tool}")
    return allowed


def _validate_caller_scope(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LedgerError("INVALID_ARGUMENT", message="caller_scope must be an object")
    obj = require_keys(value, set(), {"project", "tools"})
    project = obj.get("project", None)
    if project is not None:
        project = require_scope(project)
    tools = obj.get("tools") or []
    if not isinstance(tools, list):
        raise LedgerError("INVALID_ARGUMENT", message="caller_scope.tools must be a list")
    return {"project": project, "tools": [require_scope(item) for item in tools]}


def decision_surface(*parts: str | None) -> str:
    return " ".join(part for part in parts if part)


def volatile_kinds(text: str) -> list[str]:
    found: list[str] = []
    if VERSION_RE.search(text):
        found.append("version")
    if SHA_RE.search(text):
        found.append("sha")
    if PR_ID_RE.search(text):
        found.append("pr-or-project-id")
    if MODEL_RE.search(text):
        found.append("model-name")
    words = set(re.findall(r"[a-z0-9]+", text.casefold()))
    if words & STATUS_WORDS:
        found.append("status-word")
    return found


def pure_status(text: str) -> bool:
    words = re.findall(r"[a-z0-9]+", text.casefold())
    if not words:
        return True
    if any(word in RULE_CUES for word in words):
        return False
    skip = {"a", "an", "the", "is", "are", "was", "were", "on", "to", "for", "and", "of", "in"}
    content = [word for word in words if word not in skip]
    statusish = [word for word in content if word in STATUS_WORDS or word in {"status", "update"}]
    return bool(statusish) and len(statusish) * 2 >= len(content)


def time_bound(text: str) -> bool:
    return TIME_BOUND_RE.search(text) is not None


def assert_save_text(text: str, *, kind: str, expires_at: str | None, replace_when: str | None) -> None:
    if kind == "state" or pure_status(text):
        raise LedgerError("INVALID_ARGUMENT", message=TRACKER_MESSAGE)
    kinds = volatile_kinds(text)
    if kinds:
        raise LedgerError(
            "INVALID_ARGUMENT",
            message=f"{kinds[0]} goes only in evidence. {TRACKER_MESSAGE}",
        )
    if time_bound(text) and not expires_at and not replace_when:
        raise LedgerError(
            "INVALID_ARGUMENT",
            message="a time-bound decision needs expires_at or replace_when; nothing was saved",
        )


def require_ts(value: Any, *, allow_null: bool = False) -> str | None:
    if value is None:
        if allow_null:
            return None
        raise LedgerError("INVALID_ARGUMENT")
    text = require_str(value, 32)
    if not TIME_RE.fullmatch(text):
        raise LedgerError("INVALID_ARGUMENT")
    try:
        parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise LedgerError("INVALID_ARGUMENT") from exc
    ms = parsed.microsecond // 1000
    canonical = parsed.strftime("%Y-%m-%dT%H:%M:%S") + f".{ms:03d}Z"
    if canonical != text:
        raise LedgerError("INVALID_ARGUMENT")
    return text


def require_int(value: Any, *, min_v: int, max_v: int | None = None) -> int:
    if type(value) is not int:
        raise LedgerError("INVALID_ARGUMENT")
    if value < min_v or (max_v is not None and value > max_v):
        raise LedgerError("INVALID_ARGUMENT")
    return value


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def validate_evidence_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 4:
        raise LedgerError("INVALID_ARGUMENT")
    out = []
    for item in value:
        obj = require_keys(item, {"ref", "revision", "sha256", "state"})
        ref = require_str(obj["ref"], 512)
        rev = obj["revision"]
        if rev is not None:
            rev = require_str(rev, 128)
        sha = obj["sha256"]
        if sha is not None:
            sha = require_str(sha, 64)
            if not SHA256_RE.fullmatch(sha):
                raise LedgerError("INVALID_ARGUMENT")
        state = require_enum(obj["state"], EVIDENCE_STATE)
        out.append({"ref": ref, "revision": rev, "sha256": sha, "state": state})
    return out


def validate_approval(value: Any, *, allow_owner_attested: bool) -> dict[str, Any]:
    obj = require_keys(value, {"state", "authority_ref"})
    state = require_enum(obj["state"], APPROVAL_STATE)
    if state == "owner_attested" and not allow_owner_attested:
        raise LedgerError("INVALID_ARGUMENT")
    ref = obj["authority_ref"]
    if state == "none":
        if ref is not None:
            raise LedgerError("INVALID_ARGUMENT")
    else:
        if ref is None:
            raise LedgerError("INVALID_ARGUMENT")
        ref = require_str(ref, 256)
    return {"state": state, "authority_ref": ref}


def validate_body(value: Any, *, allow_owner_attested: bool) -> dict[str, Any]:
    obj = require_keys(
        value,
        {
            "kind",
            "summary",
            "situation",
            "action",
            "rationale",
            "constraints",
            "applicability",
            "entities",
            "evidence",
            "sensitivity",
            "approval",
        },
        {"trace", "scope"},
    )
    kind = require_enum(obj["kind"], KIND)
    rationale_empty = kind == "observation"
    body = {
        "kind": kind,
        "summary": require_str(obj["summary"], 160),
        "situation": require_str(obj["situation"], 500),
        "action": require_str(obj["action"], 300),
        "rationale": require_str(obj["rationale"], 800, allow_empty=rationale_empty),
        "constraints": _str_list(obj["constraints"], 160, 8),
        "applicability": require_str(obj["applicability"], 320),
        "entities": [nfc(item) for item in _str_list(obj["entities"], 128, 8)],
        "evidence": validate_evidence_list(obj["evidence"]),
        "sensitivity": require_enum(obj["sensitivity"], SENSITIVITY),
        "approval": validate_approval(obj["approval"], allow_owner_attested=allow_owner_attested),
    }
    if "scope" in obj:
        body["scope"] = parse_applicability_scope(obj["scope"])
    if "trace" in obj:
        body["trace"] = validate_trace(obj["trace"])
        if len(body["summary"].split()) > DECISION_WORDS or len(body["rationale"].split()) > WHY_WORDS:
            raise LedgerError("INVALID_ARGUMENT", message="trace body: summary or rationale over the word limit")
    if len(canonical_json(body).encode("utf-8")) > MAX_BODY:
        raise LedgerError("LIMIT_EXCEEDED")
    return body


HELP_EFFECTS = ("not_helped", "misled")
REASON_WORDS = 20


def validate_outcome(value: Any) -> dict[str, Any]:
    obj = require_keys(
        value, {"status", "note", "evidence"}, {"task_id", "applied_event_id", "helpful", "effect", "reason"}
    )
    result = {
        "status": require_enum(obj["status"], OUTCOME_STATUS),
        "note": require_str(obj["note"], 500, allow_empty=True),
        "evidence": validate_evidence_list(obj["evidence"]),
    }
    # Schema advertises task_id as string | null. JSON null is absent, not invalid.
    if "task_id" in obj and obj["task_id"] is not None:
        result["task_id"] = require_uuid(obj["task_id"])
    if "applied_event_id" in obj and obj["applied_event_id"] is not None:
        result["applied_event_id"] = require_uuid(obj["applied_event_id"])
    helpful = obj.get("helpful")
    if helpful is not None:
        if type(helpful) is not bool:
            raise LedgerError("INVALID_ARGUMENT", message="helpful must be true or false")
        result["helpful"] = helpful
    elif "applied_event_id" in result:
        raise LedgerError(
            "INVALID_ARGUMENT",
            message="an applied outcome needs helpful true|false; nothing was saved",
        )
    effect, reason = obj.get("effect"), obj.get("reason")
    if helpful is False:
        if effect not in HELP_EFFECTS:
            raise LedgerError(
                "INVALID_ARGUMENT",
                message="helpful false needs effect not_helped|misled; nothing was saved",
            )
        result["effect"] = effect
        result["reason"] = _line(reason, "reason", max_chars=160, max_words=REASON_WORDS)
    elif effect is not None or reason is not None:
        raise LedgerError(
            "INVALID_ARGUMENT",
            message="effect and reason go only with helpful false; nothing was saved",
        )
    return result


def validate_applied(value: Any) -> dict[str, Any]:
    obj = require_keys(value, {"task_id", "decision_ids"})
    ids = obj["decision_ids"]
    if not isinstance(ids, list) or not ids or len(ids) > 8:
        raise LedgerError("INVALID_ARGUMENT")
    decision_ids = [require_uuid(item) for item in ids]
    if len(decision_ids) != len(set(decision_ids)):
        raise LedgerError("INVALID_ARGUMENT")
    return {"task_id": require_uuid(obj["task_id"]), "decision_ids": decision_ids}


def _str_list(value: Any, max_item: int, max_n: int) -> list[str]:
    if not isinstance(value, list) or len(value) > max_n:
        raise LedgerError("INVALID_ARGUMENT")
    return [require_str(item, max_item) for item in value]


def validate_created_payload(value: Any, *, allow_owner_attested: bool) -> dict[str, Any]:
    obj = require_keys(value, {"body", "outcome"})
    return {
        "body": validate_body(obj["body"], allow_owner_attested=allow_owner_attested),
        "outcome": validate_outcome(obj["outcome"]),
    }


def validate_payload(event_type: str, value: Any, *, allow_owner_attested: bool) -> dict[str, Any]:
    if event_type == "created":
        return validate_created_payload(value, allow_owner_attested=allow_owner_attested)
    if event_type == "outcome":
        return validate_outcome(value)
    if event_type == "corrected":
        obj = require_keys(value, {"body", "reason"})
        return {
            "body": validate_body(obj["body"], allow_owner_attested=allow_owner_attested),
            "reason": require_str(obj["reason"], 500),
        }
    if event_type == "superseded":
        obj = require_keys(value, {"replacement_id", "reason"})
        return {
            "replacement_id": require_uuid(obj["replacement_id"]),
            "reason": require_str(obj["reason"], 500),
        }
    if event_type == "conflict_opened":
        obj = require_keys(value, {"other_id", "reason"})
        return {
            "other_id": require_uuid(obj["other_id"]),
            "reason": require_str(obj["reason"], 500),
        }
    if event_type == "conflict_resolved":
        obj = require_keys(value, {"other_id", "opened_event_id", "reason"})
        return {
            "other_id": require_uuid(obj["other_id"]),
            "opened_event_id": require_uuid(obj["opened_event_id"]),
            "reason": require_str(obj["reason"], 500),
        }
    if event_type == "attested":
        obj = require_keys(value, {"approval", "reason"})
        approval = validate_approval(obj["approval"], allow_owner_attested=True)
        if approval["state"] != "owner_attested":
            raise LedgerError("INVALID_ARGUMENT")
        return {"approval": approval, "reason": require_str(obj["reason"], 500)}
    if event_type == "applied":
        return validate_applied(value)
    if event_type == "archived":
        obj = require_keys(value, {"reason"})
        return {"reason": require_str(obj["reason"], 500)}
    raise LedgerError("INVALID_ARGUMENT")


def validate_record_input(
    value: Any, *, allow_owner_attested: bool
) -> dict[str, Any]:
    obj = require_keys(value, {"request_id", "occurred_at", "body", "outcome"}, {"actor", "decision_id"})
    out = {
        "request_id": require_uuid(obj["request_id"]),
        "occurred_at": require_ts(obj["occurred_at"], allow_null=True),
        "body": validate_body(obj["body"], allow_owner_attested=allow_owner_attested),
        "outcome": validate_outcome(obj["outcome"]),
    }
    if obj.get("decision_id"):
        out["decision_id"] = require_uuid(obj["decision_id"])
    actor = require_actor(obj.get("actor"))
    if actor:
        out["actor"] = actor
    return out


def validate_append_input(
    value: Any, *, allow_owner_attested: bool
) -> dict[str, Any]:
    obj = require_keys(
        value,
        {
            "request_id",
            "decision_id",
            "expected_revision",
            "occurred_at",
            "event_type",
            "payload",
        },
        {"actor"},
    )
    event_type = require_enum(
        obj["event_type"],
        APPEND_TYPES + (("attested", "archived") if allow_owner_attested else ()),
    )
    return {
        "request_id": require_uuid(obj["request_id"]),
        "decision_id": require_uuid(obj["decision_id"]),
        "expected_revision": require_int(obj["expected_revision"], min_v=1),
        "occurred_at": require_ts(obj["occurred_at"], allow_null=True),
        "event_type": event_type,
        "payload": validate_payload(event_type, obj["payload"], allow_owner_attested=allow_owner_attested),
        **({"actor": require_actor(obj["actor"])} if obj.get("actor") else {}),
    }


def validate_find_input(value: Any) -> dict[str, Any]:
    obj = require_keys(
        value,
        {"query"},
        {"entities", "constraints", "include_superseded", "limit", "offset", "task_id", "actor", "caller_scope"},
    )
    include = obj.get("include_superseded", False)
    if type(include) is not bool:
        raise LedgerError("INVALID_ARGUMENT")
    limit = obj.get("limit", 3)
    if type(limit) is int and limit > 3:
        raise LedgerError("INVALID_ARGUMENT", message="find limit max is 3")
    offset = obj.get("offset", 0)
    return {
        "query": require_str(obj["query"], 240, allow_empty=True),
        "entities": [nfc(item) for item in _str_list(obj.get("entities") or [], 128, 4)],
        "constraints": _str_list(obj.get("constraints") or [], 160, 4),
        "include_superseded": include,
        "limit": require_int(limit, min_v=1, max_v=3),
        "offset": require_int(offset, min_v=0, max_v=30),
        "task_id": require_uuid(obj["task_id"]) if obj.get("task_id") else None,
        "actor": require_actor(obj.get("actor")),
        "caller_scope": _validate_caller_scope(obj["caller_scope"]) if "caller_scope" in obj else None,
    }


def validate_get_input(value: Any) -> dict[str, Any]:
    obj = require_keys(value, {"decision_id"}, {"expected_revision", "actor"})
    out: dict[str, Any] = {"decision_id": require_uuid(obj["decision_id"]), "actor": require_actor(obj.get("actor"))}
    if "expected_revision" in obj:
        out["expected_revision"] = require_int(obj["expected_revision"], min_v=1)
    return out



# ---- compact decision traces (v2) -------------------------------------------------------
# A trace is what happened in one case: decision + why + short typed fields. Rules (policy)
# live in skills; a trace only names the rule it applied. Limits are enforced, never trimmed.
TRACE_TYPES = ("user_decision", "agent_conclusion")
TRACE_OUTCOMES = ("success", "failed", "partial", "pending")
FIX_KINDS = ("supersedes",)
# A/B (Guru 2026-10-17): inside the window find is ON on odd IST calendar days, OFF on even days.
# Saves and outcomes stay on. scripts/ledger_loop.py carries the same rule for its own log.
IST = timezone(timedelta(hours=5, minutes=30))
AB_START, AB_END = date(2026, 10, 18), date(2026, 10, 31)


def ab_arm(day: date) -> str:
    if not AB_START <= day <= AB_END:
        return "none"
    return "on" if day.day % 2 else "off"


def ist_today() -> date:
    """Today in IST; LEDGER_AB_DATE=YYYY-MM-DD pins it for dry runs and tests."""
    raw = os.environ.get("LEDGER_AB_DATE", "").strip()
    return date.fromisoformat(raw) if raw else datetime.now(IST).date()
CONTEXT_WORDS = 10
DECISION_WORDS = 15
WHY_WORDS = 20
ACTOR_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")


def require_actor(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not ACTOR_RE.fullmatch(value):
        raise LedgerError(
            "INVALID_ARGUMENT",
            message="actor must be a lowercase slug like smm-bot (a-z 0-9 . _ -, max 40)",
        )
    return value


def _line(value: Any, field: str, *, max_chars: int, max_words: int | None = None, required: bool = True) -> str | None:
    if value is None or value == "":
        if required:
            raise LedgerError("INVALID_ARGUMENT", message=f"{field} is required; nothing was saved")
        return None
    if not isinstance(value, str) or "\x00" in value:
        raise LedgerError("INVALID_ARGUMENT", message=f"{field} must be a string; nothing was saved")
    if "\n" in value or "\r" in value:
        raise LedgerError("INVALID_ARGUMENT", message=f"{field} must be one line; nothing was saved")
    words = len(value.split())
    if max_words is not None and words > max_words:
        raise LedgerError(
            "INVALID_ARGUMENT",
            message=f"{field} is {words} words; max {max_words}. Shorten it; nothing was saved",
        )
    if len(value) > max_chars:
        raise LedgerError(
            "INVALID_ARGUMENT",
            message=f"{field} is {len(value)} chars; max {max_chars}. Shorten it; nothing was saved",
        )
    return value.strip()


def _short_list(value: Any, field: str, *, max_n: int, max_chars: int) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > max_n:
        raise LedgerError("INVALID_ARGUMENT", message=f"{field} takes at most {max_n} items; nothing was saved")
    return [nfc(_line(item, field, max_chars=max_chars)) for item in value]  # type: ignore[arg-type]


def validate_trace(value: Any) -> dict[str, Any]:
    """The typed trace fields stored inside a body. Shared by save and replay."""
    obj = require_keys(
        value,
        {"type", "criticality", "approver"},
        {
            "rule", "exception", "override_by", "inputs", "state", "precedent_ids", "task_id",
            "fix_kind", "fix_of", "context", "expires_at", "replace_when",
        },
    )
    rtype = obj["type"]
    if rtype not in TRACE_TYPES:
        raise LedgerError("INVALID_ARGUMENT", message="type must be user_decision or agent_conclusion")
    crit = obj["criticality"]
    if type(crit) is not int or not 1 <= crit <= 5:
        raise LedgerError("INVALID_ARGUMENT", message="criticality must be an integer 1-5; nothing was saved")
    approver = _line(obj["approver"], "approver", max_chars=40)
    if rtype == "user_decision" and approver != "guru":
        raise LedgerError("INVALID_ARGUMENT", message="a user_decision needs approver guru; nothing was saved")
    out: dict[str, Any] = {"type": rtype, "criticality": crit, "approver": approver}
    for key, limit in (("rule", 80), ("exception", 80), ("override_by", 40), ("state", 60)):
        got = _line(obj.get(key), key, max_chars=limit, required=False)
        if got is not None:
            out[key] = got
    inputs = _short_list(obj.get("inputs"), "inputs", max_n=4, max_chars=60)
    if inputs:
        out["inputs"] = inputs
    prec = obj.get("precedent_ids") or []
    if not isinstance(prec, list) or len(prec) > 4:
        raise LedgerError("INVALID_ARGUMENT", message="precedent_ids takes at most 4 ids; nothing was saved")
    prec = [require_uuid(item) for item in prec]
    if len(prec) != len(set(prec)):
        raise LedgerError("INVALID_ARGUMENT", message="precedent_ids repeat; nothing was saved")
    if prec:
        out["precedent_ids"] = prec
    if obj.get("task_id"):
        out["task_id"] = require_uuid(obj["task_id"])
    if obj.get("fix_kind") is not None or obj.get("fix_of") is not None:
        if obj.get("fix_kind") not in FIX_KINDS:
            raise LedgerError("INVALID_ARGUMENT", message="fix_kind must be supersedes; nothing was saved")
        out["fix_kind"] = obj["fix_kind"]
        out["fix_of"] = require_uuid(obj.get("fix_of"))
        if out["fix_of"] in prec:
            raise LedgerError("INVALID_ARGUMENT", message="the decision you fix is not also a precedent; nothing was saved")
        out["context"] = _line(obj.get("context"), "context", max_chars=80, max_words=CONTEXT_WORDS)
    elif obj.get("context") is not None:
        raise LedgerError("INVALID_ARGUMENT", message="context goes with supersedes; nothing was saved")
    if obj.get("expires_at"):
        out["expires_at"] = require_ts(obj["expires_at"])
    if obj.get("replace_when"):
        out["replace_when"] = _line(obj["replace_when"], "replace_when", max_chars=80, max_words=12)
    return out


def validate_save_input(value: Any) -> dict[str, Any]:
    """Compact save: decision <=15 words, why <=20 words, everything else short typed fields."""
    obj = require_keys(
        value,
        {"request_id", "type", "decision", "why", "entities", "scope"},
        {
            "criticality", "rule", "exception", "override_by", "approver", "inputs", "state",
            "outcome", "precedent_ids", "task_id", "actor", "supersedes", "context",
            "kind", "expires_at", "replace_when", "evidence",
        },
    )
    rtype = obj["type"]
    if rtype not in TRACE_TYPES:
        raise LedgerError("INVALID_ARGUMENT", message="type must be user_decision or agent_conclusion")
    decision = _line(obj["decision"], "decision", max_chars=120, max_words=DECISION_WORDS)
    why = _line(obj["why"], "why", max_chars=160, max_words=WHY_WORDS)
    entities = _short_list(obj["entities"], "entities", max_n=4, max_chars=40)
    if not entities:
        raise LedgerError("INVALID_ARGUMENT", message="entities needs 1-4 names (repo, plugin, site, bot); nothing was saved")
    crit = obj.get("criticality")
    if crit is None:
        if rtype == "agent_conclusion":
            raise LedgerError("INVALID_ARGUMENT", message="agent_conclusion needs criticality 1-5; nothing was saved")
        crit = 5
    actor = require_actor(obj.get("actor"))
    approver = obj.get("approver") or ("guru" if rtype == "user_decision" else actor)
    if approver is None:
        raise LedgerError("INVALID_ARGUMENT", message="approver (or actor) is required; nothing was saved")
    kind = obj.get("kind") or "decision"
    if kind not in ("decision", "state"):
        raise LedgerError("INVALID_ARGUMENT", message="kind must be decision or state; nothing was saved")
    scope = parse_applicability_scope(obj["scope"])
    expires_at = require_ts(obj["expires_at"]) if obj.get("expires_at") else None
    replace_when = (
        _line(obj["replace_when"], "replace_when", max_chars=80, max_words=12) if obj.get("replace_when") else None
    )
    inputs = obj.get("inputs") or []
    input_text = " ".join(inputs) if isinstance(inputs, list) else ""
    assert_save_text(
        decision_surface(decision, why, obj.get("rule"), obj.get("exception"), obj.get("state"), input_text),
        kind=kind,
        expires_at=expires_at,
        replace_when=replace_when,
    )
    evidence_in = obj.get("evidence") or []
    if isinstance(evidence_in, list):
        evidence_in = [
            {"ref": item, "revision": None, "sha256": None, "state": "unverified"} if isinstance(item, str) else item
            for item in evidence_in
        ]
    evidence = validate_evidence_list(evidence_in)
    trace_in = {
        "type": rtype,
        "criticality": crit,
        "approver": approver,
        **{k: obj[k] for k in ("rule", "exception", "override_by", "inputs", "state", "precedent_ids", "task_id", "expires_at", "replace_when") if obj.get(k) is not None},
    }
    fixes = [k for k in FIX_KINDS if obj.get(k)]
    if fixes:
        trace_in["fix_kind"] = fixes[0]
        trace_in["fix_of"] = obj[fixes[0]]
        if not obj.get("context"):
            raise LedgerError(
                "INVALID_ARGUMENT",
                message=f"{fixes[0]} needs context (<=10 words: where the old decision failed); nothing was saved",
            )
    if obj.get("context") is not None:
        trace_in["context"] = obj["context"]
    if rtype == "user_decision" and not trace_in.get("override_by"):
        trace_in["override_by"] = "guru"
    trace = validate_trace(trace_in)
    outcome = obj.get("outcome") or "pending"
    if outcome not in TRACE_OUTCOMES:
        raise LedgerError("INVALID_ARGUMENT", message="outcome must be success, failed, partial or pending")
    return {
        "request_id": require_uuid(obj["request_id"]),
        "decision": decision,
        "why": why,
        "entities": entities,
        "trace": trace,
        "outcome": {"partial": "inconclusive"}.get(outcome, outcome),
        "actor": actor,
        "scope": scope,
        "kind": kind,
        "evidence": evidence,
    }


def request_hash(tool: str, arguments: dict[str, Any]) -> str:
    payload = dict(arguments)
    payload.pop("request_id", None)
    return sha256_text(canonical_json({"tool": tool, "arguments": payload}))


def fts_tokens(query: str) -> list[str]:
    folded = nfc(query).casefold()
    tokens: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if not buf:
            return
        tok = "".join(buf)
        buf.clear()
        if 0 < len(tok) <= FTS_TOKEN_MAX:
            tokens.append(tok)

    for ch in folded:
        if ch.isalnum():
            buf.append(ch)
        else:
            flush()
    flush()
    return tokens[:FTS_TOKEN_LIMIT]


def truncate_unicode(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    if max_len <= 1:
        return "…"
    return text[: max_len - 1] + "…"


def ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def tool_content(payload: dict[str, Any], *, structured: bool) -> tuple[str, dict[str, Any] | None]:
    """JSON for one MCP tool result. structured=False is a single text copy (find and get)."""
    text = canonical_json(payload)
    if not structured:
        return text, None
    return text, payload


def fail_dict(exc: LedgerError) -> dict[str, Any]:
    return exc.as_dict()
