#!/usr/bin/env python3
"""Rank caller-supplied rewordings against the local ledger.

Reads the ledger. Does not write to it. The fixture path is an argument.
This file does not contain decision ids or rewordings.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN))

DATA = Path(os.environ.get("CONTEXT_LEDGER_DATA") or Path.home() / ".context-ledger")


def _runtime_python() -> Path | None:
    lock = PLUGIN / "requirements.lock"
    if not lock.is_file():
        return None
    sha = hashlib.sha256(lock.read_bytes()).hexdigest()
    base = DATA / "runtime" / sha
    py = base / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return py if py.is_file() else None


def _use_runtime_python() -> None:
    if os.environ.get("CONTEXT_LEDGER_RECALL_RUNTIME") == "1":
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
    env["CONTEXT_LEDGER_RECALL_RUNTIME"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    os.execve(str(py), [str(py), str(Path(__file__).resolve()), *sys.argv[1:]], env)


if __name__ == "__main__":
    _use_runtime_python()

import importlib.util  # noqa: E402

from context_ledger.contracts import CANDIDATE_CAP, fts_tokens  # noqa: E402
from context_ledger.store import Store  # noqa: E402

_LOOKUP = PLUGIN / "skills" / "context-ledger" / "scripts" / "lookup.py"
_spec = importlib.util.spec_from_file_location("context_ledger_lookup", _LOOKUP)
if _spec is None or _spec.loader is None:
    raise RuntimeError("lookup script missing")
_lookup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_lookup)


def _fail(error: str) -> int:
    json.dump({"recall": "unavailable", "error": error}, sys.stdout)
    sys.stdout.write("\n")
    return 1


def _text(value: object, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise ValueError(label)
    return value.strip()


def _uuid(value: object) -> str:
    try:
        parsed = uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("target_id") from exc
    if str(parsed) != value:
        raise ValueError("target_id")
    return str(parsed)


def _cases(path: Path) -> list[dict]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("fixture") from exc
    if not isinstance(obj, dict) or set(obj) != {"cases"} or not isinstance(obj["cases"], list):
        raise ValueError("fixture")
    if not obj["cases"] or len(obj["cases"]) > 12:
        raise ValueError("fixture")
    cases = []
    seen = set()
    for item in obj["cases"]:
        if not isinstance(item, dict) or set(item) != {"name", "situation", "need", "target_id"}:
            raise ValueError("fixture")
        name = _text(item["name"], "name", 40)
        if name in seen:
            raise ValueError("name")
        seen.add(name)
        cases.append(
            {
                "name": name,
                "situation": _text(item["situation"], "situation", 320),
                "need": _text(item["need"], "need", 160),
                "target_id": _uuid(item["target_id"]),
            }
        )
    return cases


def _binding() -> dict:
    path = DATA / "binding.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("binding") from exc
    if not isinstance(raw, dict):
        raise ValueError("binding")
    ledger_dir = raw.get("ledger_dir")
    scope_id = raw.get("scope_id")
    if not isinstance(ledger_dir, str) or not isinstance(scope_id, str):
        raise ValueError("binding")
    return {"ledger_dir": ledger_dir, "scope_id": scope_id}


def _open(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma query_only=ON")
    mode = conn.execute("pragma query_only").fetchone()[0]
    if mode != 1:
        conn.close()
        raise ValueError("readonly")
    return conn


def _records(conn: sqlite3.Connection, scope_id: str) -> list[dict]:
    rows = conn.execute(
        "select projection_json from current_records "
        "where scope_id=? and sensitivity='model_safe' and lifecycle='active' "
        "order by decision_id asc",
        (scope_id,),
    ).fetchall()
    records = []
    for row in rows:
        rec = json.loads(row["projection_json"])
        if rec.get("lifecycle") != "active":
            continue
        if rec.get("body", {}).get("sensitivity") != "model_safe":
            continue
        records.append(rec)
    return records


def _select_order(embedding, situation: str, need: str, records: list[dict]) -> list[tuple[float, dict]]:
    scored = []
    for rec in records:
        distance = _lookup._fit_distance(embedding, situation, need, rec)
        scored.append((distance, rec))
    scored.sort(key=lambda item: (item[0], item[1]["decision_id"]))
    return scored


def _find_order(conn: sqlite3.Connection, scope_id: str, query: str, by_id: dict[str, dict]) -> list[dict]:
    tokens = fts_tokens(query)
    if not tokens:
        return []
    match = " OR ".join('"' + token.replace('"', "") + '"' for token in tokens)
    rows = conn.execute(
        "select c.decision_id from search_fts f "
        "join current_records c on c.decision_id=f.decision_id "
        "where c.scope_id=? and c.sensitivity='model_safe' and lifecycle='active' "
        "and search_fts match ? "
        "order by c.updated_at desc, c.decision_id asc limit ?",
        (scope_id, match, CANDIDATE_CAP + 1),
    ).fetchall()
    ids = [row["decision_id"] for row in rows[:CANDIDATE_CAP]]
    ranked = [by_id[did] for did in ids if did in by_id]
    dummy = Store.__new__(Store)
    parsed = {"entities": [], "constraints": []}
    ranked.sort(key=lambda rec: Store._rank(dummy, rec, parsed, tokens))
    return ranked


def _rank_of(order_ids: list[str], target_id: str) -> int | None:
    try:
        return order_ids.index(target_id) + 1
    except ValueError:
        return None


def _report(cases: list[dict], records: list[dict], conn: sqlite3.Connection, scope_id: str) -> dict:
    embedding = _lookup._sentence_embedding()
    by_id = {rec["decision_id"]: rec for rec in records}
    rows = []
    select_top1 = 0
    find_top3 = 0
    for case in cases:
        order = _select_order(embedding, case["situation"], case["need"], records)
        ids = [rec["decision_id"] for _, rec in order]
        select_rank = _rank_of(ids, case["target_id"])
        gap = None
        if len(order) > 1:
            gap = order[1][0] - order[0][0]
        nearest = order[0][1]["body"]["summary"] if order else None
        find_ids = [
            rec["decision_id"]
            for rec in _find_order(conn, scope_id, case["situation"], by_id)
        ]
        find_rank = _rank_of(find_ids, case["target_id"])
        if select_rank == 1:
            select_top1 += 1
        if find_rank is not None and find_rank <= 3:
            find_top3 += 1
        target_distance = None
        if select_rank is not None:
            target_distance = order[select_rank - 1][0]
        rows.append(
            {
                "name": case["name"],
                "target_id": case["target_id"],
                "select_rank": select_rank,
                "select_distance": target_distance,
                "gap_first_second": gap,
                "nearest_summary": nearest,
                "find_rank": find_rank,
            }
        )
    return {
        "records": len(records),
        "select_top1": f"{select_top1}/{len(cases)}",
        "find_top3": f"{find_top3}/{len(cases)}",
        "cases": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only recall ranks for a local fixture")
    parser.add_argument("--fixture", required=True, type=Path)
    args = parser.parse_args()
    try:
        cases = _cases(args.fixture)
        binding = _binding()
        db = Path(binding["ledger_dir"]) / "ledger.sqlite3"
        if not db.is_file():
            return _fail("UNAVAILABLE")
        conn = _open(db)
    except ValueError as exc:
        return _fail(str(exc))
    try:
        records = _records(conn, binding["scope_id"])
        payload = _report(cases, records, conn, binding["scope_id"])
    except RuntimeError as exc:
        return _fail(str(exc))
    finally:
        conn.close()
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
