"""Ledger viewer v1: read-only data for the MCP App tools and the Mini localhost page."""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["LEDGER_AB_DATE"] = "2026-10-19"  # an odd A/B day: find on

from context_ledger.contracts import LedgerError, canonical_json  # noqa: E402
from context_ledger.store import Store, init_ledger  # noqa: E402
from context_ledger.viewer import REPLY_CAP, LedgerView, connect_ro  # noqa: E402
from context_ledger.viewer_local import make_handler  # noqa: E402


def _u() -> str:
    return str(uuid.uuid4())


def _body(summary: str, sensitivity: str = "model_safe") -> dict:
    return {"kind": "decision", "summary": summary, "situation": summary, "action": summary,
            "rationale": "fixture reason", "constraints": [], "applicability": "any",
            "entities": ["fixture"], "scope": "fleet", "evidence": [], "sensitivity": sensitivity,
            "approval": {"state": "none", "authority_ref": None}}


class ViewerBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name) / "data"
        self.data.mkdir(mode=0o700)
        init_ledger(self.data, None, "viewer-test", "tester")
        self.store = Store(self.data, actor_channel="agent")
        self._quiet = contextlib.redirect_stderr(io.StringIO())
        self._quiet.__enter__()
        self.kite = self.store.save({"request_id": _u(), "type": "user_decision", "actor": "efficient",
                                     "decision": "Kite holdings are read through Mini Comet only",
                                     "why": "The broker session is trusted only on the Mini",
                                     "entities": ["kite"], "rule": "broker-session", "scope": "fleet"}, mcp=True)["decision_id"]
        for i in range(30):
            self.store.save({"request_id": _u(), "type": "agent_conclusion", "criticality": 2, "actor": "orn",
                             "decision": f"Fixture conclusion {i} about train fares and seat quotas",
                             "why": "Filler so the list needs more than one page", "entities": ["travel"],
                             "scope": "fleet"}, mcp=True)
        self.secret = self.store.record({"request_id": _u(), "occurred_at": None,
                                         "body": _body("Local only kite note", "local_only"),
                                         "outcome": {"status": "pending", "note": "", "evidence": []}}, mcp=False)["decision_id"]
        self.view = LedgerView(self.data)

    def tearDown(self) -> None:
        self._quiet.__exit__(None, None, None)
        self.store.close()
        self._tmp.cleanup()

    def apply_and_close(self, did: str, actor: str, helpful: bool = True) -> str:
        task = _u()
        rev = self.store.get({"decision_id": did}, mcp=True)["record"]["revision"]
        ap = self.store.append_event({"request_id": _u(), "decision_id": did, "expected_revision": rev, "occurred_at": None,
                                      "event_type": "applied", "actor": actor,
                                      "payload": {"task_id": task, "decision_ids": [did]}}, mcp=True)
        payload = {"status": "success", "note": "", "evidence": [], "task_id": task,
                   "applied_event_id": ap["event_id"], "helpful": helpful}
        if not helpful:
            payload.update(effect="misled", reason="stale rule")
        self.store.append_event({"request_id": _u(), "decision_id": did, "expected_revision": rev + 1, "occurred_at": None,
                                 "event_type": "outcome", "actor": actor, "payload": payload}, mcp=True)
        return task


class ReadOnlyTests(ViewerBase):
    def _fingerprint(self) -> tuple[str, int]:
        db = self.store.db_path
        conn = sqlite3.connect(db)
        n = conn.execute("select count(*) from access_log").fetchone()[0]
        conn.close()
        return hashlib.sha256(db.read_bytes()).hexdigest(), n

    def test_reads_never_change_the_ledger_or_access_log(self) -> None:
        self.store.find({"query": "kite holdings comet", "actor": "efficient"}, mcp=True)
        self.store.close()
        before = self._fingerprint()
        self.view = LedgerView(self.data)  # construction must not open or migrate the DB either
        self.assertEqual(self._fingerprint(), before)
        self.view.query({})
        self.view.query({"q": "kite holdings"})
        self.view.activity({})
        self.view.stats({})
        self.assertEqual(self._fingerprint(), before)

    def test_connection_refuses_writes(self) -> None:
        conn = connect_ro(self.store.db_path)
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("insert into access_log(ts,tool,caller,n,bytes,ids,ms) values ('t','find','x',0,0,'[]',0)")
        conn.close()

    def test_bad_arguments_are_named_errors(self) -> None:
        for fn, args in ((self.view.query, {"limit": 99}), (self.view.query, {"cursor": -1}),
                         (self.view.activity, {"kind": "delete"}), (self.view.stats, {"x": 1})):
            with self.assertRaises(LedgerError) as ctx:
                fn(args)
            self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")


