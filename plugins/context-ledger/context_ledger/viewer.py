"""Read-only ledger viewer: data for the MCP App tools and the Mini localhost page.

Every read opens SQLite with mode=ro and never touches access_log, so viewer traffic
cannot skew the activity numbers it shows. Only model_safe records are shown.
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

from context_ledger import ranking
from context_ledger.contracts import AB_END, AB_START, LedgerError, ab_arm, canonical_json, ist_today, parse_json
from context_ledger.store import Store

REPLY_CAP = 16 * 1024  # bytes per page, per plan.md section 3
PAGE_MAX = 25
ACTIVITY_PAGE_MAX = 120


def connect_ro(db_path: Path) -> sqlite3.Connection:
    path = Path(db_path).resolve()
    if not path.is_file():
        raise LedgerError("UNAVAILABLE", message="ledger unreachable")
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma query_only=1")
    return conn


def _int(args: dict[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    value = args.get(key, default)
    if type(value) is not int or not lo <= value <= hi:
        raise LedgerError("INVALID_ARGUMENT", message=f"{key} must be an integer {lo}-{hi}")
    return value


def _str(args: dict[str, Any], key: str, limit: int = 200) -> str:
    value = args.get(key) or ""
    if not isinstance(value, str) or len(value) > limit:
        raise LedgerError("INVALID_ARGUMENT", message=f"{key} must be a string of at most {limit} chars")
    return value.strip()


def _outcome_kind(payload: dict[str, Any]) -> str:
    """helped / not_helped / misled from the helpful mark; unmarked when the agent gave none."""
    if payload.get("helpful") is True:
        return "helped"
    if payload.get("helpful") is False:
        return payload.get("effect") if payload.get("effect") in ("not_helped", "misled") else "not_helped"
    return "unmarked"


def _task_logs() -> list[Path]:
    """A/B task logs ledger_loop.py writes (DATA/ledger-loop/tasks.jsonl). LEDGER_VIEWER_TASK_LOGS
    (os.pathsep list) overrides; default is every known DATA dir on this machine."""
    raw = os.environ.get("LEDGER_VIEWER_TASK_LOGS", "").strip()
    if raw:
        return [Path(x).expanduser() for x in raw.split(os.pathsep) if x]
    home = Path.home()
    return [home / ".context-ledger/ledger-loop/tasks.jsonl", home / ".agents/plugin-data/context-ledger/ledger-loop/tasks.jsonl"]


def _stats_module():
    """agent-context-efficiency/scripts/ledger_stats.py owns the A/B maths; load it, never copy it."""
    raw = os.environ.get("LEDGER_STATS_SCRIPT", "").strip()
    path = Path(raw).expanduser() if raw else Path.home() / ".agents/skills/agent-context-efficiency/scripts/ledger_stats.py"
    if not path.is_file():
        return None
    st = path.stat()
    if st.st_uid != os.getuid() or st.st_mode & 0o022:
        raise LedgerError("UNAVAILABLE", message="ledger_stats.py must be owned by this user and not group/world-writable")
    spec = importlib.util.spec_from_file_location("ledger_stats", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class LedgerView:
    def __init__(self, plugin_data: Path) -> None:
        # Store is used only for its binding and its read-only tier maths; it never opens the DB.
        self.store = Store(plugin_data, actor_channel="ui")
        self.db_path = self.store.db_path
        self.scope_id = self.store.binding["scope_id"]

    def _conn(self) -> sqlite3.Connection:
        return connect_ro(self.db_path)

    # ---------- records ----------
    def _records(self, conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
        rows = conn.execute(
            "select projection_json from current_records where scope_id=? and sensitivity='model_safe'",
            (self.scope_id,),
        ).fetchall()
        return {p["decision_id"]: p for p in (parse_json(r["projection_json"]) for r in rows)}

    def _links(self, conn: sqlite3.Connection, visible: set[str]) -> tuple[dict, dict, dict, dict]:
        cites: dict[str, list[str]] = {}
        cited: dict[str, list[str]] = {}
        for r in conn.execute("select src, dst from links order by src, dst"):
            if r["src"] in visible and r["dst"] in visible:
                cites.setdefault(r["src"], []).append(r["dst"])
                cited.setdefault(r["dst"], []).append(r["src"])
        fixes: dict[str, dict[str, str]] = {}
        fixed_by: dict[str, dict[str, str]] = {}
        for r in conn.execute("select src, dst, kind, context from fix_links order by src"):
            if r["src"] in visible and r["dst"] in visible:
                fixes[r["src"]] = {"id": r["dst"], "kind": r["kind"], "context": r["context"]}
                fixed_by[r["dst"]] = {"id": r["src"], "kind": r["kind"], "context": r["context"]}
        return cites, cited, fixes, fixed_by

    def _card(self, rec: dict[str, Any], links: tuple, tiers: dict[str, str]) -> dict[str, Any]:
        cites, cited, fixes, fixed_by = links
        body = rec["body"]
        trace = body.get("trace") or {}
        rtype = ranking.record_type(body)
        did = rec["decision_id"]
        conf = rec.get("confidence") or {}
        card: dict[str, Any] = {
            "id": did,
            "type": rtype,
            "d": ranking.words(body["summary"], 15) if rtype == "legacy" else body["summary"],
            "why": ranking.words(body.get("rationale") or "", 20) if rtype == "legacy" else body.get("rationale") or "",
            "entities": list(body.get("entities") or [])[:4],
            "at": rec.get("created_at"),
            "lifecycle": rec.get("lifecycle"),
            "by": (rec.get("provenance") or {}).get("actor_id"),
            "approval": (body.get("approval") or {}).get("state"),
            "conf": {
                "score": conf.get("score"),
                "ok": conf.get("successes", 0),
                "fail": conf.get("failures", 0),
                "n": conf.get("sample_count", 0),
            },
            "tier": tiers.get(did, "hint"),
        }
        for key in ("rule", "exception", "override_by", "approver", "context", "state"):
            if trace.get(key):
                card[key] = trace[key]
        if trace.get("criticality"):
            card["crit"] = trace["criticality"]
        if rec.get("replacement_id"):
            card["replaced_by"] = rec["replacement_id"]
        if did in fixes:
            card["fixes"] = fixes[did]
        if did in fixed_by:
            card["fixed_by"] = fixed_by[did]
        if cites.get(did):
            card["cites"] = cites[did][:8]
        if cited.get(did):
            card["cited_by"] = cited[did][:8]
        return card

    def query(self, args: dict[str, Any]) -> dict[str, Any]:
        q = _str(args, "q")
        limit = _int(args, "limit", PAGE_MAX, 1, PAGE_MAX)
        cursor = _int(args, "cursor", 0, 0, 100000)
        conn = self._conn()
        try:
            recs = self._records(conn)
            links = self._links(conn, set(recs))
            tiers = self.tiers(conn, recs)
        finally:
            conn.close()
        if q:
            ordered = self._search(recs, q)
        else:
            ordered = sorted(recs, key=lambda d: (recs[d].get("created_at") or "", d), reverse=True)
        items: list[dict[str, Any]] = []
        size = 0
        pos = cursor
        while pos < len(ordered) and len(items) < limit:
            card = self._card(recs[ordered[pos]], links, tiers)
            n = len(canonical_json(card).encode("utf-8")) + 1
            if items and size + n > REPLY_CAP - 256:
                break
            items.append(card)
            size += n
            pos += 1
        return {"items": items, "total": len(ordered), "next_cursor": pos if pos < len(ordered) else None, "q": q}

    @staticmethod
    def _search(recs: dict[str, dict[str, Any]], q: str) -> list[str]:
        """find's own BM25 corpus and relevance floor, so search shows what agents would retrieve.
        An id prefix (8+ hex chars) also matches."""
        needle = q.casefold()
        by_id = [d for d in recs if len(needle) >= 8 and d.startswith(needle)]
        qterms = ranking.query_terms(q)
        if not qterms:
            return by_id
        corpus = ranking.Corpus((d, r["body"], r.get("revision")) for d, r in recs.items())
        scored = []
        for d in recs:
            bm25, cov, matched = corpus.score(d, qterms)
            if ranking.relevant(cov, matched, len(qterms)):
                scored.append((-bm25, d))
        return by_id + [d for _, d in sorted(scored) if d not in by_id]

    # ---------- activity ----------
    def _events(self, conn: sqlite3.Connection, visible: set[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for r in conn.execute("select seq, ts, tool, caller, task_id, query, n, ids, ms from access_log"):
            raw = parse_json(r["ids"])
            ids = [i for i in raw if i in visible]
            if len(ids) != len(raw):
                continue  # touched a hidden record: drop the whole row, query text included
            ev = {"k": "lookup", "t": r["ts"], "a": r["caller"], "tool": r["tool"], "task": r["task_id"],
                  "ids": ids, "hits": len(ids), "ms": r["ms"]}
            if r["tool"] == "find" and r["query"] and ids:
                ev["q"] = r["query"][:120]  # a miss keeps no query text: it may have been an owner-only search
            out.append(ev)
        for r in conn.execute(
            "select decision_id, event_json, received_at from events where "
            "json_extract(event_json,'$.event_type') in ('applied','outcome')"
        ):
            if r["decision_id"] not in visible:
                continue
            ev_json = parse_json(r["event_json"])
            p = ev_json["payload"]
            actor = (ev_json.get("provenance") or {}).get("actor_id")
            if ev_json["event_type"] == "applied":
                ids = [i for i in (p.get("decision_ids") or [r["decision_id"]]) if i in visible]
                out.append({"k": "applied", "t": r["received_at"], "a": actor, "task": p.get("task_id"), "ids": ids})
            else:
                ev = {"k": "outcome", "t": r["received_at"], "a": actor, "task": p.get("task_id"),
                      "ids": [r["decision_id"]], "o": _outcome_kind(p), "ok": p.get("status") == "success"}
                if p.get("reason"):
                    ev["r"] = str(p["reason"])[:160]
                out.append(ev)
        out.sort(key=lambda e: e["t"] or "", reverse=True)
        return out

    def activity(self, args: dict[str, Any]) -> dict[str, Any]:
        actor = _str(args, "actor", 80)
        did = _str(args, "decision_id", 64)
        kind = _str(args, "kind", 16)
        since = _str(args, "since", 40)
        if kind and kind not in ("lookup", "applied", "outcome"):
            raise LedgerError("INVALID_ARGUMENT", message="kind must be lookup, applied or outcome")
        limit = _int(args, "limit", ACTIVITY_PAGE_MAX, 1, ACTIVITY_PAGE_MAX)
        cursor = _int(args, "cursor", 0, 0, 1000000)
        conn = self._conn()
        try:
            visible = set(self._records(conn))
            evs = self._events(conn, visible)
        finally:
            conn.close()
        evs = [e for e in evs if (not actor or e["a"] == actor) and (not did or did in e["ids"])
               and (not kind or e["k"] == kind) and (not since or (e["t"] or "") >= since)]
        items: list[dict[str, Any]] = []
        size = 0
        pos = cursor
        while pos < len(evs) and len(items) < limit:
            n = len(canonical_json(evs[pos]).encode("utf-8")) + 1
            if items and size + n > REPLY_CAP - 256:
                break
            items.append(evs[pos])
            size += n
            pos += 1
        return {"items": items, "total": len(evs), "next_cursor": pos if pos < len(evs) else None}

    # ---------- tiers + stats (logic owned by the retrieval-v2 code) ----------
    def tiers(self, conn: sqlite3.Connection, recs: dict[str, dict[str, Any]]) -> dict[str, str]:
        """find's own tier (ranking.trust_tier via Store._tier). No query context here, so no
        context-specific fix demotes a record; find may still show it as hint in that context."""
        return {did: self.store._tier(conn, rec, False) for did, rec in recs.items()}

    def stats(self, args: dict[str, Any]) -> dict[str, Any]:
        if args:
            raise LedgerError("INVALID_ARGUMENT", message="ledger_stats takes no arguments")
        conn = self._conn()
        try:
            recs = self._records(conn)
            tiers = self.tiers(conn, recs)
        finally:
            conn.close()
        tier_counts: dict[str, int] = {}
        for t in tiers.values():
            tier_counts[t] = tier_counts.get(t, 0) + 1
        today = ist_today()
        out: dict[str, Any] = {
            "today": today.isoformat(),
            "today_arm": ab_arm(today),
            "window": [AB_START.isoformat(), AB_END.isoformat()],
            "tiers": tier_counts,
        }
        mod = _stats_module()
        logs = [p for p in _task_logs() if p.is_file()]
        if mod is None:
            out["ab"] = None
            out["ab_note"] = "ledger_stats.py not installed on this machine"
            return out
        rows = mod.load(logs)
        out["ab"] = {arm: mod.arm_stats([r for r in rows if r.get("arm") == arm]) for arm in ("on", "off")}
        out["logs"] = len(logs)
        days = []
        d = AB_START
        while d <= AB_END:
            day_rows = [r for r in rows if r.get("date") == d.isoformat()]
            days.append({
                "date": d.isoformat(),
                "arm": ab_arm(d),
                "tasks": len(day_rows),
                "gated": sum(1 for r in day_rows if r.get("gated")),
                "hit_tasks": sum(1 for r in day_rows if r.get("gated") and r.get("helpful", 0) > 0),
                "fail_open": sum(1 for r in day_rows if r.get("gated") and r.get("fail_open")),
            })
            d += timedelta(days=1)
        out["days"] = days
        return out
