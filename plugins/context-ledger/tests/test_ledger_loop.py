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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "ledger_loop.py"
sys.path.insert(0, str(ROOT))

from context_ledger.contracts import LedgerError, canonical_json, fail_dict, ok  # noqa: E402
from context_ledger.store import Store, init_ledger  # noqa: E402


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
        self.assert_fail(self.run_loop("close", self.task), "no fix is saved")
        self.store.save({"request_id": _u(), "type": "agent_conclusion", "criticality": 4, "actor": "tester",
                         "decision": "Review through Cursor agent instead", "why": "old path lapsed",
                         "entities": ["review"], "supersedes": d, "context": "code review", "task_id": self.task},
                        mcp=False)
        self.assert_pass(self.run_loop("close", self.task), 1)

    def test_not_helped_failed_task_needs_no_fix(self) -> None:
        d = self.decision()
        self.start([d])
        ev = self.applied(d, [d])
        self.outcome(d, status="failed", applied_event_id=ev, helpful=False, effect="misled", reason="pointed at a dead path")
        self.assert_pass(self.run_loop("close", self.task), 1)


class StoreModeTests(LedgerLoopBase):
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
        self.applied(a, [a, b])
        self.outcome(a)
        r = self.run_loop("close", self.task)
        self.assert_fail(r, f"no outcome for applied {b}")
        self.assertNotIn(a, r.stderr.split("fix:")[0])
        self.outcome(b, status="failed")
        self.assert_pass(self.run_loop("close", self.task), 2)

    def test_pending_outcome_is_not_final(self) -> None:
        d = self.decision()
        self.start()
        self.applied(d, [d])
        self.outcome(d, status="pending")
        self.assert_fail(self.run_loop("close", self.task), f"outcome not final for {d}=pending")
        self.outcome(d, status="inconclusive")
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
            try:
                if name == "find":
                    payload = ok(self.store.find(args, mcp=True))
                elif name == "save":
                    payload = ok(self.store.save(args, mcp=True))
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
        handler = type("H", (_FakeMcp,), {"store": Store(self.data)})
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

    def box_run(self, *args: str, token: str = TOKEN) -> subprocess.CompletedProcess:
        return self.run_loop(*args, data=self.box,
                             env={"CONTEXT_LEDGER_MCP_TOKEN": token, "CONTEXT_LEDGER_MCP_URL": self.url})

    def box_start(self, found: list[str]) -> None:
        r = self.box_run("start", self.task, "--bot", "box", "--query", "q", "--found", ",".join(found))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_remote_follows_applied_ids_and_fails_then_passes(self) -> None:
        a, b = self.decision("a"), self.decision("b")
        self.box_start([a])
        ev = self.applied(a, [a, b])
        self.outcome(a, applied_event_id=ev)
        self.assert_fail(self.box_run("close", self.task), f"no outcome for applied {b}")
        self.outcome(b)
        out = self.assert_pass(self.box_run("close", self.task), 2)
        self.assertIn(f"[remote {self.url}, 2 decision(s) read", out)

    def test_remote_parses_sse(self) -> None:
        self.handler.sse = True
        d = self.decision()
        self.box_start([d])
        self.applied(d, [d])
        self.assert_fail(self.box_run("close", self.task), f"no outcome for applied {d}")
        self.outcome(d)
        self.assert_pass(self.box_run("close", self.task), 1)

    def test_remote_extra_decision_flag(self) -> None:
        d = self.decision()
        self.box_start([])
        self.applied(d, [d])
        self.assert_pass(self.box_run("close", self.task), 0)
        self.assert_fail(self.box_run("close", self.task, "--decision", d), f"no outcome for applied {d}")

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
        self.assert_fail(self.box_run("close", self.task), "no fix is saved")
        r = self.box_run("save", self.task, "--bot", "box", "--type", "agent_conclusion", "--crit", "4",
                         "--decision", "Review through Cursor agent instead", "--why", "old path lapsed",
                         "--entity", "review", "--supersedes", d, "--context", "code review")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assert_pass(self.box_run("close", self.task), 1)

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
                         "--decision", "Codex ChatGPT login rejects gpt-6.1-sol; use an API-key login",
                         "--why", "HTTP 400 model not supported with a ChatGPT account",
                         "--entity", "codex-cli")
        self.assertEqual(r.returncode, 0, r.stderr)
        saved = json.loads(r.stdout)
        self.assertTrue(saved["saved"], saved)
        r = self.box_run("find", self.task, "--bot", "box", "--query", "codex rejects model with chatgpt account")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["matches"][0]["decision_id"], saved["decision_id"])

    def test_save_rejects_long_decision_loudly(self) -> None:
        r = self.box_run("save", self.task, "--bot", "box", "--type", "agent_conclusion", "--crit", "3",
                         "--decision", " ".join(["word"] * 16), "--why", "short why", "--entity", "x")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("max 15", r.stderr)


if __name__ == "__main__":
    unittest.main()
