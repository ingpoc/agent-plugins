"""Closed request/event schemas, limits, and validation."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, TypedDict

SCHEMA_VERSION = 1
MAX_REQUEST_LINE = 64 * 1024
MAX_EVENT = 12 * 1024
MAX_BODY = 4 * 1024
MAX_FIND_RESPONSE = 4 * 1024
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
RETRYABLE = {"BUSY", "MAINTENANCE"}

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
)
CHANNEL = ("agent", "owner_cli")
LIFECYCLE = ("active", "superseded")
APPEND_TYPES = (
    "outcome",
    "corrected",
    "superseded",
    "conflict_opened",
    "conflict_resolved",
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


AppendPayload = (
    OutcomeInput
    | CorrectedPayload
    | SupersededPayload
    | ConflictOpenedPayload
    | ConflictResolvedPayload
)
McpAppendPayload = (
    OutcomeInput
    | McpCorrectedPayload
    | SupersededPayload
    | ConflictOpenedPayload
    | ConflictResolvedPayload
)
AppendEventType = Literal[
    "outcome",
    "corrected",
    "superseded",
    "conflict_opened",
    "conflict_resolved",
]


class LedgerError(Exception):
    def __init__(self, code: str, retryable: bool | None = None) -> None:
        if code not in ERROR_CODES:
            code = "INTERNAL"
        self.code = code
        self.retryable = RETRYABLE.__contains__(code) if retryable is None else retryable
        super().__init__(code)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": False, "error": {"code": self.code, "retryable": self.retryable}}


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
    if len(canonical_json(body).encode("utf-8")) > MAX_BODY:
        raise LedgerError("LIMIT_EXCEEDED")
    return body


def validate_outcome(value: Any) -> dict[str, Any]:
    obj = require_keys(value, {"status", "note", "evidence"})
    return {
        "status": require_enum(obj["status"], OUTCOME_STATUS),
        "note": require_str(obj["note"], 500, allow_empty=True),
        "evidence": validate_evidence_list(obj["evidence"]),
    }


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
    raise LedgerError("INVALID_ARGUMENT")


def validate_record_input(
    value: Any, *, allow_owner_attested: bool
) -> dict[str, Any]:
    obj = require_keys(value, {"request_id", "occurred_at", "body", "outcome"})
    return {
        "request_id": require_uuid(obj["request_id"]),
        "occurred_at": require_ts(obj["occurred_at"], allow_null=True),
        "body": validate_body(obj["body"], allow_owner_attested=allow_owner_attested),
        "outcome": validate_outcome(obj["outcome"]),
    }


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
    )
    event_type = require_enum(
        obj["event_type"],
        APPEND_TYPES + (("attested",) if allow_owner_attested else ()),
    )
    return {
        "request_id": require_uuid(obj["request_id"]),
        "decision_id": require_uuid(obj["decision_id"]),
        "expected_revision": require_int(obj["expected_revision"], min_v=1),
        "occurred_at": require_ts(obj["occurred_at"], allow_null=True),
        "event_type": event_type,
        "payload": validate_payload(event_type, obj["payload"], allow_owner_attested=allow_owner_attested),
    }


def validate_find_input(value: Any) -> dict[str, Any]:
    obj = require_keys(
        value,
        {"query", "entities", "constraints"},
        {"include_superseded", "limit", "offset"},
    )
    include = obj.get("include_superseded", False)
    if type(include) is not bool:
        raise LedgerError("INVALID_ARGUMENT")
    limit = obj.get("limit", 3)
    offset = obj.get("offset", 0)
    return {
        "query": require_str(obj["query"], 240, allow_empty=True),
        "entities": [nfc(item) for item in _str_list(obj["entities"], 128, 4)],
        "constraints": _str_list(obj["constraints"], 160, 4),
        "include_superseded": include,
        "limit": require_int(limit, min_v=1, max_v=3),
        "offset": require_int(offset, min_v=0, max_v=30),
    }


def validate_get_input(value: Any) -> dict[str, Any]:
    obj = require_keys(value, {"decision_id"}, {"expected_revision"})
    out: dict[str, Any] = {"decision_id": require_uuid(obj["decision_id"])}
    if "expected_revision" in obj:
        out["expected_revision"] = require_int(obj["expected_revision"], min_v=1)
    return out


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


def fail_dict(exc: LedgerError) -> dict[str, Any]:
    return exc.as_dict()
