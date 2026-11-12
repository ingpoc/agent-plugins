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

RENDERER_PATH = Path(__file__).with_name("render.py")
SPEC = importlib.util.spec_from_file_location("change_list_renderer", RENDERER_PATH)
RENDERER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RENDERER)

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


def write(path, original, store, findings, validate=True):
    if store["kind"] == "json":
        document = store["document"]
        document[store["collection"]] = findings
        if validate and store["collection"] == "findings":
            RENDERER.render(document, path.parent)
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
    batch.add_argument("--inventory", type=Path, help="optional installed-plugin inventory JSON")
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
            existing_run = store["document"].get("tracking", {}).get("verification_run")
            run_actions = {"start-verification", "start-check", "finish-check", "complete-verification", "correct-check", "set-stage", "set-activity"}
            if (existing_run or any(isinstance(op, dict) and op.get("action") == "rollover" for op in operations)) and not args.render:
                raise ValueError("rollover requires --render so the page stays current")
            if (existing_run or any(isinstance(op, dict) and op.get("action") in run_actions for op in operations)) and not args.render:
                raise ValueError("verification progress requires --render so the page stays current")
            started_checks = set()
            for operation in operations:
                if not isinstance(operation, dict):
                    raise ValueError("batch operation must be an object")
                action = operation.get("action")
                if action == "introspect":
                    # One Introspect = one record and at most ONE WF- task listing every issue found, never a task per issue.
                    issues = operation.get("issues", [])
                    result = operation.get("result")
                    if not isinstance(issues, list) or any(
                            not isinstance(i, dict) or not all(isinstance(i.get(k), str) and i[k].strip() for k in ("issue", "evidence"))
                            or i.get("outcome") not in ("fixed", "next_cycle") for i in issues):
                        raise ValueError("introspect issues must be [{issue, evidence, outcome: fixed|next_cycle}]; no evidence, no issue")
                    if not issues and (not isinstance(result, str) or not result.strip() or "\n" in result or "\r" in result):
                        raise ValueError("introspect requires result: one nonempty line, e.g. \"no issues\"")
                    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
                    record = {"result": (result or "").strip() or f"{len(issues)} issue(s)", "at": stamp}
                    if issues:
                        used = [int(f["id"][3:]) for f in findings if re.fullmatch(r"WF-\d+", str(f.get("id", "")))]
                        task_id = f"WF-{max(used, default=0) + 1:02d}"
                        clean = [{"issue": i["issue"].strip(), "evidence": i["evidence"].strip(), "outcome": i["outcome"]} for i in issues]
                        pending = [i["issue"] for i in clean if i["outcome"] == "next_cycle"]
                        task = {"id": task_id, "title": f"Introspect {stamp[:10]}: {len(issues)} issue(s), {len(pending)} for next cycle",
                                "source": "Agent reported", "status": "untested" if pending else "verified_fixed",
                                "observed": f"Introspect found {len(clean)} issue(s): {len(clean) - len(pending)} fixed during Introspect, {len(pending)} left for the next cycle.",
                                "recommendation": "; ".join(pending) or "None; all fixed during Introspect.",
                                "verification": "" if pending else "All issues fixed and checked during Introspect.",
                                "issues": clean}
                        if not pending:
                            task["verification_refs"] = [i["evidence"].strip() for i in issues]
                        validate_detail_limits(task)
                        validate_ids(findings + [task])
                        findings.append(task)
                        record["task"] = task_id
                    store["document"].setdefault("tracking", {})["introspect"] = record
                    continue
                if action in ("set-stage", "set-activity", "rollover"):
                    target = "report_rollover" if action == "rollover" else operation.get("stage")
                    tracking = store["document"].setdefault("tracking", {})
                    if target == "report_rollover" and "install" in tracking.get("stages_reached", []) and not tracking.get("introspect"):
                        raise ValueError("full cycle (reached install): record {\"action\":\"introspect\",\"result\":\"no issues\"} or its findings before report_rollover")
                    if target in RENDERER.WORKFLOW_STAGES and target not in tracking.setdefault("stages_reached", []):
                        tracking["stages_reached"].append(target)
                if action == "set-stage":
                    stage = operation.get("stage")
                    if stage not in (*RENDERER.WORKFLOW_STAGES, None) or "stage" not in operation:
                        raise ValueError("set-stage requires a valid stage or null")
                    store["document"].setdefault("tracking", {})["workflow"] = {"stage": stage, "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                    continue
                if action == "set-activity":
                    stage, ids, activity = operation.get("stage"), operation.get("ids"), operation.get("now")
                    if stage not in RENDERER.WORKFLOW_STAGES:
                        raise ValueError("set-activity requires a valid stage")
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
                    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
                    store["document"].setdefault("tracking", {})["workflow"] = {
                        "stage": stage, "updated_at": timestamp, "now": f"{timestamp} — {activity.strip()}"}
                    continue
                if action == "rollover":
                    # One step at merge time: exclude every verified_fixed finding, list the rest as carried over,
                    # point the page header at the new PR and worktree, and set the stage (was five manual edits).
                    pr, worktree = operation.get("pull_request"), operation.get("worktree")
                    if not isinstance(pr, dict) or not pr.get("label") or not pr.get("url") or not isinstance(worktree, str) or not worktree.strip():
                        raise ValueError("rollover requires pull_request {label,url} and worktree")
                    tracking = store["document"].setdefault("tracking", {})
                    fixed = [item["id"] for item in findings if item.get("status") == "verified_fixed"]
                    rollover = tracking.setdefault("rollover", {})
                    rollover["source_pr"] = operation.get("source_pr", rollover.get("source_pr"))
                    rollover["excluded_ids"] = sorted(set(rollover.get("excluded_ids", [])) | set(fixed))
                    rollover["unresolved_ids"] = [item["id"] for item in findings if item.get("status") not in CLOSED_STATUSES]
                    tracking["pull_request"], tracking["worktree"] = {"label": pr["label"], "url": pr["url"]}, worktree.strip()
                    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
                    note = str(operation.get("now") or f"Rolled over to {pr['label']}").strip().splitlines()[0]
                    tracking["workflow"] = {"stage": "report_rollover", "updated_at": timestamp, "now": f"{timestamp} — {note}"}
                    run = tracking.pop("verification_run", None)  # a run never carries into the next PR's list
                    if isinstance(run, dict):
                        tracking.setdefault("verification_history", []).append({**run, "status": "interrupted" if run.get("status") == "active" else run.get("status")})
                    tracking.pop("stages_reached", None)  # a new cycle starts after rollover
                    tracking.pop("introspect", None)
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
            if args.render and (args.render.resolve() == path.resolve() or args.render.resolve() == RENDERER.TEMPLATE.resolve()):
                raise ValueError("render output must differ from input and template")
            inventory = RENDERER.load_inventory(args.inventory) if args.inventory else None
            rendered = RENDERER.render(store["document"], path.parent, inventory)
            html_original = args.render.read_bytes() if args.render and args.render.exists() else None
            write(path, original, store, findings, validate=False)
            if args.render:
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
