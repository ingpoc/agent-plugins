#!/usr/bin/env python3
"""Read and edit individual findings in the authoritative JSON export."""
import argparse
import copy
from datetime import datetime, timezone
import importlib.util
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

import live_state

RENDERER_PATH = Path(__file__).with_name("render.py")
SPEC = importlib.util.spec_from_file_location("change_list_renderer", RENDERER_PATH)
RENDERER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RENDERER)


def stamp_plan_group(group, now):
    codex = group.get("codex")
    verify = group.get("verify")
    if isinstance(codex, dict):
        if codex.get("status") == "running": codex["updated_at"] = now
        for record in (group, codex):
            start, end = record.get("started_at"), record.get("ended_at")
            if isinstance(start, str) and isinstance(end, str):
                try: record["duration_s"] = max(0, int((datetime.fromisoformat(end.replace("Z", "+00:00")) - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds()))
                except ValueError: pass
    if isinstance(verify, dict):
        start, end = verify.get("started_at"), verify.get("ended_at")
        if isinstance(start, str) and isinstance(end, str):
            try: verify["duration_s"] = max(0, int((datetime.fromisoformat(end.replace("Z", "+00:00")) - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds()))
            except ValueError: pass


def warn_summary(item, adding=False):
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


def validate_record(record):
    fields = {"check", "outcome", "build", "session", "timestamp", "reference"}
    if not isinstance(record, dict) or set(record) != fields or any(not isinstance(record[k], str) or not record[k].strip() for k in fields):
        raise ValueError("evidence requires nonempty check, outcome, build, session, timestamp and reference strings")
    if record["timestamp"] != "unknown":
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", record["timestamp"]):
                raise ValueError()
            if datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00")).tzinfo is None:
                raise ValueError()
        except ValueError:
            raise ValueError("evidence timestamp must be ISO 8601 with timezone or unknown") from None


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
    outcome = normalized_text(record.get("outcome"))
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
        outcome = normalized_text(record.get("outcome"))
        if outcome and (outcome not in latest_outcomes or evidence_timestamp(record, index) > evidence_timestamp(records[latest_outcomes[outcome]], latest_outcomes[outcome])):
            latest_outcomes[outcome] = index
        kind = evidence_class(record)
        if kind and (kind not in latest or evidence_timestamp(record, index) > evidence_timestamp(records[latest[kind]], latest[kind])):
            latest[kind] = index
        if referenced_evidence(item, record):
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
        return
    if action == "start-verification":
        new = operation.get("run")
        fields = {"id", "build", "revision", "installed_at", "install_evidence"}
        if not isinstance(new, dict) or set(new) != fields or any(not isinstance(new[k], str) or not new[k].strip() for k in fields):
            raise ValueError("verification run requires id, build, revision, installed_at and install_evidence")
        validate_record({"check":"install", "outcome":"installed", "build":new["build"], "session":new["id"], "timestamp":new["installed_at"], "reference":new["install_evidence"]})
        if new["installed_at"] == "unknown":
            raise ValueError("installed_at must be a known timestamp")
        history = tracking.setdefault("verification_history", [])
        if not isinstance(history, list) or any(not isinstance(x, dict) for x in history):
            raise ValueError("verification_history must be an array of runs")
        if new["id"] in [x.get("id") for x in history] or (isinstance(run, dict) and new["id"] == run.get("id")):
            raise ValueError("verification run id must be unique")
        if run:
            history.append({**run, "status": "interrupted" if run.get("status") == "active" else run.get("status")})
        tracking["verification_run"] = {**new, "status":"active",
                                        "checks":{item["id"]:{"state":"pending"} for item in findings}}
        return
    reopens = action == "start-check" and operation.get("recheck") is True and isinstance(run, dict) and run.get("status") == "complete"
    if not isinstance(run, dict) or (run.get("status") != "active" and not reopens) or operation.get("run_id") != run.get("id"):
        raise ValueError("active verification run id is required")
    if action == "complete-verification":
        if any(run.get("checks", {}).get(item["id"], {}).get("state") not in ("fixed", "not_fixed", "blocked", "inconclusive") for item in findings):
            raise ValueError("finish or explicitly block every issue before completing the run")
        run["status"] = "complete"
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
    evidence, _ = validate_new_observation(evidence, operation, run, check.get("observed_at"), "finish-check evidence", check)
    if outcome == "fixed" and (item.get("status") != "verified_fixed" or not item.get("verification_refs")):
        raise ValueError("fixed requires verified_fixed status and verification references")
    if outcome == "not_fixed":
        item["status"] = "failing"
    checks[item["id"]] = {**check, "state":outcome, "evidence":evidence,
                           "finished_at":datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")}


def atomic_write(path, original, content):
    current = path.read_bytes() if path.exists() else None
    if current != original:
        raise ValueError("data file changed during this operation; reload the finding and retry")
    mode = stat.S_IMODE(path.stat().st_mode) if current is not None else 0o644
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as temp:
            os.fchmod(temp.fileno(), mode)
            temp.write(content)
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def live_write(path, original, content):
    if original is None:
        return atomic_write(path, None, content)
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



INTROSPECT_SECTIONS = ("delete", "simplify", "optimize", "automate")


def validate_introspect_sections(sections):
    if not isinstance(sections, dict) or any(name not in sections for name in INTROSPECT_SECTIONS):
        raise ValueError("introspect requires sections delete, simplify, optimize, automate")
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
            if not isinstance(text, str) or not text.strip() or not isinstance(evidence, str) or not evidence.strip():
                raise ValueError(f"introspect section {name} items require item and evidence")
            if outcome == "fixed inline":
                clean_items.append({"item": text.strip(), "evidence": evidence.strip(), "outcome": "fixed inline"})
            elif outcome == "issue":
                issue_id = item.get("issue_id")
                if not isinstance(issue_id, str) or not issue_id.strip() or re.fullmatch(r"WF-\d+", issue_id.strip()):
                    raise ValueError(f"introspect section {name} code items need a non-WF issue id")
                clean_items.append({"item": text.strip(), "evidence": evidence.strip(), "outcome": "issue", "issue_id": issue_id.strip()})
            else:
                raise ValueError(f"introspect section {name} outcome must be 'fixed inline' or 'issue'")
        cleaned[name] = {"items": clean_items}
    return cleaned


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
        verify = group.get("verify") if isinstance(group.get("verify"), dict) else {}
        verify.update({"status": status, "fixed": fixed, "failed": failed, "total": total})
        group["verify"] = verify

def touch_workflow(document):
    tracking = document.get("tracking")
    if isinstance(tracking, dict):
        tracking["cycle"] = RENDERER.rollover_cycle(tracking)
    workflow = tracking.get("workflow") if isinstance(tracking, dict) else None
    if isinstance(workflow, dict):
        workflow["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def require_rollover_gate(findings, tracking, force, action):
    fixed, threshold, met = RENDERER.rollover_gate(findings, tracking)
    if met:
        return
    if not force:
        raise ValueError(f"report_rollover gate not met: {fixed} of {threshold} verified_fixed findings and open findings remain; pass --force to override")
    rollover = tracking.setdefault("rollover", {})
    if not isinstance(rollover, dict):
        raise ValueError("tracking.rollover must be an object")
    forced = rollover.setdefault("forced_gate_overrides", [])
    if not isinstance(forced, list):
        raise ValueError("tracking.rollover.forced_gate_overrides must be an array")
    forced.append({"cycle": RENDERER.rollover_cycle(tracking), "action": action,
                   "forced_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                   "verified_fixed": fixed, "threshold": threshold})


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


def prepare_rollover(document, operation, force=False):
    findings = document.get("findings")
    tracking = document.setdefault("tracking", {})
    pr, worktree = operation.get("pull_request"), operation.get("worktree")
    if not isinstance(pr, dict) or not pr.get("label") or not pr.get("url") or not isinstance(worktree, str) or not worktree.strip():
        raise ValueError("rollover requires pull_request {label,url} and worktree")
    if "install" in tracking.get("stages_reached", []) and not tracking.get("introspect"):
        raise ValueError("full cycle (reached install): record introspect with --sections before rollover")
    require_rollover_gate(findings, tracking, force, "rollover")
    rollover = tracking.setdefault("rollover", {})
    if not isinstance(rollover, dict):
        raise ValueError("tracking.rollover must be an object")
    excluded = rollover.get("excluded_ids", [])
    if not isinstance(excluded, list) or any(not isinstance(identifier, str) for identifier in excluded):
        raise ValueError("tracking.rollover.excluded_ids must be an array of finding IDs")
    fixed = [item["id"] for item in findings if item.get("status") == "verified_fixed"]
    source_pr = operation.get("source_pr")
    if source_pr is None:
        match = re.search(r"/pull/(\d+)", str(tracking.get("pull_request", {}).get("url", "")))
        source_pr = int(match.group(1)) if match else rollover.get("source_pr")
    rollover["source_pr"] = source_pr
    rollover["excluded_ids"] = sorted(set(excluded) | set(fixed))
    rollover["unresolved_ids"] = [item["id"] for item in findings if item.get("status") not in CLOSED_STATUSES]
    if tracking.get("merged"):
        rollover["source_merge"] = tracking.pop("merged")
    tracking["pull_request"], tracking["worktree"] = {"label": pr["label"], "url": pr["url"]}, worktree.strip()
    timestamp = operation.get("now") or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    validate_record({"check":"rollover", "outcome":"started", "build":"unknown", "session":"rollover",
                     "timestamp":timestamp, "reference":"rollover"})
    round_number = RENDERER.rollover_round(tracking)
    tracking.setdefault("round_history", []).append({"round": round_number, "fixed": len(fixed), "ended_at": timestamp})
    tracking["round"] = 1
    tracking["cycle"] = RENDERER.rollover_cycle(tracking) + 1
    tracking["workflow"] = {"stage":"collect", "updated_at":timestamp}
    run = tracking.pop("verification_run", None)
    if isinstance(run, dict):
        tracking.setdefault("verification_history", []).append({**run, "status":"interrupted" if run.get("status") == "active" else run.get("status")})
    tracking.pop("stages_reached", None)
    tracking.pop("introspect", None)
    for item in findings:
        if item.get("status") == "in_progress":
            item["status"] = "untested"
    return document


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
    document = prepare_rollover(json.loads(live.read_text(encoding="utf-8")), operation, force)
    return live_state.register_rollover(record, live, document)[2]


def missing_report_file(error, base):
    filename = Path(error.filename or "")
    return filename.is_absolute() and filename.resolve().is_relative_to(Path(base).resolve())


def warn_missing_report_file(error, base):
    filename = Path(error.filename).resolve()
    print(f"warning: missing report capture {filename.relative_to(Path(base).resolve())}; tracker update saved and page render skipped", file=sys.stderr)


def write(path, original, store, findings, validate=True, touch=True):
    if store["kind"] == "json":
        document = store["document"]
        document[store["collection"]] = findings
        if validate and store["collection"] == "findings":
            base = live_state.evidence_base(path)
            try:
                RENDERER.render(document, base)
            except FileNotFoundError as error:
                if not missing_report_file(error, base):
                    raise
                warn_missing_report_file(error, base)
        if touch:
            touch_workflow(document)
        content = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    else:
        data = json.dumps(findings, ensure_ascii=True, separators=(",", ":")).replace("<", "\\u003c")
        text = store["text"][:store["start"]] + data + store["text"][store["end"]:]
        content = text.encode("utf-8")
    atomic_write(path, original, content)


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
    batch.add_argument("--force", action="store_true", help="override the verified-fix rollover gate and record the override")
    batch.add_argument("--collection", default="findings")
    return parser


def main(argv=None):
    parser = parser_for()
    args = parser.parse_args(argv)
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
                    (live_write if run else atomic_write)(args.render, html_original, rendered.encode("utf-8"))
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
                new_live = rollover_live(path, operations[0], args.force)
                print(f"Created new live list {new_live}; the current viewer stays on the old list until bcl start begins Collect")
                return 0
            existing_run = store["document"].get("tracking", {}).get("verification_run")
            run_actions = {"start-verification", "start-check", "finish-check", "complete-verification", "correct-check", "set-stage", "set-activity", "set-plan", "update-plan-group"}
            if (existing_run or any(isinstance(op, dict) and op.get("action") in run_actions for op in operations)) and not args.render:
                raise ValueError("verification progress requires --render so the page stays current")
            started_checks = set()
            for operation in operations:
                if not isinstance(operation, dict):
                    raise ValueError("batch operation must be an object")
                action = operation.get("action")
                if action in ("set-plan", "update-plan-group"):
                    tracking = store["document"].setdefault("tracking", {})
                    cycle = RENDERER.rollover_cycle(tracking)
                    if action == "set-plan":
                        plan = copy.deepcopy(operation.get("plan"))
                        if not isinstance(plan, dict) or type(plan.get("round")) is not int or not isinstance(plan.get("groups"), list):
                            raise ValueError("set-plan requires plan {round, groups}")
                        groups = plan["groups"]
                        ids = [group.get("id") for group in groups if isinstance(group, dict)]
                        if len(ids) != len(groups) or any(not isinstance(i, str) or not i.strip() for i in ids) or len(ids) != len(set(ids)):
                            raise ValueError("plan groups require unique nonempty ids")
                        finding_ids = {item["id"] for item in findings}
                        states = {"pending", "running", "blocked", "done", "failed"}
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
                            verify_data = group.get("verify", {})
                            if verify_data.get("summary") is not None and (not isinstance(verify_data["summary"], str) or len(verify_data["summary"].split()) > 10):
                                raise ValueError(f"plan group {group['id']} verify.summary must be at most 10 words")
                            if not isinstance(group.get("depends_on", []), list) or any(not isinstance(i, str) or i not in ids for i in group.get("depends_on", [])):
                                raise ValueError(f"plan group {group['id']} has invalid dependencies")
                        plan["cycle"] = cycle
                        plan["round"] = RENDERER.rollover_round(tracking)
                        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
                        for group in groups: stamp_plan_group(group, stamp)
                        tracking["plan"] = plan
                    else:
                        plan = tracking.get("plan")
                        if not isinstance(plan, dict) or plan.get("cycle", cycle) != cycle:
                            raise ValueError("update-plan-group requires a plan for the current cycle")
                        group = next((g for g in plan.get("groups", []) if g.get("id") == operation.get("id")), None)
                        patch = operation.get("patch")
                        if group is None or not isinstance(patch, dict) or set(patch) - {"status", "codex", "verify", "label", "verify_summary", "name", "worktree", "finding_ids", "depends_on", "reason", "files", "file_notes"}:
                            raise ValueError("update-plan-group requires id and a valid group patch")
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
                        if "verify" in patch and patch["verify"].get("summary") is not None and (not isinstance(patch["verify"]["summary"], str) or len(patch["verify"]["summary"].split()) > 10):
                            raise ValueError("verify.summary must be at most 10 words")
                        group.update(patch)
                        stamp_plan_group(group, datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"))
                        plan["round"] = RENDERER.rollover_round(tracking)
                        if "status" in patch and patch["status"] not in {"pending", "running", "blocked", "done", "failed"}:
                            raise ValueError("invalid plan group status")
                    continue
                if action in ("set-stage", "set-activity"):
                    target = operation.get("stage")
                    tracking = store["document"].setdefault("tracking", {})
                    cycle = RENDERER.rollover_cycle(tracking)
                    if target == "install" and (not isinstance(tracking.get("plan"), dict) or tracking["plan"].get("cycle", cycle) != cycle or not tracking["plan"].get("groups")):
                        raise ValueError("install requires a recorded plan with at least one group for the current cycle")
                    if target == "verify" and "install" not in tracking.get("stages_reached", []):
                        raise ValueError("verify requires install to be reached in the current cycle")
                if action == "introspect":
                    # One Introspect = one WF- task with four sections. Code follow-ups are separate non-WF issues.
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
                    if result:
                        summary = result.strip()
                    elif all_none:
                        summary = "Introspect: no issues"
                    else:
                        summary = "Introspect recorded"
                    if result and (not isinstance(result, str) or "\n" in result or "\r" in result):
                        raise ValueError("introspect result must be one nonempty line")
                    used = [int(f["id"][3:]) for f in findings if re.fullmatch(r"WF-\d+", str(f.get("id", "")))]
                    task_id = f"WF-{max(used, default=0) + 1:02d}"
                    task = {"id": task_id, "title": f"Introspect {stamp[:10]}: {summary}",
                            "source": "Agent reported", "status": "verified_fixed",
                            "observed": summary,
                            "recommendation": "Do the linked code issues: " + ", ".join(linked) if linked else "None; all items were fixed inline or none.",
                            "verification": "Inline items were done during Introspect." if not all_none else "Clean cycle; every section is none with a reason.",
                            "details": {"fixed_in": "Introspect"},
                            "sections": sections}
                    task["verification_refs"] = linked or ["Introspect"]
                    validate_detail_limits(task)
                    validate_ids(findings + [task])
                    findings.append(task)
                    run = store["document"].get("tracking", {}).get("verification_run", {})
                    if isinstance(run, dict) and run.get("status") in ("active", "complete"):
                        run["status"] = "active"
                        run.setdefault("checks", {})[task_id] = {"state": "pending"}
                    record = {"result": summary, "at": stamp, "task": task_id, "sections": sections}
                    store["document"].setdefault("tracking", {})["introspect"] = record
                    plan = store["document"].setdefault("tracking", {}).get("plan")
                    if isinstance(plan, dict):
                        plan["status"] = "closed"
                    continue
                if action in ("set-stage", "set-activity", "rollover"):
                    target = "report_rollover" if action == "rollover" else operation.get("stage")
                    tracking = store["document"].setdefault("tracking", {})
                    previous_stage = tracking.get("workflow", {}).get("stage")
                    if previous_stage == "merged" and target != "merged":
                        raise ValueError("merged is terminal; start a new list with bcl rollover")
                    if target == "report_rollover" and "install" in tracking.get("stages_reached", []) and not tracking.get("introspect"):
                        raise ValueError("full cycle (reached install): record introspect with --sections before report_rollover")
                    if target == "report_rollover" and action != "set-activity":
                        require_rollover_gate(findings, tracking, args.force, action)
                    if target in ("plan_fix", "install", "verify") and action == "set-stage" and not any(item.get("status") == "in_progress" for item in findings):
                        raise ValueError(f"{target} requires at least one finding with status in_progress; log the finding and set it in progress first")
                    if target in RENDERER.WORKFLOW_STAGES and target not in tracking.setdefault("stages_reached", []):
                        tracking["stages_reached"].append(target)
                if action == "set-stage":
                    stage = operation.get("stage")
                    if stage not in (*RENDERER.WORKFLOW_STAGES, None) or "stage" not in operation:
                        raise ValueError("set-stage requires a valid stage or null")
                    tracking = store["document"].setdefault("tracking", {})
                    if stage == "merged":
                        merged = validate_merged_record(operation.get("merged"))
                        tracking["merged"] = copy.deepcopy(merged)
                        updated_at = merged["merged_at"]
                    else:
                        updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
                    tracking["workflow"] = {"stage": stage, "updated_at": updated_at}
                    continue
                if action == "set-activity":
                    stage, ids, activity = operation.get("stage"), operation.get("ids"), operation.get("now")
                    if stage not in RENDERER.WORKFLOW_STAGES:
                        raise ValueError("set-activity requires a valid stage")
                    if stage == "merged":
                        raise ValueError("use set-stage with merge metadata to mark a list merged")
                    if not isinstance(ids, list) or any(not isinstance(identifier, str) or not identifier.strip() for identifier in ids) or len(ids) != len(set(ids)):
                        raise ValueError("set-activity ids must be a list of unique finding IDs")
                    if not isinstance(activity, str) or not activity.strip() or "\n" in activity or "\r" in activity:
                        raise ValueError("set-activity now must be one nonempty line")
                    affected = [findings[find_index(findings, identifier)] for identifier in ids]
                    closed = [item["id"] for item in affected if item.get("status") in CLOSED_STATUSES]
                    if closed and operation.get("reopen") is not True:
                        raise ValueError(f"set-activity cannot reopen closed findings {', '.join(closed)}; pass \"reopen\": true")
                    for item in affected:
                        item["status"] = "in_progress"
                    if stage == "report_rollover":
                        require_rollover_gate(findings, store["document"].setdefault("tracking", {}), args.force, action)
                    if stage in ("plan_fix", "install", "verify") and (not ids or not any(item.get("status") == "in_progress" for item in findings)):
                        raise ValueError(f"set-activity for {stage} requires a nonempty ID list and an in_progress finding; log a finding and set it in progress first")
                    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
                    if (stage == "plan_fix" and previous_stage in ("verify", "introspect", "gate")
                            and not RENDERER.rollover_gate(findings, tracking)[2]):
                        round_number = RENDERER.rollover_round(tracking)
                        history = tracking.setdefault("round_history", [])
                        history.append({"round": round_number, "fixed": RENDERER.rollover_gate(findings, tracking)[0], "ended_at": timestamp})
                        tracking["round"] = round_number + 1
                        if isinstance(tracking.get("plan"), dict): tracking["plan"]["round"] = round_number + 1
                    tracking["workflow"] = {"stage": stage, "updated_at": timestamp, "now": f"{timestamp} — {activity.strip()}"}
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
                    if re.fullmatch(r"WF-\d+", str(item.get("id", ""))):
                        raise ValueError("WF- tasks come only from one introspect call with sections (one task per Introspect)")
                    warn_summary(item, adding=True)
                    validate_detail_limits(item)
                    trim_verification_evidence(item)
                    run = store["document"].get("tracking", {}).get("verification_run", {})
                    validate_ids(findings + [item])
                    findings.append(item)
                    if run.get("status") in ("active", "complete"):
                        run["status"] = "active"
                        run["checks"][item["id"]] = {"state":"pending"}
                    continue
                index = find_index(findings, operation.get("id"))
                if action == "update":
                    patch = operation.get("patch")
                    if not isinstance(patch, dict):
                        raise ValueError("patch must be a JSON object")
                    if "check" in patch or "check_history" in patch:
                        raise ValueError("use verification actions to update check progress")
                    updated = merge_patch(dict(findings[index]), patch)
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
                    records = findings[index].setdefault("verification_evidence", [])
                    if not isinstance(records, list):
                        raise ValueError("verification_evidence must be an array")
                    records.append(record)
                    validate_detail_limits(findings[index])
                    trim_verification_evidence(findings[index])
                else:
                    raise ValueError("unknown batch action")
            validate_ids(findings)
            store["document"]["findings"] = findings
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
                warn_missing_report_file(error, base)
                rendered = None
            html_original = args.render.read_bytes() if args.render and args.render.exists() else None
            write(path, original, store, findings, validate=False, touch=False)
            if args.render and rendered is not None:
                try:
                    run = store["document"].get("tracking", {}).get("verification_run")
                    (live_write if run else atomic_write)(args.render, html_original, rendered.encode("utf-8"))
                except (OSError, ValueError) as error:
                    raise ValueError(f"JSON updated but render failed: {error}") from error
            print(f"Applied {len(operations)} operations" + (f"; rendered {args.render}" if args.render else ""))
            return 0
        if store["kind"] == "json" and store["document"].get("tracking", {}).get("verification_run", {}).get("status") == "active":
            raise ValueError("use batch with --render while verification is active")
        if args.command == "record-evidence":
            if store["kind"] != "json":
                raise ValueError("record-evidence requires authoritative JSON")
            record = read_json(args.record)
            validate_record(record)
            item = findings[find_index(findings, args.id)]
            records = item.setdefault("verification_evidence", [])
            if not isinstance(records, list):
                raise ValueError("verification_evidence must be an array")
            records.append(record)
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


if __name__ == "__main__":
    raise SystemExit(main())
