#!/usr/bin/env python3
"""Read and edit individual findings in the authoritative JSON export."""
import argparse
import copy
import hashlib
from datetime import datetime, timezone
import importlib.util
import json
import os
import re
import stat
import sys
import tempfile
import uuid
from pathlib import Path

import tracker_lock

import live_state
import check_install_sessions
import workflow_definition
import check_gate_count

RENDERER_PATH = Path(__file__).with_name("render.py")
SPEC = importlib.util.spec_from_file_location("change_list_renderer", RENDERER_PATH)
RENDERER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RENDERER)


def stamp_plan_group(group, now):
    group["updated_at"] = now
    codex = group.get("codex")
    verify = group.get("verify")
    if isinstance(codex, dict):
        if codex.get("status") == "running": codex["updated_at"] = now
        for record in (group, codex):
            start, end = record.get("started_at"), record.get("ended_at")
            if isinstance(start, str) and isinstance(end, str):
                try: record["duration_s"] = max(0, int((datetime.fromisoformat(end.replace("Z", "+00:00")) - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds()))
                except (ValueError, TypeError) as error:
                    raise ValueError("invalid plan timestamps; correct started_at and ended_at") from error
    if isinstance(verify, dict):
        start, end = verify.get("started_at"), verify.get("ended_at")
        if isinstance(start, str) and isinstance(end, str):
            try: verify["duration_s"] = max(0, int((datetime.fromisoformat(end.replace("Z", "+00:00")) - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds()))
            except (ValueError, TypeError) as error:
                raise ValueError("invalid verification timestamps; correct started_at and ended_at") from error


def warn_summary(item, adding=False):
    validate_plain_evidence({field: item[field] for field in ("title", "summary", "impact", "done_when")
                             if isinstance(item.get(field), str) and item[field].strip()})
    summary = item.get("summary")
    if adding and not summary:
        print("warning: add summary, impact and done_when in plain words", file=sys.stderr)
    elif isinstance(summary, str) and RENDERER.looks_like_log_text(summary):
        print("warning: summary looks like log text; use plain words", file=sys.stderr)

MAX_TRACKER_BYTES = 320 * 1024
MAX_DETAIL_CHARS = 600
MAX_DETAIL_KEYS = 8
CLOSED_STATUSES = {"verified_fixed", "closed"}
DETAIL_DENY_PATTERNS = tuple(re.compile(pattern, re.I) for pattern in (
    r"\bcurrent step\b",
    r"\b(?:previous|earlier)\b.*\b(?:verification|test evidence|installation|acceptance attempt|self-check|controlled test|conclusion correction)\b",
    r"\bopen\s*code\b.*\b(?:pilot|provider review)\b",
    r"\bmanifest workflow trial\b",
    r"\brollover skill experiment\b",
    r"\bskill experiment pilot\b",
    r"\bevaluation workflow\b",
    r"\b(?:updated workflow|marker discovery|verification recheck)\b.*\b(?:pilot|experiment|trial|review|recheck)\b",
    r"\bverification recheck\b",
    r"\bastra low\b.*\b(?:experiment|review)\b",
    r"\b(?:source|installed|live) pilot\b",
    r"\b(?:capture|evidence) hardening\b",
    r"\b(?:experiment quality correction|rollover recovery test|comparison eligibility|review provenance|reusable workflow direction|recommendation test coverage)\b",
))

def array_end(text, start):
    depth = 0
    quoted = False
    escaped = False
    for index in range(start, len(text)):
        character = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character == "[":
            depth += 1
        elif character == "]":
            depth -= 1
            if depth == 0:
                return index + 1
    raise ValueError("unterminated JSON array in HTML")


def html_region(text, collection):
    if collection == "findings":
        tags = list(re.finditer(
            r'<script\b(?=[^>]*\bid=["\']findings-data["\'])[^>]*>', text, re.IGNORECASE))
        if tags:
            if len(tags) != 1:
                raise ValueError("HTML must contain one findings-data block")
            start = tags[0].end()
            close = re.search(r"</script\s*>", text[start:], re.IGNORECASE)
            if close is None:
                raise ValueError("findings-data block is missing its closing script tag")
            return start, start + close.start()
    if not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", collection):
        raise ValueError("collection must be a simple JavaScript identifier")
    assignments = list(re.finditer(
        r"\b(?:const|let|var)\s+" + re.escape(collection) + r"\s*=", text))
    if len(assignments) != 1:
        raise ValueError(f"expected one JSON array named {collection} in HTML")
    start = assignments[0].end()
    while start < len(text) and text[start].isspace():
        start += 1
    if start >= len(text) or text[start] != "[":
        raise ValueError(f"{collection} is not an array")
    return start, array_end(text, start)


def load(path, collection):
    path = path.resolve(strict=True)
    original = path.read_bytes()
    if path.suffix.lower() in {".html", ".htm"}:
        text = original.decode("utf-8")
        start, end = html_region(text, collection)
        findings = json.loads(text[start:end])
        store = {"kind": "html", "text": text, "start": start, "end": end, "collection": collection}
    else:
        document = json.loads(original)
        if not isinstance(document, dict) or not isinstance(document.get(collection), list):
            raise ValueError(f"data file must be an object with a {collection} array")
        findings = document[collection]
        store = {"kind": "json", "document": document, "collection": collection}
    validate_ids(findings)
    return path, original, store, findings


def validate_ids(findings):
    seen = set()
    for index, item in enumerate(findings):
        if not isinstance(item, dict):
            raise ValueError(f"findings[{index}] must be an object")
        identifier = item.get("id")
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError(f"findings[{index}].id must be a nonempty string")
        if identifier in seen:
            raise ValueError(f"duplicate id: {identifier}")
        seen.add(identifier)


def read_json(path):
    if str(path) == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(path).read_text(encoding="utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid JSON input: {error}") from error


def find_index(findings, identifier):
    for index, item in enumerate(findings):
        if item["id"] == identifier:
            return index
    raise ValueError(f"finding not found: {identifier}")


def display_record(item, include_inline_evidence):
    def redact(value):
        if isinstance(value, dict):
            return {key: redact(child) for key, child in value.items()}
        if isinstance(value, list):
            return [redact(child) for child in value]
        if (not include_inline_evidence and isinstance(value, str)
                and value.startswith("data:image/") and "," in value):
            header, payload = value.split(",", 1)
            return f"[inline {header[5:]} omitted; about {len(payload) * 3 // 4} bytes; use --include-evidence to retrieve]"
        return value

    return redact(copy.deepcopy(item))


def merge_patch(target, patch):
    """Apply JSON Merge Patch semantics: objects merge, arrays replace, null deletes."""
    for key, value in patch.items():
        if key == "id":
            raise ValueError("finding IDs are immutable")
        if value is None:
            target.pop(key, None)
        elif isinstance(value, dict):
            current = target.get(key)
            if not isinstance(current, dict):
                current = {}
            target[key] = merge_patch(current, value)
        else:
            target[key] = value
    return target


RAW_EVIDENCE_PATTERNS = (
    re.compile(r"\b[\w.-]+\.(?:py|js|ts|tsx|jsx|json|html|css|sh|log|swift|md|yaml|yml)\b", re.I),
    re.compile(r"\b[a-z][a-z0-9]*_[a-z0-9_]+\b", re.I),
    re.compile(r"\b[a-z][\w-]*\.[a-z_][\w]*\b", re.I),
    re.compile(r"\b[a-f0-9]{7,}\b", re.I),
    re.compile(r"(?<!\w)--[\w-]+"),
    re.compile(r"\b(?:Named tests|Log)\s*:", re.I),
)


def validate_plain_evidence(fields):
    for label, value in fields.items():
        if "/" in value or any(pattern.search(value) for pattern in RAW_EVIDENCE_PATTERNS):
            raise ValueError(f"{label} contains technical identifiers; move them to reference or details")


def validate_record(record):
    fields = {"checked", "expected", "observed", "verdict", "build", "session", "timestamp", "reference"}
    identified = fields | {"check_id"}
    optional_details = {"details"}
    legacy = {"check", "outcome", "build", "session", "timestamp", "reference"}
    allowed = (fields, identified, legacy, fields | optional_details, identified | optional_details, legacy | optional_details)
    if not isinstance(record, dict) or set(record) not in allowed or any(not isinstance(record[k], str) or (k != "details" and not record[k].strip()) for k in record):
        raise ValueError("evidence requires nonempty verification and metadata strings")
    if set(record) in (fields, identified) and not all(record[k].strip() for k in ("checked", "expected", "observed")):
        raise ValueError("verification reason requires checked, expected and observed parts")
    validate_plain_evidence({k: record[k] for k in ("checked", "expected", "observed", "verdict") if k in record})
    if record["timestamp"] != "unknown":
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", record["timestamp"]):
                raise ValueError()
            if datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00")).tzinfo is None:
                raise ValueError()
        except ValueError:
            raise ValueError("evidence timestamp must be ISO 8601 with timezone or unknown") from None


def validate_verification_reason(reason):
    parts = {match.group(1).title(): match.group(2).strip() for match in
             re.finditer(r"(?:^|[;\n])\s*(Checked|Expected|Observed)\s*:\s*([^;\n]*)", reason, re.I)}
    if any(not parts.get(label) for label in ("Checked", "Expected", "Observed")):
        raise ValueError("verification reason requires nonempty Checked, Expected and Observed parts")
    verdict = re.search(r"(?:^|[;\n])\s*Verdict because\s*:\s*([^;\n]*)", reason, re.I)
    validate_plain_evidence({**parts, **({"verdict": verdict.group(1).strip()} if verdict else {})})


def verification_check(document, finding_id, check_id):
    tracking = document.get("tracking", {})
    runs = [*tracking.get("verification_history", [])]
    archive = tracking.get("archive")
    archive_path = Path(archive["path"]) if isinstance(archive, dict) and isinstance(archive.get("path"), str) else None
    if archive_path and archive_path.is_file():
        runs.extend(row for row in json.loads(archive_path.read_text(encoding="utf-8"))
                    if isinstance(row, dict) and row.get("record_type") == "verification_history")
    runs.append(tracking.get("verification_run", {}))
    matches = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        check = run.get("checks", {}).get(finding_id)
        if isinstance(check, dict):
            if check.get("check_id") == check_id:
                matches.append(check)
            matches.extend(previous for previous in check.get("previous_checks", [])
                           if isinstance(previous, dict) and previous.get("check_id") == check_id)
    if len(matches) > 1:
        raise ValueError("check_id matches multiple verification checks")
    return matches[0] if matches else None


def store_verification_evidence(document, item, record):
    records = item.setdefault("verification_evidence", [])
    if not isinstance(records, list):
        raise ValueError("verification_evidence must be an array")
    check = verification_check(document, item["id"], record["check_id"])
    if check is not None:
        check.update(record)
        item["verification_evidence"] = [previous for previous in records
                                          if not isinstance(previous, dict) or previous.get("check_id") != record["check_id"]]
        return
    matches = [i for i, previous in enumerate(records) if isinstance(previous, dict) and
               previous.get("check_id") == record["check_id"]]
    if matches:
        records[matches[0]] = record
        item["verification_evidence"] = [previous for i, previous in enumerate(records)
                                          if i == matches[0] or not isinstance(previous, dict) or
                                          previous.get("check_id") != record["check_id"]]
    else:
        records.append(record)


def validate_plan_group_fields(group, round_number):
    status = group.get("status")
    valid_statuses = RENDERER.PLAN_STATUSES | set(RENDERER.PLAN_STATUS_ALIASES) | {"done"}
    if status not in valid_statuses:
        raise ValueError(f"plan group {group.get('id')} has invalid status")
    valid_phases = RENDERER.PLAN_PHASES | set(RENDERER.PLAN_PHASE_ALIASES) | {"blocked", "deferred"}
    if "phase" in group and group["phase"] not in valid_phases:
        raise ValueError(f"plan group {group.get('id')} has invalid phase")
    if status in ("blocked", "failed") and group.get("phase") not in RENDERER.PLAN_PHASES:
        raise ValueError(f"plan group {group.get('id')} requires a phase when {status}")
    reason = group.get("blocked_reason")
    if reason is not None and (not isinstance(reason, str) or len(reason.split()) > 6):
        raise ValueError(f"plan group {group.get('id')} blocked_reason must be at most 6 words")
    if status == "blocked" and not reason:
        raise ValueError(f"plan group {group.get('id')} requires blocked_reason")
    if status == "deferred" and group.get("parked_to") is None:
        group["parked_to"] = round_number + 1
    parked_to = group.get("parked_to")
    if parked_to is not None and (type(parked_to) is not int or parked_to < 1):
        raise ValueError(f"plan group {group.get('id')} parked_to must be a positive round number")
    if status == "parked" and parked_to is None:
        raise ValueError(f"plan group {group.get('id')} requires parked_to")
    for field in ("build", "updated_at", "quota_wait_until"):
        if field in group and not isinstance(group[field], str):
            raise ValueError(f"plan group {group.get('id')} {field} must be a string")
    if group.get("updated_at") and RENDERER.parse_instant(group["updated_at"]) is None:
        raise ValueError(f"plan group {group.get('id')} updated_at must be an ISO 8601 timestamp")
    if group.get("quota_wait_until") and RENDERER.parse_instant(group["quota_wait_until"]) is None:
        raise ValueError(f"plan group {group.get('id')} quota_wait_until must be an ISO 8601 timestamp")


def require_override_reason(reason, action):
    if not isinstance(reason, str) or not reason.strip() or "\n" in reason or "\r" in reason:
        raise ValueError(f"{action} override requires a one-line nonblank --reason")
    return reason.strip()


def parse_review_reply(reply):
    if not isinstance(reply, str):
        return None, None
    first = next((line for line in reply.splitlines() if line.strip()), None)
    if first not in ("VERDICT: PASS", "VERDICT: CHANGES"):
        return None, None
    verdict = first.partition(":")[2].strip()
    body = _section_body(reply, "Must-fix")
    if not body:
        return None, None
    if verdict == "PASS" and body.casefold().rstrip(".") != "none":
        return None, None
    if verdict == "CHANGES" and not any(re.match(r"\s*(?:[-*+]\s+|\d+[.)]\s+)", line) for line in body.splitlines()):
        return None, None
    return verdict, body


def _section_body(reply, title):
    lines = reply.splitlines()
    heading = rf"\s*(?:#{{1,6}}\s*)?\**{re.escape(title)}\**\s*(?::\**\s*(.*)|\s*)"
    start = next((index for index, line in enumerate(lines) if re.fullmatch(heading, line, re.I)), None)
    if start is None:
        return ""
    end = next((index for index in range(start + 1, len(lines))
                if re.match(r"^\s*(?:#{1,6}\s+|\**[A-Z][\w -]*\**:)", lines[index])), len(lines))
    inline = re.fullmatch(heading, lines[start], re.I).group(1) or ""
    return "\n".join([inline, *lines[start + 1:end]]).strip()


def _lane_groups(document, ids):
    tracking = document.setdefault("tracking", {})
    plan = tracking.get("plan")
    if (not isinstance(ids, list) or not ids or len(ids) != len(set(ids))
            or not isinstance(plan, dict) or plan.get("cycle", 1) != RENDERER.rollover_cycle(tracking)
            or plan.get("round", 1) != RENDERER.rollover_round(tracking)):
        raise ValueError("job requires unique IDs covered by the current recorded plan")
    groups = [group for group in plan.get("groups", []) if set(ids).intersection(group.get("finding_ids", []))]
    covered = {identifier for group in groups for identifier in set(ids).intersection(group.get("finding_ids", []))}
    if not groups or covered != set(ids):
        raise ValueError("job IDs must be covered by current plan groups")
    for group in groups:
        _lane_history(group, "failure_history")
        _lane_history(group, "plan_review_history")
    return tracking, plan, groups


def _lane_history(group, field):
    history = group.get(field, [])
    if isinstance(history, dict):
        if any(not isinstance(rows, list) for rows in history.values()):
            raise ValueError(f"{group.get('id')} {field} legacy entries must be arrays")
        history = [row for rows in history.values() if isinstance(rows, list) for row in rows]
    if not isinstance(history, list) or any(not isinstance(row, dict) for row in history):
        raise ValueError(f"{group.get('id')} {field} must be an array")
    ordered = list(enumerate(history))
    def chronology(item):
        index, row = item
        return (_history_instant(row) or datetime.min.replace(tzinfo=timezone.utc), index)
    ordered.sort(key=chronology)
    unique, seen = [], set()
    for _, row in ordered:
        key = ((row.get("attempt_id"),) if field == "failure_history" else
               (row.get("job_id"), row.get("after_failure")))
        if key[0] and key in seen:
            continue
        if key[0]:
            seen.add(key)
        unique.append(row)
    group[field] = unique
    if field == "failure_history":
        failures = _counted_failures(unique)
        if len(failures) >= 2 and not _unblock_covers_failures(group, failures):
            group.update(status="blocked", phase=group.get("phase", "fix"))
            group.setdefault("blocked_reason", "Two counted lane failures require an owner unblock")
            group.setdefault("failure_block_reason", group["blocked_reason"])
    return unique


def _counted_failures(rows):
    counted, attempts = [], set()
    for row in rows:
        attempt = row.get("attempt_id")
        if row.get("tool_fault") or row.get("source") == "plan_review" or (attempt and attempt in attempts):
            continue
        if attempt:
            attempts.add(attempt)
        counted.append(row)
    return counted


def _history_instant(row):
    return RENDERER.parse_instant(row.get("at") or row.get("timestamp") or row.get("recorded_at") or "")


def _unblock_covers_failures(group, failures):
    if len(failures) < 2:
        return True
    history = group.get("unblock_history", [])
    if not isinstance(history, list) or any(not isinstance(row, dict) for row in history):
        raise ValueError(f"{group.get('id')} unblock_history must be an array")
    for row in history:
        if not row.get("owner_override"):
            continue
        covered = set(row.get("covered_attempts", []))
        unblocked_at = _history_instant(row)
        if all((failure.get("attempt_id") in covered) or
               (unblocked_at and _history_instant(failure) and unblocked_at > _history_instant(failure))
               for failure in failures):
            return True
    return False


def plan_candidate_sha256(plan):
    candidate = copy.deepcopy(plan)
    for group in candidate.get("groups", []):
        for field in ("status", "phase", "presuite", "updated_at", "failure_history", "unblock_history",
                      "plan_review_history", "blocked_reason", "failure_block_reason"):
            group.pop(field, None)
        verify = group.get("verify")
        if isinstance(verify, dict):
            for field in ("status", "fixed", "failed", "total", "failure"):
                verify.pop(field, None)
            if not verify:
                group.pop("verify", None)
        codex = group.get("codex")
        if isinstance(codex, dict):
            for field in ("job", "fix_status", "review_status", "plan_review_status", "reviewer_model", "must_fix_note"):
                codex.pop(field, None)
            if not codex:
                group.pop("codex", None)
    return hashlib.sha256(json.dumps(candidate, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def carry_plan_runtime_state(groups, previous, same_round):
    old = {group.get("id"): group for group in previous.get("groups", [])} if isinstance(previous, dict) else {}
    protected_codex = ("job", "fix_status", "review_status", "plan_review_status", "reviewer_model", "must_fix_note")
    protected_group = ("status", "phase", "presuite", "failure_history", "plan_review_history", "unblock_history", "blocked_reason", "failure_block_reason")
    for prior in old.values():
        _lane_history(prior, "failure_history")
        _lane_history(prior, "plan_review_history")
    def gated(group):
        return group.get("status") == "blocked" or bool(_counted_failures(_lane_history(group, "failure_history")))

    replacements = {group.get("id"): group for group in groups}
    for identifier, prior in old.items():
        if gated(prior):
            replacement = replacements.get(identifier)
            if not replacement or set(replacement.get("finding_ids", [])) != set(prior.get("finding_ids", [])):
                raise ValueError(f"set-plan cannot change gated lane {identifier}; keep its id and findings")
    for group in groups:
        prior = old.get(group.get("id"))
        unchanged = prior and set(group.get("finding_ids", [])) == set(prior.get("finding_ids", []))
        if prior and unchanged and (same_round or gated(prior)):
            for field in protected_group:
                if field in prior and (same_round or field in ("failure_history", "plan_review_history", "unblock_history", "blocked_reason", "failure_block_reason") or prior.get("status") == "blocked" and field == "status"):
                    group[field] = copy.deepcopy(prior[field])
                else:
                    group.pop(field, None)
            codex = group.setdefault("codex", {})
            previous_codex = prior.get("codex") if isinstance(prior.get("codex"), dict) else {}
            for field in protected_codex if same_round else ():
                if field in previous_codex:
                    codex[field] = copy.deepcopy(previous_codex[field])
                else:
                    codex.pop(field, None)
            if group.get("status") == "blocked" and not group.get("phase"):
                group["phase"] = prior.get("phase") or "fix"
            continue
        codex = group.get("codex") if isinstance(group.get("codex"), dict) else {}
        claims_runtime = (
            "job" in codex or codex.get("fix_status") not in (None, "pending")
            or codex.get("review_status") not in (None, "pending")
            or codex.get("plan_review_status") not in (None, "pending")
            or bool(codex.get("reviewer_model")) or bool(codex.get("must_fix_note"))
            or (isinstance(group.get("presuite"), dict) and group["presuite"].get("status") not in (None, "pending"))
        )
        if RENDERER.plan_status(group) == "verified" or claims_runtime:
            raise ValueError("set-plan cannot claim completed job state; run bcl job for the current lane")


def _record_lane_failure(document, identifier, attempt_id, source, reason):
    if not isinstance(document.get("tracking", {}).get("plan"), dict):
        return
    tracking, plan, groups = _lane_groups(document, [identifier])
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    for group in groups:
        rows = _lane_history(group, "failure_history")
        if not any(row.get("attempt_id") == attempt_id for row in rows):
            rows.append({"attempt_id": attempt_id, "source": source, "reason": reason,
                         "cycle": plan.get("cycle"), "round": plan.get("round"), "at": stamp})
        if len(_counted_failures(rows)) >= 2:
            group.update(status="blocked", phase=group.get("phase", "fix"),
                         blocked_reason="Second failure", failure_block_reason=reason)
            codex = group.setdefault("codex", {})
            codex["review_status"] = "blocked"
            for item in document.get("findings", []):
                if item.get("id") in group.get("finding_ids", []):
                    item.update(status="blocked", needs=reason)


def unblock_lane(document, operation):
    tracking, plan, groups = _lane_groups(document, operation.get("ids"))
    reason = require_override_reason(operation.get("reason"), "unblock")
    tool_faults = operation.get("tool_faults", [])
    if (not isinstance(tool_faults, list)
            or any(not isinstance(value, str) or not value.strip() for value in tool_faults)
            or len(tool_faults) != len(set(tool_faults))):
        raise ValueError("unblock tool_faults must be unique nonempty attempt IDs")
    stamp = operation.get("timestamp")
    if not isinstance(stamp, str) or RENDERER.parse_instant(stamp) is None:
        raise ValueError("unblock timestamp must be an ISO 8601 instant")
    lanes, known_faults = [], set()
    for group in groups:
        if group.get("status") != "blocked":
            raise ValueError(f"unblock requires blocked lane {group.get('id')}")
        history = group.setdefault("unblock_history", [])
        if not isinstance(history, list):
            raise ValueError(f"{group.get('id')} unblock_history must be an array")
        rows = _lane_history(group, "failure_history")
        known_faults.update(row["attempt_id"] for row in rows
                            if isinstance(row.get("attempt_id"), str))
        lanes.append((group, history, rows))
    unknown = set(tool_faults) - known_faults
    if unknown:
        raise ValueError(f"unblock names unknown attempt ID(s): {', '.join(sorted(unknown))}")
    for group, history, rows in lanes:
        lane_faults = set(tool_faults).intersection(row.get("attempt_id") for row in rows)
        for row in rows:
            if row.get("attempt_id") in lane_faults:
                row.update(tool_fault=True, tool_fault_reason=reason)
        owner_override = len(_counted_failures(rows)) >= 2
        if owner_override and tool_faults:
            raise ValueError(f"unblock lane {group.get('id')} has two or more counted failures; owner override requires no --tool-fault")
        entry = {"at": stamp, "reason": reason, "tool_faults": sorted(lane_faults),
                 "covered_attempts": [row["attempt_id"] for row in _counted_failures(rows)
                                      if isinstance(row.get("attempt_id"), str)],
                 "by_cycle": plan.get("cycle", tracking.get("cycle", 1)),
                 "round": plan.get("round", tracking.get("round", 1))}
        if owner_override:
            entry["owner_override"] = True
        history.append(entry)
        group.update(status="running", phase="fix")
        group.pop("blocked_reason", None)
        group.pop("failure_block_reason", None)
        codex = group.setdefault("codex", {})
        codex.pop("review_status", None)
        codex.pop("plan_review_status", None)
        for item in document.get("findings", []):
            if item.get("id") in group.get("finding_ids", []) and item.get("status") == "blocked":
                item["status"] = "in_progress"


def job_transition(document, operation):
    tracking, plan, groups = _lane_groups(document, operation.get("ids"))
    phase, action = operation.get("phase"), operation["action"]
    if phase not in ("plan_review", "fix", "review", "presuite"):
        raise ValueError("job phase must be plan_review, fix, review, or presuite")
    cycle = plan.get("cycle", tracking.get("cycle", 1))
    round_number = plan.get("round", tracking.get("round", 1))
    if operation.get("cycle") != cycle or operation.get("round") != round_number:
        raise ValueError("job belongs to a stale plan cycle or round")
    stamp = operation.get("timestamp")
    if not isinstance(stamp, str) or RENDERER.parse_instant(stamp) is None:
        raise ValueError("job timestamp must be an ISO 8601 instant")
    if action in ("attach-job", "heartbeat-job"):
        job_id = operation.get("job_id")
        for group in groups:
            job = (group.get("codex") or {}).get("job")
            if not isinstance(job, dict) or job.get("id") != job_id or job.get("phase") != phase or "exit_code" in job:
                raise ValueError(f"stale {phase} job update for lane {group['id']}")
            if action == "attach-job":
                pid = operation.get("pid")
                if type(pid) is not int or pid < 1:
                    raise ValueError("job pid must be a positive integer")
                job["pid"] = pid
            job["heartbeat_at"] = stamp
        return
    if action == "start-job":
        job = operation.get("job")
        if not isinstance(job, dict) or not isinstance(job.get("id"), str) or not job["id"].strip():
            raise ValueError("job reservation requires an id")
        if operation.get("load_override") is not None:
            reason = require_override_reason(operation["load_override"], "machine-load")
            tracking.setdefault("machine_load_overrides", []).append(
                {"cycle": cycle, "round": round_number, "phase": phase, "reason": reason, "at": stamp})
        if job.get("plan_sha256") != plan_candidate_sha256(plan):
            raise ValueError("job reservation belongs to a stale plan candidate")
        for group in groups:
            codex = group.setdefault("codex", {})
            if group.get("status") in ("blocked", "parked", "deferred") or (
                    group.get("status") == "failed" and phase not in ("fix", "plan_review")):
                recovery = "; run bcl unblock --reason" if group.get("status") == "blocked" else ""
                raise ValueError(f"job cannot start blocked, parked, deferred, or failed lane {group['id']}{recovery}")
            failures = _counted_failures(_lane_history(group, "failure_history"))
            if phase == "fix":
                if len(failures) >= 2 and not _unblock_covers_failures(group, failures):
                    raise ValueError(f"{group['id']} is blocked after two counted failures; run bcl unblock --reason")
                if failures:
                    reviews = _lane_history(group, "plan_review_history")
                    latest_at = _history_instant(failures[-1])
                    ambiguous = sum(_history_instant(row) == latest_at for row in failures) > 1 and latest_at is not None
                    if not any(entry.get("status") == "passed" and entry.get("after_failure") == failures[-1].get("attempt_id")
                               and (not ambiguous or _history_instant(entry) and _history_instant(entry) > latest_at)
                               for entry in reviews):
                        raise ValueError(f"{group['id']} requires a passed plan_review after failure")
                codex.pop("review_status", None)
                codex.pop("plan_review_status", None)
                group.pop("presuite", None)
                group.update(status="running", phase="fix")
                codex["fix_status"] = "running"
            elif phase == "review":
                if group.get("phase") != "review" or codex.get("fix_status") != "passed":
                    raise ValueError(f"review requires lane {group['id']} to finish fix")
                group.update(status="review", phase="review")
                codex["review_status"] = "running"
                if isinstance(job.get("model"), str):
                    codex["reviewer_model"] = job["model"]
            elif phase == "presuite":
                if (codex.get("fix_status") != "passed" or
                        codex.get("review_status") not in ("passed", "skipped_one_file")):
                    raise ValueError(f"presuite requires lane {group['id']} review to pass or be skipped_one_file")
                group.update(status="review", phase="review")
            else:
                group.update(status="review", phase="plan_review")
            codex["job"] = {**job, "phase": phase, "started_at": stamp, "cycle": cycle, "round": round_number}
        workflow = tracking.get("workflow") if isinstance(tracking.get("workflow"), dict) else {}
        tracking["workflow"] = {**workflow, "stage": "review" if phase in ("plan_review", "review", "presuite") else "plan_fix",
                                 "updated_at": stamp, "now": f"{stamp} — {phase.replace('_', ' ').title()} started"}
        stages = tracking.setdefault("stages_reached", [])
        global_stage = tracking["workflow"]["stage"]
        if global_stage not in stages:
            stages.append(global_stage)
    elif action == "finish-job":
        job_id, exit_code = operation.get("job_id"), operation.get("exit_code")
        if not isinstance(job_id, str) or type(exit_code) is not int:
            raise ValueError("job completion requires a job id and integer exit code")
        if operation.get("plan_sha256") != plan_candidate_sha256(plan):
            raise ValueError("job completion belongs to a stale plan candidate")
        for group in groups:
            job = (group.get("codex") or {}).get("job")
            if (not isinstance(job, dict) or job.get("id") != job_id or job.get("phase") != phase
                    or job.get("cycle") != cycle or job.get("round") != round_number
                    or job.get("plan_sha256") != operation.get("plan_sha256")
                    or group.get("status") == "blocked" or "exit_code" in job):
                raise ValueError(f"stale {phase} completion for lane {group['id']}")
        reply = operation.get("reply")
        verdict, must_fix = parse_review_reply(reply) if phase in ("review", "plan_review") else (None, None)
        for group in groups:
            codex = group.setdefault("codex", {})
            job = codex.get("job")
            job.update(exit_code=exit_code, finished_at=stamp)
            if phase in ("review", "plan_review"):
                status = verdict if exit_code == 0 else None
                if status == "PASS":
                    job["finished_phase_status"] = "passed"
                    if phase == "plan_review":
                        codex["plan_review_status"] = "passed"
                        counted = _counted_failures(_lane_history(group, "failure_history"))
                        if counted and (not _section_body(reply, "ROOT CAUSE") or not _section_body(reply, "FAILING TEST")):
                            raise ValueError("plan_review after a failure requires ROOT CAUSE and FAILING TEST sections")
                        _lane_history(group, "plan_review_history").append(
                            {"job_id": job_id, "after_failure": counted[-1].get("attempt_id") if counted else None,
                             "status": "passed", "cycle": cycle, "round": round_number, "at": stamp})
                    else:
                        codex["review_status"] = "passed"
                        group.update(status="review", phase="review")
                else:
                    incomplete = verdict is None
                    state = "incomplete" if incomplete else "changes_requested"
                    job["finished_phase_status"] = state
                    reason = ("Review incomplete: strict verdict or Must-fix section missing" if incomplete
                              else must_fix)
                    job["must_fix"] = reason
                    if phase == "review":
                        codex["review_status"] = "failed" if incomplete else state
                        codex["must_fix_note"] = reason
                        group.update(status="running", phase="fix")
                        for identifier in set(operation["ids"]).intersection(group.get("finding_ids", [])):
                            _record_lane_failure(document, identifier, job_id, phase, reason)
                    else:
                        codex["plan_review_status"] = "failed" if incomplete else state
            else:
                passed = exit_code == 0
                job["finished_phase_status"] = "passed" if passed else "failed"
                if phase == "fix":
                    codex["fix_status"] = "passed" if passed else "failed"
                    group.update(status="review" if passed else "failed", phase="review" if passed else "fix")
                    if not passed:
                        for identifier in set(operation["ids"]).intersection(group.get("finding_ids", [])):
                            _record_lane_failure(document, identifier, job_id, "fix", f"Fix job exited {exit_code}")
                elif phase == "presuite":
                    group["presuite"] = {"status": "passed" if passed else "failed",
                                         "exit_code": exit_code, "finished_at": stamp}
            job["heartbeat_at"] = stamp
    else:
        raise ValueError("unknown job transition")


def normalized_text(value):
    return " ".join(value.split()).casefold() if isinstance(value, str) else ""


def validate_observed_at(value, run, field):
    if not isinstance(value, str) or not value.strip() or value == "unknown":
        raise ValueError(f"{field} requires observed_at for this verification run")
    try:
        validate_record({"check":field, "outcome":"observed", "build":run["build"],
                         "session":run["id"], "timestamp":value, "reference":"observation"})
        observed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        installed = datetime.fromisoformat(run["installed_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
    except (KeyError, ValueError):
        raise ValueError(f"{field} observed_at must be ISO 8601 with timezone for this verification run") from None
    if observed < installed:
        raise ValueError(f"{field} observed_at must be at or after this verification run's install time")
    return value


def has_observation_for_run(check, run):
    try:
        validate_observed_at(check.get("observed_at"), run, "verification check")
    except ValueError:
        return False
    return True


def validate_new_observation(value, operation, run, observed_at, field, check=None):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} requires an observation")
    provenance = (operation, check or {})
    if any(source.get("carried_over") is True or source.get("no_new_observation") is True
           or source.get("new_observation") is False for source in provenance):
        raise ValueError(f"{field} is marked as carried over or having no new observation")
    return value.strip(), validate_observed_at(observed_at, run, field)


def detail_key_denied(key):
    return isinstance(key, str) and any(pattern.search(key) for pattern in DETAIL_DENY_PATTERNS)


def validate_detail_limits(item):
    details = item.get("details", {})
    if not isinstance(details, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in details.items()):
        raise ValueError(f"{item.get('id', 'finding')}.details must contain string keys and values")
    if len(details) > MAX_DETAIL_KEYS:
        raise ValueError(f"{item.get('id', 'finding')}.details exceeds {MAX_DETAIL_KEYS} keys; run compact")
    for key, value in details.items():
        if detail_key_denied(key):
            raise ValueError(f"{item.get('id', 'finding')}.details key {key!r} is process or history noise")
        if len(value) > MAX_DETAIL_CHARS:
            raise ValueError(f"{item.get('id', 'finding')}.details[{key!r}] exceeds {MAX_DETAIL_CHARS} characters; run compact")


def referenced_evidence(item, record):
    reference = normalized_text(record.get("reference"))
    references = item.get("verification_refs", [])
    if not isinstance(references, list):
        return False
    token_pattern = re.compile(r"\bsref_[a-f0-9]{8,}\b|\b[a-f0-9]{32,}\b", re.I)
    record_tokens = set(token_pattern.findall(reference))
    for value in references:
        candidate = normalized_text(value)
        if candidate and (candidate in reference or reference in candidate):
            return True
        if record_tokens & set(token_pattern.findall(candidate)):
            return True
    return False


def evidence_class(record):
    outcome = normalized_text(record.get("verdict", record.get("outcome")))
    if re.search(r"\b(?:inconclusive|blocked)\b", outcome):
        return "inconclusive"
    if re.search(r"\b(?:fail(?:ed|ure)?|not[_ ]fixed|regression)\b", outcome):
        return "failing"
    if re.search(r"\b(?:pass(?:ed)?|success(?:ful)?|verified[_ ]fixed|fixed)\b", outcome):
        return "passing"
    return None


def evidence_timestamp(record, index):
    value = record.get("timestamp", "")
    if not isinstance(value, str):
        return float("-inf"), index
    match = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))", value)
    if match:
        try:
            return datetime.fromisoformat(match.group(1).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp(), index
        except ValueError:
            pass
    return float("-inf"), index


def trim_verification_evidence(item):
    if "verification_evidence" not in item:
        return
    records = item["verification_evidence"]
    if not isinstance(records, list):
        raise ValueError(f"{item.get('id', 'finding')}.verification_evidence must be an array")
    latest_outcomes = {}
    latest = {}
    keep = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError(f"{item.get('id', 'finding')}.verification_evidence entries must be objects")
        outcome = normalized_text(record.get("verdict", record.get("outcome")))
        if outcome and (outcome not in latest_outcomes or evidence_timestamp(record, index) > evidence_timestamp(records[latest_outcomes[outcome]], latest_outcomes[outcome])):
            latest_outcomes[outcome] = index
        kind = evidence_class(record)
        if kind and (kind not in latest or evidence_timestamp(record, index) > evidence_timestamp(records[latest[kind]], latest[kind])):
            latest[kind] = index
        if referenced_evidence(item, record):
            keep.add(index)
        if record.get("check_id"):
            keep.add(index)
    keep.update(latest_outcomes.values())
    keep.update(latest.values())
    item["verification_evidence"] = [record for index, record in enumerate(records) if index in keep]


def compact_document(document):
    findings = document.get("findings")
    if not isinstance(findings, list):
        raise ValueError("compact requires authoritative findings JSON")
    before = [(item.get("id"), item.get("status")) for item in findings]
    tracking = document.setdefault("tracking", {})
    if not isinstance(tracking, dict):
        raise ValueError("tracking must be an object")
    history = tracking.get("verification_history", [])
    if not isinstance(history, list) or any(not isinstance(run, dict) for run in history):
        raise ValueError("verification_history must be an array of runs")
    compacted_runs = []
    for run in history:
        checks = run.get("checks", {})
        if not isinstance(checks, dict):
            raise ValueError("verification run checks must be an object")
        for item_id, check in list(checks.items()):
            if not isinstance(check, dict):
                raise ValueError("verification checks must be objects")
            state = check.get("state")
            marked_carryover = (check.get("carried_over") is True or check.get("no_new_observation") is True
                                or check.get("new_observation") is False)
            if state in ("inconclusive", "blocked") and (marked_carryover or not has_observation_for_run(check, run)):
                del checks[item_id]
        if run.get("status") == "interrupted":
            run["checks"] = {item_id: check for item_id, check in checks.items() if check.get("state") not in ("checking", "pending")}
            checks = run["checks"]
        if run.get("status") == "interrupted" and not checks:
            continue
        compacted_runs.append(run)
    tracking["verification_history"] = compacted_runs
    tracking.pop("archived_findings", None)
    for item in findings:
        details = item.get("details")
        if details is not None:
            if not isinstance(details, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in details.items()):
                raise ValueError(f"{item.get('id', 'finding')}.details must contain string keys and values")
            kept = [(key, value[:MAX_DETAIL_CHARS] if len(value) > MAX_DETAIL_CHARS else value)
                    for key, value in details.items() if not detail_key_denied(key)]
            item["details"] = dict(kept[-MAX_DETAIL_KEYS:])
        trim_verification_evidence(item)
    after = [(item.get("id"), item.get("status")) for item in findings]
    if before != after:
        raise ValueError("compact cannot change finding IDs or statuses")
    return document


def verification_action(document, findings, operation):
    tracking = document.setdefault("tracking", {})
    if not isinstance(tracking, dict):
        raise ValueError("tracking must be an object")
    action = operation["action"]
    run = tracking.get("verification_run")
    if action == "correct-check":
        if not isinstance(run, dict) or operation.get("run_id") != run.get("id"):
            raise ValueError("current verification run id is required")
        item = findings[find_index(findings, operation.get("id"))]
        check = run.get("checks", {}).get(item["id"])
        outcome, reason = operation.get("outcome"), operation.get("reason")
        if not isinstance(check, dict) or check.get("state") not in ("fixed", "not_fixed", "blocked", "inconclusive"):
            raise ValueError("a finished check is required")
        if outcome not in ("not_fixed", "blocked", "inconclusive") or not isinstance(reason, str) or not reason.strip():
            raise ValueError("correction requires an outcome and reason")
        validate_verification_reason(reason)
        reason, _ = validate_new_observation(reason, operation, run, check.get("observed_at"), "correction reason", check)
        corrections = check.setdefault("corrections", [])
        corrections.append({"previous": copy.deepcopy({k: v for k, v in check.items() if k != "corrections"}),
                            "previous_verification": item.get("verification", ""),
                            "reason": reason, "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        check["state"] = outcome
        check["evidence"] = reason
        item["verification"] = reason
        if item["status"] == "verified_fixed" or outcome == "not_fixed":
            item["status"] = "failing" if outcome == "not_fixed" else "investigation"
        if outcome == "not_fixed":
            _record_lane_failure(document, item["id"], run["id"], "verify", reason)
        return
    if action == "start-verification":
        new = operation.get("run")
        fields = {"id", "build", "revision", "installed_at", "install_evidence"}
        if not isinstance(new, dict) or set(new) != fields or any(not isinstance(new[k], str) or not new[k].strip() for k in fields):
            raise ValueError("verification run requires id, build, revision, installed_at and install_evidence")
        if new["installed_at"] == "unknown":
            raise ValueError("installed_at must be a known timestamp")
        if new["build"].strip().casefold() == "unknown" or new["revision"].strip().casefold() == "unknown":
            raise ValueError("verification run requires a known build and revision")
        history = tracking.setdefault("verification_history", [])
        if not isinstance(history, list) or any(not isinstance(x, dict) for x in history):
            raise ValueError("verification_history must be an array of runs")
        if new["id"] in [x.get("id") for x in history] or (isinstance(run, dict) and new["id"] == run.get("id")):
            raise ValueError("verification run id must be unique")
        if run:
            history.append({**run, "status": "interrupted" if run.get("status") == "active" else run.get("status")})
        tracking["verification_run"] = {**new, "cycle": RENDERER.rollover_cycle(tracking), "status":"active",
                                        "checks":{item["id"]:{"state":"pending"} for item in findings}}
        return
    if action == "reserve-shared-check":
        if run.get("status") != "active" or operation.get("run_id") != run.get("id"):
            raise ValueError("active verification run id is required")
        previous = run.get("shared_check")
        reason = operation.get("reason")
        if previous is not None:
            if not isinstance(previous, dict) or previous.get("state") != "failed":
                raise ValueError("shared check already recorded for this verification run")
            if not isinstance(reason, str) or not reason.strip() or "\n" in reason:
                raise ValueError("shared check failed for this verification run; rerun it with --reason \"why\"")
        command = operation.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("shared check command must be nonempty text")
        if previous is not None:
            attempts = run.setdefault("shared_check_attempts", [])
            if not isinstance(attempts, list):
                raise ValueError("tracking.verification_run.shared_check_attempts must be an array")
            attempts.append(previous)
        run["shared_check"] = {"command": command, "state": "checking"}
        if previous is not None:
            run["shared_check"]["rerun_reason"] = reason.strip()
        return
    reopens = action == "start-check" and operation.get("recheck") is True and isinstance(run, dict) and run.get("status") == "complete"
    if not isinstance(run, dict) or (run.get("status") != "active" and not reopens) or operation.get("run_id") != run.get("id"):
        raise ValueError("active verification run id is required")
    if action == "stop-verification":
        # BCL-99: stopping a run keeps every finished verdict; only unfinished checks become stopped-inconclusive,
        # and those do not uncount an issue that was already verified fixed.
        reason = operation.get("reason")
        if not isinstance(reason, str) or not reason.strip() or "\n" in reason:
            raise ValueError("stop-verification requires a one-line reason")
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        for check in run.get("checks", {}).values():
            if check.get("state") in ("pending", "checking"):
                check.setdefault("started_at", stamp)
                check.update(state="inconclusive", stopped=True, evidence=f"Run stopped: {reason.strip()}",
                             observation=f"Run stopped: {reason.strip()}", observed_at=stamp,
                             updated_at=stamp, finished_at=stamp)
        shared = run.get("shared_check")
        if isinstance(shared, dict) and shared.get("state") == "checking":
            shared.update(state="failed", stopped=True, finished_at=stamp)
        run["status"] = "complete"
        run["stopped"] = {"reason": reason.strip(), "at": stamp}
        return
    if action == "complete-verification":
        if any(check.get("state") not in ("fixed", "not_fixed", "blocked", "inconclusive")
               for check in run.get("checks", {}).values()):
            raise ValueError("finish or explicitly block every issue in the run before completing it")
        run["status"] = "complete"
        return
    if action == "finish-shared-check":
        check = run.get("shared_check")
        if not isinstance(check, dict) or check.get("state") != "checking":
            raise ValueError("shared check is not in progress")
        code = operation.get("exit_code")
        if type(code) is not int:
            raise ValueError("shared check exit_code must be an integer")
        finished_at = operation.get("finished_at")
        if not isinstance(finished_at, str) or not finished_at.strip():
            raise ValueError("shared check finished_at must be nonempty text")
        check.update(state="passed" if code == 0 else "failed", exit_code=code, finished_at=finished_at)
        return
    item = findings[find_index(findings, operation.get("id"))]
    checks = run.get("checks", {})
    check = checks.get(item["id"])
    if not isinstance(check, dict):
        raise ValueError("issue does not belong to the active verification run")
    if action == "start-check":
        finished = check.get("state") in ("fixed", "not_fixed", "blocked", "inconclusive")
        if finished and operation.get("recheck") is not True:
            raise ValueError("issue check is already finished")
        if not finished and check.get("state") not in ("pending", "checking"):
            raise ValueError("issue check cannot be started")
        observation, observed_at = validate_new_observation(operation.get("observation"), operation, run,
                                                             operation.get("observed_at"), "start-check observation")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        history = check.get("previous_checks", [])
        if not isinstance(history, list):
            raise ValueError("previous_checks must be an array")
        if finished:
            history = [*history, {k: copy.deepcopy(v) for k, v in check.items() if k != "previous_checks"}]
        if reopens:
            run["status"] = "active"
        checks[item["id"]] = {"state":"checking", "observation":observation, "observed_at":observed_at,
                               "started_at":now if finished else check.get("started_at", now), "updated_at":now,
                               **({"previous_checks":history} if history else {})}
        return
    if action != "finish-check" or check.get("state") != "checking":
        raise ValueError("start the issue check before finishing it")
    outcome = operation.get("outcome")
    evidence = operation.get("evidence")
    if outcome not in ("fixed", "not_fixed", "blocked", "inconclusive") or not isinstance(evidence, str) or not evidence.strip():
        raise ValueError("finish-check requires an outcome and evidence or reason")
    details = operation.get("details", "")
    if not isinstance(details, str):
        raise ValueError("finish-check details must be text")
    validate_verification_reason(evidence)
    evidence, _ = validate_new_observation(evidence, operation, run, check.get("observed_at"), "finish-check evidence", check)
    if outcome == "blocked" and (not isinstance(operation.get("blocker"), str) or not operation["blocker"].strip()):
        raise ValueError("blocked requires a named external blocker field")
    if outcome == "fixed" and (item.get("status") != "verified_fixed" or not item.get("verification_refs")):
        raise ValueError("fixed requires verified_fixed status and verification references")
    if outcome == "not_fixed":
        item["status"] = "failing"
        _record_lane_failure(document, item["id"], run["id"], "verify", evidence)
    checks[item["id"]] = {**check, "state":outcome, "check_id":uuid.uuid4().hex, "evidence":evidence,
                           **({"details":details} if details else {}),
                           **({"blocker":operation["blocker"].strip()} if outcome == "blocked" else {}),
                           "finished_at":datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")}


def atomic_write(path, original, content, *, tracker=True):
    current = path.read_bytes() if path.exists() else None
    if current != original:
        raise ValueError("data file changed during this operation; reload the finding and retry")
    mode = stat.S_IMODE(path.stat().st_mode) if current is not None else 0o644
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as temp:
            if hasattr(os, 'fchmod'):
                os.fchmod(temp.fileno(), mode)
            else:
                os.chmod(temp_name, mode)
            temp.write(content)
            temp.flush()
            os.fsync(temp.fileno())
        if tracker and path.resolve().is_relative_to(live_state.STATE.resolve()):
            registered = any(Path(record['live']).resolve() == path.resolve()
                             for record in live_state.registry().values())
            if not registered:
                raise ValueError('tracker under state directory is not registered; repair registry before writing')
            path.with_suffix('.unsnapshotted').write_text(hashlib.sha256(content).hexdigest() + '\n')
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def report_write(path, original, content):
    return atomic_write(path, original, content, tracker=False)


def live_write(path, original, content):
    if original is None:
        return report_write(path, None, content)
    with path.open("r+b") as target:
        if target.read() != original:
            raise ValueError("rendered page changed during this operation; retry")
        try:
            target.seek(0)
            target.write(content)
            target.truncate()
            target.flush()
            os.fsync(target.fileno())
        except OSError:
            target.seek(0)
            target.write(original)
            target.truncate()
            target.flush()
            raise


def read_sibling_json(path):
    original = path.read_bytes() if path.exists() else None
    if original is None:
        return None, []
    rows = json.loads(original.decode("utf-8"))
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"{path.name} must contain an array of records")
    return original, rows


def sibling_json_bytes(rows):
    return (json.dumps(rows, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def current_pr(tracking):
    pull_request = tracking.get("pull_request", {}) if isinstance(tracking, dict) else {}
    if not isinstance(pull_request, dict):
        pull_request = {}
    for value in (pull_request.get("url"), pull_request.get("label")):
        match = re.search(r"(?:/pull/|#)(\d+)", str(value or ""))
        if match:
            return int(match.group(1))
    rollover = tracking.get("rollover", {}) if isinstance(tracking, dict) else {}
    return rollover.get("source_pr") if isinstance(rollover, dict) else None


def archive_entry(finding, pr, stamp):
    return {**copy.deepcopy(finding), "archived_from_pr": pr, "archived_at": stamp}


def archive_identity(entry):
    finding = {key: value for key, value in entry.items()
               if key not in ("archived_from_pr", "archived_at")}
    return (entry.get("id"), entry.get("archived_from_pr"),
            json.dumps(finding, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def append_archive_entries(existing, additions):
    rows = list(existing)
    identities = {archive_identity(row) for row in rows}
    for row in additions:
        identity = archive_identity(row)
        if identity not in identities:
            rows.append(row)
            identities.add(identity)
    return rows


def archive_cycle_history(tracking, through_cycle):
    moved = []
    for key in ("verification_history", "introspect_history"):
        history = tracking.get(key, [])
        if not isinstance(history, list) or any(not isinstance(row, dict) for row in history):
            raise ValueError(f"tracking.{key} must be an array of records")
        moved.extend({**copy.deepcopy(row), "record_type": key, "archived_cycle": row["cycle"]}
                     for row in history if type(row.get("cycle")) is int and row["cycle"] <= through_cycle)
        tracking[key] = [row for row in history
                         if not (type(row.get("cycle")) is int and row["cycle"] <= through_cycle)]
    return moved


def parked_finding(entry):
    return {key: copy.deepcopy(value) for key, value in entry.items()
            if key not in ("needs", "parked_at", "parked_from_pr")}


def append_parked_entries(existing, additions):
    rows = list(existing)
    by_id = {row.get("id"): row for row in rows}
    for row in additions:
        previous = by_id.get(row.get("id"))
        if previous is None:
            rows.append(row)
            by_id[row.get("id")] = row
        elif parked_finding(previous) != parked_finding(row):
            raise ValueError(f"parked finding {row.get('id')} conflicts with the live finding")
    return rows


def validate_parked_rows(rows):
    identifiers = [row.get("id") for row in rows]
    if (any(not isinstance(identifier, str) or not identifier.strip() for identifier in identifiers)
            or len(identifiers) != len(set(identifiers))
            or any(row.get("status") != "blocked" or not isinstance(row.get("needs"), str)
                   or not row["needs"].strip() for row in rows)):
        raise ValueError("parked.json entries require unique blocked findings and needs values")


def is_introspect_finding(finding):
    details = finding.get("details")
    return (re.fullmatch(r"WF-\d+", str(finding.get("id", ""))) is not None
            and finding.get("status") in CLOSED_STATUSES
            and isinstance(details, dict) and details.get("fixed_in") == "Introspect")


def introspect_history_record(finding, tracking):
    current = tracking.get("introspect") if isinstance(tracking, dict) else None
    current = current if isinstance(current, dict) and current.get("task") == finding.get("id") else {}
    legacy_date = re.match(r"^Introspect (\d{4}-\d\d-\d\d):", str(finding.get("title", "")))
    details = finding.get("details", {})
    provenance = {}
    for field in ("cycle", "round"):
        values = [source[field] for source in (finding, details, current) if source.get(field) is not None]
        if values and any(value != values[0] for value in values[1:]):
            raise ValueError(f"completed Introspect record {finding['id']} has conflicting {field}")
        provenance[field] = values[0] if values else None
    cycle, round_number = provenance["cycle"], provenance["round"]
    record = {
        "cycle": cycle, "round": round_number,
        "attribution": "attributed" if cycle is not None and round_number is not None else "unattributed",
        "result": current.get("result") or finding.get("observed") or finding.get("title") or "Introspect recorded",
        "at": current.get("at") or finding.get("fixed_at") or finding.get("time") or finding.get("at")
              or details.get("fixed_at") or details.get("time") or finding.get("created_at")
              or (legacy_date.group(1) if legacy_date else None),
        "steps": copy.deepcopy(finding.get("steps", current.get("steps", []))),
        "none": copy.deepcopy(finding.get("none", current.get("none", []))),
        "sections": copy.deepcopy(finding.get("sections", current.get("sections", {}))),
        "source_task": finding["id"],
    }
    for key in ("fixed_at", "time"):
        if key in finding and key in details and finding[key] != details[key]:
            raise ValueError(f"completed Introspect record {finding['id']} has conflicting {key}")
        if key in finding or key in details:
            record[key] = copy.deepcopy(finding[key] if key in finding else details[key])
    if (any(value is not None and (type(value) is not int or value < 1)
            for value in (cycle, round_number))
            or not isinstance(record["result"], str)
            or record["at"] is not None and not isinstance(record["at"], str)
            or not isinstance(record["steps"], list) or not isinstance(record["none"], list)
            or not isinstance(record["sections"], dict)):
        raise ValueError(f"completed Introspect record {finding['id']} is incomplete")
    return record


def append_introspect_records(tracking, findings):
    history = tracking.setdefault("introspect_history", [])
    if not isinstance(history, list) or any(not isinstance(record, dict) for record in history):
        raise ValueError("tracking.introspect_history must be an array of records")
    for finding in findings:
        record = introspect_history_record(finding, tracking)
        if record not in history:
            history.append(record)


def update_sidecar_summary(tracking, archive_file, archive_rows, parked_file, parked_rows):
    if archive_rows:
        tracking["archive"] = {"path": str(archive_file), "count": len(archive_rows)}
    else:
        tracking.pop("archive", None)
    if parked_rows:
        tracking["parked"] = {"path": str(parked_file), "count": len(parked_rows),
                               "needs": list(dict.fromkeys(row["needs"] for row in parked_rows))}
    else:
        tracking.pop("parked", None)



INTROSPECT_SECTIONS = ("delete", "simplify", "optimize", "automate")


def validate_introspect_sections(sections):
    if not isinstance(sections, dict) or list(sections) != list(INTROSPECT_SECTIONS):
        raise ValueError("introspect sections must be ordered delete, simplify, optimize, automate")
    cleaned = {}
    for name in INTROSPECT_SECTIONS:
        section = sections[name]
        if not isinstance(section, dict):
            raise ValueError(f"introspect section {name} must be an object")
        if "none" in section:
            reason = section.get("none")
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError(f"introspect section {name} 'none' requires a reason")
            if section.get("items"):
                raise ValueError(f"introspect section {name} cannot have both none and items")
            cleaned[name] = {"none": reason.strip()}
            continue
        items = section.get("items")
        if not isinstance(items, list) or not items:
            raise ValueError(f"introspect section {name} needs items or none plus a reason")
        clean_items = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError(f"introspect section {name} items must be objects")
            text = item.get("item") or item.get("step")
            evidence = item.get("evidence")
            outcome = item.get("outcome")
            proof = item.get("proof")
            if not isinstance(text, str) or not text.strip() or not isinstance(evidence, str) or not evidence.strip():
                raise ValueError(f"introspect section {name} items require item and evidence")
            if outcome == "fixed inline":
                if not isinstance(proof, str) or not re.search(
                        r"\btest_[A-Za-z0-9_]+\b|(?:^|\s)(?:python3?|pytest|bcl|make|npm|pnpm|yarn|swift|xcodebuild|cargo|go)\s+",
                        proof):
                    raise ValueError(f"{text.strip()}: fixed inline requires a named command or test in proof")
                clean_items.append({"item": text.strip(), "evidence": evidence.strip(),
                                    "outcome": "fixed inline", "proof": proof.strip()})
            elif outcome == "issue":
                issue_id = item.get("issue_id")
                if not isinstance(issue_id, str) or not issue_id.strip() or re.fullmatch(r"WF-\d+", issue_id.strip()):
                    raise ValueError(f"introspect section {name} code items need a non-WF issue id")
                clean_items.append({"item": text.strip(), "evidence": evidence.strip(), "outcome": "issue", "issue_id": issue_id.strip()})
            else:
                raise ValueError(f"introspect section {name} outcome must be 'fixed inline' or 'issue'")
        cleaned[name] = {"items": clean_items}
    return cleaned



MAX_GROUP_ISSUES = 4


def regresses_against_main(item):
    details = item.get("details") if isinstance(item.get("details"), dict) else {}
    return str(details.get("regression_against_main", "")).strip().lower() in ("yes", "true")


def require_restore_lane(group, findings):
    """BCL-95: a lane holding a regression against main restores main's behaviour first; a redesign is its own finding."""
    by_id = {item["id"]: item for item in findings}
    regressed = [i for i in group.get("finding_ids", []) if i in by_id and regresses_against_main(by_id[i])]
    if regressed and group.get("restores_main") is not True:
        raise ValueError(f"plan group {group.get('id')}: {', '.join(regressed)} regress against main; this lane must restore "
                         "main's behaviour first (set restores_main true) and any redesign must be filed as its own finding")

MAX_ROUND_LANES = 4


def validate_plan_groups(plan: dict) -> None:
    """Check the group size and the round's lane cap before a fix round starts."""
    groups = plan.get("groups") if isinstance(plan, dict) else None
    if not isinstance(groups, list):
        return
    # count lanes as the page does; finished, deferred, parked, blocked and failed lanes do not take a slot
    active = [g for g in groups if isinstance(g, dict)
              and RENDERER.plan_status(g) not in {"verified", "deferred", "parked", "blocked", "failed"}]
    if len(active) > MAX_ROUND_LANES:
        raise ValueError(f"a round runs at most {MAX_ROUND_LANES} lanes (plan has {len(active)}); defer the rest to the next round")
    for group in groups:
        if not isinstance(group, dict):
            continue
        state = group["status"] if "status" in group else group.get("phase")
        if state in {"deferred", "done", "verified", "parked"}:
            continue
        ids = group.get("finding_ids") or group.get("ids") or []
        if isinstance(ids, list) and len(ids) > MAX_GROUP_ISSUES:
            gid = group.get("id") or group.get("name") or "group"
            raise ValueError(f"{gid}: cap a parallel fix group at about {MAX_GROUP_ISSUES} issues (has {len(ids)}); split it")


def finding_verified(item, tracking):
    run = tracking.get('verification_run') if isinstance(tracking, dict) else None
    checks = run.get('checks') if isinstance(run, dict) else None
    check = checks.get(item.get('id'), {}) if isinstance(checks, dict) else {}
    if not isinstance(check, dict):
        return False
    return (item.get('status') == 'verified_fixed' and bool(item.get('verification_refs'))
            and RENDERER.check_keeps_verified(check))


def validate_dependencies(groups, findings, tracking):
    by_id = {group['id']: group for group in groups}
    by_finding = {item['id']: item for item in findings}
    assignments = {}
    for group in groups:
        if RENDERER.plan_status(group) == 'parked':
            continue
        for identifier in group.get('finding_ids', []):
            if identifier in assignments:
                raise ValueError(f"finding {identifier} appears in multiple current-round groups: {assignments[identifier]}, {group['id']}")
            assignments[identifier] = group['id']
    visiting, visited = set(), set()
    def visit(identifier):
        if identifier in visiting:
            raise ValueError(f"dependency cycle involving {identifier}; remove the cycle")
        if identifier in visited:
            return
        visiting.add(identifier)
        for dependency in by_id[identifier].get('depends_on', []):
            if dependency not in by_id:
                raise ValueError(f"{identifier} references unknown dependency {dependency}")
            visit(dependency)
        visiting.remove(identifier)
        visited.add(identifier)
    for identifier in by_id:
        visit(identifier)
    prerequisites = {dep for group in groups for dep in group.get('depends_on', [])}
    for identifier in prerequisites:
        group = by_id[identifier]
        if RENDERER.plan_status(group) == 'verified':
            ids = group.get('finding_ids', [])
            if not ids or any(not finding_verified(by_finding.get(i, {}), tracking) for i in ids):
                raise ValueError(f"{identifier} requires verified findings with verification references")
    for group in groups:
        if RENDERER.plan_status(group) in {'running', 'review', 'installing', 'verifying', 'verified'}:
            pending = [dep for dep in group.get('depends_on', []) if RENDERER.plan_status(by_id[dep]) != 'verified']
            if pending:
                raise ValueError(f"{group['id']} must wait for verified dependencies: {', '.join(pending)}")


def invalidate_dependents(document, previous_document):
    tracking = document.get('tracking', {})
    groups = RENDERER.plan_groups_for_round(tracking.get('plan'), RENDERER.rollover_round(tracking))
    current = {item['id']: item for item in document.get('findings', [])}
    previous_tracking = previous_document.get('tracking', {})
    changed = {item['id'] for item in previous_document.get('findings', [])
               if finding_verified(item, previous_tracking) and not finding_verified(current.get(item['id'], {}), tracking)}
    invalid = {group['id'] for group in groups if changed.intersection(group.get('finding_ids', []))}
    if not invalid:
        return
    affected = set(invalid)
    while True:
        expanded = affected | {group['id'] for group in groups if affected.intersection(group.get('depends_on', []))}
        if expanded == affected:
            break
        affected = expanded
    timestamp = datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    blocked = False
    for group in groups:
        status = RENDERER.plan_status(group)
        if (group['id'] in invalid and status == 'verified') or (
                group['id'] in affected - invalid and status in {'running', 'review', 'installing', 'verifying', 'verified'}):
            group['phase'] = RENDERER.plan_phase(group)
            group['status'] = 'blocked'
            group['blocked_reason'] = 'Prerequisite verification changed'
            if group['id'] in affected - invalid:
                for identifier in group.get('finding_ids', []):
                    item = current.get(identifier, {})
                    if item.get('status') in {'verified_fixed', 'in_progress'}:
                        item['status'] = 'blocked'
            stamp_plan_group(group, timestamp)
            blocked = True
    if blocked and isinstance(tracking.get('workflow'), dict):
        tracking['workflow']['updated_at'] = timestamp
        tracking['workflow']['now'] = f'{timestamp} — Prerequisite verification changed'


def stamp_stage_model(group, stage, model, timestamp):
    """Record who works the lane in install or verify; no model means a scripted check."""
    if model is None:
        group.pop("stage_model", None)
    else:
        group["stage_model"] = {"stage": stage, "model": model, "at": timestamp}


def validate_stage_model(model):
    if model is not None and (not isinstance(model, str) or not model.strip() or "\n" in model
                              or "\r" in model or len(model) > 120):
        raise ValueError("set-activity model must be one line of at most 120 characters")
    return model.strip() if isinstance(model, str) else None


def correct_round(tracking, operation, timestamp):
    """BCL-88: move a wrong round counter forward with a stated reason, kept in history."""
    current = RENDERER.rollover_round(tracking)
    target, reason = operation.get("round"), operation.get("reason")
    if type(target) is not int or target <= current:
        raise ValueError(f"correct-round round must be an integer above the current round {current}")
    if (not isinstance(reason, str) or "\n" in reason or "\r" in reason
            or len(reason.split()) < 3):
        raise ValueError("correct-round requires a one-line reason of at least three words")
    plan = tracking.get("plan")
    if isinstance(plan, dict) and plan.get("round", current) == current:
        plan["round"] = target
    tracking["round"] = target
    history = tracking.setdefault("round_corrections", [])
    if not isinstance(history, list):
        raise ValueError("tracking.round_corrections must be an array")
    history.append({"from": current, "to": target, "reason": reason.strip(), "corrected_at": timestamp})
    workflow = tracking.get("workflow")
    if isinstance(workflow, dict):
        workflow["updated_at"] = timestamp
        workflow["now"] = f"{timestamp} — Round corrected {current} → {target}"


def rerun_verify(document, operation, timestamp):
    """BCL-90: put a failed lane back to verify-ready once per round, keeping why and what failed."""
    tracking = document.setdefault("tracking", {})
    round_number = RENDERER.rollover_round(tracking)
    reason, ids = operation.get("reason"), operation.get("ids")
    if (not isinstance(reason, str) or "\n" in reason or "\r" in reason
            or len(reason.split()) < 3):
        raise ValueError("rerun-verify requires a one-line reason of at least three words")
    if not isinstance(ids, list) or not ids or any(not isinstance(i, str) for i in ids):
        raise ValueError("rerun-verify requires lane ids")
    groups = {group.get("id"): group for group in RENDERER.plan_groups_for_round(tracking.get("plan"), round_number)}
    for identifier in ids:
        group = groups.get(identifier)
        if group is None:
            raise ValueError(f"rerun-verify lane {identifier} is not in the current round")
        if group.get("status") != "failed":
            raise ValueError(f"rerun-verify lane {identifier} has not failed; only a failed lane's check can be rerun")
        history = group.get("verify_reruns", [])
        if not isinstance(history, list):
            raise ValueError(f"plan group {identifier} verify_reruns must be an array")
        if any(isinstance(entry, dict) and entry.get("round") == round_number for entry in history):
            raise ValueError(f"lane {identifier} was already rerun this round; fix it in a new round instead")
        verify = group.get("verify") if isinstance(group.get("verify"), dict) else {}
        history.append({"round": round_number, "reason": reason.strip(), "at": timestamp,
                        "previous_failure": verify.get("failure")})
        group["verify_reruns"] = history
        group["verify"] = {key: value for key, value in verify.items() if key != "failure"}
        group.update(status="installing", phase="install")
        stamp_plan_group(group, timestamp)


def start_verification_lanes(document, ids, timestamp, model=None):
    tracking = document.get("tracking", {})
    groups = RENDERER.plan_groups_for_round(
        tracking.get("plan"), RENDERER.rollover_round(tracking))
    selected = set(ids)
    for group in groups:
        if RENDERER.plan_status(group) != "parked" and selected.intersection(group.get("finding_ids", [])):
            group.update(status="verifying", phase="verify")
            stamp_stage_model(group, "verify", model, timestamp)
            stamp_plan_group(group, timestamp)


def sync_plan_verify(document):
    findings = {item.get("id"): item for item in document.get("findings", []) if isinstance(item, dict)}
    plan = document.get("tracking", {}).get("plan")
    if not isinstance(plan, dict) or not isinstance(plan.get("groups"), list):
        return
    for group in plan["groups"]:
        if not isinstance(group, dict):
            continue
        ids = group.get("finding_ids") or []
        states = [(findings.get(identifier) or {}).get("status") for identifier in ids]
        total = len(ids)
        fixed = sum(state == "verified_fixed" for state in states)
        failed = sum(state == "failing" for state in states)
        open_count = sum(state not in ("verified_fixed", "failing", "closed") for state in states)
        if total == 0 or (not fixed and not failed):
            status = "pending"
        elif fixed == total:
            status = "verified"
        elif failed and not open_count and not fixed:
            status = "failed"
        else:
            status = "partial"
        install_reached = "install" in (document.get("tracking") or {}).get("stages_reached", [])
        checks_pending = RENDERER.checks_pending_reason(group)
        if fixed == total and total > 0 and (
                (group.get("status") == "blocked" and checks_pending)
                or (group.get("status") == "verifying" and checks_pending)):
            group["status"] = "verified"
            group.pop("blocked_reason", None)
        elif group.get("status") == "blocked" and checks_pending and install_reached:
            group["status"] = "verifying"
            group["phase"] = "verify"
        if RENDERER.plan_status(group) == "verifying":
            if total and fixed == total:
                group.update(status="verified", phase="verify")
                group.pop("blocked_reason", None)
            elif failed and not open_count:
                group.update(status="failed", phase="verify")
                group.pop("blocked_reason", None)
        verify = group.get("verify") if isinstance(group.get("verify"), dict) else {}
        verify.update({"status": status, "fixed": fixed, "failed": failed, "total": total})
        group["verify"] = verify

def touch_workflow(document):
    tracking = document.get("tracking")
    if isinstance(tracking, dict):
        tracking["cycle"] = RENDERER.rollover_cycle(tracking)


def require_rollover_gate(findings, tracking, force, action, reason=None):
    data = {"findings": findings, "tracking": tracking}
    fixed, threshold, met = (check_gate_count.rollover_gate_count(data) if action == "rollover"
                             else workflow_definition.run_stage_check("report_rollover", data))
    if met:
        return
    if not force:
        reason = (f"rollover gate not met: {fixed} of {threshold} verified_fixed findings"
                  if action == "rollover" else
                  f"report gate not met: {fixed} of {threshold} verified_fixed findings and open findings remain")
        raise ValueError(f"{reason}; pass --force to override")
    reason = require_override_reason(reason, action)
    rollover = tracking.setdefault("rollover", {})
    if not isinstance(rollover, dict):
        raise ValueError("tracking.rollover must be an object")
    forced = rollover.setdefault("forced_gate_overrides", [])
    if not isinstance(forced, list):
        raise ValueError("tracking.rollover.forced_gate_overrides must be an array")
    forced.append({"cycle": RENDERER.rollover_cycle(tracking), "action": action,
                   "forced_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                   "verified_fixed": fixed, "threshold": threshold, "reason": reason})


def validate_merged_record(merged):
    if not isinstance(merged, dict):
        raise ValueError("merged stage requires merge metadata")
    commit = merged.get("commit_sha")
    if not isinstance(commit, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit, re.I):
        raise ValueError("merged.commit_sha must be a full Git commit SHA")
    merged_at = merged.get("merged_at")
    if not isinstance(merged_at, str):
        raise ValueError("merged.merged_at must be an ISO 8601 timestamp with timezone")
    validate_record({"check":"merge", "outcome":"merged", "build":"unknown", "session":"merge",
                     "timestamp":merged_at, "reference":"merge"})
    stacked = merged.get("stacked_prs")
    if not isinstance(stacked, dict) or stacked.get("status") not in ("complete", "pending", "partial"):
        raise ValueError("merged.stacked_prs requires status complete, pending or partial")
    return merged



def introspect_covers_current_round(tracking):
    return RENDERER.introspect_record_for_round(tracking) is not None


def require_current_round_introspect(tracking, action):
    if not introspect_covers_current_round(tracking):
        raise ValueError(f"record this round's introspect with --sections before {action}")


def should_advance_plan_round(tracking, previous_stage):
    if previous_stage not in ("verify", "introspect", "gate") or not introspect_covers_current_round(tracking):
        return False
    groups = RENDERER.plan_groups_for_round(
        tracking.get("plan"), RENDERER.rollover_round(tracking))
    return bool(groups) and all(RENDERER.plan_status(group) in ("verified", "blocked", "failed", "parked")
                                for group in groups)


def advance_plan_round(tracking, findings, timestamp):
    round_number = RENDERER.rollover_round(tracking)
    fixed = RENDERER.rollover_gate(findings, tracking)[0]
    tracking.setdefault("round_history", []).append(
        {"round": round_number, "fixed": fixed, "ended_at": timestamp})
    tracking["round"] = round_number + 1
    if isinstance(tracking.get("plan"), dict):
        tracking["plan"]["round"] = round_number + 1
    loop = {"plan_fix", "install", "verify", "introspect", "gate"}
    tracking["stages_reached"] = [step for step in tracking.get("stages_reached", []) if step not in loop]
    tracking["stages_reached"].append("plan_fix")


def prepare_rollover(document, operation, force=False):
    findings = document.get("findings")
    tracking = document.setdefault("tracking", {})
    pr, worktree = operation.get("pull_request"), operation.get("worktree")
    if not isinstance(pr, dict) or not pr.get("label") or not pr.get("url") or not isinstance(worktree, str) or not worktree.strip():
        raise ValueError("rollover requires pull_request {label,url} and worktree")
    if "install" in tracking.get("stages_reached", []):
        require_current_round_introspect(tracking, "rollover")
    require_rollover_gate(findings, tracking, force, "rollover", operation.get("override_reason"))
    rollover = tracking.setdefault("rollover", {})
    if not isinstance(rollover, dict):
        raise ValueError("tracking.rollover must be an object")
    excluded = rollover.get("excluded_ids", [])
    if not isinstance(excluded, list) or any(not isinstance(identifier, str) for identifier in excluded):
        raise ValueError("tracking.rollover.excluded_ids must be an array of finding IDs")
    fixed = [item["id"] for item in findings
             if item.get("status") == "verified_fixed" and not is_introspect_finding(item)]
    source_pr = operation.get("source_pr")
    if source_pr is None:
        match = re.search(r"/pull/(\d+)", str(tracking.get("pull_request", {}).get("url", "")))
        source_pr = int(match.group(1)) if match else rollover.get("source_pr")
    archived_from_pr = source_pr
    rollover["source_pr"] = source_pr
    excluded = set(excluded) | {item["id"] for item in findings if item.get("status") in CLOSED_STATUSES}
    rollover["unresolved_ids"] = [item["id"] for item in findings if item.get("status") not in CLOSED_STATUSES]
    if tracking.get("merged"):
        rollover["source_merge"] = tracking.pop("merged")
    worktree_name = Path(worktree.strip().rstrip("/\\")).name
    if not worktree_name:
        raise ValueError("rollover worktree must include a worktree name")
    tracking["pull_request"], tracking["worktree"] = {"label": pr["label"], "url": pr["url"]}, worktree_name
    timestamp = operation.get("now") or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    validate_record({"check":"rollover", "outcome":"started", "build":"unknown", "session":"rollover",
                     "timestamp":timestamp, "reference":"rollover"})
    intro_items = [item for item in findings if is_introspect_finding(item)]
    archive_items = [item for item in findings if item.get("status") in CLOSED_STATUSES
                     and item.get("id") in excluded and not is_introspect_finding(item)]
    append_introspect_records(tracking, intro_items)
    archived = [archive_entry(item, archived_from_pr, timestamp) for item in archive_items]
    removed = {item["id"] for item in archive_items + intro_items}
    document["findings"] = [item for item in findings if item.get("id") not in removed]
    current_ids = {item["id"] for item in document["findings"]}
    rollover["excluded_ids"] = sorted(excluded & current_ids)
    round_number = RENDERER.rollover_round(tracking)
    tracking.setdefault("round_history", []).append({"round": round_number, "fixed": len(fixed), "ended_at": timestamp})
    tracking["round"] = 1
    tracking["cycle"] = RENDERER.rollover_cycle(tracking) + 1
    tracking.pop("plan", None)
    tracking["workflow"] = {"stage":"collect", "updated_at":timestamp}
    run = tracking.pop("verification_run", None)
    if isinstance(run, dict):
        tracking.setdefault("verification_history", []).append({**run, "status":"interrupted" if run.get("status") == "active" else run.get("status")})
    tracking.pop("stages_reached", None)
    tracking.pop("introspect", None)
    for item in document["findings"]:
        if item.get("status") == "in_progress":
            item["status"] = "untested"
    return document, archived


def validate_rollover_target(record, live, document):
    # Match registration checks before changing the shared archive.
    new_id = live_state.list_id(document, live)
    new_key = f"{record['repo']}/{new_id}"
    if new_key in live_state.registry():
        raise ValueError(f"new list is already adopted: {new_key}")
    target = (live_state.STATE.resolve() / live_state.safe_component(record["repo"].replace("/", "--"))
              / f"{live_state.safe_component(new_id)}.json")
    if target.exists():
        raise ValueError(f"new live list already exists: {target}")
    for field, fallback in (("snapshot", f"pr-{new_id}-findings.json"), ("html", f"pr-{new_id}.html")):
        artifact = live_state._renamed_report_path(record[field], record["id"], new_id, fallback)
        if (Path(record["home"]) / artifact).exists():
            raise ValueError(f"new list report already exists: {Path(record['home']) / artifact}")
    json.dumps(document, ensure_ascii=False)


def rollover_live(path, operation, force=False):
    path = Path(path).resolve()
    if not path.is_relative_to(live_state.STATE.resolve()):
        raise ValueError("rollover requires an adopted live tracker; run bcl adopt first")
    record = next((record for record in live_state.registry().values()
                   if Path(record["live"]).resolve() == path), None)
    if not record:
        raise ValueError("rollover requires an adopted live tracker; run bcl adopt first")
    live = path
    if path != live and path.read_bytes() != live.read_bytes():
        raise ValueError(f"in-repo tracker differs from live state; live file: {live}")
    document, archive_additions = prepare_rollover(json.loads(live.read_text(encoding="utf-8")), operation, force)
    tracking = document.setdefault("tracking", {})
    archive_additions.extend(archive_cycle_history(tracking, RENDERER.rollover_cycle(tracking) - 1))
    archive_file, parked_file = live_state.archive_path(live), live_state.parked_path(live)
    archive_original, archive_rows = read_sibling_json(archive_file)
    parked_original, parked_rows = read_sibling_json(parked_file)
    validate_parked_rows(parked_rows)
    updated_archive = append_archive_entries(archive_rows, archive_additions)
    update_sidecar_summary(document.setdefault("tracking", {}), archive_file, updated_archive,
                           parked_file, parked_rows)
    validate_rollover_target(record, live, document)
    archive_changed = updated_archive != archive_rows
    archive_content = sibling_json_bytes(updated_archive)
    if archive_changed:
        atomic_write(archive_file, archive_original, archive_content, tracker=False)
    try:
        return live_state.register_rollover(record, live, document, operation["worktree"].strip())[2]
    except BaseException as error:
        if archive_changed:
            try:
                if archive_original is None:
                    if archive_file.read_bytes() != archive_content:
                        raise ValueError("archive changed during rollover")
                    archive_file.unlink()
                else:
                    atomic_write(archive_file, archive_content, archive_original, tracker=False)
            except (OSError, ValueError) as rollback_error:
                raise ValueError(f"rollover failed ({error}); archive rollback failed: {rollback_error}") from error
        raise


def missing_report_file(error, base):
    filename = Path(error.filename or "")
    return filename.is_absolute() and filename.resolve().is_relative_to(Path(base).resolve())


def write(path, original, store, findings, validate=True, touch=True):
    missing_capture = None
    if store["kind"] == "json":
        document = store["document"]
        document[store["collection"]] = findings
        if touch and store['collection'] == 'findings':
            invalidate_dependents(document, json.loads(original))
            sync_plan_verify(document)
        if validate and store["collection"] == "findings":
            base = live_state.evidence_base(path)
            try:
                RENDERER.render(document, base)
            except FileNotFoundError as error:
                if not missing_report_file(error, base):
                    raise
                missing_capture = error
        if touch:
            touch_workflow(document)
        content = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    else:
        data = json.dumps(findings, ensure_ascii=True, separators=(",", ":")).replace("<", "\\u003c")
        text = store["text"][:store["start"]] + data + store["text"][store["end"]:]
        content = text.encode("utf-8")
    atomic_write(path, original, content)
    if missing_capture:
        path.with_suffix(".unsnapshotted").write_text("missing report capture\n")
        raise ValueError(f"JSON saved but report capture is missing: {missing_capture.filename}; restore it and render/snapshot, do not repeat the mutation")


def parser_for():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    listing = commands.add_parser("list", help="list finding IDs and short summaries")
    listing.add_argument("data", type=Path)
    listing.add_argument("--collection", default="findings")

    audit = commands.add_parser("audit", help="flag potentially stale or repeated details for review")
    audit.add_argument("data", type=Path)
    audit.add_argument("--collection", default="findings")

    compact = commands.add_parser("compact", help="remove superseded tracker noise and render the result")
    compact.add_argument("data", type=Path)
    compact.add_argument("--render", type=Path, help="render the compacted tracker to this HTML file")
    compact.add_argument("--inventory", type=Path, help="optional installed-plugin inventory JSON")

    get = commands.add_parser("get", help="print one finding as JSON")
    get.add_argument("data", type=Path)
    get.add_argument("id")
    get.add_argument("--collection", default="findings")
    get.add_argument("--include-evidence", action="store_true",
                     help="include large inline image payloads in the returned record")

    add = commands.add_parser("add", help="add one finding from a JSON file or stdin")
    add.add_argument("data", type=Path)
    add.add_argument("entry", help="JSON object path, or - for stdin")
    add.add_argument("--collection", default="findings")

    update = commands.add_parser("update", help="merge a JSON patch into one finding")
    update.add_argument("data", type=Path)
    update.add_argument("id")
    update.add_argument("patch", help="JSON object path, or - for stdin")
    update.add_argument("--collection", default="findings")

    record = commands.add_parser("record-evidence", help="append a structured verification record to JSON")
    record.add_argument("data", type=Path)
    record.add_argument("id")
    record.add_argument("record", help="JSON object path, or - for stdin")
    record.add_argument("--collection", default="findings")

    rewrite = commands.add_parser("rewrite-evidence", help="replace an existing evidence record with structured verification fields")
    rewrite.add_argument("data", type=Path)
    rewrite.add_argument("id")
    rewrite.add_argument("record", help="structured record JSON path, or - for stdin")
    rewrite.add_argument("--collection", default="findings")

    delete = commands.add_parser("delete", help="delete one finding by ID")
    delete.add_argument("data", type=Path)
    delete.add_argument("id")
    delete.add_argument("--collection", default="findings")
    batch = commands.add_parser("batch", help="apply JSON operations together and optionally render")
    batch.add_argument("data", type=Path)
    batch.add_argument("operations", help="JSON array path, or - for stdin")
    batch.add_argument("--render", type=Path, help="render the resulting authoritative JSON to this HTML file")
    batch.add_argument("--base", type=Path, help="resolve relative evidence paths from this tracker snapshot directory")
    batch.add_argument("--inventory", type=Path, help="optional installed-plugin inventory JSON")
    batch.add_argument("--force", action="store_true", help="override a gate and record the reason")
    batch.add_argument("--reason", help="one-line reason for a forced gate or machine-load override")
    batch.add_argument("--collection", default="findings")
    return parser


def run(args, parser):
    try:
        path, original, store, findings = load(args.data, getattr(args, "collection", "findings"))
        if args.command == "audit":
            document = store.get("document", {})
            tracking = document.get("tracking", {})
            run = tracking.get("verification_run", {}) if isinstance(tracking, dict) else {}
            versions = set(re.findall(r"\b\d+\.\d+\.\d+\b", run.get("build", ""))) if isinstance(run, dict) else set()
            warnings = []
            violations = []
            if store["kind"] == "json" and len(original) > MAX_TRACKER_BYTES:
                violations.append({"kind": "tracker_over_budget", "bytes": len(original), "budget": MAX_TRACKER_BYTES,
                                   "message": "run manage_findings.py compact"})
            for item in findings:
                seen = {}
                details = item.get("details", {})
                if not isinstance(details, dict):
                    violations.append({"id": item["id"], "kind": "invalid_details"})
                    continue
                if len(details) > MAX_DETAIL_KEYS:
                    violations.append({"id": item["id"], "kind": "too_many_details", "message": "run compact"})
                for key, value in details.items():
                    if detail_key_denied(key):
                        violations.append({"id": item["id"], "kind": "denied_detail_key", "field": key, "message": "run compact"})
                    if not isinstance(value, str):
                        violations.append({"id": item["id"], "kind": "invalid_detail_value", "field": key})
                        continue
                    if len(value) > MAX_DETAIL_CHARS:
                        violations.append({"id": item["id"], "kind": "detail_too_long", "field": key, "message": "run compact"})
                    normalized = " ".join(value.split()).casefold()
                    if normalized and normalized in seen:
                        warnings.append({"id": item["id"], "kind": "duplicate_detail", "fields": [seen[normalized], key]})
                    seen[normalized] = key
                    mentioned = set(re.findall(r"\b\d+\.\d+\.\d+\b", value))
                    if versions and mentioned and mentioned.isdisjoint(versions) and re.search(r"\b(latest|current|is installed)\b", key + " " + value, re.I):
                        warnings.append({"id": item["id"], "kind": "possibly_stale", "field": key})
                trimmed = copy.deepcopy(item)
                trim_verification_evidence(trimmed)
                if len(trimmed.get("verification_evidence", [])) != len(item.get("verification_evidence", [])):
                    violations.append({"id": item["id"], "kind": "too_much_verification_evidence", "message": "run compact"})
            if isinstance(tracking, dict):
                if "archived_findings" in tracking:
                    violations.append({"kind": "archived_findings", "message": "run compact"})
                for history_run in tracking.get("verification_history", []):
                    checks = history_run.get("checks", {}) if isinstance(history_run, dict) else {}
                    for item_id, check in checks.items() if isinstance(checks, dict) else []:
                        if not isinstance(check, dict):
                            continue
                        marked_carryover = (check.get("carried_over") is True or check.get("no_new_observation") is True
                                            or check.get("new_observation") is False)
                        if check.get("state") in ("inconclusive", "blocked") and (marked_carryover or not has_observation_for_run(check, history_run)):
                            violations.append({"id": item_id, "kind": "carryover_check", "run": history_run.get("id"), "message": "run compact"})
            print(json.dumps(warnings + violations, ensure_ascii=False, indent=2))
            return 1 if violations else 0
        if args.command == "compact":
            if store["kind"] != "json" or store["collection"] != "findings":
                raise ValueError("compact requires authoritative findings JSON")
            if args.inventory and not args.render:
                raise ValueError("--inventory requires --render")
            if args.render and (args.render.resolve() == path.resolve() or args.render.resolve() == RENDERER.TEMPLATE.resolve()):
                raise ValueError("render output must differ from input and template")
            compact_document(store["document"])
            touch_workflow(store["document"])
            inventory = RENDERER.load_inventory(args.inventory) if args.inventory else None
            rendered = RENDERER.render(store["document"], path.parent, inventory) if args.render else None
            content = (json.dumps(store["document"], ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            html_original = args.render.read_bytes() if args.render and args.render.exists() else None
            atomic_write(path, original, content)
            if args.render:
                try:
                    run = store["document"].get("tracking", {}).get("verification_run")
                    (live_write if run else report_write)(args.render, html_original, rendered.encode("utf-8"))
                except (OSError, ValueError) as error:
                    raise ValueError(f"JSON compacted but render failed: {error}") from error
            print(f"Compacted {path}: {len(original)} -> {len(content)} bytes" + (f"; rendered {args.render}" if args.render else ""))
            return 0
        if args.command == "list":
            rows = [{"id": item["id"], "title": item.get("title"),
                     "source": item.get("source", item.get("label")), "status": item.get("status")}
                    for item in findings]
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return 0
        if args.command == "get":
            item = findings[find_index(findings, args.id)]
            print(json.dumps(display_record(item, args.include_evidence), ensure_ascii=False, indent=2))
            return 0
        if args.command == "batch":
            if store["kind"] != "json" or store["collection"] != "findings":
                raise ValueError("batch requires authoritative findings JSON")
            if args.inventory and not args.render:
                raise ValueError("--inventory requires --render")
            operations = read_json(args.operations)
            if not isinstance(operations, list) or not operations:
                raise ValueError("batch requires a nonempty operations array")
            if any(isinstance(op, dict) and op.get("action") == "rollover" for op in operations):
                if len(operations) != 1 or not isinstance(operations[0], dict):
                    raise ValueError("rollover must be the only batch operation")
                operations[0].setdefault("override_reason", args.reason)
                new_live = rollover_live(path, operations[0], args.force)
                print(f"Created new live list {new_live}; the current viewer stays on the old list until bcl start begins Collect")
                return 0
            existing_run = store["document"].get("tracking", {}).get("verification_run")
            original_plan = copy.deepcopy(store['document'].get('tracking', {}).get('plan'))
            run_actions = {"start-verification", "start-check", "finish-check", "complete-verification", "stop-verification", "correct-check", "reserve-shared-check", "finish-shared-check", "set-stage", "set-activity", "set-plan", "update-plan-group", "archive", "park", "unpark", "correct-round", "rerun-verify", "start-job", "attach-job", "heartbeat-job", "finish-job", "skip-review", "record-cleanup", "archive-history", "unblock-lane"}
            if (existing_run or any(isinstance(op, dict) and op.get("action") in run_actions for op in operations)) and not args.render:
                raise ValueError("verification progress requires --render so the page stays current")
            started_checks = set()
            sidecars = {}

            def sidecar(name):
                if name not in sidecars:
                    sibling = (live_state.archive_path(path) if name == "archive"
                               else live_state.parked_path(path))
                    original_sidecar, rows = read_sibling_json(sibling)
                    if name == "parked":
                        validate_parked_rows(rows)
                    sidecars[name] = {"path": sibling, "original": original_sidecar,
                                      "initial": copy.deepcopy(rows), "rows": rows, "additions": []}
                return sidecars[name]

            for operation in operations:
                if not isinstance(operation, dict):
                    raise ValueError("batch operation must be an object")
                action = operation.get("action")
                if action in ("start-job", "attach-job", "heartbeat-job", "finish-job"):
                    job_transition(store["document"], operation)
                    continue
                if action == "unblock-lane":
                    unblock_lane(store["document"], operation)
                    continue
                if action == "skip-review":
                    tracking, _, groups = _lane_groups(store["document"], operation.get("ids"))
                    for group in groups:
                        codex = group.setdefault("codex", {})
                        if not codex.get("one_file") or codex.get("fix_status") != "passed":
                            raise ValueError("skipped_one_file requires a one-file lane with a passed fix job")
                        codex["review_status"] = "skipped_one_file"
                        group.update(status="review", phase="review")
                    continue
                if action == "record-cleanup":
                    tracking = store["document"].setdefault("tracking", {})
                    run = tracking.get("verification_run")
                    if not isinstance(run, dict) or run.get("id") != operation.get("run_id"):
                        raise ValueError("cleanup requires the current verification run id")
                    outcome = operation.get("outcome")
                    if outcome not in ("passed", "failed", "not_run"):
                        raise ValueError("cleanup outcome must be passed, failed, or not_run")
                    reason = operation.get("reason", "")
                    if not isinstance(reason, str) or (outcome == "failed" and not reason.strip()):
                        raise ValueError("failed cleanup requires a reason")
                    run["cleanup"] = {"outcome": outcome, "reason": reason.strip(),
                                       "at": operation.get("timestamp") or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")}
                    continue
                if action == "archive-history":
                    tracking = store["document"].setdefault("tracking", {})
                    current_cycle = operation.get("through_cycle", RENDERER.rollover_cycle(tracking) - 1)
                    if type(current_cycle) is not int or current_cycle < 0:
                        raise ValueError("archive-history through_cycle must be a nonnegative integer")
                    archive_state = sidecar("archive")
                    moved = archive_cycle_history(tracking, current_cycle)
                    if moved:
                        archive_state["rows"] = append_archive_entries(archive_state["rows"], moved)
                        update_sidecar_summary(tracking, archive_state["path"], archive_state["rows"],
                                               sidecar("parked")["path"], sidecar("parked")["rows"])
                    continue
                if action in ("archive", "park", "unpark"):
                    tracking = store["document"].setdefault("tracking", {})
                    rollover = tracking.get("rollover", {})
                    if not isinstance(rollover, dict):
                        raise ValueError("tracking.rollover must be an object")
                    excluded = rollover.get("excluded_ids", [])
                    if not isinstance(excluded, list) or any(not isinstance(identifier, str) for identifier in excluded):
                        raise ValueError("tracking.rollover.excluded_ids must be an array of finding IDs")
                    excluded = set(excluded)
                    archive_state, parked_state = sidecar("archive"), sidecar("parked")
                    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
                    if action == "archive":
                        eligible = [item for item in findings if is_introspect_finding(item)
                                    or item.get("status") in CLOSED_STATUSES and item.get("id") in excluded]
                        requested = operation.get("ids")
                        if requested is None:
                            selected = eligible
                        elif (not isinstance(requested, list) or not requested
                              or any(not isinstance(identifier, str) or not identifier.strip() for identifier in requested)
                              or len(requested) != len(set(requested))):
                            raise ValueError("archive ids must be a nonempty list of unique finding IDs")
                        else:
                            eligible_by_id = {item["id"]: item for item in eligible}
                            missing = set(requested) - eligible_by_id.keys()
                            if missing:
                                raise ValueError("archive requires closed excluded findings or completed Introspect records: "
                                                 + ", ".join(sorted(missing)))
                            selected = [eligible_by_id[identifier] for identifier in requested]
                        selected_ids = {item["id"] for item in selected}
                        introspect_items = [item for item in selected if is_introspect_finding(item)]
                        archive_items = [item for item in selected if not is_introspect_finding(item)]
                        append_introspect_records(tracking, introspect_items)
                        additions = [archive_entry(item, current_pr(tracking), timestamp) for item in archive_items]
                        archive_state["rows"] = append_archive_entries(archive_state["rows"], additions)
                        findings[:] = [item for item in findings if item["id"] not in selected_ids]
                    elif action == "park":
                        ids, needs = operation.get("ids"), operation.get("needs")
                        if (not isinstance(ids, list) or not ids
                                or any(not isinstance(identifier, str) or not identifier.strip() for identifier in ids)
                                or len(ids) != len(set(ids))):
                            raise ValueError("park ids must be a nonempty list of unique finding IDs")
                        if not isinstance(needs, str) or not needs.strip() or "\n" in needs or "\r" in needs:
                            raise ValueError("park needs must be one nonempty line")
                        items = [findings[find_index(findings, identifier)] for identifier in ids]
                        if any(item.get("status") != "blocked" for item in items):
                            raise ValueError("only blocked findings can be parked")
                        additions = [{**copy.deepcopy(item), "needs": needs.strip(),
                                      "parked_at": timestamp, "parked_from_pr": current_pr(tracking)}
                                     for item in items]
                        parked_state["additions"].extend(additions)
                        parked_state["rows"] = append_parked_entries(parked_state["rows"], additions)
                        ids = set(ids)
                        findings[:] = [item for item in findings if item["id"] not in ids]
                    else:
                        ids = operation.get("ids")
                        if (not isinstance(ids, list) or not ids
                                or any(not isinstance(identifier, str) or not identifier.strip() for identifier in ids)
                                or len(ids) != len(set(ids))):
                            raise ValueError("unpark ids must be a nonempty list of unique finding IDs")
                        by_id = {row["id"]: row for row in parked_state["rows"] if isinstance(row.get("id"), str)}
                        if set(ids) - by_id.keys():
                            raise ValueError("unpark requires parked finding IDs: " + ", ".join(sorted(set(ids) - by_id.keys())))
                        live_by_id = {item["id"]: item for item in findings}
                        for identifier in ids:
                            restored = parked_finding(by_id[identifier])
                            restored["status"] = "blocked"
                            existing = live_by_id.get(identifier)
                            if existing is not None and existing.get("status") != "blocked":
                                raise ValueError(f"unpark conflicts with live finding {identifier}")
                            if existing is not None and existing != restored:
                                raise ValueError(f"unpark conflicts with changed live finding {identifier}")
                            if existing is None:
                                findings.append(restored)
                                live_by_id[identifier] = restored
                                run = tracking.get("verification_run")
                                if isinstance(run, dict) and run.get("status") in ("active", "complete"):
                                    run["status"] = "active"
                                    run.setdefault("checks", {})[identifier] = {"state": "pending"}
                        ids = set(ids)
                        parked_state["rows"] = [row for row in parked_state["rows"] if row.get("id") not in ids]
                    current_ids = {item["id"] for item in findings}
                    if "rollover" in tracking:
                        rollover["excluded_ids"] = sorted(excluded & current_ids)
                        rollover["unresolved_ids"] = [item["id"] for item in findings
                                                       if item.get("status") not in CLOSED_STATUSES]
                    run = tracking.get("verification_run")
                    if isinstance(run, dict) and isinstance(run.get("checks"), dict):
                        run["checks"] = {identifier: check for identifier, check in run["checks"].items()
                                          if identifier in current_ids}
                    update_sidecar_summary(tracking, archive_state["path"], archive_state["rows"],
                                           parked_state["path"], parked_state["rows"])
                    continue
                if action in ("correct-round", "rerun-verify"):
                    stamp = operation.get("timestamp")
                    if stamp is None:
                        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
                    elif not isinstance(stamp, str) or RENDERER.parse_instant(stamp) is None:
                        raise ValueError(f"{action} timestamp must be an ISO 8601 instant with timezone")
                    if action == "correct-round":
                        correct_round(store["document"].setdefault("tracking", {}), operation, stamp)
                    else:
                        rerun_verify(store["document"], operation, stamp)
                    continue
                if action in ("set-plan", "update-plan-group"):
                    tracking = store["document"].setdefault("tracking", {})
                    cycle = RENDERER.rollover_cycle(tracking)
                    if action == "set-plan":
                        previous_stage = tracking.get("workflow", {}).get("stage")
                        current_round = RENDERER.rollover_round(tracking)
                        current_plan = tracking.get("plan")
                        requested_plan = operation.get("plan")
                        if (not isinstance(requested_plan, dict) or type(requested_plan.get("round")) is not int
                                or not isinstance(requested_plan.get("groups"), list)):
                            raise ValueError("set-plan requires plan {round, groups}")
                        validate_plan_groups(requested_plan)
                        same_current_plan = (
                            isinstance(current_plan, dict)
                            and current_plan.get("cycle", cycle) == cycle
                            and type(current_plan.get("round", current_round)) is int
                            and current_plan.get("round", current_round) == current_round
                            and requested_plan["round"] == current_round
                        )
                        starting_new_round = should_advance_plan_round(tracking, previous_stage)
                        if previous_stage in ("verify", "introspect", "gate") and not same_current_plan:
                            require_current_round_introspect(tracking, "returning to plan_fix")
                        expected_round = current_round + 1 if starting_new_round else current_round
                        if requested_plan["round"] != expected_round:
                            if requested_plan["round"] == current_round + 1:
                                require_current_round_introspect(tracking, f"starting round {requested_plan['round']}")
                                groups = RENDERER.plan_groups_for_round(current_plan, current_round)
                                open_lanes = [f"{group['id']} ({RENDERER.plan_status(group)})" for group in groups
                                              if RENDERER.plan_status(group) not in ("verified", "blocked", "failed", "parked")]
                                if open_lanes:
                                    raise ValueError(f"cannot start round {requested_plan['round']}: round {current_round} has open lanes: {', '.join(open_lanes)}")
                            raise ValueError(f"set-plan plan.round must be {expected_round}; got {requested_plan['round']}")
                        if starting_new_round:
                            advance_plan_round(tracking, findings,
                                               datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"))
                        carry_plan_runtime_state(requested_plan["groups"], current_plan, same_current_plan and not starting_new_round)
                        plan = copy.deepcopy(requested_plan)
                        groups = plan["groups"]
                        for group in groups:
                            _lane_history(group, "failure_history")
                            _lane_history(group, "plan_review_history")
                        ids = [group.get("id") for group in groups if isinstance(group, dict)]
                        if len(ids) != len(groups) or any(not isinstance(i, str) or not i.strip() for i in ids) or len(ids) != len(set(ids)):
                            raise ValueError("plan groups require unique nonempty ids")
                        finding_ids = {item["id"] for item in findings}
                        states = RENDERER.PLAN_STATUSES | set(RENDERER.PLAN_STATUS_ALIASES) | {"done"}
                        for key in ("merge", "build", "verify"):
                            if key in plan and not isinstance(plan[key], dict):
                                raise ValueError(f"plan.{key} must be an object")
                        for group in groups:
                            if not isinstance(group.get("finding_ids"), list) or any(not isinstance(i, str) or i not in finding_ids for i in group["finding_ids"]):
                                raise ValueError(f"plan group {group['id']} references an unknown finding")
                            if group.get("status") not in states:
                                raise ValueError(f"plan group {group['id']} has invalid status")
                            if any(key in group and not isinstance(group[key], str) for key in ("name", "label", "verify_summary", "worktree")):
                                raise ValueError(f"plan group {group['id']} name, label, verify_summary and worktree must be strings")
                            if len(group.get("verify_summary", "").split()) > 10:
                                raise ValueError(f"plan group {group['id']} verify_summary must be at most 10 words")
                            if not isinstance(group.get("codex", {}), dict) or not isinstance(group.get("verify", {}), dict):
                                raise ValueError(f"plan group {group['id']} codex and verify must be objects")
                            check_install_sessions.validate_group_codex(group)
                            verify_data = group.get("verify", {})
                            if verify_data.get("summary") is not None and (not isinstance(verify_data["summary"], str) or len(verify_data["summary"].split()) > 10):
                                raise ValueError(f"plan group {group['id']} verify.summary must be at most 10 words")
                            if not isinstance(group.get("depends_on", []), list) or any(not isinstance(i, str) or i not in ids for i in group.get("depends_on", [])):
                                raise ValueError(f"plan group {group['id']} has invalid dependencies")
                            validate_plan_group_fields(group, RENDERER.rollover_round(tracking))
                            require_restore_lane(group, findings)
                        validate_dependencies(groups, findings, tracking)
                        plan["cycle"] = cycle
                        plan["round"] = RENDERER.rollover_round(tracking)
                        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
                        for group in groups: stamp_plan_group(group, stamp)
                        tracking["plan"] = plan
                        if not same_current_plan or starting_new_round:
                            current_workflow = tracking.get("workflow") if isinstance(tracking.get("workflow"), dict) else {}
                            workflow = {"stage": "plan_fix", "updated_at": stamp, "now": f"{stamp} — Plan recorded"}
                            if current_workflow.get("stale_after_min") is not None:
                                workflow["stale_after_min"] = current_workflow["stale_after_min"]
                            tracking["workflow"] = workflow
                            if "plan_fix" not in tracking.setdefault("stages_reached", []):
                                tracking["stages_reached"].append("plan_fix")
                    else:
                        plan = tracking.get("plan")
                        if not isinstance(plan, dict) or plan.get("cycle", cycle) != cycle:
                            raise ValueError("update-plan-group requires a plan for the current cycle")
                        for lane in plan.get("groups", []):
                            _lane_history(lane, "failure_history")
                            _lane_history(lane, "plan_review_history")
                        if 'expected_plan_sha256' in operation:
                            actual = hashlib.sha256(json.dumps(original_plan, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')).hexdigest()
                            if operation['expected_plan_sha256'] != actual:
                                raise ValueError('plan changed; refusing group update')
                        if any(key in operation for key in ('expected_group', 'expected_cycle', 'expected_round')):
                            previous = original_plan if isinstance(original_plan, dict) else {}
                            previous_group = next((g for g in previous.get('groups', []) if g.get('id') == operation.get('id')), None)
                            if (set(('expected_group', 'expected_cycle', 'expected_round')) - operation.keys()
                                    or operation['expected_group'] != previous_group
                                    or operation['expected_cycle'] != previous.get('cycle')
                                    or operation['expected_round'] != previous.get('round')):
                                raise ValueError('plan group changed; refusing group update')
                        group = next((g for g in plan.get("groups", []) if g.get("id") == operation.get("id")), None)
                        patch = operation.get("patch")
                        if group is None or not isinstance(patch, dict) or set(patch) - {"status", "codex", "verify", "label", "verify_summary", "name", "worktree", "finding_ids", "depends_on", "reason", "files", "file_notes", "phase", "blocked_reason", "parked_to", "build", "updated_at", "presuite", "quota_wait_until", "retry_cause"}:
                            raise ValueError("update-plan-group requires id and a valid group patch")
                        if group.get("status") == "blocked" and patch.get("status", "blocked") != "blocked":
                            raise ValueError(f"blocked lane {group.get('id')} can only be reopened with bcl unblock --reason")
                        if "finding_ids" in patch and (group.get("status") == "blocked" or _counted_failures(_lane_history(group, "failure_history"))) and set(patch["finding_ids"]) != set(group.get("finding_ids", [])):
                            raise ValueError(f"update-plan-group cannot change gated lane {group.get('id')} findings")
                        if any(key in patch and not isinstance(patch[key], str) for key in ("label", "verify_summary", "name", "worktree", "reason")):
                            raise ValueError("label, name, verify_summary, worktree and reason must be strings")
                        if len(patch.get("verify_summary", "").split()) > 10:
                            raise ValueError("verify_summary must be at most 10 words")
                        if "finding_ids" in patch and (not isinstance(patch["finding_ids"], list) or any(i not in {f["id"] for f in findings} for i in patch["finding_ids"])):
                            raise ValueError("finding_ids must reference tracked findings")
                        if "depends_on" in patch and (not isinstance(patch["depends_on"], list) or any(i not in {g.get("id") for g in plan.get("groups", [])} for i in patch["depends_on"])):
                            raise ValueError("depends_on must reference plan groups")
                        if "verify" in patch and not isinstance(patch["verify"], dict) or "codex" in patch and not isinstance(patch["codex"], dict):
                            raise ValueError("codex and verify patches must be objects")
                        if any(key in patch.get("codex", {}) for key in ("job", "fix_status", "review_status", "plan_review_status", "must_fix_note")):
                            raise ValueError("job receipts and review state can only change through bcl job")
                        if ((patch.get("status") == "running" and patch.get("phase", group.get("phase")) == "fix")
                                or (patch.get("status") == "review" and patch.get("phase", group.get("phase")) in ("review", "plan_review"))):
                            raise ValueError("use bcl job to start a lane phase")
                        if "verify" in patch and patch["verify"].get("summary") is not None and (not isinstance(patch["verify"]["summary"], str) or len(patch["verify"]["summary"].split()) > 10):
                            raise ValueError("verify.summary must be at most 10 words")
                        activity_fields = ('status', 'phase', 'verify', 'blocked_reason', 'codex')
                        before_activity = {key: copy.deepcopy(group.get(key)) for key in activity_fields}
                        if operation.get("merge_codex") and "codex" in patch:
                            patch["codex"] = {**group.get("codex", {}), **patch["codex"]}
                        group.update(patch)
                        check_install_sessions.validate_group_codex(group)
                        validate_plan_group_fields(group, RENDERER.rollover_round(tracking))
                        require_restore_lane(group, findings)
                        validate_plan_groups(plan)
                        validate_dependencies(plan['groups'], findings, tracking)
                        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
                        stamp_plan_group(group, stamp)
                        if any(group.get(key) != before_activity[key] for key in activity_fields):
                            workflow = tracking.get('workflow')
                            if isinstance(workflow, dict):
                                workflow['updated_at'] = stamp
                                workflow['now'] = f"{stamp} — Lane {group['id']} updated"
                        plan["round"] = RENDERER.rollover_round(tracking)
                    continue
                if action in ("set-stage", "set-activity"):
                    target = operation.get("stage")
                    tracking = store["document"].setdefault("tracking", {})
                    cycle = RENDERER.rollover_cycle(tracking)
                    if target == "report_rollover" and "install" in tracking.get("stages_reached", []):
                        require_current_round_introspect(tracking, "report_rollover")
                    if target == "install" and (not isinstance(tracking.get("plan"), dict) or tracking["plan"].get("cycle", cycle) != cycle or not tracking["plan"].get("groups")):
                        raise ValueError("install requires a recorded plan with at least one group for the current cycle")
                    if target == "install" and action == "set-stage":
                        workflow_definition.run_stage_check("install", tracking["plan"])
                    if target in workflow_definition.TRANSITIONS:
                        workflow_definition.require_transition(target, tracking.get("stages_reached", []))
                    if target == "merged":
                        fixed, threshold, met = workflow_definition.run_stage_check(
                            "merged", {"findings": findings, "tracking": tracking})
                        if not met:
                            reason = operation.get("override_reason")
                            if not args.force:
                                raise ValueError(f"merge requires {threshold} verified_fixed findings; count is {fixed}; "
                                                 "pass --force with --reason to record an already merged pull request")
                            reason = require_override_reason(reason, "merged")
                            rollover = tracking.setdefault("rollover", {})
                            if not isinstance(rollover, dict):
                                raise ValueError("tracking.rollover must be an object")
                            forced = rollover.setdefault("forced_gate_overrides", [])
                            if not isinstance(forced, list):
                                raise ValueError("tracking.rollover.forced_gate_overrides must be an array")
                            forced.append({"cycle": RENDERER.rollover_cycle(tracking), "action": "merged",
                                           "forced_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                                           "verified_fixed": fixed, "threshold": threshold, "reason": reason.strip()})
                if action == "introspect":
                    tracking = store["document"].setdefault("tracking", {})
                    workflow_definition.require_transition("introspect", tracking.get("stages_reached", []))
                    coverage = workflow_definition.run_stage_check("introspect",
                        operation.get("sections"), operation.get("steps"), operation.get("none"), tracking)
                    sections = validate_introspect_sections(operation.get("sections"))
                    issues = operation.get("issues") or []
                    if issues:
                        raise ValueError("code follow-ups must be separate issues linked from sections; do not pass issues/next_cycle on introspect")
                    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
                    linked = [item["issue_id"] for section in sections.values() if "items" in section for item in section["items"] if item.get("outcome") == "issue"]
                    finding_ids = {item.get("id") for item in findings}
                    missing = [identifier for identifier in linked if identifier not in finding_ids]
                    if missing:
                        raise ValueError("introspect section links unknown issue ids: " + ", ".join(missing))
                    all_none = all("none" in section for section in sections.values())
                    result = operation.get("result")
                    if result is not None and (not isinstance(result, str) or not result.strip()
                                               or "\n" in result or "\r" in result):
                        raise ValueError("introspect result must be one nonempty line")
                    if result is not None:
                        summary = result.strip()
                    elif all_none:
                        summary = "Introspect: no issues"
                    else:
                        summary = "Introspect recorded"
                    record = {"result": summary, "at": stamp, "steps": coverage["steps"],
                              "none": coverage["none"], "sections": sections,
                              "round": RENDERER.rollover_round(tracking),
                              "cycle": RENDERER.rollover_cycle(tracking)}
                    history = tracking.setdefault("introspect_history", [])
                    if not isinstance(history, list) or any(not isinstance(item, dict) for item in history):
                        raise ValueError("tracking.introspect_history must be an array of records")
                    tracking["introspect"] = record
                    history.append(copy.deepcopy(record))
                    reached = tracking.setdefault("stages_reached", [])
                    if "introspect" not in reached:
                        reached.append("introspect")
                    plan = store["document"].setdefault("tracking", {}).get("plan")
                    if isinstance(plan, dict):
                        plan["status"] = "closed"
                    continue
                if action in ("set-stage", "set-activity", "rollover"):
                    target = "report_rollover" if action == "rollover" else operation.get("stage")
                    tracking = store["document"].setdefault("tracking", {})
                    if operation.get("load_override") is not None:
                        reason = require_override_reason(operation["load_override"], "machine-load")
                        tracking.setdefault("machine_load_overrides", []).append(
                            {"cycle": RENDERER.rollover_cycle(tracking), "phase": target,
                             "reason": reason, "at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")})
                    previous_stage = tracking.get("workflow", {}).get("stage")
                    if previous_stage == "merged" and target != "merged":
                        raise ValueError("merged is terminal; start a new list with bcl rollover")
                    if target == "plan_fix" and previous_stage in ("verify", "introspect", "gate"):
                        require_current_round_introspect(tracking, "returning to plan_fix")
                    if target == "report_rollover" and "install" in tracking.get("stages_reached", []):
                        require_current_round_introspect(tracking, "rollover" if action == "rollover" else "report_rollover")
                    if target == "report_rollover" and action != "set-activity":
                        require_rollover_gate(findings, tracking, args.force, action,
                                              operation.get("override_reason", args.reason))
                    if target in ("plan_fix", "install", "verify") and action == "set-stage" and not any(item.get("status") == "in_progress" for item in findings):
                        raise ValueError(f"{target} requires at least one finding with status in_progress; log the finding and set it in progress first")
                    if target in RENDERER.WORKFLOW_STAGES and target not in tracking.setdefault("stages_reached", []):
                        tracking["stages_reached"].append(target)
                if action == "set-stage":
                    stage = operation.get("stage")
                    if stage not in (*RENDERER.WORKFLOW_STAGES, None) or "stage" not in operation:
                        raise ValueError("set-stage requires a valid stage or null")
                    tracking = store["document"].setdefault("tracking", {})
                    current_workflow = tracking.get("workflow") if isinstance(tracking.get("workflow"), dict) else {}
                    stale_after = operation.get("stale_after_min", current_workflow.get("stale_after_min"))
                    if stale_after is not None and (type(stale_after) is not int or stale_after < 1):
                        raise ValueError("stale_after_min must be a positive integer")
                    if stage == "merged":
                        merged = validate_merged_record(operation.get("merged"))
                        tracking["merged"] = copy.deepcopy(merged)
                        updated_at = merged["merged_at"]
                    else:
                        updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
                    tracking["workflow"] = {"stage": stage, "updated_at": updated_at}
                    if stale_after is not None:
                        tracking["workflow"]["stale_after_min"] = stale_after
                    continue
                if action == "set-activity":
                    stage, ids, activity = operation.get("stage"), operation.get("ids"), operation.get("now")
                    if stage == "install" and isinstance(ids, list) and not ids:
                        raise ValueError("set-activity for install requires a nonempty ID list and an in_progress finding; log a finding and set it in progress first")
                    if 'expected_plan_sha256' in operation:
                        actual = hashlib.sha256(json.dumps(original_plan, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')).hexdigest()
                        if operation['expected_plan_sha256'] != actual:
                            raise ValueError('plan changed; refusing activity update')
                    if stage not in RENDERER.WORKFLOW_STAGES:
                        raise ValueError("set-activity requires a valid stage")
                    current_workflow = tracking.get("workflow") if isinstance(tracking.get("workflow"), dict) else {}
                    stale_after = operation.get("stale_after_min", current_workflow.get("stale_after_min"))
                    if stale_after is not None and (type(stale_after) is not int or stale_after < 1):
                        raise ValueError("stale_after_min must be a positive integer")
                    if stage == "merged":
                        raise ValueError("use set-stage with merge metadata to mark a list merged")
                    if not isinstance(ids, list) or any(not isinstance(identifier, str) or not identifier.strip() for identifier in ids) or len(ids) != len(set(ids)):
                        raise ValueError("set-activity ids must be a list of unique finding IDs")
                    if not isinstance(activity, str) or not activity.strip() or "\n" in activity or "\r" in activity:
                        raise ValueError("set-activity now must be one nonempty line")
                    affected = [findings[find_index(findings, identifier)] for identifier in ids]
                    if stage in ('plan_fix', 'review', 'install', 'verify'):
                        plan = tracking.get('plan', {})
                        groups = RENDERER.plan_groups_for_round(plan, RENDERER.rollover_round(tracking))
                        if not groups:
                            raise ValueError('work requires a plan for the current round')
                        if groups:
                            matched = [group for group in groups if RENDERER.plan_status(group) != 'parked'
                                       and set(ids).intersection(group.get('finding_ids', []))]
                            covered = {identifier for group in matched
                                       for identifier in group.get('finding_ids', [])}
                            if any(identifier not in covered for identifier in ids):
                                raise ValueError('work requires an eligible current-round plan group for every finding')
                            validate_dependencies(groups, findings, tracking)
                            if stage == 'install':
                                workflow_definition.run_stage_check('install', plan, ids)
                                for group in matched:
                                    codex = group.get('codex') if isinstance(group.get('codex'), dict) else {}
                                    suite = group.get('presuite') if isinstance(group.get('presuite'), dict) else {}
                                    if (codex.get('fix_status') != 'passed'
                                            or codex.get('review_status') not in ('passed', 'skipped_one_file')
                                            or suite.get('status') != 'passed'):
                                        raise ValueError(f"install requires passed fix, review, and presuite jobs for lane {group['id']}")
                        by_id = {group['id']: group for group in groups}
                        for group in matched:
                            if set(ids).intersection(group.get('finding_ids', [])):
                                pending = [dep for dep in group.get('depends_on', [])
                                           if dep not in by_id or RENDERER.plan_status(by_id[dep]) != 'verified']
                                if pending:
                                    raise ValueError(f"{group['id']} must wait for verified dependencies: {', '.join(pending)}")
                    closed = [item["id"] for item in affected if item.get("status") in CLOSED_STATUSES]
                    if closed and operation.get("reopen") is not True:
                        raise ValueError(f"set-activity cannot reopen closed findings {', '.join(closed)}; pass \"reopen\": true")
                    for item in affected:
                        item["status"] = "in_progress"
                    if stage == "report_rollover":
                        require_rollover_gate(findings, store["document"].setdefault("tracking", {}), args.force, action,
                                              operation.get("override_reason", args.reason))
                    if stage in ("plan_fix", "install", "verify") and (not ids or not any(item.get("status") == "in_progress" for item in findings)):
                        raise ValueError(f"set-activity for {stage} requires a nonempty ID list and an in_progress finding; log a finding and set it in progress first")
                    timestamp = operation.get("timestamp")
                    if timestamp is None:
                        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
                    elif (not isinstance(timestamp, str)
                          or not re.search(r"(?:Z|[+-]\d\d:\d\d)$", timestamp)
                          or RENDERER.parse_instant(timestamp) is None):
                        raise ValueError("set-activity timestamp must be an ISO 8601 instant with timezone")
                    stage_model = validate_stage_model(operation.get("model"))
                    if stage == 'install':
                        for group in matched:
                            if set(ids).intersection(group.get('finding_ids', [])):
                                install = group.get('install') if isinstance(group.get('install'), dict) else {}
                                group.update(status='installing', phase='install',
                                             install={**install, 'status': 'running', 'started_at': timestamp})
                                stamp_stage_model(group, 'install', stage_model, timestamp)
                    elif stage == 'verify':
                        start_verification_lanes(store["document"], ids, timestamp, stage_model)
                    tracking["workflow"] = {"stage": stage, "updated_at": timestamp, "now": f"{timestamp} — {activity.strip()}"}
                    if stale_after is not None:
                        tracking["workflow"]["stale_after_min"] = stale_after
                    continue
                if action in run_actions:
                    if action == "finish-check" and operation.get("id") in started_checks:
                        raise ValueError("render the checking state before finishing the issue")
                    verification_action(store["document"], findings, operation)
                    if action == "start-check":
                        started_checks.add(operation.get("id"))
                    continue
                if action == "add":
                    item = operation.get("entry")
                    if not isinstance(item, dict):
                        raise ValueError("entry must be a JSON object")
                    warn_summary(item, adding=True)
                    validate_detail_limits(item)
                    trim_verification_evidence(item)
                    validate_ids(findings + [item])
                    findings.append(item)
                    continue
                index = find_index(findings, operation.get("id"))
                if action == "update":
                    patch = operation.get("patch")
                    if not isinstance(patch, dict):
                        raise ValueError("patch must be a JSON object")
                    if 'expected_status' in operation and (not isinstance(operation['expected_status'], str)
                            or findings[index].get('status') != operation['expected_status']):
                        raise ValueError(f"{findings[index]['id']} status changed; refusing update")
                    if "check" in patch or "check_history" in patch:
                        raise ValueError("use verification actions to update check progress")
                    carried_from = patch.get("carried_from")
                    if "carried_from" in patch and carried_from is not None and not (
                            type(carried_from) is int and carried_from > 0 or isinstance(carried_from, str) and carried_from.strip()):
                        raise ValueError("carried_from must be a positive round number or nonempty cycle")
                    updated = merge_patch(dict(findings[index]), patch)
                    if regresses_against_main(updated):
                        plan = store["document"].get("tracking", {}).get("plan", {})
                        updated_findings = [updated if item.get("id") == updated["id"] else item for item in findings]
                        for group in plan.get("groups", []):
                            if updated["id"] in group.get("finding_ids", []):
                                require_restore_lane(group, updated_findings)
                    warn_summary(updated)
                    validate_detail_limits(updated)
                    trim_verification_evidence(updated)
                    findings[index] = updated
                elif action == "delete":
                    run = store["document"].get("tracking", {}).get("verification_run", {})
                    if run.get("status") == "active":
                        raise ValueError("cannot delete an issue during an active verification run")
                    if run.get("status") == "complete":
                        run["checks"].pop(findings[index]["id"], None)
                    del findings[index]
                elif action == "record-evidence":
                    record = operation.get("record")
                    validate_record(record)
                    if "checked" not in record or "check_id" not in record:
                        raise ValueError("verification evidence requires checked, expected, observed and check_id")
                    store_verification_evidence(store["document"], findings[index], record)
                    validate_detail_limits(findings[index])
                    trim_verification_evidence(findings[index])
                else:
                    raise ValueError("unknown batch action")
            validate_ids(findings)
            store["document"]["findings"] = findings
            invalidate_dependents(store["document"], json.loads(original))
            sync_plan_verify(store["document"])
            if args.render and (args.render.resolve() == path.resolve() or args.render.resolve() == RENDERER.TEMPLATE.resolve()):
                raise ValueError("render output must differ from input and template")
            touch_workflow(store["document"])
            inventory = RENDERER.load_inventory(args.inventory) if args.inventory else None
            base = args.base or live_state.evidence_base(path)
            try:
                rendered = RENDERER.render(store["document"], base, inventory)
            except FileNotFoundError as error:
                if not missing_report_file(error, base):
                    raise
                missing_capture = error
                rendered = None
            html_original = args.render.read_bytes() if args.render and args.render.exists() else None
            parked_after_prewrite = None
            if "archive" in sidecars:
                state = sidecars["archive"]
                if state["rows"] != state["initial"]:
                    atomic_write(state["path"], state["original"], sibling_json_bytes(state["rows"]), tracker=False)
            if "parked" in sidecars:
                state = sidecars["parked"]
                parked_after_prewrite = append_parked_entries(state["initial"], state["additions"])
                if parked_after_prewrite != state["initial"]:
                    atomic_write(state["path"], state["original"], sibling_json_bytes(parked_after_prewrite), tracker=False)
            write(path, original, store, findings, validate=False, touch=False)
            if ("parked" in sidecars and parked_after_prewrite is not None
                    and sidecars["parked"]["rows"] != parked_after_prewrite):
                state = sidecars["parked"]
                parked_original = (sibling_json_bytes(parked_after_prewrite)
                                   if parked_after_prewrite != state["initial"] else state["original"])
                try:
                    atomic_write(state["path"], parked_original, sibling_json_bytes(state["rows"]), tracker=False)
                except (OSError, ValueError) as error:
                    raise ValueError(f"JSON updated but parked state failed: {error}; retry unpark") from error
            if rendered is None:
                path.with_suffix(".unsnapshotted").write_text("missing report capture\n")
                raise ValueError(f"JSON saved but report capture is missing: {missing_capture.filename}; restore it and render/snapshot, do not repeat the mutation")
            if args.render and rendered is not None:
                try:
                    run = store["document"].get("tracking", {}).get("verification_run")
                    (live_write if run else report_write)(args.render, html_original, rendered.encode("utf-8"))
                except (OSError, ValueError) as error:
                    raise ValueError(f"JSON updated but render failed: {error}") from error
            print(f"Applied {len(operations)} operations" + (f"; rendered {args.render}" if args.render else ""))
            return 0
        if store["kind"] == "json" and store["document"].get("tracking", {}).get("verification_run", {}).get("status") == "active":
            raise ValueError("use batch with --render while verification is active")
        if args.command in ("record-evidence", "rewrite-evidence"):
            if store["kind"] != "json":
                raise ValueError("record-evidence requires authoritative JSON")
            record = read_json(args.record)
            validate_record(record)
            if "checked" not in record or "check_id" not in record:
                raise ValueError("verification evidence requires checked, expected, observed and check_id")
            item = findings[find_index(findings, args.id)]
            records = item.setdefault("verification_evidence", [])
            if not isinstance(records, list):
                raise ValueError("verification_evidence must be an array")
            if args.command == "rewrite-evidence":
                identity = (record["build"], record["session"], record["timestamp"])
                matches = [i for i, previous in enumerate(records) if isinstance(previous, dict) and
                           (previous.get("build"), previous.get("session"), previous.get("timestamp")) == identity]
                if len(matches) != 1:
                    raise ValueError("rewrite-evidence requires exactly one existing record with the same build, session and timestamp")
                records[matches[0]] = record
            else:
                store_verification_evidence(store["document"], item, record)
            validate_detail_limits(item)
            trim_verification_evidence(item)
            write(path, original, store, findings)
            print(f"Recorded evidence for {args.id}")
            return 0
        if args.command == "add":
            item = read_json(args.entry)
            if not isinstance(item, dict):
                raise ValueError("entry must be a JSON object")
            warn_summary(item, adding=True)
            validate_detail_limits(item)
            trim_verification_evidence(item)
            if store["kind"] == "json":
                run = store["document"].get("tracking", {}).get("verification_run", {})
            validate_ids(findings + [item])
            findings.append(item)
            if store["kind"] == "json" and run.get("status") in ("active", "complete"):
                run["status"] = "active"
                run["checks"][item["id"]] = {"state":"pending"}
            write(path, original, store, findings)
            print(f"Added {item['id']}")
            return 0
        if args.command == "update":
            patch = read_json(args.patch)
            if not isinstance(patch, dict):
                raise ValueError("patch must be a JSON object")
            if "check" in patch or "check_history" in patch:
                raise ValueError("use verification actions to update check progress")
            index = find_index(findings, args.id)
            updated = merge_patch(dict(findings[index]), patch)
            updated["id"] = args.id
            warn_summary(updated)
            validate_detail_limits(updated)
            trim_verification_evidence(updated)
            findings[index] = updated
            validate_ids(findings)
            write(path, original, store, findings)
            print(f"Updated {args.id}")
            return 0
        if store["kind"] == "json":
            run = store["document"].get("tracking", {}).get("verification_run", {})
            if run.get("status") == "active":
                raise ValueError("cannot delete an issue during an active verification run")
            if run.get("status") == "complete":
                run["checks"].pop(args.id, None)
        index = find_index(findings, args.id)
        del findings[index]
        write(path, original, store, findings)
        print(f"Deleted {args.id}")
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.exit(2, f"manage-findings: {error}\n")


def main(argv=None):
    parser = parser_for()
    args = parser.parse_args(argv)
    if args.command in {'audit', 'list', 'get'}:
        return run(args, parser)
    try:
        # Serialize every supported mutation against verification launch and
        # keep a single lock order for plan, finding, and tracker writes.
        with tracker_lock.exclusive_lock(tracker_lock.plan_lock_path(args.data)):
            with tracker_lock.exclusive_lock(tracker_lock.tracker_lock_path(args.data)):
                if args.command == "batch":
                    with tracker_lock.exclusive_lock(tracker_lock.tracker_lock_path(args.data.resolve().parent)):
                        return run(args, parser)
                return run(args, parser)
    except (OSError, RuntimeError) as error:
        parser.exit(2, f'manage-findings: tracker lock failed: {error}\n')


if __name__ == "__main__":
    raise SystemExit(main())
