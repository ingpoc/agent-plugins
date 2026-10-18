#!/usr/bin/env python3
"""Frozen retrieval eval for Context Ledger (v3 phase B BEFORE/AFTER gate).

Scores the two retrieval paths the protocol actually has, over one labelled case set, against a
caller-supplied ledger snapshot. Both scorers are the real production code, reached by path so the
gate cannot drift:

  select  skills/context-ledger/scripts/lookup.py   NLEmbedding fit over applicability+action,
          the helper's mandated choice. Scope-blind, no relevance floor, always returns a nearest.
          Scored through scripts/recall_check.py helpers (same fit function, same record set as
          lookup._visible: every model_safe active record in scope).
  find    context_ledger/store.py  Store.find          BM25 + IDF coverage floor, scope allowlist.
          Called for real, on a scratch copy of the snapshot, exactly as an MCP client would.

`find` is scored in two regimes:
  fleet   no caller_scope (Store's default allowlist is {"fleet"})
  scoped  the case's caller_scope, as a project-bound client would send it
A plugin that does not accept caller_scope is retried without it (scope_supported=false), the same
fallback isolation_eval.py uses.

Case schema (JSON):
  {"cases": [
     {"name": "<40 chars>",
      "query": "<terse string; the production find input, from the frozen isolation audit>",
      "situation": "<natural-language situation; the production select input>",
      "need": "<the kind of decision this work needs>",
      "targets": ["<full uuid>", ...],    # acceptable answers; [] = no durable answer exists
      "caller_scope": {"project": "<name>|null", "tools": ["<tool>", ...]},
      "note": "<why, optional>"}]}
A case with `targets: []` is a NEGATIVE case: the correct outcome is no answer. Today `select`
cannot express that, so its rank-1 record is a false positive and its distance is what calibrates
the floor.

Reads only. Pass a snapshot (sqlite3 .backup), never the live file. The scratch copy is temp and
deleted. HOME is not read or changed. Exit 0 always: the gate is BEFORE vs AFTER, not this code.
The runnable fixture holds real decision ids and query text: keep it outside the published repo.

usage:
  retrieval_eval.py --db SNAP.sqlite3 --scope SCOPE --fixture CASES.json \\
                    --plugin PLUGIN_DIR [--out OUT.json] [--label NAME]
Run it with the plugin runtime interpreter (the one that can import objc/NaturalLanguage).
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"missing module: {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _scratch_store(db: Path, plugin: Path, scope: str):
    """Copy the snapshot into a temp data dir the plugin can bind to. Deletes with `tmp`."""
    tmp = tempfile.TemporaryDirectory(prefix="cl-retrieval-eval-")
    data = Path(tmp.name) / "data"
    ledger = Path(tmp.name) / "ledger"
    data.mkdir(mode=0o700)
    ledger.mkdir(mode=0o700)
    shutil.copyfile(db, ledger / "ledger.sqlite3")
    os.chmod(ledger / "ledger.sqlite3", 0o600)
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        ledger_id = src.execute("select ledger_id from ledger_meta where singleton=1").fetchone()[0]
    finally:
        src.close()
    binding = {
        "schema_version": 1,
        "ledger_dir": str(ledger),
        "ledger_id": ledger_id,
        "scope_id": scope,
        "actor_id": "retrieval-eval",
    }
    bpath = data / "binding.json"
    bpath.write_text(json.dumps(binding, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    os.chmod(bpath, 0o600)
    from context_ledger.store import Store  # type: ignore

    return tmp, Store(data, actor_channel="agent")


def _snapshot_rows(conn: sqlite3.Connection, ids: list[str]) -> dict[str, dict]:
    """Rows the snapshot holds for ids, whatever their scope, sensitivity or lifecycle."""
    out: dict[str, dict] = {}
    for did in ids:
        row = conn.execute(
            "select scope_id, sensitivity, lifecycle from current_records where decision_id=?",
            (did,),
        ).fetchone()
        if row is not None:
            out[did] = dict(row)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Frozen retrieval eval (read-only)")
    ap.add_argument("--db", required=True, type=Path)
    ap.add_argument("--scope", required=True)
    ap.add_argument("--fixture", required=True, type=Path)
    ap.add_argument("--plugin", required=True, type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--label", default="")
    args = ap.parse_args()

    plugin = args.plugin.resolve()
    sys.path.insert(0, str(plugin))
    os.environ["LEDGER_AB_DATE"] = "2026-10-17"  # pre-A/B-window day: find always on
    rc = _load("context_ledger_recall_check", plugin / "scripts" / "recall_check.py")
    lookup = rc._lookup

    cases = json.loads(args.fixture.read_text(encoding="utf-8"))["cases"]
    if not cases:
        raise SystemExit("empty fixture")

    try:
        embedding = lookup._sentence_embedding()
    except RuntimeError as exc:
        json.dump({"retrieval": "unavailable", "error": str(exc)}, sys.stdout)
        sys.stdout.write("\n")
        return 1

    conn = rc._open(args.db)
    try:
        records = rc._records(conn, args.scope)
        by_id = {rec["decision_id"]: rec for rec in records}
        # A target the snapshot holds but the served set does not (archived, private, another
        # ledger scope) is a finding, not a fixture error: migration can make a labelled answer
        # unreachable. Only an id the snapshot lacks entirely is fatal.
        not_servable: dict[str, dict[str, dict]] = {}
        for case in cases:
            missing = [t for t in case.get("targets") or [] if t not in by_id]
            if not missing:
                continue
            known = _snapshot_rows(conn, missing)
            unknown = [t for t in missing if t not in known]
            if unknown:
                raise SystemExit(f"{case['name']}: target not in snapshot: {unknown}")
            not_servable[case["name"]] = known

        tmp, store = _scratch_store(args.db, plugin, args.scope)
        try:
            rows = []
            scope_supported = True
            for case in cases:
                targets = list(case.get("targets") or [])

                select = {}
                for variant, situation in (("natural", case["situation"]), ("terse", case["query"])):
                    order = rc._select_order(embedding, situation, case["need"], records)
                    ids = [rec["decision_id"] for _, rec in order]
                    rank = None
                    for target in targets:
                        got = rc._rank_of(ids, target)
                        if got is not None and (rank is None or got < rank):
                            rank = got
                    top = order[0] if order else None
                    select[variant] = {
                        "rank": rank,
                        "target_distance": round(order[rank - 1][0], 4) if rank else None,
                        "top_id": top[1]["decision_id"][:8] if top else None,
                        "top_distance": round(top[0], 4) if top else None,
                        "gap_1_2": round(order[1][0] - order[0][0], 4) if len(order) > 1 else None,
                        "top_is_target": bool(top and top[1]["decision_id"] in targets),
                    }

                find = {}
                for regime in ("fleet", "scoped"):
                    call = {
                        "query": case["query"],
                        "entities": [],
                        "constraints": [],
                        "limit": 3,
                        "actor": f"retrieval-eval.{case['name']}",
                    }
                    if regime == "scoped" and case.get("caller_scope"):
                        call["caller_scope"] = case["caller_scope"]
                    err = None
                    with contextlib.redirect_stderr(io.StringIO()):
                        try:
                            result = store.find(call, mcp=False)
                        except Exception as exc:  # noqa: BLE001
                            if getattr(exc, "code", None) == "INVALID_ARGUMENT" and "caller_scope" in call:
                                scope_supported = False
                                call.pop("caller_scope")
                                try:
                                    result = store.find(call, mcp=False)
                                except Exception as exc2:  # noqa: BLE001
                                    err = f"{type(exc2).__name__}:{getattr(exc2, 'code', '')}"
                                    result = {}
                            else:
                                err = f"{type(exc).__name__}:{getattr(exc, 'code', '')}"
                                result = {}
                    matches = result.get("matches", []) if isinstance(result, dict) else []
                    ids = [m["decision_id"] for m in matches]
                    rank = None
                    for target in targets:
                        got = rc._rank_of(ids, target)
                        if got is not None and (rank is None or got < rank):
                            rank = got
                    find[regime] = {
                        "rank": rank,
                        "hits": [i[:8] for i in ids],
                        "n": len(ids),
                        "error": err,
                        "arm": result.get("arm") if isinstance(result, dict) else None,
                    }
                rows.append(
                    {"name": case["name"], "negative": not targets,
                     "targets": [t[:8] for t in targets], "select": select, "find": find,
                     "not_servable": not_servable.get(case["name"], {})}
                )
        finally:
            with contextlib.suppress(Exception):
                store.close()
            tmp.cleanup()
    finally:
        conn.close()

    pos = [r for r in rows if not r["negative"]]
    neg = [r for r in rows if r["negative"]]

    def rank_block(ranks: list, label: str, extra: dict | None = None) -> dict:
        out = {
            "cases": len(ranks),
            "top1": f"{sum(1 for x in ranks if x == 1)}/{len(ranks)}",
            "top3": f"{sum(1 for x in ranks if x is not None and x <= 3)}/{len(ranks)}",
            "not_returned_at_all": f"{sum(1 for x in ranks if x is None)}/{len(ranks)}",
            "mrr": round(sum(1 / x for x in ranks if x is not None) / len(ranks), 4) if ranks else None,
        }
        out.update(extra or {})
        return out

    def select_block(regime: str) -> dict:
        ranks = [r["select"][regime]["rank"] for r in pos]
        dists = [r["select"][regime]["target_distance"] for r in pos if r["select"][regime]["target_distance"]]
        answered = [r for r in neg if r["select"][regime]["top_id"]]
        return rank_block(
            ranks,
            f"select_{regime}",
            {
                "mean_target_distance": round(sum(dists) / len(dists), 4) if dists else None,
                "negative_cases_answered": f"{len(answered)}/{len(neg)}",
                "negative_top_distance_min": round(min(r["select"][regime]["top_distance"] for r in answered), 4) if answered else None,
                "negative_top_distance_max": round(max(r["select"][regime]["top_distance"] for r in answered), 4) if answered else None,
            },
        )

    def find_block(regime: str) -> dict:
        ranks = [r["find"][regime]["rank"] for r in pos]
        errors = sorted({r["find"][regime]["error"] for r in rows if r["find"][regime]["error"]})
        return rank_block(
            ranks,
            f"find_{regime}",
            {
                "negative_cases_answered": f"{sum(1 for r in neg if r['find'][regime]['n'])}/{len(neg)}",
                "mean_hits_per_query": round(sum(r["find"][regime]["n"] for r in rows) / len(rows), 2),
                "total_hits": sum(r["find"][regime]["n"] for r in rows),
                "errors": errors,
            },
        )

    payload = {
        "label": args.label,
        "snapshot": args.db.name,
        "plugin": str(plugin),
        "ab_date_pin": os.environ["LEDGER_AB_DATE"],
        "records_in_scope_model_safe_active": len(records),
        "cases": len(rows),
        "positive": len(pos),
        "negative": len(neg),
        "select_natural": select_block("natural"),
        "select_terse": select_block("terse"),
        "find_fleet": find_block("fleet"),
        "find_scoped": find_block("scoped"),
        "caller_scope_supported": scope_supported,
        "targets_not_servable": sorted(not_servable),
        "rows": rows,
    }
    text = json.dumps(payload, indent=1, sort_keys=True)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    json.dump(payload, sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
