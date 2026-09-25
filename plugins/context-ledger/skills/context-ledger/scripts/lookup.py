#!/usr/bin/env python3
"""Bounded agent lookup and capture through the server's mcp=True gate."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PLUGIN))

from context_ledger.store import Store  # noqa: E402

DATA = Path(
    os.environ.get("CONTEXT_LEDGER_DATA")
    or os.environ.get("PLUGIN_DATA")
    or Path.home() / ".context-ledger"
)


def _now() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _fail(kind: str) -> int:
    json.dump({kind: "unavailable"}, sys.stdout)
    sys.stdout.write("\n")
    return 1


def _json_arg(value: str) -> dict:
    return json.loads(sys.stdin.read() if value == "-" else value)


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    find = sub.add_parser("find")
    find.add_argument("--query", required=True)
    get = sub.add_parser("get")
    get.add_argument("--id", required=True)
    record = sub.add_parser("record")
    record.add_argument("--json", required=True)
    append = sub.add_parser("append")
    append.add_argument("--json", required=True)
    closeout = sub.add_parser("closeout")
    closeout.add_argument("--json", required=True)
    args = parser.parse_args()
    fail_kind = "write" if args.cmd in {"record", "append", "closeout"} else "lookup"
    store = Store(DATA, actor_channel="agent")
    try:
        store.open()
        try:
            if args.cmd == "find":
                payload = store.find(
                    {
                        "query": args.query,
                        "entities": [],
                        "constraints": [],
                        "limit": 3,
                        "offset": 0,
                    },
                    mcp=True,
                )
            elif args.cmd == "get":
                rec = store.get({"decision_id": args.id}, mcp=True)["record"]
                payload = {
                    "id": rec["decision_id"],
                    "summary": rec["body"]["summary"],
                    "action": rec["body"]["action"],
                    "applicability": rec["body"]["applicability"],
                    "outcome": rec["outcome"]["status"],
                    "confidence": rec["confidence"],
                    "lifecycle": rec["lifecycle"],
                    "staleness": rec["staleness"],
                    "has_conflict": bool(rec.get("conflicts")),
                }
            elif args.cmd == "closeout":
                payload = _closeout(store, _json_arg(args.json))
            elif args.cmd == "append":
                event = _json_arg(args.json)
                rec = store.get({"decision_id": event["decision_id"]}, mcp=True)["record"]
                receipt = store.append_event(
                    {"request_id": str(uuid.uuid4()), "occurred_at": _now(),
                     "expected_revision": rec["revision"], **event},
                    mcp=True,
                )
                payload = {"written": [event["decision_id"]], "receipt": receipt}
            else:
                request = _json_arg(args.json)
                if set(request) == {"summary", "reason", "applicability", "evidence_ref"}:
                    if not all(str(value).strip() for value in request.values()):
                        raise ValueError("empty candidate field")
                    evidence = [{"ref": request["evidence_ref"], "revision": None,
                                 "sha256": None, "state": "unverified"}]
                    request = {
                        "body": {
                            "kind": "decision", "summary": request["summary"],
                            "situation": "Parent proposed a reusable decision at closeout.",
                            "action": request["summary"], "rationale": request["reason"],
                            "constraints": [], "applicability": request["applicability"],
                            "entities": [], "evidence": evidence,
                            "sensitivity": "model_safe",
                            "approval": {"state": "self_reported",
                                         "authority_ref": request["evidence_ref"]},
                        },
                        "outcome": {"status": "pending", "note": "", "evidence": evidence},
                    }
                receipt = store.record(
                    {"request_id": str(uuid.uuid4()), "occurred_at": _now(), **request},
                    mcp=True,
                )
                payload = {"written": [receipt["decision_id"]]}
        finally:
            store.close()
    except Exception:
        return _fail(fail_kind)
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")
    return 0


def _closeout(store: Store, body: dict) -> dict:
    if isinstance(body.get("closeout"), dict):
        body = body["closeout"]
    if set(body) != {"task_id", "outcomes"}:
        return {"written": [], "write": "unavailable"}
    task_id = body["task_id"]
    try:
        if str(uuid.UUID(task_id)) != task_id:
            return {"written": [], "write": "unavailable"}
    except (ValueError, TypeError, AttributeError):
        return {"written": [], "write": "unavailable"}
    outcomes = body.get("outcomes")
    expected_item_keys = {"id", "applied", "status", "note", "evidence_ref"}
    if not isinstance(outcomes, list) or any(
        not isinstance(item, dict) or set(item) != expected_item_keys
        for item in outcomes
    ):
        return {"written": [], "write": "unavailable"}
    ids = [item.get("id") for item in outcomes]
    if len(ids) != len(set(ids)) or any(
        item.get("applied") is not True
        or item.get("status") not in {"success", "failed", "inconclusive"}
        or not str(item.get("note") or "").strip()
        or not str(item.get("evidence_ref") or "").strip()
        for item in outcomes
    ):
        return {"written": [], "write": "unavailable"}
    written: list[str] = []
    feedback: list[dict] = []
    try:
        for item in outcomes:
            result = _outcome(store, task_id, item["id"], item["status"],
                              item["note"], item["evidence_ref"])
            written.extend(result["written"])
            feedback.append({"id": item["id"], "confidence": result["confidence"]})
    except Exception:
        return {"task_id": task_id, "written": written, "feedback": feedback,
                "write": "unavailable"}
    return {"task_id": task_id, "written": written, "feedback": feedback}


def _outcome(store: Store, task_id: str, decision_id: str, status: str,
             note: str, evidence_ref: str) -> dict:
    rec = store.get({"decision_id": decision_id}, mcp=True)["record"]
    store.append_event(
        {
            "request_id": str(uuid.uuid4()),
            "decision_id": decision_id,
            "expected_revision": rec["revision"],
            "occurred_at": _now(),
            "event_type": "outcome",
            "payload": {
                "status": status,
                "note": note[:500],
                "task_id": task_id,
                "evidence": [
                    {
                        "ref": evidence_ref,
                        "revision": None,
                        "sha256": None,
                        "state": "unverified",
                    }
                ],
            },
        },
        mcp=True,
    )
    updated = store.get({"decision_id": decision_id}, mcp=True)["record"]
    return {"written": [decision_id], "confidence": updated["confidence"]}


if __name__ == "__main__":
    raise SystemExit(main())
