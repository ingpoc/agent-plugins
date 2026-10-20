"""Request-scoped lookup and feedback regressions; all ledger data is synthetic."""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from context_ledger.store import Store, init_ledger  # noqa: E402
from context_ledger.contracts import LedgerError, _validate_caller_scope  # noqa: E402

LOOKUP = ROOT / "skills/context-ledger/scripts/lookup.py"


def _u() -> str:
    return str(uuid.uuid4())


def _body(summary: str, scope: str, *, situation: str | None = None, action: str | None = None) -> dict:
    return {
        "kind": "decision", "summary": summary, "situation": situation or summary,
        "action": action or summary, "rationale": "synthetic request-scope fixture",
        "constraints": [], "applicability": situation or summary, "entities": ["scope-test"],
        "scope": scope, "evidence": [], "sensitivity": "model_safe",
        "approval": {"state": "none", "authority_ref": None},
    }


class RequestScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"
        self.data.mkdir(mode=0o700)
        init_ledger(self.data, None, "alpha", "scope-tester")
        self.store = Store(self.data, actor_channel="agent")
        self.env = {
            **os.environ, "CONTEXT_LEDGER_DATA": str(self.data),
            "PYTHONNOUSERSITE": "1", "LEDGER_AB_DATE": "2026-10-19",
        }

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def record(self, summary: str, scope: str, **kw) -> str:
        return self.store.record({
            "request_id": _u(), "occurred_at": None, "body": _body(summary, scope, **kw),
            "outcome": {"status": "pending", "note": "", "evidence": []},
        }, mcp=True)["decision_id"]

    def helper(
        self, command: str, args: list[str], body: dict | None = None, *, env: dict | None = None,
    ) -> tuple[subprocess.CompletedProcess, dict]:
        proc = subprocess.run(
            [sys.executable, str(LOOKUP), command, *args],
            input=json.dumps(body) if body is not None else None,
            text=True, capture_output=True, env={**self.env, **(env or {})},
        )
        return proc, json.loads(proc.stdout)

    def event_count(self) -> int:
        self.store.open()
        return self.store.conn.execute("select count(*) from events").fetchone()[0]

    def test_lookup_cli_defaults_to_binding_scope_and_accepts_fleet_override(self) -> None:
        fleet = self.record("Shared comet lease lookup scope rule", "fleet")
        project = self.record("Shared comet lease lookup scope rule alpha", "project:alpha")
        other = self.record("Shared comet lease lookup scope rule beta", "project:beta")
        default, bound = self.helper("find", ["--query", "shared comet lease lookup scope rule"])
        fleet_only, explicit = self.helper(
            "find", ["--query", "shared comet lease lookup scope rule", "--caller-scope", "{}"])
        self.assertEqual(default.returncode, 0, default.stderr)
        self.assertEqual(fleet_only.returncode, 0, fleet_only.stderr)
        bound_ids = {m["decision_id"] for m in bound["matches"]}
        explicit_ids = {m["decision_id"] for m in explicit["matches"]}
        self.assertTrue({fleet, project} <= bound_ids)
        self.assertNotIn(other, bound_ids)
        self.assertIn(fleet, explicit_ids)
        self.assertNotIn(project, explicit_ids)
        self.assertNotIn(other, explicit_ids)

    @unittest.skipUnless(platform.system() == "Darwin", "installed NaturalLanguage embedding is macOS-only")
    def test_select_uses_explicit_fleet_scope_instead_of_binding_project(self) -> None:
        project = self.record(
            "Alpha caller scope route", "project:alpha",
            situation="Need project alpha scope choice", action="Choose alpha scope route",
        )
        self.record(
            "Fleet kitchen cleanup", "fleet",
            situation="Clean the shared kitchen after lunch", action="Wash the coffee cups",
        )
        base = {
            "situation": "Need project alpha scope choice",
            "need": "Choose alpha scope route", "query": "alpha caller scope route",
        }
        bound_proc, bound = self.helper("select", ["--json", "-"], base)
        fleet_proc, fleet_only = self.helper(
            "select", ["--json", "-"], {**base, "caller_scope": {}})
        self.assertEqual(bound_proc.returncode, 0, bound_proc.stderr)
        self.assertEqual(bound["decision"]["id"], project)
        self.assertEqual(fleet_proc.returncode, 0, fleet_proc.stderr)
        self.assertNotEqual((fleet_only.get("decision") or {}).get("id"), project)

    def test_feedback_scope_violation_is_rejected_before_any_outcome_write(self) -> None:
        alpha = self.record("Alpha scoped feedback decision", "project:alpha")
        beta = self.record("Beta scoped feedback decision", "project:beta")
        task = _u()
        for decision_id in (alpha, beta):
            rec = self.store.get({"decision_id": decision_id}, mcp=True)["record"]
            self.store.append_event({
                "request_id": _u(), "decision_id": decision_id,
                "expected_revision": rec["revision"], "occurred_at": None,
                "event_type": "applied",
                "payload": {"task_id": task, "decision_ids": [decision_id]},
            }, mcp=True)
        before = self.event_count()
        proc, result = self.helper("feedback", ["--json", "-"], {
            "task_id": task, "caller_scope": {"project": "alpha", "tools": []},
            "provided": [
                {"id": alpha, "helped": True, "issue": "alpha action applied"},
                {"id": beta, "helped": True, "issue": "out-of-scope action applied"},
            ],
        })
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(result["written"], [])
        self.assertEqual(result["write"], "unavailable")
        after = self.event_count()
        self.assertEqual(after, before)
        for decision_id in (alpha, beta):
            rec = self.store.get({"decision_id": decision_id}, mcp=True)["record"]
            self.assertEqual(rec["revision"], 2)  # only the applied receipt exists
            self.assertEqual(rec["outcome"]["status"], "pending")

    def test_feedback_scope_preflight_rejects_invalid_context_before_any_write(self) -> None:
        cases = (
            ("omitted context with explicit null scope", False, None, None),
            ("context with missing scope", True, {"project": "alpha", "tools": []}, "missing"),
            ("context with mismatched scope", True, {"project": "alpha", "tools": []}, "project:beta"),
            ("null caller context", True, None, "project:alpha"),
        )
        for label, include_context, caller_scope, requested_scope in cases:
            with self.subTest(case=label):
                decision = self.record(f"Alpha applied feedback decision {label}", "project:alpha")
                task = _u()
                rec = self.store.get({"decision_id": decision}, mcp=True)["record"]
                self.store.append_event({
                    "request_id": _u(), "decision_id": decision,
                    "expected_revision": rec["revision"], "occurred_at": None,
                    "event_type": "applied", "payload": {"task_id": task, "decision_ids": [decision]},
                }, mcp=True)
                missing = {
                    "situation": "A synthetic helper needs a scoped validation instruction",
                    "need": "Validate the current project scope before retrying",
                }
                if requested_scope != "missing":
                    missing["scope"] = requested_scope
                body = {
                    "task_id": task,
                    "provided": [{"id": decision, "helped": True, "issue": "applied first"}],
                    "would_have_helped": missing,
                }
                if include_context:
                    body["caller_scope"] = caller_scope
                before = self.event_count()
                proc, result = self.helper("feedback", ["--json", "-"], body)
                self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(result["written"], [])
                self.assertEqual(result["write"], "unavailable")
                self.assertEqual(self.event_count(), before)
                after = self.store.get({"decision_id": decision}, mcp=True)["record"]
                self.assertEqual(after["revision"], 2)
                self.assertEqual(after["outcome"]["status"], "pending")

    def test_caller_scope_tools_rejects_null_false_and_empty_string(self) -> None:
        for tools in (None, False, ""):
            with self.subTest(tools=tools), self.assertRaises(LedgerError):
                _validate_caller_scope({"project": "alpha", "tools": tools})

    def test_invalid_scope_is_rejected_before_lookup_cli_off_arm(self) -> None:
        self.store.open()
        before = self.store.conn.execute("select count(*) from access_log where tool='find'").fetchone()[0]
        proc, result = self.helper(
            "find", ["--query", "scope validation off arm", "--caller-scope", "null"],
            env={"LEDGER_AB_DATE": "2026-10-20"},
        )
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotEqual(result.get("arm"), "off")
        after = self.store.conn.execute("select count(*) from access_log where tool='find'").fetchone()[0]
        self.assertEqual(after, before)

    @unittest.skipUnless(platform.system() == "Darwin", "installed NaturalLanguage embedding is macOS-only")
    def test_feedback_duplicate_check_uses_requested_scope(self) -> None:
        situation = "A synthetic helper needs a scoped validation instruction"
        need = "Validate the current project scope before retrying"
        beta = self.record(
            need, "project:beta", situation=situation, action=need,
        )
        base = {
            "task_id": _u(), "caller_scope": {"project": "alpha", "tools": []},
            "provided": [], "would_have_helped": {
                "situation": situation, "need": need, "scope": "project:alpha",
            },
        }
        first_proc, first = self.helper("feedback", ["--json", "-"], base)
        self.assertEqual(first_proc.returncode, 0, first_proc.stdout + first_proc.stderr)
        self.assertIn("written", first)
        self.assertEqual(len(first["written"]), 1)
        self.assertIsNone(first["similar"])
        created_id = first["written"][0]
        created = self.store.get({"decision_id": created_id}, mcp=True)["record"]
        self.assertEqual(created["body"]["scope"], "project:alpha")
        self.assertNotEqual(created_id, beta)  # beta's near duplicate must not suppress alpha capture

        before = self.event_count()
        retry = {**base, "task_id": _u()}
        second_proc, second = self.helper("feedback", ["--json", "-"], retry)
        self.assertEqual(second_proc.returncode, 0, second_proc.stdout + second_proc.stderr)
        self.assertEqual(second["similar"], created_id)
        self.assertEqual(second["written"], [])
        self.assertEqual(self.event_count(), before)

        # A project duplicate cannot suppress a gap explicitly saved at fleet scope.
        fleet_situation = "Agents need one reusable fleet connection timeout rule"
        fleet_need = "Retry the connection after the shared timeout"
        project_duplicate = self.record(
            fleet_need, "project:alpha", situation=fleet_situation, action=fleet_need,
        )
        fleet_gap_proc, fleet_gap = self.helper("feedback", ["--json", "-"], {
            "task_id": _u(), "caller_scope": {"project": "alpha", "tools": []},
            "provided": [], "would_have_helped": {
                "situation": fleet_situation, "need": fleet_need, "scope": "fleet",
            },
        })
        self.assertEqual(fleet_gap_proc.returncode, 0, fleet_gap_proc.stdout + fleet_gap_proc.stderr)
        self.assertEqual(len(fleet_gap["written"]), 1)
        fleet_created = self.store.get({"decision_id": fleet_gap["written"][0]}, mcp=True)["record"]
        self.assertEqual(fleet_created["body"]["scope"], "fleet")
        self.assertNotEqual(fleet_created["decision_id"], project_duplicate)

        # A touched tool's duplicate cannot suppress a project-scoped gap.
        project_situation = "Alpha project needs a stable validation retry rule"
        project_need = "Check alpha validation before retrying"
        tool_duplicate = self.record(
            project_need, "tool:jira", situation=project_situation, action=project_need,
        )
        project_gap_proc, project_gap = self.helper("feedback", ["--json", "-"], {
            "task_id": _u(), "caller_scope": {"project": "alpha", "tools": ["jira"]},
            "provided": [], "would_have_helped": {
                "situation": project_situation, "need": project_need, "scope": "project:alpha",
            },
        })
        self.assertEqual(project_gap_proc.returncode, 0, project_gap_proc.stdout + project_gap_proc.stderr)
        self.assertEqual(len(project_gap["written"]), 1)
        project_created = self.store.get({"decision_id": project_gap["written"][0]}, mcp=True)["record"]
        self.assertEqual(project_created["body"]["scope"], "project:alpha")
        self.assertNotEqual(project_created["decision_id"], tool_duplicate)


if __name__ == "__main__":
    unittest.main()
