"""Explicit, evidence-backed Ledger pilot. Snapshot refresh only reads saved decisions."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import re
import sys
import tempfile
import time
from pathlib import Path

# Vendored: home-relative defaults; the MCP server passes the configured data dir and optional Decider.
STATE = Path(os.environ.get("OBSERVABILITY_DATA_DIR") or Path.home() / ".local/share/developer-observability/data") / "recommendations.json"
DECIDER = Path(os.environ["OBSERVABILITY_DECIDER_SCRIPT"]) if os.environ.get("OBSERVABILITY_DECIDER_SCRIPT") else None
TARGET = "context-ledger"
TTL = 86400
TRANSITIONS = {"open": {"investigating", "closed"}, "investigating": {"closed"}}
PURPOSES = {f"context-ledger.{purpose}": purpose for purpose in ("lookup", "development", "mixed")}
LOOKUP_TOOLS = {"find", "select", "get", "lookup", "get_decision", "find_decisions", "select_decisions"}
ACTIONS = {
    "audit_attribution": {
        "when": "Purpose remains unknown or helper-role totals mix lookup and development work.",
        "action": "Separate Ledger lookup cost from development and unknown-purpose helpers.",
        "completion": "Classify cited sessions using explicit task/call evidence; report lookup, development, mixed and unknown costs separately.",
    },
    "inspect_access": {
        "when": "Routine lookup sessions are identified; investigate their orchestration overhead without claiming a proven fix.",
        "action": "Inspect Ledger helper setup, reuse and result consumption.",
        "completion": "For cited routine lookups, report helper starts, reuse, parent consumption and total parent-plus-child cost; preserve unknown outcomes.",
    },
    "compare_value": {
        "when": "Routine access is separated from development; a paired value comparison is still missing.",
        "action": "Compare matched tasks with and without Ledger retrieval.",
        "completion": "Use the same task inputs and correctness checks; compare retries, total parent-plus-child effective tokens and elapsed time. Record inconclusive results.",
    },
    "wait_for_evidence": {
        "when": "No attributable lookup evidence exists, or sources cannot be resolved.",
        "action": "Collect attributable Ledger session evidence.",
        "completion": "Resolve source sessions and explicit task/call evidence before recommending architecture or deletion.",
    },
}


def collect(since: float, until: float, root: Path | None = None, *, sessions=None) -> dict:
    from . import token_burn  # lazy: only the findings producer ships it
    root = token_burn.SESSIONS if root is None else root
    """Completed-task evidence; patterns describe activity, never establish task purpose."""
    if sessions is None:
        sessions = token_burn.load_sessions(since, root)
    rows = []
    for session in sessions:
        completed = [session["turns"].get(turn, [None, None])[1] for turn in session["finals"]]
        completed_at = max((at for at in completed if at is not None and since <= at < until), default=None)
        if completed_at is None:
            continue
        requests = [r for r in session["requests"] if r["ts"] is not None and since <= r["ts"] <= completed_at]
        if not requests:
            continue
        items = [it for it in session["items"] if it.get("ts") is not None and since <= it["ts"] <= completed_at]
        commands = [it.get("cmd", "") for it in items if it["kind"] == "command"]
        lookup = sum(bool(re.search(r"lookup\.py\s+(find|select|get)\b", cmd)) for cmd in commands)
        operations = [it.get("tool", "") for it in items if it["kind"] == "mcp" and it.get("server") == "context-ledger"]
        mcp_lookups = sum(op in LOOKUP_TOOLS for op in operations)
        lookup += mcp_lookups
        development = sum(bool(re.search(r"context-ledger|ledger[_-]", cmd, re.I))
                          and bool(re.search(r"\b(test\w*|pytest|unittest|ruff|git diff)\b", cmd))
                          for cmd in commands)
        edited = [path for it in items if it["kind"] == "edit" for path in it.get("paths", [])]
        development += sum("context-ledger" in path for path in edited)
        reference = any("context-ledger" in json.dumps(it) for it in items)
        explicit = session.get("task_purpose")
        purpose = PURPOSES.get(explicit, "unknown") if isinstance(explicit, str) else "unknown"
        if not session["agent_path"] and not (lookup or operations or purpose != "unknown"):
            continue
        if not (lookup or development or reference or purpose != "unknown" or "ledger" in session["agent_path"].lower()):
            continue
        observed = "mixed" if lookup and development else "lookup" if lookup else "development" if development else "none"
        effective = sum(r["input"] - r["cached"] + r["output"] + token_burn.CACHED_WEIGHT * r["cached"] for r in requests)
        rows.append({"session_id": session["id"], "parent_id": session["parent"],
                     "task_path": session["agent_path"] or "root", "role": session["role"] or "root",
                     "source": session.get("source_path", "").replace(str(Path.home()), "~", 1), "purpose": purpose,
                     "purpose_basis": "session_meta.task_purpose" if purpose != "unknown" else "unknown",
                     "observed_activity": observed, "completed_at": completed_at,
                     "lookup_calls": lookup, "development_signals": development,
                     "other_ledger_operations": len(operations) - mcp_lookups,
                     "requests": len(requests), "effective_tokens": round(effective)})
    rows.sort(key=lambda row: row["session_id"])
    return {"window": {"since": since, "until": until}, "sessions": rows,
            "cost_by_purpose": {purpose: sum(r["effective_tokens"] for r in rows if r["purpose"] == purpose)
                                         for purpose in ("lookup", "development", "mixed", "unknown")},
            "limits": ["References and task names alone do not establish routine lookup purpose.",
                       "Only explicit namespaced task_purpose metadata establishes purpose; observations remain separate.",
                       "Observed lookup calls do not prove the whole session served only retrieval.",
                       "Effective tokens use the existing cache-weighted estimate; they are not billed currency.",
                       "Parent cost and prevented mistakes are not measured here.",
                       "This pilot covers local Codex sessions; other harnesses remain unassessed."]}


def eligible(evidence: dict) -> dict:
    rows = evidence["sessions"]
    if not rows or any(not row.get("source") for row in rows):
        labels = {"wait_for_evidence"}
    elif any(row["purpose"] in ("unknown", "mixed") for row in rows):
        labels = {"audit_attribution"}
    elif any(row["purpose"] == "lookup" for row in rows):
        labels = {"inspect_access", "compare_value"}
    else:
        labels = {"wait_for_evidence"}
    return {label: ACTIONS[label]["when"] for label in ACTIONS if label in labels}


def choose(evidence: dict) -> dict:
    criteria = eligible(evidence)
    if len(criteria) == 1:
        return {"choice": next(iter(criteria)), "origin": "deterministic_evidence_gate",
                "confidence": None, "low_confidence": False}
    if DECIDER is None or not DECIDER.is_file():
        raise ValueError("decider_unavailable: no local Decider configured")
    spec = importlib.util.spec_from_file_location("local_decider", DECIDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Send aggregates only; raw transcripts and local file paths stay out of the model input.
    state = {"purpose_counts": {p: sum(r["purpose"] == p for r in evidence["sessions"])
                                for p in evidence["cost_by_purpose"]},
             "effective_tokens": evidence["cost_by_purpose"], "limits": evidence["limits"]}
    question = {"action": {"type": "choice", "instructions": "Choose the next justified Ledger investigation. Do not infer no value from cost. No paired outcome comparison exists.", "criteria": criteria}}
    started = time.monotonic()
    response = module.call(module.DEFAULT_URL, "/v1/decisions",
                           {"model": module.DEFAULT_MODEL, "state": json.dumps(state), "questions": question}, timeout=10)
    decision = module.summarize(response.get("answers"), "action", criteria)
    return {**decision, "origin": "decider", "model": response.get("model", module.DEFAULT_MODEL),
            "usage": response.get("usage"), "ms": round((time.monotonic() - started) * 1000)}


def evidence_key(evidence: dict) -> str:
    return hashlib.sha256(json.dumps(evidence["sessions"], sort_keys=True).encode()).hexdigest()


def read_packet(path: Path) -> dict:
    packet = json.loads(path.read_text())
    if (not isinstance(packet, dict) or packet.get("schema") != 1 or
            not isinstance(packet.get("evidence"), dict) or
            not isinstance(packet["evidence"].get("sessions"), list) or
            not isinstance(packet["evidence"].get("cost_by_purpose"), dict) or
            packet.get("evidence_key") != evidence_key(packet["evidence"])):
        raise ValueError(f"invalid evidence packet: {path}")
    return packet


def prepare(sessions: list, since: float, now: float, runtime_dir: Path) -> dict:
    """Called by the existing snapshot scan; no model, second scan or duplicate packet write."""
    evidence = collect(since, now, sessions=sessions)
    key = evidence_key(evidence)
    path = runtime_dir / "recommendation-evidence.json"
    try:
        previous = read_packet(path) if path.exists() else None
    except (ValueError, OSError):
        previous = None  # Rebuild derived cache from this scan; consumers still fail closed.
    if previous is None or previous["evidence_key"] != key:
        _write_private(path, {"schema": 1, "evidence_key": key, "prepared_at": now, "evidence": evidence})
    return {"evidence_key": key, "observed_at": now, "sessions": len(evidence["sessions"])}


def _write_private(path: Path, value) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=1, sort_keys=True)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load(path: Path = STATE) -> list:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return []
    if not isinstance(data, dict) or data.get("schema") not in (1, 2) or not isinstance(data.get("records"), list):
        raise ValueError(f"invalid recommendation state: {path}")
    for record in data["records"]:
        if data["schema"] == 1 and isinstance(record, dict):
            old = record.get("status")
            if old in ("proposed", "applied", "verified", "inconclusive"):
                record["legacy_status"] = old
                record["status"] = {"proposed": "open", "applied": "investigating",
                                    "verified": "closed", "inconclusive": "closed"}[old]
                if record["status"] == "closed":
                    record["outcome"] = "resolved" if old == "verified" else "inconclusive"
        if (not isinstance(record, dict) or
                any(key not in record for key in ("id", "status", "expires_at", "action", "completion_check", "decision", "history")) or
                record["status"] not in (*TRANSITIONS, "closed") or
                not isinstance(record["expires_at"], (int, float)) or
                not math.isfinite(record["expires_at"]) or
                not isinstance(record["action"], str) or
                not isinstance(record["completion_check"], str) or
                not isinstance(record["history"], list) or
                not isinstance(record["decision"], dict) or
                not isinstance(record["decision"].get("low_confidence"), bool)):
            raise ValueError(f"invalid recommendation record: {path}")
        confidence = record["decision"].get("confidence")
        if confidence is not None and (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                                       or not 0 <= confidence <= 1):
            raise ValueError(f"invalid decision confidence: {path}")
        if record["status"] == "closed" and record.get("outcome") not in ("resolved", "inconclusive"):
            raise ValueError(f"closed recommendation needs an outcome: {path}")
    return data["records"]


def propose(records: list, evidence: dict, now: float, decision_fn=choose) -> dict:
    key = evidence_key(evidence)
    active = next((r for r in reversed(records) if r.get("target") == TARGET and r["status"] in TRANSITIONS), None)
    if active and (active["status"] != "open" or active["expires_at"] > now):
        return active  # never duplicate work already in progress
    latest = next((r for r in reversed(records) if r.get("target") == TARGET), None)
    if latest and latest["status"] not in TRANSITIONS and latest.get("evidence_key") == key:
        return latest  # no new source evidence since the last completed investigation
    decision = decision_fn(evidence)
    if decision["choice"] not in eligible(evidence):
        raise ValueError("decision selected an ineligible action")
    if active:
        active.update(status="closed", outcome="inconclusive")
        active["history"].append({"at": now, "status": "closed", "outcome": "inconclusive", "note": "expired before investigation"})
    action = ACTIONS[decision["choice"]]
    identity = hashlib.sha256(json.dumps([now, evidence], sort_keys=True).encode()).hexdigest()[:16]
    record = {"id": identity, "label": "Recommendation", "owner": "Codex", "target": TARGET,
              "status": "open", "generated_at": now, "expires_at": now + TTL,
              "action": action["action"], "completion_check": action["completion"],
              "scope": "Read-only investigation; no skill deletion or access-policy changes.",
              "decision": decision, "evidence": evidence,
              "evidence_key": key,
              "history": [{"at": now, "status": "open"}]}
    records.append(record)
    return record


def transition(record: dict, status: str, proof: dict, now: float) -> None:
    if status not in TRANSITIONS.get(record["status"], set()):
        raise ValueError(f"invalid transition: {record['status']} -> {status}")
    if not isinstance(proof, dict) or not isinstance(proof.get("note"), str) or not proof["note"].strip():
        raise ValueError("proof needs a nonempty note")
    if status == "closed":
        outcome, refs = proof.get("outcome"), proof.get("references")
        if outcome not in ("resolved", "inconclusive"):
            raise ValueError("closed investigation needs outcome resolved or inconclusive")
        if outcome == "resolved" and (not isinstance(refs, list) or not refs or
                                      not all(isinstance(ref, str) and Path(ref).expanduser().is_file() for ref in refs)):
            raise ValueError("resolved investigation needs existing local evidence references")
        record["outcome"] = outcome
    record["status"] = status
    record["history"].append({"at": now, "status": status, "proof": proof})


def fresh_observation(observation: dict, now: float) -> bool:
    observed_at = observation.get("observed_at")
    return (isinstance(observed_at, (int, float)) and not isinstance(observed_at, bool)
            and math.isfinite(observed_at) and 0 <= now - observed_at <= 300
            and isinstance(observation.get("evidence_key"), str))


def presentation(record: dict, observation: dict, now: float, *, retained=True) -> dict:
    """Fresh evidence can justify advice, never authenticate a repair."""
    observed_at = observation.get("observed_at")
    fresh = fresh_observation(observation, now)
    changed = observation["evidence_key"] != record.get("evidence_key") if fresh else None
    expired = record["expires_at"] <= now
    return {**record, "retained": retained, "evidence_changed": changed,
            "last_validated_at": observed_at if fresh and not changed else None,
            "expired": expired, "acceptance_status": "not_assessed",
            "actionable": (record["status"] in TRANSITIONS and not expired and fresh
                           and changed is False and not record["decision"]["low_confidence"])}


def attach(report: dict, path: Path, now: float) -> None:
    """No model calls or session scans in the snapshot path."""
    try:
        records = load(path)
    except (ValueError, OSError) as error:
        report["recommendation_error"] = str(error)
        return
    current = next((r for r in reversed(records) if r.get("target") == TARGET), None)
    if current:
        current = presentation(current, report.get("evidence_packet", {}), now)
    report["recommendations"] = [current] if current else []
    if not current:
        return
    prefix = ("Inconclusive investigation" if current.get("outcome") == "inconclusive" else
              "Closed investigation; acceptance not assessed" if current["status"] == "closed" else
              "Expired recommendation" if current["expired"] else
              "Uncertain recommendation" if current["decision"]["low_confidence"] else
              "Evidence changed; review investigation" if current["evidence_changed"] else
              "Evidence freshness unknown" if current["last_validated_at"] is None else "Recommendation")
    summary = f"{prefix}: {current['action']}"
    if current["decision"].get("confidence") is not None:
        summary += f" ({current['decision']['confidence']:.0%} choice confidence)"
    for card in report.get("cards", []):
        if (card["kind"], card["name"]) != ("rule", "ledger lookup"):
            continue
        card["recommendation"] = current
        card["why"] = summary[:240]
        card["tell"] = f"Read recommendation {current['id']} via bin/kindle-recommendation show in kindle-project"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=STATE)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("propose")
    sub.add_parser("show")
    sub.add_parser("evidence")
    update = sub.add_parser("update")
    update.add_argument("id")
    update.add_argument("status", choices=("investigating", "closed"))
    update.add_argument("--proof", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "show":
            print(json.dumps([presentation(r, {}, time.time()) for r in load(args.state)], indent=2))
            return 0
        if args.command == "evidence":
            print(json.dumps(read_packet(args.state.parent / "recommendation-evidence.json"), indent=2))
            return 0
        args.state.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock = args.state.with_suffix(".lock")
        with lock.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            records = load(args.state)
            original_records = list(records)
            now = time.time()
            if args.command == "propose":
                packet = read_packet(args.state.parent / "recommendation-evidence.json")
                findings = json.loads((args.state.parent / "findings.json").read_text())
                observed = findings.get("token_burn", {}).get("evidence_packet", {})
                if (observed.get("evidence_key") != packet["evidence_key"] or
                        not fresh_observation(observed, now)):
                    raise ValueError("evidence observation stale or incomplete; check the snapshot service")
                result = propose(records, packet["evidence"], now)
                result_view = presentation(result, observed, now, retained=result in original_records)

            else:
                result = next((r for r in records if r["id"] == args.id), None)
                if result is None:
                    raise ValueError(f"unknown recommendation: {args.id}")
                transition(result, args.status, json.loads(args.proof.read_text()), now)
                result_view = presentation(result, {}, now)
            _write_private(args.state, {"schema": 2, "records": records})
            print(json.dumps(result_view, indent=2))
        return 0
    except Exception as error:  # CLI boundary: never invent a recommendation on failure
        print(f"recommendation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
