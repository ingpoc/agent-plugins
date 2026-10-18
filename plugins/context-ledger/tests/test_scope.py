"""Scope filter, save rejection, expiry, the 3-hit cap, and the migration dry-run.

Data dirs are tempfile paths. HOME is not read or changed.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import sys  # noqa: E402

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from context_ledger.contracts import LedgerError  # noqa: E402
from context_ledger.store import Store, init_ledger  # noqa: E402
import migrate_scope  # noqa: E402
from synthetic_scope_db import build  # noqa: E402
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("isolation_eval", ROOT / "scripts" / "isolation_eval.py")
iso = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(iso)  # type: ignore[union-attr]
FIXTURE = ROOT / "tests" / "fixtures" / "isolation_30.json"


def _u() -> str:
    return str(uuid.uuid4())


def _body(summary: str, entities: list[str], **extra) -> dict:
    body = {
        "kind": "decision", "summary": summary, "situation": summary, "action": summary,
        "rationale": extra.pop("rationale", "because the rule holds"),
        "constraints": [], "applicability": extra.pop("applicability", "a lasting rule"),
        "entities": entities, "evidence": [], "sensitivity": "model_safe",
        "approval": {"state": "none", "authority_ref": None},
    }
    body.update(extra)
    return body


class ScopeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._home = os.environ.get("HOME")
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name) / "data"
        self.data.mkdir(mode=0o700)
        init_ledger(self.data, None, "scope-test", "tester")
        self.store = Store(self.data, actor_channel="agent")

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()
        self.assertEqual(os.environ.get("HOME"), self._home)

    def record(self, summary: str, entities: list[str], **extra) -> str:
        return self.store.record(
            {
                "request_id": _u(), "occurred_at": None, "body": _body(summary, entities, **extra),
                "outcome": {"status": "pending", "note": "", "evidence": []},
            },
            mcp=True,
        )["decision_id"]

    def save(self, **kw) -> str:
        args = {
            "request_id": _u(), "type": "agent_conclusion", "criticality": 3, "actor": "test-bot",
            "entities": ["kite"], "scope": "fleet", "decision": "Kite reads stay on the Mini",
            "why": "because the Mini holds the session", **kw,
        }
        return self.store.save(args, mcp=True)["decision_id"]

    def find(self, query: str, binding: dict) -> list[str]:
        return [
            hit["decision_id"]
            for hit in self.store.find(
                {"query": query, "entities": [], "caller_scope": binding}, mcp=True
            )["matches"]
        ]


class ScopeFilterTests(ScopeCase):
    def test_nesting_keeps_fleet_project_and_touched_tools(self) -> None:
        fleet = self.record("Comet lease rule for any agent", ["comet"], scope="fleet",
                            rationale="because one agent holds the comet lease")
        project = self.record("Alpha deploy uses a blue green switch", ["alpha"], scope="project:alpha",
                              rationale="because alpha switches blue to green")
        other = self.record("Beta deploy uses a blue green switch", ["beta"], scope="project:beta",
                            rationale="because beta switches blue to green")
        tool = self.record("Render health check stays on the service", ["render"], scope="tool:render",
                           rationale="because the render health check gates the deploy")
        alpha = {"project": "alpha", "tools": ["render"]}
        self.assertIn(fleet, self.find("comet lease rule for any agent", alpha))
        self.assertIn(project, self.find("alpha deploy uses a blue green switch", alpha))
        self.assertNotIn(other, self.find("beta deploy uses a blue green switch", alpha))
        self.assertIn(tool, self.find("render health check stays on the service", alpha))
        unbound = {"project": None, "tools": []}
        self.assertEqual(self.find("comet lease rule for any agent", unbound), [fleet])
        self.assertEqual(self.find("alpha deploy uses a blue green switch", unbound), [])
        beta = {"project": "beta", "tools": ["render"]}
        self.assertIn(tool, self.find("render health check stays on the service", beta))
        self.assertNotIn(project, self.find("alpha deploy uses a blue green switch", beta))

    def test_entity_without_text_does_not_hit(self) -> None:
        self.record("Garden watering schedule for courtyard beds", ["alpha"], scope="project:alpha")
        hits = self.find("comet browser lease held", {"project": "alpha", "tools": ["comet"]})
        self.assertEqual(hits, [])

    def test_expired_decision_is_hidden(self) -> None:
        self.save(decision="Kite reads stay on the Mini", why="because the Mini holds the session",
                  expires_at="2000-01-01T00:00:00.000Z")
        live = self.save(decision="Kite reads stay on the Mini", why="because the Mini holds the session",
                         expires_at="2099-01-01T00:00:00.000Z")
        hits = self.find("kite reads stay on the mini", {"project": None, "tools": []})
        self.assertEqual(hits, [live])

    def test_find_returns_at_most_three(self) -> None:
        for i in range(5):
            self.record(
                f"Kite reads stay on the Mini path {i}",
                ["kite"],
                scope="fleet",
                rationale="because the Mini holds the kite session",
            )
        hits = self.find("kite reads stay on the mini", {"project": None, "tools": []})
        self.assertEqual(len(hits), 3)

    def test_find_without_caller_scope_is_fleet_only(self) -> None:
        fleet = self.record(
            "Shared comet lease rule for agents", ["comet"], scope="fleet",
            rationale="because the comet lease is shared",
        )
        project = self.record(
            "Shared comet lease rule for agents in alpha", ["alpha"], scope="project:alpha",
            rationale="because alpha holds a comet lease",
        )
        hits = [
            hit["decision_id"]
            for hit in self.store.find(
                {"query": "shared comet lease rule for agents", "entities": []}, mcp=True
            )["matches"]
        ]
        self.assertIn(fleet, hits)
        self.assertNotIn(project, hits)


class SaveContractTests(ScopeCase):
    def test_scope_is_required(self) -> None:
        with self.assertRaises(LedgerError) as ctx:
            self.store.save(
                {
                    "request_id": _u(), "type": "agent_conclusion", "criticality": 3,
                    "decision": "Kite reads stay on the Mini", "why": "because the Mini holds the session",
                    "entities": ["kite"], "actor": "test-bot",
                },
                mcp=True,
            )
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")

    def test_record_refuses_a_missing_scope(self) -> None:
        body = _body("Keep the gate closed", ["kite"])
        with self.assertRaises(LedgerError) as ctx:
            self.store.record(
                {
                    "request_id": _u(), "occurred_at": None, "body": body,
                    "outcome": {"status": "pending", "note": "", "evidence": []},
                },
                mcp=True,
            )
        self.assertIn("scope", ctx.exception.message or "")
        saved = self.store.record(
            {
                "request_id": _u(), "occurred_at": None, "body": body,
                "outcome": {"status": "pending", "note": "", "evidence": []},
            },
            mcp=False,
        )
        self.assertTrue(saved["decision_id"])

    def test_status_and_volatile_tokens_are_rejected(self) -> None:
        cases = [
            {"kind": "state", "decision": "Keep the gate closed", "why": "because the gate is the rule"},
            {"decision": "PR 15 merged", "why": "status update"},
            {"decision": "Pinned at 0.1.26", "why": "because the pin moved"},
            {"decision": "sha abcdef1 is the build", "why": "because that commit shipped"},
            {"decision": "The pull request is merged", "why": "status line"},
            {"decision": "Ship the rule on 2026-10-17", "why": "because the date is the pin"},
        ]
        for extra in cases:
            with self.subTest(extra=extra):
                with self.assertRaises(LedgerError) as ctx:
                    self.save(**extra)
                self.assertIn("tracker", ctx.exception.message)

    def test_model_name_and_a_rule_that_mentions_status_are_saved(self) -> None:
        model = self.save(decision="Use gpt-6.1 for reviews", why="because that model answered")
        rule = self.save(decision="CI must stay passing before merge", why="because the gate is the rule")
        self.assertTrue(model and rule)

    def test_time_bound_needs_a_condition(self) -> None:
        with self.assertRaises(LedgerError) as ctx:
            self.save(decision="Use the fallback reviewer until the login works",
                      why="because the primary login is down")
        self.assertIn("expires_at", ctx.exception.message)
        saved = self.save(
            decision="Use the fallback reviewer until the login works",
            why="because the primary login is down",
            replace_when="the primary login works",
        )
        self.assertTrue(saved)

    def test_evidence_may_hold_the_pin(self) -> None:
        saved = self.save(
            decision="Reject the pinned model and use a key login",
            why="because the account cannot use that model",
            evidence=["gpt-6.1-sol ChatGPT"],
        )
        body = self.store.get({"decision_id": saved}, mcp=True)["record"]["body"]
        self.assertEqual(body["scope"], "fleet")
        self.assertIn("gpt-6.1-sol", body["evidence"][0]["ref"])


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._home = os.environ.get("HOME")
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name) / "data"

    def tearDown(self) -> None:
        self._tmp.cleanup()
        self.assertEqual(os.environ.get("HOME"), self._home)

    def test_dry_run_then_apply_on_a_temp_copy(self) -> None:
        build(self.data)
        store = Store(self.data, actor_channel="owner_cli")
        try:
            rows = migrate_scope.plan(store)
        finally:
            store.close()
        actions = {}
        for row in rows:
            actions[row["action"]] = actions.get(row["action"], 0) + 1
            self.assertTrue({"id", "old_scope", "new_scope", "kind", "action", "reason"} <= set(row))
        self.assertGreaterEqual(actions.get("archive", 0), 2)
        self.assertGreaterEqual(actions.get("rewrite", 0), 1)
        self.assertGreaterEqual(actions.get("rescope", 0), 1)
        by_id = {row["id"][:8]: row for row in rows}
        self.assertIn(by_id["2599cee1"]["new_scope"], {"fleet", "tool:comet"})
        self.assertNotEqual(by_id["2599cee1"]["new_scope"], "project:irctc")
        self.assertEqual(by_id["4e5d0bad"]["action"], "archive")
        self.assertNotEqual(by_id["a4abc29c"]["action"], "archive")
        self.assertEqual(by_id["6c7fd233"]["new_scope"], "project:context-ledger")
        store = Store(self.data, actor_channel="owner_cli")
        try:
            migrate_scope.apply(store, rows, on="2026-10-17", data=self.data)
            again = migrate_scope.plan(store)
            fleet = store.find(
                {"query": "cursor cli review keychain prompt", "caller_scope": {"project": "aadhaarchain", "tools": ["cursor-cli"]}},
                mcp=False,
            )
        finally:
            store.close()
        archive = json.loads((self.data / "archive" / "2026-10-17.json").read_text(encoding="utf-8"))
        self.assertTrue(archive and all(item["body"]["summary"] for item in archive))
        self.assertTrue(all(row["reason"] in {"already at new scope", "already archived"} for row in again))
        ids = [hit["decision_id"][:8] for hit in fleet["matches"]]
        self.assertIn("d7f6cc8d", ids)
        self.assertNotIn("6c7fd233", ids)

    def test_refuses_the_live_ledger(self) -> None:
        live = Path.home() / ".context-ledger"
        with self.assertRaises(SystemExit):
            migrate_scope._refuse_live(live)

    def test_refuses_any_root_bound_to_the_live_ledger_dir(self) -> None:
        live = Path(self._tmp.name) / "live"
        other = Path(self._tmp.name) / "other"
        copy = Path(self._tmp.name) / "copy"
        for root, ledger in ((live, live / "ledger"), (other, live / "ledger"), (copy, copy / "ledger")):
            root.mkdir(mode=0o700)
            (root / "binding.json").write_text(json.dumps({"ledger_dir": str(ledger)}), encoding="utf-8")
        with self.assertRaises(SystemExit):
            migrate_scope._refuse_live(live, [live])
        with self.assertRaises(SystemExit):
            migrate_scope._refuse_live(other, [live])
        migrate_scope._refuse_live(copy, [live])
        bare = Path(self._tmp.name) / "bare"
        (bare / "ledger").mkdir(parents=True, mode=0o700)
        sneaky = Path(self._tmp.name) / "sneaky"
        sneaky.mkdir(mode=0o700)
        (sneaky / "binding.json").write_text(json.dumps({"ledger_dir": str(bare / "ledger")}), encoding="utf-8")
        with self.assertRaises(SystemExit):
            migrate_scope._refuse_live(sneaky, [bare])

    def test_fleet_entity_maps_to_fleet(self) -> None:
        scope, _ = migrate_scope.infer_scope(_body("Save a decision not a status", ["fleet", "context-ledger"]))
        self.assertEqual(scope, "fleet")
        scope, _ = migrate_scope.infer_scope(_body("Send handoffs with priority", ["handoff", "fleet"]))
        self.assertEqual(scope, "fleet")
        scope, _ = migrate_scope.infer_scope(_body("Send handoffs with priority", ["fleet"], scope="project:fleet"))
        self.assertEqual(scope, "fleet")

    def test_named_ids_and_scope_rules(self) -> None:
        def row(prefix: str, summary: str, rationale: str, entities: list[str]) -> dict:
            return {
                "id": f"{prefix}-0000-4000-8000-000000000001",
                "revision": 1,
                "lifecycle": "active",
                "body": _body(summary, entities, rationale=rationale),
            }

        kept = [
            ("4f7f4626", "Keep a pin out of a lasting rule",
             "The rule stays because a pin is not the rule. The pin was 1.2.3."),
            ("e47b091b", "Keep a commit pin out of a lasting rule",
             "The rule stays because a commit pin is not the rule. The pin was abcdef1."),
            ("7ab0366c", "Keep a pull request id out of a lasting rule",
             "The rule stays because a pull request id is not the rule. See pr 19."),
            ("fd157e72", "Keep a date out of a lasting rule",
             "The rule stays because a date is not the rule. The date was 2026-10-17."),
        ]
        for prefix, summary, rationale in kept:
            item = migrate_scope.classify(row(prefix, summary, rationale, ["skills"]))
            self.assertEqual(item["action"], "rewrite", prefix)
            self.assertNotEqual(item["action"], "archive", prefix)
            self.assertEqual(item["new_scope"], "fleet", prefix)
        model = migrate_scope.classify(row(
            "3bfca7a8",
            "Use a named model only when the rule needs that name",
            "gpt-6.1 is a label and not a pin",
            ["skills"],
        ))
        self.assertNotEqual(model["action"], "archive")
        self.assertEqual(model["new_scope"], "fleet")
        passing = migrate_scope.classify(row(
            "4bafacf3",
            "CI must stay passing before merge",
            "because a passing check is the rule",
            ["agent-plugins"],
        ))
        self.assertNotEqual(passing["action"], "archive")
        self.assertEqual(passing["new_scope"], "fleet")
        # The other half of that rule. Vocabulary of the project's own delivery artifacts — its
        # PR, its findings, the version it ships — does scope to the project. The two records the
        # 2026-10-17 isolation audit flagged as mis-scoped read like this, so an unrelated caller
        # was receiving them. ci/merge alone must stay fleet (the row above locks that).
        own_pr = migrate_scope.classify(row(
            "ee000000",
            "The project's own pull request needs approval before it lands",
            "because that PR is not ready",
            ["open-resource-network"],
        ))
        self.assertEqual(own_pr["new_scope"], "project:open-resource-network")
        delivery = migrate_scope.classify(row(
            "ff000000",
            "Ship the plugin only after the Codex findings are fixed",
            "because the findings block accepting that version",
            ["agent-plugins"],
        ))
        self.assertEqual(delivery["new_scope"], "project:agent-plugins")
        review = migrate_scope.classify(row(
            "a4abc29c",
            "Cursor CLI review answers with the cursor agent reviewer",
            "That review half is separate from the keychain prompt on Cursor CLI",
            ["cursor-cli"],
        ))
        self.assertNotEqual(review["action"], "archive")
        self.assertEqual(review["new_scope"], "tool:cursor-cli")
        status = migrate_scope.classify(row(
            "4e5d0bad",
            "Codex re-review failed.",
            "sha abcdef1 and pr 15 merged.",
            ["open-resource-network"],
        ))
        self.assertEqual(status["action"], "archive")
        comet = migrate_scope.classify(row(
            "2599cee1",
            "Comet browser lease held by another agent waits for release",
            "The comet browser lease stays with the agent that holds it",
            ["comet", "irctc"],
        ))
        self.assertIn(comet["new_scope"], {"fleet", "tool:comet"})
        self.assertNotEqual(comet["new_scope"], "project:irctc")
        for prefix in ("41badbdc", "77b5eb43", "cb291f09", "d81dd34c", "f06e057d", "d897c824"):
            item = migrate_scope.classify(row(
                prefix,
                "Fleet lesson. Holds in another project.",
                "The lesson is not about this project.",
                ["open-resource-network"],
            ))
            self.assertEqual(item["new_scope"], "fleet", prefix)
            self.assertNotEqual(item["action"], "archive", prefix)
        for topic in ("skills", "keychain", "chrome", "shell", "travel", "irctc"):
            item = migrate_scope.classify(row(
                "aa000000",
                "Keep the lasting rule for this topic",
                "because the topic is not a project",
                [topic],
            ))
            self.assertEqual(item["new_scope"], "fleet", topic)
        own = migrate_scope.classify(row(
            "bb000000",
            "check.py merge gate stays in this repo",
            "because the merge gate is the product",
            ["open-resource-network", "git"],
        ))
        self.assertEqual(own["new_scope"], "project:open-resource-network")
        tool = migrate_scope.classify(row(
            "cc000000",
            "The comet lease waits for release",
            "because one agent holds the lease",
            ["aadhaarchain", "comet"],
        ))
        self.assertEqual(tool["new_scope"], "tool:comet")
        wider = migrate_scope.classify(row(
            "dd000000",
            "Use both tools for the same job",
            "because neither tool owns the rule",
            ["git", "python"],
        ))
        self.assertEqual(wider["new_scope"], "fleet")

    def test_isolation_after(self) -> None:
        build(self.data)
        store = Store(self.data, actor_channel="owner_cli")
        try:
            migrate_scope.apply(store, migrate_scope.plan(store), on="2026-10-17", data=self.data)
        finally:
            store.close()
        db = self.data / "ledger" / "ledger.sqlite3"
        before = db.read_bytes()
        rep = iso.run(db, ROOT, FIXTURE, "caller_scope")
        self.assertEqual(db.read_bytes(), before)
        metrics = rep["metrics"]["all"]
        self.assertEqual(metrics["cross_project_leaks"], 0)
        self.assertEqual(metrics["offtopic_fillers"], 0)
        self.assertEqual(metrics["fleet_lessons_found"], metrics["fleet_lessons_expected"])
        self.assertLessEqual(metrics["mean_bytes"], 697.9)
        self.assertEqual(metrics["errors"], 0)
        self.assertTrue(all(row["scope_passed"] for row in rep["finds"]))
        out = Path(self._tmp.name) / "after.json"
        out.write_text(json.dumps(rep, indent=1) + "\n", encoding="utf-8")
        self.after = rep


if __name__ == "__main__":
    unittest.main()