class QueryTests(ViewerBase):
    def test_pages_cover_every_model_safe_record_within_the_cap(self) -> None:
        seen, cursor = [], 0
        while cursor is not None:
            page = self.view.query({"cursor": cursor})
            self.assertLessEqual(len(canonical_json(page).encode()), REPLY_CAP)
            seen += [c["id"] for c in page["items"]]
            cursor = page["next_cursor"]
        self.assertEqual(len(seen), 31)
        self.assertEqual(len(set(seen)), 31)
        self.assertNotIn(self.secret, seen)

    def test_card_carries_decision_why_and_trace(self) -> None:
        card = self.view.query({"q": self.kite[:8]})["items"][0]
        self.assertEqual(card["d"], "Kite holdings are read through Mini Comet only")
        self.assertEqual(card["why"], "The broker session is trusted only on the Mini")
        self.assertEqual(card["type"], "user_decision")
        self.assertEqual(card["rule"], "broker-session")
        self.assertEqual(card["by"], "efficient")
        self.assertIn(card["tier"], ("trusted", "user_decision", "hint"))

    def test_search_uses_find_ranking_and_hides_local_only(self) -> None:
        hits = [c["id"] for c in self.view.query({"q": "kite holdings comet"})["items"]]
        self.assertEqual(hits[0], self.kite)
        self.assertNotIn(self.secret, hits)
        self.assertEqual(self.view.query({"q": "zebra unicorn"})["items"], [])
        self.assertEqual([c["id"] for c in self.view.query({"q": self.kite[:8]})["items"]][:1], [self.kite])

    def test_tier_matches_find_tier(self) -> None:
        for _ in range(3):
            self.apply_and_close(self.kite, "efficient")
        card = self.view.query({"q": self.kite[:8]})["items"][0]
        self.assertEqual(card["tier"], "trusted")
        self.assertEqual(card["conf"]["ok"], 3)
        found = self.store.find({"query": "kite holdings comet"}, mcp=True)["matches"][0]
        self.assertEqual(found["tier"], card["tier"])


class ActivityTests(ViewerBase):
    def test_timeline_merges_lookups_applied_and_outcomes_with_actor(self) -> None:
        self.store.find({"query": "kite holdings comet", "actor": "grok-bot", "task_id": _u()}, mcp=True)
        task = self.apply_and_close(self.kite, "efficient", helpful=False)
        items = self.view.activity({})["items"]
        kinds = {e["k"] for e in items}
        self.assertEqual(kinds, {"lookup", "applied", "outcome"})
        look = next(e for e in items if e["k"] == "lookup" and e["tool"] == "find")
        self.assertEqual((look["a"], look["q"]), ("grok-bot", "kite holdings comet"))
        self.assertIn(self.kite, look["ids"])
        out = next(e for e in items if e["k"] == "outcome")
        self.assertEqual((out["a"], out["task"], out["o"], out["r"]), ("efficient", task, "misled", "stale rule"))
        self.assertEqual([e["t"] for e in items], sorted((e["t"] for e in items), reverse=True))
        only = self.view.activity({"kind": "outcome", "actor": "efficient"})["items"]
        self.assertEqual([e["k"] for e in only], ["outcome"])

    def test_local_only_ids_and_their_queries_never_leave(self) -> None:
        self.store.find({"query": "local only kite note", "actor": "owner"}, mcp=False)
        self.store.find({"query": "zebra unicorn private", "actor": "owner"}, mcp=False)
        blob = canonical_json(self.view.activity({}))
        self.assertNotIn(self.secret, blob)
        self.assertNotIn("local only kite note", blob)
        self.assertNotIn("zebra unicorn private", blob)

    def test_writable_stats_script_is_refused(self) -> None:
        script = Path(self._tmp.name) / "ledger_stats.py"
        script.write_text("def load(p): return []\ndef arm_stats(r): return {}\n")
        script.chmod(0o666)
        os.environ["LEDGER_STATS_SCRIPT"] = str(script)
        try:
            with self.assertRaises(LedgerError):
                self.view.stats({})
        finally:
            os.environ.pop("LEDGER_STATS_SCRIPT")


