#!/usr/bin/env python3
"""Bounded agent lookup and capture through the server's mcp=True gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PLUGIN))

DATA = Path(
    os.environ.get("CONTEXT_LEDGER_DATA")
    or Path.home() / ".context-ledger"
)


def _runtime_python() -> Path | None:
    lock = PLUGIN / "requirements.lock"
    if not lock.is_file():
        return None
    sha = hashlib.sha256(lock.read_bytes()).hexdigest()
    base = DATA / "runtime" / sha
    py = base / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return py if py.is_file() else None


def _use_runtime_python() -> None:
    """Use the setup interpreter when this one has no objc."""
    if os.environ.get("CONTEXT_LEDGER_LOOKUP_RUNTIME") == "1":
        return
    try:
        import objc  # noqa: F401
    except ImportError:
        py = _runtime_python()
    else:
        return
    if py is None:
        return
    env = os.environ.copy()
    env["CONTEXT_LEDGER_LOOKUP_RUNTIME"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    os.execve(str(py), [str(py), str(Path(__file__).resolve()), *sys.argv[1:]], env)


if __name__ == "__main__":
    _use_runtime_python()

from context_ledger.store import Store  # noqa: E402

# NLEmbedding sentence embedding, cosine distance (1 - cosine similarity).
# https://developer.apple.com/documentation/naturallanguage/finding-similarities-between-pieces-of-text
# A fit is the situation against applicability plus the needed decision against the action.
SIMILAR_MAX_DISTANCE = 1.75
# Need vs action, on its own. A paraphrase of the stored action is near 0.3.
# A different action in the same situation is past 1.0.
ACTION_SIMILAR_MAX_DISTANCE = 0.6
_EMBEDDING = None


def _now() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _fail(kind: str, error: str | None = None) -> int:
    payload: dict = {kind: "unavailable"}
    if error:
        payload["error"] = error
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")
    return 1


def _sentence_embedding():
    """On-device English sentence embedding. No lexical stand-in."""
    global _EMBEDDING
    if _EMBEDDING is not None:
        return _EMBEDDING
    try:
        import objc
    except ImportError as exc:
        raise RuntimeError("EMBEDDING_UNAVAILABLE") from exc
    objc.loadBundle(
        "NaturalLanguage",
        bundle_path="/System/Library/Frameworks/NaturalLanguage.framework",
        module_globals={},
    )
    embedding = objc.lookUpClass("NLEmbedding").sentenceEmbeddingForLanguage_("en")
    if embedding is None:
        raise RuntimeError("EMBEDDING_UNAVAILABLE")
    _EMBEDDING = embedding
    return embedding


def _distance(embedding, left: str, right: str) -> float:
    return float(embedding.distanceBetweenString_andString_distanceType_(left, right, 0))


def _text(value: object, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise ValueError(label)
    return value.strip()


def _visible(store: Store) -> list[dict]:
    store.open()
    rows = store.conn.execute(
        "select decision_id from current_records "
        "where scope_id=? and sensitivity='model_safe' and lifecycle='active' "
        "order by decision_id asc",
        (store.binding["scope_id"],),
    ).fetchall()
    return [
        store.get({"decision_id": row["decision_id"]}, mcp=True)["record"]
        for row in rows
    ]


def _fit_distance(embedding, situation: str, need: str, record: dict) -> float:
    body = record["body"]
    return _distance(embedding, situation, body["applicability"]) + _distance(
        embedding, need, body["action"]
    )


def _best(embedding, situation: str, need: str, records: list[dict]):
    chosen = None
    chosen_distance = None
    for record in records:
        distance = _fit_distance(embedding, situation, need, record)
        nearer = chosen is None or distance < chosen_distance
        tie = (
            chosen is not None
            and chosen_distance is not None
            and abs(distance - chosen_distance) <= 1e-9
            and record["decision_id"] < chosen["decision_id"]
        )
        if nearer or tie:
            chosen = record
            chosen_distance = distance
    return chosen, chosen_distance


def _flag(record: dict) -> str | None:
    if record["lifecycle"] == "superseded":
        replacement = record.get("replacement_id")
        if replacement:
            return f"superseded_by {replacement}"
        return "superseded"
    if record["outcome"]["status"] == "failed":
        return "status failed"
    return None


def _decision_view(record: dict) -> dict:
    body = record["body"]
    return {
        "id": record["decision_id"],
        "summary": body["summary"],
        "action": body["action"],
        "applicability": body["applicability"],
        "outcome": record["outcome"]["status"],
        "flag": _flag(record),
        "approval": body["approval"]["state"],
    }


def _similar(embedding, situation: str, need: str, records: list[dict], excluded: set[str]):
    """A precedent matches only when the needed action is near, not the situation alone."""
    chosen = None
    chosen_distance = None
    for record in records:
        if record["decision_id"] in excluded:
            continue
        action_distance = _distance(embedding, need, record["body"]["action"])
        if action_distance > ACTION_SIMILAR_MAX_DISTANCE:
            continue
        distance = _distance(embedding, situation, record["body"]["applicability"]) + action_distance
        if distance > SIMILAR_MAX_DISTANCE:
            continue
        nearer = chosen is None or distance < chosen_distance
        tie = (
            chosen is not None
            and chosen_distance is not None
            and abs(distance - chosen_distance) <= 1e-9
            and record["decision_id"] < chosen["decision_id"]
        )
        if nearer or tie:
            chosen = record
            chosen_distance = distance
    return chosen


def _gap_rationale(items: list[tuple[str, bool, str]]) -> str:
    parts = []
    for decision_id, helped, issue in items:
        if helped:
            continue
        parts.append(f"{decision_id}: {issue.strip() or 'did not help'}")
    text = "The provided decisions left this case unresolved."
    if parts:
        text = f"{text} {'; '.join(parts)}"
    return text[:800]


def _select(store: Store, body: dict) -> dict:
    if not isinstance(body, dict):
        raise ValueError("body")
    situation = _text(body.get("situation"), "situation", 320)
    need = _text(body.get("need"), "need", 160)
    embedding = _sentence_embedding()
    chosen, distance = _best(embedding, situation, need, _visible(store))
    if chosen is None or distance is None:
        return {"decision": None, "distance": None}
    return {"decision": _decision_view(chosen), "distance": distance}


def _uuid_text(value: object) -> str:
    try:
        if str(uuid.UUID(str(value))) != value:
            raise ValueError("task_id")
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("task_id") from exc
    return str(value)


def _parse_feedback(body: dict) -> tuple[str, list[tuple[str, bool, str]], tuple[str, str] | None] | dict:
    if not isinstance(body, dict):
        return {"written": [], "write": "unavailable"}
    try:
        task_id = _uuid_text(body.get("task_id"))
    except ValueError:
        return {"written": [], "write": "unavailable"}
    provided = body.get("provided")
    if not isinstance(provided, list):
        return {"written": [], "write": "unavailable"}
    items: list[tuple[str, bool, str]] = []
    seen: set[str] = set()
    for item in provided:
        if (
            not isinstance(item, dict)
            or set(item) - {"id", "helped", "issue"}
            or "id" not in item
            or "helped" not in item
            or item["helped"] not in (True, False)
        ):
            return {"written": [], "write": "unavailable"}
        issue = item.get("issue", "")
        if not isinstance(issue, str):
            return {"written": [], "write": "unavailable"}
        try:
            decision_id = _uuid_text(item["id"])
        except ValueError:
            return {"written": [], "write": "unavailable"}
        if decision_id in seen:
            return {"written": [], "write": "unavailable"}
        seen.add(decision_id)
        items.append((decision_id, item["helped"], issue))
    missing = body.get("would_have_helped", None)
    if missing is None:
        return task_id, items, None
    if not isinstance(missing, dict) or set(missing) != {"situation", "need"}:
        return {"written": [], "write": "unavailable"}
    try:
        situation = _text(missing.get("situation"), "situation", 320)
        need = _text(missing.get("need"), "need", 160)
    except ValueError:
        return {"written": [], "write": "unavailable"}
    return task_id, items, (situation, need)


def _feedback(store: Store, body: dict) -> dict:
    parsed = _parse_feedback(body)
    if isinstance(parsed, dict):
        return parsed
    task_id, items, missing = parsed
    try:
        for decision_id, _helped, _issue in items:
            store.get({"decision_id": decision_id}, mcp=True)
    except Exception:
        return {"written": [], "write": "unavailable"}
    embedding = _sentence_embedding() if missing is not None else None
    outcomes: list[str] = []
    try:
        for decision_id, helped, issue in items:
            note = issue.strip() or ("helped" if helped else "did not help")
            _outcome(
                store,
                task_id,
                decision_id,
                "success" if helped else "inconclusive",
                note,
                f"task://{task_id}",
            )
            outcomes.append(decision_id)
        if missing is None:
            return {
                "task_id": task_id,
                "outcomes": outcomes,
                "similar": None,
                "written": [],
            }
        situation, need = missing
        excluded = {decision_id for decision_id, helped, _issue in items if not helped}
        chosen = _similar(embedding, situation, need, _visible(store), excluded)
        if chosen is not None:
            return {
                "task_id": task_id,
                "outcomes": outcomes,
                "similar": chosen["decision_id"],
                "written": [],
            }
        receipt = store.record(
            {
                "request_id": str(uuid.uuid4()),
                "occurred_at": _now(),
                "body": {
                    "kind": "decision",
                    "summary": need,
                    "situation": situation,
                    "action": need,
                    "rationale": _gap_rationale(items),
                    "constraints": [],
                    "applicability": situation,
                    "entities": [],
                    "evidence": [
                        {
                            "ref": f"task://{task_id}",
                            "revision": None,
                            "sha256": None,
                            "state": "unverified",
                        }
                    ],
                    "sensitivity": "model_safe",
                    "approval": {"state": "none", "authority_ref": None},
                },
                "outcome": {"status": "pending", "note": "", "evidence": []},
            },
            mcp=True,
        )
    except RuntimeError as exc:
        if str(exc) == "EMBEDDING_UNAVAILABLE":
            raise
        return {"task_id": task_id, "outcomes": outcomes, "written": [], "write": "unavailable"}
    except Exception:
        return {"task_id": task_id, "outcomes": outcomes, "written": [], "write": "unavailable"}
    return {
        "task_id": task_id,
        "outcomes": outcomes,
        "similar": None,
        "written": [receipt["decision_id"]],
    }


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
    select = sub.add_parser("select")
    select.add_argument("--json", required=True)
    feedback = sub.add_parser("feedback")
    feedback.add_argument("--json", required=True)
    args = parser.parse_args()
    fail_kind = "write" if args.cmd in {"record", "append", "closeout", "feedback"} else "lookup"
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
            elif args.cmd == "select":
                payload = _select(store, _json_arg(args.json))
            elif args.cmd == "feedback":
                payload = _feedback(store, _json_arg(args.json))
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
    except RuntimeError as exc:
        if str(exc) == "EMBEDDING_UNAVAILABLE":
            return _fail(fail_kind, "EMBEDDING_UNAVAILABLE")
        return _fail(fail_kind)
    except Exception:
        return _fail(fail_kind)
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")
    if args.cmd == "feedback" and isinstance(payload, dict) and payload.get("write") == "unavailable":
        return 1
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
