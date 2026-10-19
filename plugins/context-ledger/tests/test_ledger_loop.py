"""Tests for scripts/ledger_loop.py: the fail-loud closeout check reads the ledger itself."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "ledger_loop.py"
sys.path.insert(0, str(ROOT))

from context_ledger.contracts import LedgerError, canonical_json, fail_dict, ok  # noqa: E402
from context_ledger.store import Store, init_ledger  # noqa: E402
os.environ["LEDGER_AB_DATE"] = "2026-10-17"  # pre-A/B-window day: find on; A/B tests pin their own days


def _u() -> str:
    return str(uuid.uuid4())


def _body(summary: str) -> dict:
    return {
        "kind": "decision",
        "summary": summary,
        "situation": "closeout check test",
        "action": "test action",
        "rationale": "test rationale",
        "constraints": [],
        "applicability": "tests only",
        "entities": ["ledger-loop-test"],
        "scope": "fleet",
        "evidence": [],
        "sensitivity": "model_safe",
        "approval": {"state": "none", "authority_ref": None},
    }


EVIDENCE = [{"ref": "test://run/1", "revision": None, "sha256": None, "state": "reported_verified"}]


class LedgerLoopBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name) / "data"
        self.data.mkdir(mode=0o700)
        init_ledger(self.data, None, "loop-test", "tester")
        self.store = Store(self.data)
        self.task = _u()
        self.env = {k: v for k, v in os.environ.items()
                    if k not in {"CONTEXT_LEDGER_MCP_TOKEN", "CONTEXT_LEDGER_MCP_URL", "LEDGER_LOOP_LOG",
                                 "CONTEXT_LEDGER_DATA", "CONTEXT_LEDGER_PLUGIN_ROOT"}}

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def run_loop(self, *args: str, data: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
        e = dict(self.env)
        e.update(env or {})
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--data", str(data or self.data), *args],
            env=e, capture_output=True, text=True, timeout=60,
        )

    def decision(self, summary: str = "loop test decision") -> str:
        receipt = self.store.record(
            {"request_id": _u(), "occurred_at": None, "body": _body(summary),
             "outcome": {"status": "pending", "note": "", "evidence": []}},
            mcp=False,
        )
        return receipt["decision_id"]

    def revision(self, decision_id: str) -> int:
        return self.store.get({"decision_id": decision_id}, mcp=False)["record"]["revision"]

    def applied(self, decision_id: str, ids: list[str], task: str | None = None) -> str:
        receipt = self.store.append_event(
            {"request_id": _u(), "decision_id": decision_id, "expected_revision": self.revision(decision_id),
             "occurred_at": None, "event_type": "applied",
             "payload": {"task_id": task or self.task, "decision_ids": ids}},
            mcp=False,
        )
        return receipt["event_id"]

    def outcome(self, decision_id: str, status: str = "success", evidence=None, task: str | None = None,
                applied_event_id: str | None = None, helpful: bool | None = True,
                effect: str | None = None, reason: str | None = None) -> None:
        payload = {"status": status, "note": f"test {status}", "evidence": EVIDENCE if evidence is None else evidence,
                   "task_id": task or self.task}
        if applied_event_id:
            payload["applied_event_id"] = applied_event_id
        if helpful is not None:
            payload["helpful"] = helpful
        if effect:
            payload["effect"] = effect
        if reason:
            payload["reason"] = reason
        self.store.append_event(
            {"request_id": _u(), "decision_id": decision_id, "expected_revision": self.revision(decision_id),
             "occurred_at": None, "event_type": "outcome", "payload": payload},
            mcp=False,
        )

    def start(self, found: list[str] | None = None) -> None:
        r = self.run_loop("start", self.task, "--bot", "test", "--query", "closeout check test",
                          "--found", ",".join(found or []))
        self.assertEqual(r.returncode, 0, r.stderr)

    def applied_events(self, decision_id: str | None = None) -> list[dict]:
        conn = self.store.open()
        try:
            rows = conn.execute(
                "select event_json from events where json_extract(event_json, '$.payload.task_id') = ? "
                "and json_extract(event_json, '$.event_type') = 'applied' order by seq",
                (self.task,),
            ).fetchall()
        finally:
            self.store.close()
        events = [json.loads(row["event_json"]) for row in rows]
        if decision_id is not None:
            events = [e for e in events if decision_id in e["payload"]["decision_ids"]]
        return events

    def applied_count(self, decision_id: str | None = None) -> int:
        return len(self.applied_events(decision_id))

    def applied_event_id(self, decision_id: str | None = None) -> str:
        events = self.applied_events(decision_id)
        self.assertEqual(len(events), 1, events)
        return events[0]["event_id"]

    def rows(self) -> list[dict]:
        log = self.data / "ledger-loop" / "tasks.jsonl"
        return [json.loads(l) for l in log.read_text().splitlines()]

    def assert_fail(self, r: subprocess.CompletedProcess, needle: str, code: int = 1) -> None:
        self.assertEqual(r.returncode, code, r.stdout + r.stderr)
        lines = r.stderr.strip().splitlines()
        self.assertEqual(len(lines), 1, r.stderr)
        self.assertTrue(lines[0].startswith("FAIL "), lines[0])
        self.assertIn(needle, lines[0])
        self.assertIn("fix:", lines[0])

    def assert_pass(self, r: subprocess.CompletedProcess, n_applied: int) -> str:
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith(f"PASS {self.task}: lookup done, {n_applied} applied"), r.stdout)
        return r.stdout


class GateTests(LedgerLoopBase):
    def test_gated_out_one_word_passes_without_lookup(self) -> None:
        r = self.run_loop("close", self.task, "--gated-out", "simple")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith(f"PASS {self.task}: gated out (simple)"), r.stdout)

    def test_gated_out_needs_one_word(self) -> None:
        r = self.run_loop("close", self.task, "--gated-out", "two words")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("one lowercase word", r.stderr)

    def test_placeholder_task_id_rejected(self) -> None:
        for bad in ("TASK_ID", "<task>", "todo", "00000000-0000-0000-0000-000000000000", "11111111-1111-1111-1111-111111111111"):
            r = self.run_loop("close", bad, "--gated-out", "simple")
            self.assertNotEqual(r.returncode, 0, bad)
            self.assertIn("task_id", r.stderr)


class HelpfulGateTests(LedgerLoopBase):
    def test_outcome_without_helpful_fails(self) -> None:
        d = self.decision()
        self.start([d])
        self.applied(d, [d])
        self.outcome(d, helpful=None)
        self.assert_fail(self.run_loop("close", self.task), "without helpful true|false")

    def test_not_helped_success_needs_a_linked_fix(self) -> None:
        d = self.decision("old reviewer path")
        self.start([d])
        ev = self.applied(d, [d])
        self.outcome(d, applied_event_id=ev, helpful=False, effect="not_helped", reason="reviewer subscription lapsed")
        r = self.run_loop("close", self.task)
        self.assert_pass(r, 1)  # warns, does not fail (Guru 2026-10-17)
        self.assertIn("WARN 1 not_helped without a fix", r.stdout)
        self.store.save({"request_id": _u(), "type": "agent_conclusion", "criticality": 4, "actor": "tester",
                         "decision": "Review through Cursor agent instead", "why": "because the old path lapsed",
                         "entities": ["review"], "scope": "fleet", "supersedes": d, "context": "code review",
                         "task_id": self.task},
                        mcp=False)
        self.assert_pass(self.run_loop("close", self.task), 1)

    def test_not_helped_failed_task_needs_no_fix(self) -> None:
        d = self.decision()
        self.start([d])
        ev = self.applied(d, [d])
        self.outcome(d, status="failed", applied_event_id=ev, helpful=False, effect="misled", reason="pointed at a dead path")
        self.assert_pass(self.run_loop("close", self.task), 1)
        self.assertNotIn("WARN", self.run_loop("close", self.task).stdout)


class AppliedLinkTests(LedgerLoopBase):
    """Task attribution: close records which decisions the task applied, then verifies them."""

    def test_close_records_the_applied_link_then_requires_its_outcome(self) -> None:
        d = self.decision()
        self.start([d])
        r = self.run_loop("close", self.task, "--applied", d)
        self.assert_fail(r, f"no outcome for applied {d}")
        self.assertEqual(self.applied_count(), 1, "close must record the applied link it was asked for")
        linked = self.applied_event_id(d)
        self.assertIn(linked, r.stderr, "the fix has to name the event id the outcome cites")
        self.outcome(d, helpful=True, applied_event_id=linked)
        out = self.assert_pass(self.run_loop("close", self.task, "--applied", d), 1)
        self.assertIn("1 applied", out)
        self.assertEqual(self.applied_count(), 1, "a second close must not duplicate the applied link")

    def test_outcome_without_the_link_still_fails(self) -> None:
        """Final, evidenced and marked is not enough: ranking reads the outcome through the link."""
        d = self.decision()
        self.start([d])
        ev = self.applied(d, [d])
        self.outcome(d, helpful=True)
        self.assert_fail(self.run_loop("close", self.task), f"outcome for {d} is not linked")
        self.assertIn(ev, self.run_loop("close", self.task).stderr)
        self.outcome(d, helpful=True, applied_event_id=ev)
        self.assert_pass(self.run_loop("close", self.task), 1)

    def test_close_warns_when_the_lookup_returned_hits_and_nothing_was_declared(self) -> None:
        d = self.decision()
        self.start([d])
        out = self.assert_pass(self.run_loop("close", self.task), 0)
        self.assertIn("WARN 1 lookup hit(s) and no applied id declared: unknown, not none", out)
        self.assertIn(f"close {self.task} --applied <id>", out)
        self.assertEqual(self.applied_count(), 0, "a warning must not invent an attribution")
        self.assertEqual(self.rows()[-1]["applied_declared"], "unknown")

    def test_applied_none_declares_that_nothing_was_used(self) -> None:
        d = self.decision()
        self.start([d])
        out = self.assert_pass(self.run_loop("close", self.task, "--applied-none", "no hit fitted the task"), 0)
        self.assertNotIn("WARN", out, "a declaration settles the question instead of warning")
        self.assertEqual(self.rows()[-1]["applied_declared"], "none")

    def test_audit_needs_an_attribution_declaration(self) -> None:
        d = self.decision()
        self.start([d])
        r = self.run_loop("close", self.task, "--audit")
        self.assert_fail(r, "audit mode needs a declaration", code=2)
        self.assert_pass(self.run_loop("close", self.task, "--audit", "--applied-none", "no hit fitted"), 0)

    def test_applied_with_an_unknown_id_fails_loud(self) -> None:
        self.start()
        self.assert_fail(self.run_loop("close", self.task, "--applied", _u()), "cannot link applied")
        self.assertEqual(self.applied_count(), 0)

    def test_a_later_rejection_keeps_the_links_already_written(self) -> None:
        good = self.decision("good")
        self.start([good])
        bad = _u()
        r = self.run_loop("close", self.task, "--applied", good, "--applied", bad)
        self.assert_fail(r, f"cannot link applied {bad}")
        self.assertIn(f"already linked {good}", r.stderr)
        self.assertEqual(self.applied_count(good), 1, "the committed link must survive the failure")
        self.assertEqual(self.applied_count(bad), 0)


class ABLogTests(LedgerLoopBase):
    def test_off_day_find_skips_lookup_and_close_logs_arm_off(self) -> None:
        off = {"LEDGER_AB_DATE": "2026-10-18"}
        r = self.run_loop("find", self.task, "--bot", "efficient", "--query", "kite holdings", env=off)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["arm"], "off")
        r = self.run_loop("close", self.task, "--guru-correction", "Guru re-stated the Kite rule", env=off)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("lookup done", r.stdout)
        row = self.rows()[-1]
        self.assertEqual((row["arm"], row["date"], row["actor"], row["gated"], row["find_ran"], row["hits"]),
                         ("off", "2026-10-18", "efficient", True, False, 0))
        self.assertEqual(row["guru_correction"], "Guru re-stated the Kite rule")

    def test_off_day_close_without_start_passes(self) -> None:
        r = self.run_loop("close", self.task, "--bot", "jev", env={"LEDGER_AB_DATE": "2026-10-20"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("lookup off", r.stdout)
        self.assertEqual((self.rows()[-1]["arm"], self.rows()[-1]["actor"]), ("off", "jev"))

    def test_on_day_row_counts_hits_helpful_tokens_and_writes(self) -> None:
        on = {"LEDGER_AB_DATE": "2026-10-19"}
        d = self.decision("kite connector holdings rule")
        r = self.run_loop("find", self.task, "--bot", "efficient", "--query", "kite connector holdings rule", env=on)
        self.assertEqual(r.returncode, 0, r.stderr)
        ev = self.applied(d, [d])
        self.outcome(d, applied_event_id=ev, helpful=True)
        r = self.run_loop("save", self.task, "--bot", "efficient", "--type", "agent_conclusion", "--crit", "3",
                          "--decision", "Kite holdings come from Comet", "--why", "the connector cannot be called",
                          "--scope", "fleet",
                          "--entity", "kite", env=on)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.run_loop("close", self.task, "--repeat-of", f"{d}: re-checked the same Kite rule", env=on)
        self.assertEqual(r.returncode, 0, r.stderr)
        row = self.rows()[-1]
        self.assertEqual((row["arm"], row["find_ran"], row["hits"], row["helpful"], row["misled"], row["writes"],
                          row["fail_open"]), ("on", True, 1, 1, 0, 1, False))
        self.assertGreater(row["tokens_est"], 0)
        self.assertEqual((row["trusted_applied"], row["trusted_helpful"]), (0, 0))
        self.assertTrue(row["repeat_of"].startswith(d))

    def test_gated_out_is_logged_and_long_flags_fail(self) -> None:
        r = self.run_loop("close", self.task, "--gated-out", "simple", "--bot", "jev")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.rows()[-1]["gate"], self.rows()[-1]["gated"], self.rows()[-1]["arm"]), ("simple", False, "none"))
        self.assert_fail(self.run_loop("close", self.task, "--gated-out", "simple", "--repeat-of", " ".join(["w"] * 21)),
                         "--repeat-of needs one line")


class StoreModeTests(LedgerLoopBase):
    def test_find_uses_the_binding_project(self) -> None:
        path = self.data / "binding.json"
        binding = json.loads(path.read_text(encoding="utf-8"))
        binding["project"] = "brandgpt"
        binding["tools"] = ["git"]
        path.write_text(json.dumps(binding) + "\n", encoding="utf-8")
        path.chmod(0o600)
        summary = "Review gate stays closed for the caller"
        rationale = "because the review gate stays closed"
        brand = self.store.record(
            {"request_id": _u(), "occurred_at": None,
             "body": _body(summary) | {"scope": "project:brandgpt", "rationale": rationale},
             "outcome": {"status": "pending", "note": "", "evidence": []}},
            mcp=True,
        )["decision_id"]
        other = self.store.record(
            {"request_id": _u(), "occurred_at": None,
             "body": _body(summary) | {"scope": "project:aadhaarchain", "rationale": rationale},
             "outcome": {"status": "pending", "note": "", "evidence": []}},
            mcp=True,
        )["decision_id"]
        r = self.run_loop("find", self.task, "--bot", "efficient", "--query", "review gate stays closed")
        self.assertEqual(r.returncode, 0, r.stderr)
        ids = [hit["decision_id"] for hit in json.loads(r.stdout)["matches"]]
        self.assertIn(brand, ids)
        self.assertNotIn(other, ids)

    def test_close_without_start_fails(self) -> None:
        self.assert_fail(self.run_loop("close", self.task), "no start lookup")

    def test_start_then_close_with_nothing_applied_passes(self) -> None:
        self.start()
        out = self.assert_pass(self.run_loop("close", self.task), 0)
        self.assertIn("[store ", out)
        log = self.data / "ledger-loop" / "starts.jsonl"
        self.assertEqual(oct(log.stat().st_mode & 0o777), "0o600")

    def test_applied_without_outcome_fails_then_outcome_passes(self) -> None:
        d = self.decision()
        self.start([d])
        ev = self.applied(d, [d])
        self.assert_fail(self.run_loop("close", self.task), f"no outcome for applied {d}")
        self.outcome(d, applied_event_id=ev)
        out = self.assert_pass(self.run_loop("close", self.task), 1)
        self.assertIn("1 linked by applied_event_id", out)

    def test_every_id_in_applied_needs_its_own_outcome(self) -> None:
        a, b = self.decision("a"), self.decision("b")
        self.start()
        ev = self.applied(a, [a, b])
        self.outcome(a, applied_event_id=ev)
        r = self.run_loop("close", self.task)
        self.assert_fail(r, f"no outcome for applied {b}")
        self.assertNotIn(a, r.stderr.split("fix:")[0])
        # a link has to belong to its own decision, so b needs its own applied event to cite
        ev_b = self.applied(b, [b])
        self.outcome(b, status="failed", applied_event_id=ev_b)
        self.assert_pass(self.run_loop("close", self.task), 2)

    def test_pending_outcome_is_not_final(self) -> None:
        d = self.decision()
        self.start()
        ev = self.applied(d, [d])
        self.outcome(d, status="pending", applied_event_id=ev)
        self.assert_fail(self.run_loop("close", self.task), f"outcome not final for {d}=pending")
        self.outcome(d, status="inconclusive", applied_event_id=ev)
        self.assert_pass(self.run_loop("close", self.task), 1)

    def test_outcome_needs_usable_evidence(self) -> None:
        d = self.decision()
        self.start()
        self.applied(d, [d])
        self.outcome(d, evidence=[])
        self.assert_fail(self.run_loop("close", self.task), f"outcome without evidence for {d}")
        self.outcome(d, status="failed",
                     evidence=[{"ref": "test://gone", "revision": None, "sha256": None, "state": "missing"}])
        self.assert_fail(self.run_loop("close", self.task), f"outcome without evidence for {d}")

    def test_other_tasks_do_not_count(self) -> None:
        d = self.decision()
        other = _u()
        self.start()
        self.applied(d, [d])
        self.outcome(d, task=other)
        self.assert_fail(self.run_loop("close", self.task), f"no outcome for applied {d}")
        r = self.run_loop("close", other)
        self.assert_fail(r, "no start lookup")

    def test_bad_task_id_fails(self) -> None:
        self.assert_fail(self.run_loop("close", "not-a-uuid"), "bad task_id")
        r = self.run_loop("start", "not-a-uuid", "--bot", "t", "--query", "q")
        self.assert_fail(r, "bad task_id")

    def test_empty_query_fails(self) -> None:
        self.assert_fail(self.run_loop("start", self.task, "--bot", "t", "--query", "  "), "empty query")

    def test_corrupt_start_log_fails_loud(self) -> None:
        self.start()
        with (self.data / "ledger-loop" / "starts.jsonl").open("a") as fh:
            fh.write("{oops\n")
        self.assert_fail(self.run_loop("close", self.task), "is not JSON", code=2)

    def test_missing_ledger_file_fails_loud(self) -> None:
        self.start()
        db = Path(json.loads((self.data / "binding.json").read_text())["ledger_dir"]) / "ledger.sqlite3"
        db.rename(db.with_suffix(".moved"))
        self.assert_fail(self.run_loop("close", self.task, "--audit"), "ledger unreadable", code=2)
        r = self.run_loop("close", self.task)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("WARN fail-open", r.stdout)

    def test_unbound_without_token_fails_loud(self) -> None:
        empty = Path(self._tmp.name) / "unbound"
        empty.mkdir(mode=0o700)
        r = self.run_loop("start", self.task, "--bot", "t", "--query", "q", data=empty)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assert_fail(self.run_loop("close", self.task, "--audit", data=empty), "CONTEXT_LEDGER_MCP_TOKEN empty", code=2)


TOKEN = "t" * 40


class _FakeMcp(BaseHTTPRequestHandler):
    """Streamable-HTTP stand-in that answers `get` from the real Store, like serve-http."""

    store: Store
    sse = False
    delay = 0.0
    appends = 0
    fail_append_after: int | None = None  # HTTP 500 once this many appends have been served
    drop_append_after: int | None = None  # close the connection instead: the loop reads it as unreachable

    def log_message(self, *args) -> None:  # noqa: ANN002
        return

    def do_POST(self) -> None:  # noqa: N802
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self.send_response(401)
            self.end_headers()
            return
        msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if "id" not in msg:
            self.send_response(202)
            self.end_headers()
            return
        if self.delay:
            time.sleep(self.delay)
        if msg["method"] == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "fake"}}
        else:
            name = msg["params"]["name"]
            args = msg["params"]["arguments"]
            if name == "append":
                type(self).appends += 1
                if self.fail_append_after is not None and type(self).appends > self.fail_append_after:
                    self.send_response(500)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.drop_append_after is not None and type(self).appends > self.drop_append_after:
                    self.close_connection = True  # a link cut in half: the earlier append already committed
                    return
            try:
                if name == "find":
                    payload = ok(self.store.find(args, mcp=True))
                elif name == "save":
                    payload = ok(self.store.save(args, mcp=True))
                elif name == "append":
                    payload = ok(self.store.append_event(args, mcp=True))
                else:
                    payload = ok(self.store.get(args, mcp=True))
            except LedgerError as exc:
                payload = fail_dict(exc)
            result = {"content": [{"type": "text", "text": canonical_json(payload)}],
                      "structuredContent": payload, "isError": not payload["ok"]}
        body = json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result})
        self.send_response(200)
        self.send_header("Mcp-Session-Id", "s1")
        if self.sse:
            data = f"event: message\ndata: {body}\n\n".encode()
            self.send_header("Content-Type", "text/event-stream")
        else:
            data = body.encode()
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class RemoteModeTests(LedgerLoopBase):
    def setUp(self) -> None:
        super().setUp()
        handler = type("H", (_FakeMcp,), {"store": Store(self.data), "appends": 0,
                                        "fail_append_after": None, "drop_append_after": None})
        self.handler = handler
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/mcp"
        self.box = Path(self._tmp.name) / "box"
        self.box.mkdir(mode=0o700)

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.handler.store.close()
        super().tearDown()

    def box_run(self, *args: str, token: str = TOKEN, env: dict | None = None) -> subprocess.CompletedProcess:
        return self.run_loop(*args, data=self.box,
                             env={"CONTEXT_LEDGER_MCP_TOKEN": token, "CONTEXT_LEDGER_MCP_URL": self.url,
                                  **(env or {})})

    def box_start(self, found: list[str]) -> None:
        r = self.box_run("start", self.task, "--bot", "box", "--query", "q", "--found", ",".join(found))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_remote_follows_applied_ids_and_fails_then_passes(self) -> None:
        a, b = self.decision("a"), self.decision("b")
        self.box_start([a])
        ev = self.applied(a, [a, b])
        self.outcome(a, applied_event_id=ev)
        self.assert_fail(self.box_run("close", self.task), f"no outcome for applied {b}")
        ev_b = self.applied(b, [b])
        self.outcome(b, applied_event_id=ev_b)
        out = self.assert_pass(self.box_run("close", self.task), 2)
        self.assertIn(f"[remote {self.url}, 2 decision(s) read", out)

    def test_remote_parses_sse(self) -> None:
        self.handler.sse = True
        d = self.decision()
        self.box_start([d])
        ev = self.applied(d, [d])
        self.assert_fail(self.box_run("close", self.task), f"no outcome for applied {d}")
        self.outcome(d, applied_event_id=ev)
        self.assert_pass(self.box_run("close", self.task), 1)

    def test_remote_extra_decision_flag(self) -> None:
        d = self.decision()
        self.box_start([])
        self.applied(d, [d])
        self.assert_pass(self.box_run("close", self.task), 0)
        self.assert_fail(self.box_run("close", self.task, "--decision", d), f"no outcome for applied {d}")

    def test_the_loop_records_its_own_remote_reads_under_the_internal_actor(self) -> None:
        """The loop's revision read and check re-read are plumbing, not the task's lookup."""
        d = self.decision()
        self.box_start([d])
        looked_up = self.box_run("find", self.task, "--bot", "box", "--query", "the tool stalled")
        self.assertEqual(looked_up.returncode, 0, looked_up.stdout + looked_up.stderr)
        ev = self.applied(d, [d])
        self.outcome(d, helpful=True, applied_event_id=ev)
        self.assert_pass(self.box_run("close", self.task), 1)
        by_caller = self.handler.store.access_report()["by_caller"]
        self.assertIn("ledger-internal", by_caller)
        self.assertGreaterEqual(by_caller["ledger-internal"]["get"], 1)
        self.assertNotIn("unnamed", by_caller)
        self.assertIn("box", by_caller, "the task's own find stays under its bot")

    def test_remote_records_the_applied_link(self) -> None:
        d = self.decision()
        self.box_start([d])
        self.assert_fail(self.box_run("close", self.task, "--applied", d), f"no outcome for applied {d}")
        self.assertEqual(self.applied_count(d), 1)
        self.outcome(d, helpful=True, applied_event_id=self.applied_event_id(d))
        out = self.assert_pass(self.box_run("close", self.task, "--applied", d), 1)
        self.assertIn("1 applied", out)
        self.assertEqual(self.applied_count(d), 1, "a second close must not duplicate the applied link")

    def test_remote_applied_flag_is_a_discovery_seed(self) -> None:
        """An id that arrives only through --applied has no start row: remote discovery must read it."""
        d = self.decision()
        self.box_start([])
        ev = self.applied(d, [d])
        self.outcome(d, helpful=True, applied_event_id=ev)
        self.assert_pass(self.box_run("close", self.task, "--applied", d), 1)
        self.assert_pass(self.box_run("close", self.task, "--applied", d), 1)
        self.assertEqual(self.applied_count(d), 1, "the link must not be written twice")

    def test_remote_bad_token_fails_loud(self) -> None:
        d = self.decision()
        self.box_start([d])
        self.assert_fail(self.box_run("close", self.task, token="wrong"), "UNAUTHORIZED", code=2)

    def test_remote_unknown_decision_fails_loud(self) -> None:
        self.box_start([_u()])
        self.assert_fail(self.box_run("close", self.task), "UNAVAILABLE", code=2)

    def test_remote_unreachable_names_url(self) -> None:
        self.box_start([])
        self.httpd.shutdown()
        self.httpd.server_close()
        self.assert_fail(self.box_run("close", self.task, "--audit"), "ledger unreadable", code=2)
        r = self.box_run("close", self.task)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("WARN fail-open", r.stdout)

    def test_remote_not_helped_fix_is_read_over_mcp(self) -> None:
        d = self.decision("old reviewer path")
        self.box_start([d])
        ev = self.applied(d, [d])
        self.outcome(d, applied_event_id=ev, helpful=False, effect="not_helped", reason="reviewer subscription lapsed")
        self.assertIn("WARN 1 not_helped without a fix", self.assert_pass(self.box_run("close", self.task), 1))
        r = self.box_run("save", self.task, "--bot", "box", "--type", "agent_conclusion", "--crit", "4",
                         "--decision", "Review through Cursor agent instead", "--why", "old path lapsed",
                         "--entity", "review", "--scope", "fleet", "--supersedes", d, "--context", "code review")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("WARN", self.assert_pass(self.box_run("close", self.task), 1))

    def test_token_without_url_is_not_sent(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("taild2e98e", text)
        self.assertNotIn("DEFAULT_URL", text)
        r = self.run_loop(
            "find", self.task, "--bot", "box", "--query", "codex model rejected",
            data=self.box, env={"CONTEXT_LEDGER_MCP_TOKEN": "secret-token"},
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("CONTEXT_LEDGER_MCP_URL", r.stderr)
        self.assertNotIn("secret-token", r.stdout + r.stderr)
        self.assertNotIn("ts.net", r.stdout + r.stderr)

    def test_find_server_down_fails_open_in_one_line(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        r = self.box_run("find", self.task, "--bot", "box", "--query", "codex model rejected")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["matches"], [])
        self.assertEqual(len(r.stderr.strip().splitlines()), 1)
        self.assertTrue(r.stderr.startswith("WARN ledger find skipped"), r.stderr)

    def test_find_slow_server_fails_open_within_deadline(self) -> None:
        self.handler.delay = 3.0
        t0 = time.monotonic()
        r = self.box_run("find", self.task, "--bot", "box", "--query", "codex model rejected")
        took = time.monotonic() - t0
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stderr.startswith("WARN ledger find skipped"), r.stderr)
        self.assertLess(took, 2.9)

    def test_find_audit_mode_fails_loud(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        r = self.box_run("find", self.task, "--bot", "box", "--query", "codex model rejected", "--audit")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertTrue(r.stderr.startswith("FAIL "), r.stderr)

    def test_find_then_save_remote(self) -> None:
        r = self.box_run("save", self.task, "--bot", "box", "--type", "agent_conclusion", "--crit", "4",
                         "--decision", "Codex login rejects the pinned model and uses a key login",
                         "--why", "because the account cannot use that model",
                         "--entity", "codex-cli", "--scope", "fleet")
        self.assertEqual(r.returncode, 0, r.stderr)
        saved = json.loads(r.stdout)
        self.assertTrue(saved["saved"], saved)
        r = self.box_run("find", self.task, "--bot", "box", "--query", "codex login rejects the pinned model")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["matches"][0]["decision_id"], saved["decision_id"])

    def test_save_rejects_long_decision_loudly(self) -> None:
        r = self.box_run("save", self.task, "--bot", "box", "--type", "agent_conclusion", "--crit", "3",
                         "--decision", " ".join(["word"] * 16), "--why", "short why", "--entity", "x",
                         "--scope", "fleet")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("max 15", r.stderr)


class StorePartialLinkTests(LedgerLoopBase):
    """The store backend links one decision at a time, so a mid-way failure must not lose the first."""

    def test_a_busy_ledger_still_reports_the_link_it_wrote(self) -> None:
        sys.path.insert(0, str(ROOT / "scripts"))
        import ledger_loop

        a, b = self.decision("a"), self.decision("b")
        real = Store.append_event
        served = {"n": 0}

        def flaky(store, args, **kwargs):
            served["n"] += 1
            if served["n"] == 2:
                raise LedgerError("BUSY", retryable=True)
            return real(store, args, **kwargs)

        with mock.patch.object(Store, "append_event", flaky):
            with self.assertRaises(ledger_loop.Unavailable) as caught:
                ledger_loop._append_applied(self.data, self.task, [a, b])
        self.assertEqual(caught.exception.code, "BUSY")
        self.assertEqual([d for d, _ in caught.exception.committed], [a])
        self.assertEqual(self.applied_count(a), 1)
        self.assertEqual(self.applied_count(b), 0)


class PartialLinkTests(RemoteModeTests):
    """A link that fails mid-way is never reported as a clean closeout."""

    def test_a_failed_second_link_names_both_ids(self) -> None:
        a, b = self.decision("a"), self.decision("b")
        self.box_start([])
        self.handler.fail_append_after = 1
        r = self.box_run("close", self.task, "--applied", a, "--applied", b)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(len(r.stderr.strip().splitlines()), 1, r.stderr)
        self.assertIn(f"cannot link applied {b}", r.stderr)
        self.assertIn(f"already linked {a}", r.stderr)
        self.assertNotIn("all with final outcome", r.stdout + r.stderr)
        self.assertEqual(self.applied_count(a), 1, "the committed link must survive the failure")
        self.assertEqual(self.applied_count(b), 0)

    def test_an_unreachable_ledger_never_verifies_a_half_written_link(self) -> None:
        a, b = self.decision("a"), self.decision("b")
        self.box_start([])
        self.handler.drop_append_after = 1
        r = self.box_run("close", self.task, "--applied", a, "--applied", b)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("applied link incomplete", r.stderr)
        self.assertIn(f"no outcome for applied {a}", r.stderr, "the committed link is still verified")
        self.assertNotIn("all with final outcome", r.stdout + r.stderr)
        self.assertEqual(self.applied_count(a), 1)
        self.assertEqual(self.applied_count(b), 0)


if __name__ == "__main__":
    unittest.main()
