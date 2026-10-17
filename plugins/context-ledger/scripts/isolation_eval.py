#!/usr/bin/env python3
"""Frozen isolation eval for Context Ledger find (v3 acceptance gate, phase A).

Runs every find in tests/fixtures/isolation_30.json in-process against a COPY of a ledger
database, using the plugin at --plugin, and reports JSON plus a table:

  cross_project_leaks   hits in forbidden_ids for a project-bound caller
  unbound_project_hits  hits in forbidden_ids for an unbound caller (project null; judgment)
  offtopic_fillers      hits not in relevant_ids and with no text match (frozen judge below)
  fleet_lessons         fleet_ids found / expected
  relevant_found        relevant_ids found / labelled
  mean_bytes, mean_hits per find; zero_hit finds; zero_hit_violations (zero hits, zero_hits_ok false)

It never writes the source database: the source is opened read-only and copied with the SQLite
backup API into a fresh temp dir with its own binding.json (the plugin's --data dir). HOME is not
read or changed. Exit 0 always; the gate is BEFORE vs AFTER comparison, not this exit code.

usage: isolation_eval.py --db LEDGER.sqlite3 --plugin PLUGIN_DIR [--fixture F] [--out OUT.json]
                         [--label NAME] [--scope-arg NAME]
--scope-arg NAME also sends the caller binding ({"project", "tools"}) as find argument NAME; if the
plugin rejects it with INVALID_ARGUMENT the find is retried without it (scope_passed=false).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_FIXTURE = HERE.parent / "tests" / "fixtures" / "isolation_30.json"

# ---- frozen text-match judge: copy of context_ledger/ranking.py at main 0673cba ------------------
# Frozen so the gate does not move when v3 changes ranking. A hit "has a text match" when this
# judge's relevance floor passes for the query against the record's own text.
_STOP = frozenset(
    """a an the and or but if then else of to in on at by for from with without into onto via
    is are was were be been being am do does did doing done have has had having can could should would
    will shall may might must not no nor so too very just than that this these those it its i we you
    he she they them our my your his her their me us what which who whom whose when where why how
    all any both each few more most other some such only own same again further once here there
    about above below after before during over under up down out off use used using uses get gets
    got keep keeps kept still now new need needs make makes made also one two way ways thing things
    should agent agents task tasks""".split()
)
_TOKEN_RE = re.compile(r"[0-9a-z]+")
_W = (2.0, 1.5, 1.0)
_MIN_COVERAGE = 0.40


def _strip(word: str) -> str:
    for suf, rep in (("ies", "y"), ("ing", ""), ("ed", ""), ("er", ""), ("es", ""), ("s", "")):
        if word.endswith(suf) and len(word) - len(suf) >= 4:
            return word[: -len(suf)] + rep
    return word


def _stem(word: str) -> str:
    if word.isdigit():
        return word
    if word.startswith("un") and len(word) >= 7:
        word = word[2:]
    return _strip(_strip(word))


def _terms(text: str) -> list[str]:
    return [_stem(t) for t in _TOKEN_RE.findall(text.casefold()) if len(t) >= 2 and t not in _STOP]


def _qterms(query: str) -> list[str]:
    seen: list[str] = []
    for t in _terms(query):
        if t not in seen:
            seen.append(t)
    return seen[:12]


def _fields(body: dict) -> tuple[list[str], list[str], list[str]]:
    trace = body.get("trace") or {}
    other = " ".join([
        body.get("situation", ""), body.get("action", ""), body.get("applicability", ""),
        *body.get("constraints", []), *body.get("entities", []),
        trace.get("rule", ""), trace.get("exception", ""), trace.get("state", ""), *trace.get("inputs", []),
    ])
    return _terms(body.get("summary", "")), _terms(body.get("rationale", "")), _terms(other)


class TextJudge:
    def __init__(self, db: Path, scope: str) -> None:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "select decision_id, projection_json from current_records where scope_id=? and sensitivity='model_safe'",
                (scope,),
            ).fetchall()
        finally:
            conn.close()
        self.docs = {did: _fields(json.loads(pj)["body"]) for did, pj in rows}
        self.df: dict[str, int] = {}
        for f in self.docs.values():
            for t in set(f[0]) | set(f[1]) | set(f[2]):
                self.df[t] = self.df.get(t, 0) + 1
        self.n = max(len(self.docs), 1)

    def _idf(self, t: str) -> float:
        df = max(self.df.get(t, 0), 1)
        return math.log(1 + (self.n - df + 0.5) / (df + 0.5))

    def match(self, did: str, query: str) -> bool:
        q = _qterms(query)
        doc = self.docs.get(did)
        if not q or doc is None:
            return False
        total = sum(self._idf(t) for t in q) or 1.0
        covered, matched = 0.0, 0
        for t in q:
            if sum(w * f.count(t) for w, f in zip(_W, doc)) > 0:
                matched += 1
                covered += self._idf(t)
        return matched >= (1 if len(q) <= 2 else 2) and covered / total >= _MIN_COVERAGE


# ---- harness ----------------------------------------------------------------------------------
def _copy_db(src: Path, dst: Path) -> None:
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(str(dst))
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()
    os.chmod(dst, 0o600)


def _plugin_id(path: Path) -> dict:
    """plugin.json version, plus the git HEAD only when path is plugins/context-ledger of a checkout."""
    info: dict = {"version": None, "git_head": None}
    with contextlib.suppress(Exception):
        info["version"] = json.loads((path / "plugin.json").read_text(encoding="utf-8")).get("version")
    with contextlib.suppress(Exception):
        import subprocess
        top = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        if top and (Path(top) / "plugins" / "context-ledger").resolve() == path.resolve():
            info["git_head"] = subprocess.run(["git", "-C", top, "rev-parse", "--short", "HEAD"],
                                              capture_output=True, text=True, timeout=10).stdout.strip() or None
    return info


def _in(ids: list[str], did: str) -> bool:
    return any(did.startswith(p) for p in ids)


def _summ(rows: list[dict]) -> dict:
    n = len(rows) or 1
    hits = [h for r in rows for h in r["hits"]]
    return {
        "finds": len(rows),
        "hits": len(hits),
        "cross_project_leaks": sum(r["leaks"] for r in rows),
        "unbound_project_hits": sum(r["unbound_project_hits"] for r in rows),
        "offtopic_fillers": sum(r["fillers"] for r in rows),
        "fleet_lessons_found": sum(r["fleet_found"] for r in rows),
        "fleet_lessons_expected": sum(r["fleet_expected"] for r in rows),
        "relevant_found": sum(r["relevant_found"] for r in rows),
        "relevant_labelled": sum(len(r["relevant_ids"]) for r in rows),
        "mean_bytes": round(sum(r["bytes"] for r in rows) / n, 1),
        "mean_hits": round(len(hits) / n, 2),
        "zero_hit": sum(1 for r in rows if not r["hits"]),
        "zero_hit_violations": sum(1 for r in rows if not r["hits"] and not r["zero_hits_ok"]),
        "errors": sum(1 for r in rows if r.get("error")),
    }


def run(db: Path, plugin: Path, fixture: Path, scope_arg: str | None) -> dict:
    fx = json.loads(fixture.read_text(encoding="utf-8"))
    sys.path.insert(0, str(plugin))
    os.environ["LEDGER_AB_DATE"] = "2026-10-17"  # pre-A/B-window day: find always on
    from context_ledger.store import Store  # type: ignore
    try:
        from context_ledger.contracts import canonical_json  # type: ignore
    except Exception:  # pragma: no cover
        def canonical_json(o):  # noqa: E306
            return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        ledger_id = src.execute("select ledger_id from ledger_meta where singleton=1").fetchone()[0]
        all_ids = [r[0] for r in src.execute("select decision_id from current_records")]
    finally:
        src.close()
    scope = fx["ledger"]["scope_id"]
    prefixes = {p for f in fx["finds"] for k in ("relevant_ids", "fleet_ids", "forbidden_ids") for p in f[k]}
    ambiguous = sorted(p for p in prefixes if sum(1 for d in all_ids if d.startswith(p)) != 1)
    judge = TextJudge(db, scope)

    rows = []
    with tempfile.TemporaryDirectory(prefix="cl-iso-eval-") as tmp:
        data = Path(tmp) / "data"
        ledger = Path(tmp) / "ledger"
        data.mkdir(mode=0o700)
        ledger.mkdir(mode=0o700)
        _copy_db(db, ledger / "ledger.sqlite3")
        binding = {"schema_version": 1, "ledger_dir": str(ledger), "ledger_id": ledger_id,
                   "scope_id": scope, "actor_id": "isolation-eval"}
        bpath = data / "binding.json"
        bpath.write_text(json.dumps(binding, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        os.chmod(bpath, 0o600)
        store = Store(data, actor_channel="agent")
        try:
            for f in fx["finds"]:
                args = {"query": f["query"], "entities": f["entities"], "actor": f["actor"], "limit": 3}
                scope_passed = False
                err = None
                result = None
                with contextlib.redirect_stderr(io.StringIO()):
                    try:
                        if scope_arg:
                            try:
                                result = store.find({**args, scope_arg: f["binding"]}, mcp=False)
                                scope_passed = True
                            except Exception as exc:  # noqa: BLE001
                                if getattr(exc, "code", None) != "INVALID_ARGUMENT":
                                    raise
                        if result is None:
                            result = store.find(args, mcp=False)
                    except Exception as exc:  # noqa: BLE001
                        err = f"{type(exc).__name__}:{getattr(exc, 'code', '')}"
                        result = {"matches": [], "has_more": False}
                matches = result.get("matches", [])
                hits = [m["decision_id"] for m in matches]
                hits += [r["id"] for m in matches for r in m.get("related", [])]
                bound = f["binding"]["project"] is not None
                forb = [h for h in hits if _in(f["forbidden_ids"], h)]
                fill = [h for h in hits if not _in(f["relevant_ids"], h) and not judge.match(h, f["query"])]
                row = {
                    "id": f["id"], "query": f["query"], "project": f["binding"]["project"],
                    "hits": [h[:8] for h in hits],
                    "bytes": len(canonical_json({"ok": True, "data": result}).encode("utf-8")),
                    "leaks": len(forb) if bound else 0,
                    "unbound_project_hits": 0 if bound else len(forb),
                    "forbidden_hits": [h[:8] for h in forb],
                    "fillers": len(fill), "filler_ids": [h[:8] for h in fill],
                    "fleet_found": sum(1 for p in f["fleet_ids"] if any(h.startswith(p) for h in hits)),
                    "fleet_expected": len(f["fleet_ids"]),
                    "relevant_ids": f["relevant_ids"],
                    "relevant_found": sum(1 for p in f["relevant_ids"] if any(h.startswith(p) for h in hits)),
                    "zero_hits_ok": f["zero_hits_ok"], "scope_passed": scope_passed,
                }
                if err:
                    row["error"] = err
                rows.append(row)
        finally:
            with contextlib.suppress(Exception):
                store.close()

    entity = [r for r in rows if not r["id"].startswith("T")]
    return {
        "fixture": fixture.name,
        "db": db.name,
        "plugin_id": _plugin_id(plugin),
        "db_records": len(all_ids),
        "ambiguous_prefixes": ambiguous,
        "metrics": {"all": _summ(rows), "audit_entity_runs": _summ(entity)},
        "finds": rows,
    }


METRIC_ROWS = [
    ("cross_project_leaks", "cross_project_leaks"), ("unbound_project_hits", "unbound_project_hits"),
    ("offtopic_fillers", "offtopic_fillers"), ("fleet_lessons", None), ("relevant_found", None),
    ("mean_bytes", "mean_bytes"), ("mean_hits", "mean_hits"), ("zero_hit", "zero_hit"),
    ("zero_hit_violations", "zero_hit_violations"), ("hits", "hits"), ("errors", "errors"),
]


def table(rep: dict) -> str:
    m = rep["metrics"]
    out = [f"{'metric':<22}{'all (' + str(m['all']['finds']) + ')':>12}{'entity (' + str(m['audit_entity_runs']['finds']) + ')':>14}"]
    for name, key in METRIC_ROWS:
        vals = []
        for g in ("all", "audit_entity_runs"):
            s = m[g]
            if name == "fleet_lessons":
                vals.append(f"{s['fleet_lessons_found']}/{s['fleet_lessons_expected']}")
            elif name == "relevant_found":
                vals.append(f"{s['relevant_found']}/{s['relevant_labelled']}")
            else:
                vals.append(str(s[key]))
        out.append(f"{name:<22}{vals[0]:>12}{vals[1]:>14}")
    out.append("")
    out.append(f"{'id':<4}{'bytes':>6}  {'hits':<29}{'leak':>5}{'fill':>5}{'fleet':>7}")
    for r in rep["finds"]:
        leak = r["leaks"] or r["unbound_project_hits"]
        mark = "" if r["leaks"] or not r["unbound_project_hits"] else "u"
        out.append(f"{r['id']:<4}{r['bytes']:>6}  {','.join(r['hits']) or '-':<29}{str(leak) + mark:>5}{r['fillers']:>5}"
                   f"{str(r['fleet_found']) + '/' + str(r['fleet_expected']):>7}{'  ERR ' + r['error'] if r.get('error') else ''}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True, type=Path)
    ap.add_argument("--plugin", required=True, type=Path)
    ap.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--label")
    ap.add_argument("--scope-arg")
    a = ap.parse_args(argv)
    try:
        rep = run(a.db.resolve(), a.plugin.resolve(), a.fixture.resolve(), a.scope_arg)
        if a.label:
            rep = {"label": a.label, **rep}
    except Exception as exc:  # noqa: BLE001 - a report, never a failing gate
        rep = {"error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(rep))
        return 0
    text = json.dumps(rep, indent=1, ensure_ascii=False) + "\n"
    if a.out:
        a.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    print(table(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