class StatsTests(ViewerBase):
    def test_stats_without_the_report_script_still_answer(self) -> None:
        os.environ["LEDGER_STATS_SCRIPT"] = str(Path(self._tmp.name) / "missing.py")
        try:
            s = self.view.stats({})
        finally:
            os.environ.pop("LEDGER_STATS_SCRIPT")
        self.assertIsNone(s["ab"])
        self.assertEqual(s["today_arm"], "on")
        self.assertEqual(sum(s["tiers"].values()), 31)

    def test_stats_delegate_to_ledger_stats_load_and_arm_stats(self) -> None:
        tmp = Path(self._tmp.name)
        script = tmp / "ledger_stats.py"
        script.write_text(
            "import json\n"
            "def load(paths):\n"
            "    return [json.loads(l) for p in paths for l in p.read_text().splitlines() if l.strip()]\n"
            "def arm_stats(rows):\n"
            "    return {'gated': sum(1 for r in rows if r.get('gated')), 'marker': 'from-script'}\n")
        log = tmp / "tasks.jsonl"
        log.write_text("\n".join(json.dumps(r) for r in [
            {"ev": "task", "task_id": "a", "date": "2026-10-19", "arm": "on", "gated": True, "helpful": 1},
            {"ev": "task", "task_id": "b", "date": "2026-10-20", "arm": "off", "gated": True},
        ]) + "\n")
        os.environ.update(LEDGER_STATS_SCRIPT=str(script), LEDGER_VIEWER_TASK_LOGS=str(log))
        try:
            s = self.view.stats({})
        finally:
            os.environ.pop("LEDGER_STATS_SCRIPT")
            os.environ.pop("LEDGER_VIEWER_TASK_LOGS")
        self.assertEqual(s["ab"]["on"], {"gated": 1, "marker": "from-script"})
        self.assertEqual(s["ab"]["off"]["gated"], 1)
        days = {d["date"]: d for d in s["days"]}
        self.assertEqual(len(days), 14)
        self.assertEqual((days["2026-10-18"]["arm"], days["2026-10-19"]["arm"]), ("off", "on"))
        self.assertEqual(days["2026-10-19"]["hit_tasks"], 1)


class LocalServerTests(ViewerBase):
    def setUp(self) -> None:
        super().setUp()
        probe = ThreadingHTTPServer(("127.0.0.1", 0), lambda *a, **k: None)
        port = probe.server_address[1]
        probe.server_close()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(self.view, port, b"<!doctype html>page"))
        self.port = port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def req(self, method: str, path: str, host: str | None = None) -> tuple[int, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest(method, path, skip_host=True)
        conn.putheader("Host", host or f"127.0.0.1:{self.port}")
        conn.endheaders()
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, body

    def test_get_routes_return_real_rows(self) -> None:
        self.assertEqual(self.req("GET", "/"), (200, b"<!doctype html>page"))
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/")
        resp = conn.getresponse()
        resp.read()
        self.assertIn("frame-ancestors 'none'", resp.getheader("Content-Security-Policy"))
        self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))
        conn.close()
        status, body = self.req("GET", "/api/query")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["data"]["items"])
        for path in ("/api/activity", "/api/stats"):
            self.assertEqual(self.req("GET", path)[0], 200, path)
        status, body = self.req("GET", "/api/query?a=%7B%22limit%22%3A999%7D")
        self.assertEqual((status, json.loads(body)["error"]["code"]), (400, "INVALID_ARGUMENT"))

    def test_rebinding_host_and_writes_are_refused(self) -> None:
        self.assertEqual(self.req("GET", "/api/query", host="evil.example")[0], 403)
        for method in ("POST", "PUT", "DELETE"):
            self.assertEqual(self.req(method, "/api/query")[0], 405, method)
        self.assertEqual(self.req("GET", "/api/record")[0], 404)


class BundleTests(unittest.TestCase):
    def test_committed_bundle_matches_source(self) -> None:
        proc = subprocess.run([sys.executable, str(ROOT / "ui-src/build.py"), "--check"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_bundle_is_self_contained_and_text_safe(self) -> None:
        page = (ROOT / "ui/ledger-v2.html").read_text(encoding="utf-8")
        app = (ROOT / "ui-src/app.js").read_text(encoding="utf-8")
        self.assertNotIn("<script src", page)
        self.assertNotIn("https://", page)
        self.assertNotIn("innerHTML", app)
        self.assertNotIn("SAMPLE", app.upper().replace("SAMPLED", ""))
        for tool in ("ledger_", '"query"', '"activity"', '"stats"'):
            self.assertIn(tool, app)
        self.assertNotIn("ledger_record", app)
        self.assertIn('"Geist",ui-sans-serif,system-ui', page)


if __name__ == "__main__":
    unittest.main()
