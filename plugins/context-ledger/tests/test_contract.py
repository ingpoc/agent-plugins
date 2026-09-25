"""Contract tests for context-ledger. Table-driven stdlib unittest."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOOT = ROOT / "bin" / "context_ledger.py"
CREATOR = Path(__file__).resolve().parents[3] / "scripts" / "create_agent_plugin.py"
FIXTURE = ROOT / "tests" / "scenario_fixture.json"


def _collection_root() -> Path | None:
    env = os.environ.get("AGENT_PLUGINS_COLLECTION")
    candidates = [Path(env)] if env else []
    candidates.append(ROOT.parent.parent)
    for candidate in candidates:
        if not (candidate / "AGENTS.md").is_file():
            continue
        if not (candidate / "plugins/context-ledger/plugin.json").is_file():
            continue
        if not (candidate / ".cursor-plugin/marketplace.json").is_file():
            continue
        return candidate
    return None


def _uuid() -> str:
    return str(uuid.uuid4())


def _run(args: list[str], *, env=None, pause=None) -> subprocess.CompletedProcess:
    e = os.environ.copy()
    if env:
        e.update(env)
    e["PYTHONPATH"] = str(ROOT)
    e["PYTHONNOUSERSITE"] = "1"
    if pause is not None:
        e["CONTEXT_LEDGER_TEST_PAUSE"] = str(pause)
    return subprocess.run(
        args,
        cwd=str(ROOT),
        env=e,
        capture_output=True,
        text=True,
    )


def _boot(data: Path, command: str, extra: list[str] | None = None) -> subprocess.CompletedProcess:
    return _run([sys.executable, str(BOOT), "--data", str(data), command, *(extra or [])])


def _body(**overrides):
    base = {
        "kind": "decision",
        "summary": "Choose sqlite ledger",
        "situation": "Need scoped traces",
        "action": "Use one sqlite file",
        "rationale": "Avoid a second store",
        "constraints": ["local-only-runtime"],
        "applicability": "V1 local agents",
        "entities": ["ledger-v1"],
        "evidence": [],
        "sensitivity": "model_safe",
        "approval": {"state": "none", "authority_ref": None},
    }
    base.update(overrides)
    return base


def _outcome(status="pending", **overrides):
    base = {"status": status, "note": "", "evidence": []}
    base.update(overrides)
    return base


def _evidence(state="reported_verified", ref="doc://example"):
    return [{"ref": ref, "revision": None, "sha256": None, "state": state}]


class PackageTests(unittest.TestCase):
    def test_creator_validate(self) -> None:
        if not CREATOR.is_file():
            self.skipTest("collection scripts/create_agent_plugin.py missing")
        plugin = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
        command = json.loads((ROOT / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]["context-ledger"]["command"]
        if ROOT.name != plugin["name"] or Path(command).is_absolute():
            self.skipTest("client-transformed copy is not a portable package")
        proc = _run([sys.executable, str(CREATOR), "--validate", str(ROOT)])
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_mcp_command_is_portable(self) -> None:
        mcp = json.loads((ROOT / "mcp.json").read_text(encoding="utf-8"))
        server = mcp["mcpServers"]["context-ledger"]
        command = server["command"]
        launcher = ROOT / "bin" / "context-ledger-mcp"
        if _collection_root() is not None:
            self.assertEqual(command, "./bin/context-ledger-mcp")
            self.assertTrue(launcher.is_file())
        elif ".cursor" in ROOT.parts and Path(command).is_absolute():
            self.assertTrue(Path(command).is_file())
        else:
            self.assertIn(command, ("python", "python3", "./bin/context-ledger-mcp"))
            if command == "./bin/context-ledger-mcp":
                self.assertTrue(launcher.is_file())
        cwd = server.get("cwd")
        if ".cursor" in ROOT.parts and Path(str(cwd)).is_absolute():
            self.assertEqual(cwd, str(ROOT))
        else:
            self.assertFalse(command.startswith("/") and command.count("/") == 1)
            self.assertNotIn(" ", command)
            self.assertNotIn("${", command)
            self.assertIn(cwd, ("${PLUGIN_ROOT}", "./"))

    def test_skill_frontmatter(self) -> None:
        text = (ROOT / "skills/context-ledger/SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        self.assertIn("name: context-ledger", text)
        self.assertIn("find", text)
        self.assertIn("append_event", text)

    def test_catalogs_and_plugins_row(self) -> None:
        collection = _collection_root()
        if collection is None:
            self.skipTest("collection catalogs are not part of an installed package")
        agents = (collection / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("| `context-ledger` | `plugins/context-ledger/` |", agents)
        for rel in (
            ".cursor-plugin/marketplace.json",
            ".agents/plugins/marketplace.json",
            ".grok-plugin/marketplace.json",
        ):
            data = json.loads((collection / rel).read_text(encoding="utf-8"))
            names = [p["name"] for p in data["plugins"]]
            self.assertIn("context-ledger", names, rel)

    def test_plugin_version(self) -> None:
        plugin = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
        init = (ROOT / "context_ledger/__init__.py").read_text(encoding="utf-8")
        self.assertEqual(plugin["version"], "0.1.3")
        self.assertIn('__version__ = "0.1.3"', init)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        for axis in ("Reliability", "Robustness", "Context efficiency", "Speed", "Efficiency"):
            self.assertIn(axis, readme)
        self.assertGreaterEqual(readme.count("unmeasured"), 5)
        self.assertIn("macOS arm64", readme)
        self.assertIn("release-blocked", readme)
        self.assertNotIn("supported: Ubuntu", readme)
        self.assertIn("Release-blocked until run", agents)


class LaunchTests(unittest.TestCase):
    def test_literal_plugin_data_token_expands(self) -> None:
        """Cursor leaves ${PLUGIN_DATA} unexpanded; default is ~/.context-ledger."""
        data = Path.home() / ".context-ledger"
        if not (data / "binding.json").is_file():
            self.skipTest("default ~/.context-ledger not initialized on this host")
        env = {**os.environ}
        env.pop("PLUGIN_DATA", None)
        proc = subprocess.run(
            [sys.executable, str(BOOT), "--data", "${PLUGIN_DATA}", "doctor"],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('"ok":true', proc.stderr)

    def test_default_data_dir_without_plugin_data(self) -> None:
        env = {**os.environ}
        env.pop("PLUGIN_DATA", None)
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env["HOME"] = str(home)
            # doctor without runtime → UNAVAILABLE, but must create ~/.context-ledger
            proc = subprocess.run(
                [sys.executable, str(BOOT), "doctor"],
                cwd=str(ROOT),
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertTrue((home / ".context-ledger").is_dir())

    def test_missing_runtime_fails_without_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            os.chmod(data, 0o700)
            proc = _boot(data, "serve")
            self.assertEqual(proc.returncode, 2)
            self.assertIn("UNAVAILABLE", proc.stderr)
            self.assertFalse((data / "binding.json").exists())
            self.assertFalse(any(data.rglob("ledger.sqlite3")))

    def test_setup_online_and_doctor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            os.chmod(data, 0o700)
            proc = _boot(data, "setup")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            doctor = _boot(data, "doctor")
            # doctor without binding is UNAVAILABLE
            self.assertEqual(doctor.returncode, 2)
            self.assertIn("UNAVAILABLE", doctor.stderr)
            init = _boot(data, "init", ["--scope", "example-project", "--actor", "example-agent"])
            self.assertEqual(init.returncode, 0, init.stderr)
            doctor = _boot(data, "doctor")
            self.assertEqual(doctor.returncode, 0, doctor.stderr + doctor.stdout)


class EnsureGlobalTriggersTests(unittest.TestCase):
    def test_absent_skips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            missing = Path(tmp) / "no-agents.md"
            proc = _boot(data, "ensure-global-triggers", ["--agents-md", str(missing)])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stderr.strip())
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["data"]["skipped"], "absent")

    def test_inserts_once_then_noop(self) -> None:
        fixture = (
            "# Global AGENTS.md\n\n"
            "## BEFORE\n\n"
            "- **New repo/session gap** → entrypoint\n"
            "- Other before\n\n"
            "## AFTER\n\n"
            "- Modified AGENTS.md → `workflow lint`\n"
            "- **Durable-learning closeout** — encode friction.\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            agents = Path(tmp) / "AGENTS.md"
            agents.write_text(fixture, encoding="utf-8")
            first = _boot(data, "ensure-global-triggers", ["--agents-md", str(agents)])
            self.assertEqual(first.returncode, 0, first.stderr)
            one = json.loads(first.stderr.strip())
            self.assertTrue(one["data"]["added"])
            self.assertTrue(one["data"]["lookup"])
            self.assertTrue(one["data"]["save"])
            text = agents.read_text(encoding="utf-8")
            self.assertIn("**Ledger lookup**", text)
            self.assertIn("**Ledger save**", text)
            self.assertLess(text.index("**Ledger lookup**"), text.index("## AFTER"))
            self.assertLess(text.index("**Ledger save**"), text.index("**Durable-learning"))
            second = _boot(data, "ensure-global-triggers", ["--agents-md", str(agents)])
            self.assertEqual(second.returncode, 0, second.stderr)
            two = json.loads(second.stderr.strip())
            self.assertFalse(two["data"]["added"])
            self.assertEqual(agents.read_text(encoding="utf-8"), text)

    def test_missing_sections_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            agents = Path(tmp) / "AGENTS.md"
            agents.write_text("# no sections\n", encoding="utf-8")
            proc = _boot(data, "ensure-global-triggers", ["--agents-md", str(agents)])
            self.assertEqual(proc.returncode, 2)
            self.assertIn("UNAVAILABLE", proc.stderr)


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"
        self.data.mkdir()
        os.chmod(self.data, 0o700)
        sys.path.insert(0, str(ROOT))
        from context_ledger.store import init_ledger, Store
        from context_ledger.contracts import LedgerError

        self.init_ledger = init_ledger
        self.Store = Store
        self.LedgerError = LedgerError
        self.binding = init_ledger(self.data, None, "example-project", "example-agent")
        self.store = Store(self.data, actor_channel="owner_cli")

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def test_init_bind_and_posix(self) -> None:
        db = Path(self.binding["ledger_dir"]) / "ledger.sqlite3"
        self.assertTrue(db.is_file())
        self.assertEqual(stat.S_IMODE(db.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(Path(self.binding["ledger_dir"]).stat().st_mode), 0o700)
        with self.assertRaises(self.LedgerError) as ctx:
            self.init_ledger(self.data, None, "example-project", "example-agent")
        self.assertEqual(ctx.exception.code, "INVALID_TRANSITION")

    def test_unsafe_permissions_refused(self) -> None:
        db = Path(self.binding["ledger_dir"]) / "ledger.sqlite3"
        os.chmod(db, 0o644)
        store = self.Store(self.data)
        with self.assertRaises(self.LedgerError) as ctx:
            store.open()
        self.assertEqual(ctx.exception.code, "UNAVAILABLE")
        os.chmod(db, 0o600)

    def test_record_append_replay(self) -> None:
        rec = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        self.assertEqual(rec["revision"], 1)
        got = self.store.get({"decision_id": rec["decision_id"]}, mcp=True)
        self.assertEqual(got["record"]["outcome"]["status"], "pending")
        rec2 = self.store.append_event(
            {
                "request_id": _uuid(),
                "decision_id": rec["decision_id"],
                "expected_revision": 1,
                "occurred_at": None,
                "event_type": "outcome",
                "payload": _outcome("failed", note="experiment failed"),
            },
            mcp=False,
        )
        self.assertEqual(rec2["revision"], 2)
        got = self.store.get({"decision_id": rec["decision_id"]}, mcp=True)
        self.assertEqual(got["record"]["outcome"]["status"], "failed")
        self.assertEqual(got["record"]["revision"], 2)

    def test_lookup_helper_capture_and_evidence_closeout(self) -> None:
        helper = ROOT / "skills/context-ledger/scripts/lookup.py"
        env = {**os.environ, "CONTEXT_LEDGER_DATA": str(self.data)}

        def call(command: str, body: dict) -> dict:
            proc = subprocess.run(
                [sys.executable, str(helper), command, "--json", "-"],
                input=json.dumps(body), text=True, capture_output=True, env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            return json.loads(proc.stdout)

        created = call("record", {"body": _body(summary="Keep one ledger agent"),
                                  "outcome": _outcome("pending")})
        decision_id = created["written"][0]
        candidate = call("record", {"summary": "Let ledger agent place candidate",
            "reason": "The parent cannot inspect duplicates itself.",
            "applicability": "Session closeout with a reusable decision",
            "evidence_ref": "user:session-closeout"})
        saved = self.store.get({"decision_id": candidate["written"][0]}, mcp=True)["record"]
        self.assertEqual(saved["body"]["rationale"], "The parent cannot inspect duplicates itself.")
        self.assertEqual(saved["body"]["applicability"], "Session closeout with a reusable decision")
        got = _run([sys.executable, str(helper), "get", "--id", decision_id], env=env)
        self.assertEqual(json.loads(got.stdout)["applicability"], "V1 local agents")
        self.assertEqual(json.loads(got.stdout)["outcome"], "pending")

        appended = call("append", {"decision_id": decision_id,
            "event_type": "outcome", "payload": _outcome("inconclusive", note="Awaiting proof")})
        self.assertEqual(appended["written"], [decision_id])

        task_id = _uuid()
        closed = call("closeout", {"task_id": task_id, "outcomes": [{"id": decision_id,
            "applied": True, "status": "success", "note": "Same agent handled two actions",
            "evidence_ref": "session://agent-reuse"}]})
        self.assertEqual(closed["written"], [decision_id])
        self.assertEqual(closed["task_id"], task_id)
        self.assertEqual(closed["feedback"][0]["confidence"], {
            "score": 1.0, "successes": 1, "failures": 0,
            "inconclusive": 0, "sample_count": 1})
        rec = self.store.get({"decision_id": decision_id}, mcp=True)["record"]
        self.assertEqual(rec["outcome"]["status"], "success")
        self.assertEqual(rec["outcome"]["evidence"][0]["state"], "unverified")
        self.assertEqual(rec["outcome"]["note"], "Same agent handled two actions")

        duplicate = call("closeout", {"task_id": task_id, "outcomes": [{"id": decision_id,
            "applied": True, "status": "success", "note": "once", "evidence_ref": "session://first"},
            {"id": decision_id, "applied": True, "status": "failed", "note": "twice",
             "evidence_ref": "session://second"}]})
        self.assertEqual(duplicate, {"written": [], "write": "unavailable"})
        self.assertEqual(call("closeout", {"outcomes": [], "candidate": {
            "summary": "unreviewed", "reason": "repeatable", "evidence_ref": "session://test"}}),
            {"written": [], "write": "unavailable"})
        self.assertEqual(self.store.get({"decision_id": decision_id}, mcp=True)["record"]["revision"], 3)

    def test_task_closeout_is_idempotent_and_confidence_uses_latest_task_feedback(self) -> None:
        helper = ROOT / "skills/context-ledger/scripts/lookup.py"
        env = {**os.environ, "CONTEXT_LEDGER_DATA": str(self.data)}

        def call(body: dict) -> dict:
            proc = subprocess.run(
                [sys.executable, str(helper), "closeout", "--json", "-"],
                input=json.dumps(body), text=True, capture_output=True, env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            return json.loads(proc.stdout)

        helper_record = ROOT / "skills/context-ledger/scripts/lookup.py"
        created = subprocess.run(
            [sys.executable, str(helper_record), "record", "--json", "-"],
            input=json.dumps({"body": _body(summary="Apply known decision"),
                              "outcome": _outcome("pending")}),
            text=True, capture_output=True, env=env,
        )
        self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
        decision_id = json.loads(created.stdout)["written"][0]
        task_id = _uuid()
        success = {"task_id": task_id, "outcomes": [{
            "id": decision_id, "applied": True, "status": "success",
            "note": "Applied and helped", "evidence_ref": "session://task-a"}]}

        first = call(success)
        revision = self.store.get({"decision_id": decision_id}, mcp=True)["record"]["revision"]
        retry = call(success)
        self.assertEqual(first["written"], [decision_id])
        self.assertTrue(retry["feedback"][0]["confidence"]["score"] == 1.0)
        self.assertEqual(self.store.get({"decision_id": decision_id}, mcp=True)["record"]["revision"], revision)

        retry_request = _uuid()
        retry_event = {
            "request_id": retry_request,
            "decision_id": decision_id,
            "expected_revision": revision,
            "occurred_at": None,
            "event_type": "outcome",
            "payload": {"task_id": task_id, "status": "success",
                        "note": "Applied and helped", "evidence": [{
                            "ref": "session://task-a", "revision": None,
                            "sha256": None, "state": "unverified"}]},
        }
        self.assertTrue(self.store.append_event(retry_event, mcp=True)["replayed"])
        conflicting_retry = json.loads(json.dumps(retry_event))
        conflicting_retry["payload"]["note"] = "Reused request id"
        with self.assertRaises(self.LedgerError) as ctx:
            self.store.append_event(conflicting_retry, mcp=True)
        self.assertEqual(ctx.exception.code, "IDEMPOTENCY_CONFLICT")

        corrected = {"task_id": task_id, "outcomes": [{
            "id": decision_id, "applied": True, "status": "failed",
            "note": "Correction: applied but did not help", "evidence_ref": "session://task-a-correction"}]}
        correction = call(corrected)["feedback"][0]["confidence"]
        self.assertEqual(correction, {"score": 0.0, "successes": 0, "failures": 1,
                                      "inconclusive": 0, "sample_count": 1})

        inconclusive = call({"task_id": _uuid(), "outcomes": [{
            "id": decision_id, "applied": True, "status": "inconclusive",
            "note": "Outcome cannot be attributed yet", "evidence_ref": "session://task-b"}]})
        self.assertEqual(inconclusive["feedback"][0]["confidence"], {
            "score": 0.0, "successes": 0, "failures": 1,
            "inconclusive": 1, "sample_count": 1})
        next_task = call({"task_id": _uuid(), "outcomes": [{
            "id": decision_id, "applied": True, "status": "success",
            "note": "Applied and helped again", "evidence_ref": "session://task-c"}]})
        expected = {"score": 0.5, "successes": 1, "failures": 1,
                    "inconclusive": 1, "sample_count": 2}
        self.assertEqual(next_task["feedback"][0]["confidence"], expected)
        record = self.store.get({"decision_id": decision_id}, mcp=True)["record"]
        self.assertEqual(record["confidence"], expected)
        found = self.store.find({"query": "Apply known decision", "entities": [],
                                 "constraints": [], "limit": 3, "offset": 0}, mcp=True)
        self.assertEqual(found["matches"][0]["confidence"], expected)

    def test_idempotency_and_revision(self) -> None:
        req = _uuid()
        args = {
            "request_id": req,
            "occurred_at": None,
            "body": _body(),
            "outcome": _outcome("pending"),
        }
        a = self.store.record(args, mcp=False)
        b = self.store.record(args, mcp=False)
        self.assertTrue(b["replayed"])
        self.assertEqual(a["event_id"], b["event_id"])
        other = dict(args)
        other["body"] = _body(summary="different summary text")
        with self.assertRaises(self.LedgerError) as ctx:
            self.store.record(other, mcp=False)
        self.assertEqual(ctx.exception.code, "IDEMPOTENCY_CONFLICT")
        with self.assertRaises(self.LedgerError) as ctx:
            self.store.append_event(
                {
                    "request_id": _uuid(),
                    "decision_id": a["decision_id"],
                    "expected_revision": 9,
                    "occurred_at": None,
                    "event_type": "outcome",
                    "payload": _outcome("success", note="ok"),
                },
                mcp=False,
            )
        self.assertEqual(ctx.exception.code, "REVISION_CONFLICT")

    def test_local_only_hidden(self) -> None:
        rec = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(sensitivity="local_only", summary="secret path"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        visible = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="public sqlite choice", entities=["sqlite"]),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        found = self.store.find(
            {"query": "secret sqlite", "entities": [], "constraints": []},
            mcp=True,
        )
        ids = [m["decision_id"] for m in found["matches"]]
        self.assertNotIn(rec["decision_id"], ids)
        self.assertIn(visible["decision_id"], ids)
        with self.assertRaises(self.LedgerError) as ctx:
            self.store.get({"decision_id": rec["decision_id"]}, mcp=True)
        self.assertEqual(ctx.exception.code, "UNAVAILABLE")

    def test_failure_ranks_equal_to_success(self) -> None:
        fail = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="alpha fail", entities=["same-ent"]),
                "outcome": _outcome("failed", note="failed"),
            },
            mcp=False,
        )
        ok_rec = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="alpha ok", entities=["same-ent"]),
                "outcome": _outcome("success", note="ok"),
            },
            mcp=False,
        )
        found = self.store.find(
            {"query": "alpha", "entities": ["same-ent"], "constraints": []},
            mcp=True,
        )
        ids = [m["decision_id"] for m in found["matches"]]
        self.assertEqual(set(ids), {fail["decision_id"], ok_rec["decision_id"]})
        # same evidence none, both active: order by updated_at then uuid, not success bias
        self.assertEqual(found["matches"][0]["evidence_state"], found["matches"][1]["evidence_state"])

    def test_export_import_and_purge(self) -> None:
        rec = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        export_path = Path(self.tmp.name) / "export.jsonl"
        out = self.store.export(export_path, include_local_only=False)
        self.assertGreaterEqual(out["exported_records"], 1)
        data2 = Path(self.tmp.name) / "data2"
        data2.mkdir()
        os.chmod(data2, 0o700)
        dest = self.init_ledger(data2, None, "dest-scope", "dest-actor")
        other = self.Store(data2, actor_channel="owner_cli")
        imported = other.import_file(
            export_path,
            self.binding["ledger_id"],
            self.binding["scope_id"],
            include_local_only=False,
        )
        self.assertGreaterEqual(imported["imported_events"], 1)
        got = other.get({"decision_id": rec["decision_id"]}, mcp=True)
        self.assertTrue(got["record"]["imported"])
        other.close()
        self.store.purge(rec["decision_id"], rec["revision"], self.binding["ledger_id"])
        with self.assertRaises(self.LedgerError) as ctx:
            self.store.get({"decision_id": rec["decision_id"]}, mcp=True)
        self.assertEqual(ctx.exception.code, "UNAVAILABLE")

    def test_purge_related_keeps_survivor_retry(self) -> None:
        left = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="purge left sqlite"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        right = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="purge right sqlite"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        open_req = _uuid()
        opened = self.store.append_event(
            {
                "request_id": open_req,
                "decision_id": left["decision_id"],
                "expected_revision": left["revision"],
                "occurred_at": None,
                "event_type": "conflict_opened",
                "payload": {"other_id": right["decision_id"], "reason": "two live choices"},
            },
            mcp=False,
        )
        note_req = _uuid()
        noted = self.store.append_event(
            {
                "request_id": note_req,
                "decision_id": left["decision_id"],
                "expected_revision": opened["revision"],
                "occurred_at": None,
                "event_type": "outcome",
                "payload": _outcome("pending", note="still watching"),
            },
            mcp=False,
        )
        self.store.purge(right["decision_id"], right["revision"], self.binding["ledger_id"])
        replay_open = self.store.append_event(
            {
                "request_id": open_req,
                "decision_id": left["decision_id"],
                "expected_revision": left["revision"],
                "occurred_at": None,
                "event_type": "conflict_opened",
                "payload": {"other_id": right["decision_id"], "reason": "two live choices"},
            },
            mcp=False,
        )
        self.assertTrue(replay_open["replayed"])
        self.assertEqual(replay_open["event_id"], opened["event_id"])
        replay_note = self.store.append_event(
            {
                "request_id": note_req,
                "decision_id": left["decision_id"],
                "expected_revision": opened["revision"],
                "occurred_at": None,
                "event_type": "outcome",
                "payload": _outcome("pending", note="still watching"),
            },
            mcp=False,
        )
        self.assertTrue(replay_note["replayed"])
        self.assertEqual(replay_note["event_id"], noted["event_id"])

    def test_purge_foreign_scope_is_unavailable(self) -> None:
        rec = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="home-scope sqlite"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        self.store.scope_add("other-scope")
        other_data = Path(self.tmp.name) / "other-data"
        other_data.mkdir()
        os.chmod(other_data, 0o700)
        from context_ledger.store import bind_ledger

        bind_ledger(
            other_data,
            Path(self.binding["ledger_dir"]),
            self.binding["ledger_id"],
            "other-scope",
            "other-actor",
            replace=False,
        )
        other = self.Store(other_data, actor_channel="owner_cli")
        with self.assertRaises(self.LedgerError) as ctx:
            other.purge(rec["decision_id"], rec["revision"], self.binding["ledger_id"])
        self.assertEqual(ctx.exception.code, "UNAVAILABLE")
        got = self.store.get({"decision_id": rec["decision_id"]}, mcp=True)
        self.assertEqual(got["record"]["decision_id"], rec["decision_id"])
        other.close()

    def test_crash_before_commit(self) -> None:
        script = (
            "import sys, json\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from context_ledger.store import Store\n"
            f"s = Store({str(self.data)!r}, actor_channel='owner_cli')\n"
            "s.record({'request_id': sys.argv[1], 'occurred_at': None, "
            "'body': json.loads(sys.argv[2]), 'outcome': {'status':'pending','note':'','evidence':[]}}, mcp=False)\n"
        )
        req = _uuid()
        proc = subprocess.Popen(
            [sys.executable, "-c", script, req, json.dumps(_body(summary="crash pending"))],
            env={**os.environ, "PYTHONPATH": str(ROOT), "CONTEXT_LEDGER_TEST_PAUSE": "30"},
        )
        try:
            proc.wait(timeout=0.4)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        found = self.store.find({"query": "crash pending", "entities": [], "constraints": []}, mcp=True)
        self.assertEqual(found["matches"], [])

    def test_unknown_fields_rejected(self) -> None:
        with self.assertRaises(self.LedgerError) as ctx:
            self.store.record(
                {
                    "request_id": _uuid(),
                    "occurred_at": None,
                    "body": _body(),
                    "outcome": _outcome("pending"),
                    "extra": True,
                },
                mcp=False,
            )
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")

    def test_bind_replace(self) -> None:
        from context_ledger.store import bind_ledger

        with self.assertRaises(self.LedgerError) as ctx:
            bind_ledger(
                self.data,
                Path(self.binding["ledger_dir"]),
                self.binding["ledger_id"],
                self.binding["scope_id"],
                "other-actor",
                replace=False,
            )
        self.assertEqual(ctx.exception.code, "INVALID_TRANSITION")
        binding = bind_ledger(
            self.data,
            Path(self.binding["ledger_dir"]),
            self.binding["ledger_id"],
            self.binding["scope_id"],
            "other-actor",
            replace=True,
        )
        self.assertEqual(binding["actor_id"], "other-actor")
        self.store.close()
        self.store = self.Store(self.data, actor_channel="owner_cli")

    def test_scenario_fixture_shape_and_seed_replay(self) -> None:
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(data["format"], "context-ledger-scenarios")
        self.assertTrue(data["scenarios"])
        self.assertTrue(data["seed"])
        for row in data["scenarios"]:
            self.assertIn("id", row)
            self.assertIn("path", row)
            self.assertIn("prompt", row)
            self.assertIn("expected_tools", row)
        ids = []
        for row in data["seed"]:
            self.assertEqual(row["tool"], "record")
            args = dict(row["arguments"])
            args["request_id"] = _uuid()
            rec = self.store.record(args, mcp=False)
            ids.append(rec["decision_id"])
        found = self.store.find(
            {"query": "sqlite ledger", "entities": ["ledger-v1"], "constraints": []},
            mcp=True,
        )
        matched = {m["decision_id"] for m in found["matches"]}
        self.assertTrue(set(ids) <= matched)

    def test_duplicate_keys_and_types(self) -> None:
        from context_ledger.contracts import parse_json

        with self.assertRaises(self.LedgerError) as ctx:
            parse_json('{"a":1,"a":2}')
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")
        with self.assertRaises(self.LedgerError) as ctx:
            self.store.find({"query": "sqlite", "entities": [], "constraints": [], "limit": 9}, mcp=True)
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")

    def test_event_ceiling(self) -> None:
        from context_ledger.contracts import MAX_EVENTS

        scope = self.binding["scope_id"]
        now = "2026-01-01T00:00:00.000Z"
        rows = [
            (str(uuid.uuid4()), str(uuid.uuid4()), scope, 1, "{}", "local", now)
            for _ in range(MAX_EVENTS)
        ]
        with self.store._tx() as conn:
            conn.executemany(
                "insert into events(event_id,decision_id,scope_id,revision,event_json,ingest_kind,received_at) "
                "values (?,?,?,?,?,?,?)",
                rows,
            )
        with self.assertRaises(self.LedgerError) as ctx:
            self.store.record(
                {
                    "request_id": _uuid(),
                    "occurred_at": None,
                    "body": _body(),
                    "outcome": _outcome("pending"),
                },
                mcp=False,
            )
        self.assertEqual(ctx.exception.code, "LIMIT_EXCEEDED")

    def test_candidate_cap_incomplete(self) -> None:
        from context_ledger.contracts import CANDIDATE_CAP

        for i in range(CANDIDATE_CAP + 1):
            self.store.record(
                {
                    "request_id": _uuid(),
                    "occurred_at": None,
                    "body": _body(summary=f"capterm item {i:03d} sqlite"),
                    "outcome": _outcome("pending"),
                },
                mcp=False,
            )
        found = self.store.find({"query": "capterm", "entities": [], "constraints": []}, mcp=True)
        self.assertTrue(found["incomplete"])

    def test_projection_write_cap(self) -> None:
        from context_ledger import store as store_mod

        rec = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="projection cap sqlite"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        original = store_mod.MAX_GET_RESPONSE
        store_mod.MAX_GET_RESPONSE = 64
        try:
            with self.assertRaises(self.LedgerError) as ctx:
                self.store.append_event(
                    {
                        "request_id": _uuid(),
                        "decision_id": rec["decision_id"],
                        "expected_revision": rec["revision"],
                        "occurred_at": None,
                        "event_type": "outcome",
                        "payload": _outcome("failed", note="too large after cap"),
                    },
                    mcp=False,
                )
            self.assertEqual(ctx.exception.code, "LIMIT_EXCEEDED")
        finally:
            store_mod.MAX_GET_RESPONSE = original
        got = self.store.get({"decision_id": rec["decision_id"]}, mcp=True)
        self.assertEqual(got["record"]["revision"], rec["revision"])

    def test_malicious_import_atomic(self) -> None:
        rec = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="import source sqlite"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        export_path = Path(self.tmp.name) / "good.jsonl"
        self.store.export(export_path, include_local_only=False)
        bad_path = Path(self.tmp.name) / "bad.jsonl"
        bad_path.write_text(export_path.read_text(encoding="utf-8") + '{"type":"event","extra":true}\n')
        data2 = Path(self.tmp.name) / "dest"
        data2.mkdir()
        os.chmod(data2, 0o700)
        self.init_ledger(data2, None, "dest-scope", "dest-actor")
        other = self.Store(data2, actor_channel="owner_cli")
        with self.assertRaises(self.LedgerError) as ctx:
            other.import_file(
                bad_path,
                self.binding["ledger_id"],
                self.binding["scope_id"],
                include_local_only=False,
            )
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")
        with self.assertRaises(self.LedgerError) as ctx:
            other.get({"decision_id": rec["decision_id"]}, mcp=True)
        self.assertEqual(ctx.exception.code, "UNAVAILABLE")
        other.close()

    def test_scope_add_and_attest(self) -> None:
        added = self.store.scope_add("second-scope")
        self.assertEqual(added["scope_id"], "second-scope")
        rec = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="attest sqlite"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        attested = self.store.attest(
            rec["decision_id"],
            rec["revision"],
            _uuid(),
            "owner-ref-1",
            "owner reviewed",
        )
        self.assertEqual(attested["revision"], rec["revision"] + 1)
        got = self.store.get({"decision_id": rec["decision_id"]}, mcp=True)
        self.assertEqual(got["record"]["body"]["approval"]["state"], "owner_attested")

    def test_rebuild_keeps_foreign_scope_records_isolated(self) -> None:
        from context_ledger.store import bind_ledger

        rec_a = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="alpha-scope-a sqlite"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        self.store.scope_add("second-scope")
        data_b = Path(self.tmp.name) / "data-b"
        data_b.mkdir()
        os.chmod(data_b, 0o700)
        bind_ledger(
            data_b,
            Path(self.binding["ledger_dir"]),
            self.binding["ledger_id"],
            "second-scope",
            "other-agent",
            replace=False,
        )
        store_b = self.Store(data_b, actor_channel="owner_cli")
        rec_b = store_b.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="beta-scope-b sqlite"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        store_b.rebuild()
        found_b = store_b.find(
            {"query": "alpha-scope-a sqlite", "entities": [], "constraints": []},
            mcp=True,
        )
        self.assertNotIn(rec_a["decision_id"], [m["decision_id"] for m in found_b["matches"]])
        with self.assertRaises(self.LedgerError) as ctx:
            store_b.get({"decision_id": rec_a["decision_id"]}, mcp=True)
        self.assertEqual(ctx.exception.code, "UNAVAILABLE")
        got_a = self.store.get({"decision_id": rec_a["decision_id"]}, mcp=True)
        self.assertEqual(got_a["record"]["decision_id"], rec_a["decision_id"])
        got_b = store_b.get({"decision_id": rec_b["decision_id"]}, mcp=True)
        self.assertEqual(got_b["record"]["decision_id"], rec_b["decision_id"])
        store_b.close()

    def test_imported_evidence_not_elevated_by_copied_outcome(self) -> None:
        rec = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="imported evidence sqlite", evidence=_evidence()),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        export_path = Path(self.tmp.name) / "evidence.jsonl"
        self.store.export(export_path, include_local_only=False)
        dest = Path(self.tmp.name) / "evidence-dest"
        dest.mkdir()
        os.chmod(dest, 0o700)
        self.init_ledger(dest, None, "dest-scope", "dest-actor")
        other = self.Store(dest, actor_channel="owner_cli")
        other.import_file(
            export_path,
            self.binding["ledger_id"],
            self.binding["scope_id"],
            include_local_only=False,
        )
        got = other.get({"decision_id": rec["decision_id"]}, mcp=True)
        self.assertEqual(got["record"]["evidence_state"], "unverified")
        self.assertEqual(got["record"]["body"]["evidence"][0]["state"], "unverified")
        copied = other.append_event(
            {
                "request_id": _uuid(),
                "decision_id": rec["decision_id"],
                "expected_revision": got["record"]["revision"],
                "occurred_at": None,
                "event_type": "outcome",
                "payload": _outcome("success", note="status only", evidence=_evidence()),
            },
            mcp=False,
        )
        got = other.get({"decision_id": rec["decision_id"]}, mcp=True)
        self.assertEqual(got["record"]["evidence_state"], "unverified")
        self.assertEqual(got["record"]["outcome"]["evidence"][0]["state"], "unverified")
        other.append_event(
            {
                "request_id": _uuid(),
                "decision_id": rec["decision_id"],
                "expected_revision": copied["revision"],
                "occurred_at": None,
                "event_type": "outcome",
                "payload": _outcome(
                    "success",
                    note="new local check",
                    evidence=_evidence(ref="doc://local-check"),
                ),
            },
            mcp=False,
        )
        got = other.get({"decision_id": rec["decision_id"]}, mcp=True)
        self.assertEqual(got["record"]["evidence_state"], "reported_verified")
        self.assertEqual(got["record"]["body"]["evidence"][0]["state"], "unverified")
        self.assertEqual(got["record"]["outcome"]["evidence"][0]["ref"], "doc://local-check")
        other.close()

    def test_relationship_errors_hide_missing_and_local_only(self) -> None:
        visible = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="visible sqlite"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        hidden = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="hidden sqlite", sensitivity="local_only"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        missing = _uuid()

        def code_for(target: str, *, mcp: bool) -> str:
            with self.assertRaises(self.LedgerError) as ctx:
                self.store.append_event(
                    {
                        "request_id": _uuid(),
                        "decision_id": visible["decision_id"],
                        "expected_revision": visible["revision"],
                        "occurred_at": None,
                        "event_type": "superseded",
                        "payload": {"replacement_id": target, "reason": "replace with other"},
                    },
                    mcp=mcp,
                )
            return ctx.exception.code

        self.assertEqual(code_for(hidden["decision_id"], mcp=True), "UNAVAILABLE")
        self.assertEqual(code_for(missing, mcp=True), "UNAVAILABLE")
        self.assertEqual(code_for(missing, mcp=False), "UNAVAILABLE")

        def open_code(target: str) -> str:
            with self.assertRaises(self.LedgerError) as ctx:
                self.store.append_event(
                    {
                        "request_id": _uuid(),
                        "decision_id": visible["decision_id"],
                        "expected_revision": visible["revision"],
                        "occurred_at": None,
                        "event_type": "conflict_opened",
                        "payload": {"other_id": target, "reason": "possible clash"},
                    },
                    mcp=True,
                )
            return ctx.exception.code

        self.assertEqual(open_code(hidden["decision_id"]), "UNAVAILABLE")
        self.assertEqual(open_code(missing), "UNAVAILABLE")

    def test_conflict_resolved_from_counterpart_clears_pair(self) -> None:
        left = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="conflict left sqlite"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        right = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="conflict right sqlite"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        self.store.append_event(
            {
                "request_id": _uuid(),
                "decision_id": left["decision_id"],
                "expected_revision": left["revision"],
                "occurred_at": None,
                "event_type": "conflict_opened",
                "payload": {"other_id": right["decision_id"], "reason": "two viable choices"},
            },
            mcp=False,
        )
        opened = self.store.get({"decision_id": left["decision_id"]}, mcp=True)["record"]["conflicts"][0]
        self.store.append_event(
            {
                "request_id": _uuid(),
                "decision_id": right["decision_id"],
                "expected_revision": right["revision"],
                "occurred_at": None,
                "event_type": "conflict_resolved",
                "payload": {
                    "other_id": left["decision_id"],
                    "opened_event_id": opened["opened_event_id"],
                    "reason": "kept the later choice",
                },
            },
            mcp=False,
        )
        self.assertEqual(self.store.get({"decision_id": left["decision_id"]}, mcp=True)["record"]["conflicts"], [])
        self.assertEqual(self.store.get({"decision_id": right["decision_id"]}, mcp=True)["record"]["conflicts"], [])

    def test_export_import_linked_histories(self) -> None:
        first = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="older linked sqlite"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        second = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="newer linked sqlite"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        older, newer = (first, second) if first["decision_id"] < second["decision_id"] else (second, first)
        self.store.append_event(
            {
                "request_id": _uuid(),
                "decision_id": older["decision_id"],
                "expected_revision": 1,
                "occurred_at": None,
                "event_type": "superseded",
                "payload": {"replacement_id": newer["decision_id"], "reason": "replaced by later record"},
            },
            mcp=False,
        )
        c1 = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="pair left sqlite"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        c2 = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="pair right sqlite"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        opener, other = (c1, c2) if c1["decision_id"] < c2["decision_id"] else (c2, c1)
        self.store.append_event(
            {
                "request_id": _uuid(),
                "decision_id": opener["decision_id"],
                "expected_revision": opener["revision"],
                "occurred_at": None,
                "event_type": "conflict_opened",
                "payload": {"other_id": other["decision_id"], "reason": "both still open"},
            },
            mcp=False,
        )
        export_path = Path(self.tmp.name) / "linked.jsonl"
        self.store.export(export_path, include_local_only=False)
        dest = Path(self.tmp.name) / "linked-dest"
        dest.mkdir()
        os.chmod(dest, 0o700)
        self.init_ledger(dest, None, "dest-scope", "dest-actor")
        imported = self.Store(dest, actor_channel="owner_cli")
        imported.import_file(
            export_path,
            self.binding["ledger_id"],
            self.binding["scope_id"],
            include_local_only=False,
        )
        got_old = imported.get({"decision_id": older["decision_id"]}, mcp=True)["record"]
        self.assertEqual(got_old["lifecycle"], "superseded")
        self.assertEqual(got_old["replacement_id"], newer["decision_id"])
        got_open = imported.get({"decision_id": opener["decision_id"]}, mcp=True)["record"]
        self.assertEqual(got_open["conflicts"][0]["other_id"], other["decision_id"])
        imported.close()

    def test_reordered_imported_evidence_stays_unverified(self) -> None:
        items = _evidence(ref="doc://a") + _evidence(ref="doc://b")
        rec = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="two evidence sqlite", evidence=items),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        export_path = Path(self.tmp.name) / "reorder-ev.jsonl"
        self.store.export(export_path, include_local_only=False)
        dest = Path(self.tmp.name) / "reorder-ev-dest"
        dest.mkdir()
        os.chmod(dest, 0o700)
        self.init_ledger(dest, None, "dest-scope", "dest-actor")
        other = self.Store(dest, actor_channel="owner_cli")
        other.import_file(
            export_path,
            self.binding["ledger_id"],
            self.binding["scope_id"],
            include_local_only=False,
        )
        got = other.get({"decision_id": rec["decision_id"]}, mcp=True)
        other.append_event(
            {
                "request_id": _uuid(),
                "decision_id": rec["decision_id"],
                "expected_revision": got["record"]["revision"],
                "occurred_at": None,
                "event_type": "outcome",
                "payload": _outcome("success", note="reversed copy", evidence=list(reversed(items))),
            },
            mcp=False,
        )
        got = other.get({"decision_id": rec["decision_id"]}, mcp=True)
        self.assertEqual(got["record"]["evidence_state"], "unverified")
        other.close()

    def test_mcp_hidden_wrong_revision_is_unavailable(self) -> None:
        hidden = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="hidden rev sqlite", sensitivity="local_only"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        with self.assertRaises(self.LedgerError) as ctx:
            self.store.append_event(
                {
                    "request_id": _uuid(),
                    "decision_id": hidden["decision_id"],
                    "expected_revision": hidden["revision"] + 3,
                    "occurred_at": None,
                    "event_type": "outcome",
                    "payload": _outcome("failed", note="probe"),
                },
                mcp=True,
            )
        self.assertEqual(ctx.exception.code, "UNAVAILABLE")

    def test_import_rejects_resolve_from_wrong_pair(self) -> None:
        from context_ledger.contracts import parse_json

        a = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="pair a sqlite"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        b = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="pair b sqlite"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        c = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="pair c sqlite"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        d = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="pair d sqlite"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        self.store.append_event(
            {
                "request_id": _uuid(),
                "decision_id": a["decision_id"],
                "expected_revision": a["revision"],
                "occurred_at": None,
                "event_type": "conflict_opened",
                "payload": {"other_id": b["decision_id"], "reason": "real clash"},
            },
            mcp=False,
        )
        export_path = Path(self.tmp.name) / "wrong-pair.jsonl"
        self.store.export(export_path, include_local_only=False)
        lines = export_path.read_text(encoding="utf-8").splitlines()
        created_c = None
        opened = None
        for line in lines[1:]:
            obj = parse_json(line)
            if obj.get("type") != "event":
                continue
            event = obj["event"]
            if event["decision_id"] == c["decision_id"] and event["event_type"] == "created":
                created_c = event
            if event["event_type"] == "conflict_opened":
                opened = event
        self.assertIsNotNone(created_c)
        self.assertIsNotNone(opened)
        forged = dict(created_c)
        forged["event_id"] = _uuid()
        forged["revision"] = 2
        forged["expected_revision"] = 1
        forged["event_type"] = "conflict_resolved"
        forged["request_id"] = _uuid()
        forged["payload"] = {
            "other_id": d["decision_id"],
            "opened_event_id": opened["event_id"],
            "reason": "cite foreign opener",
        }
        bad_path = Path(self.tmp.name) / "wrong-pair-bad.jsonl"
        bad_path.write_text(
            "\n".join(lines + [json.dumps({"type": "event", "scope_id": lines and parse_json(lines[0])["scope_id"], "event": forged})])
            + "\n",
            encoding="utf-8",
        )
        dest = Path(self.tmp.name) / "wrong-pair-dest"
        dest.mkdir()
        os.chmod(dest, 0o700)
        self.init_ledger(dest, None, "dest-scope", "dest-actor")
        other = self.Store(dest, actor_channel="owner_cli")
        with self.assertRaises(self.LedgerError) as ctx:
            other.import_file(
                bad_path,
                self.binding["ledger_id"],
                self.binding["scope_id"],
                include_local_only=False,
            )
        self.assertEqual(ctx.exception.code, "INVALID_TRANSITION")
        other.close()

    def test_hidden_conflicts_count_toward_limit(self) -> None:
        from context_ledger import store as store_mod

        visible = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="limit visible sqlite"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        hidden = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="limit hidden sqlite", sensitivity="local_only"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        other = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="limit other sqlite"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        self.store.append_event(
            {
                "request_id": _uuid(),
                "decision_id": visible["decision_id"],
                "expected_revision": visible["revision"],
                "occurred_at": None,
                "event_type": "conflict_opened",
                "payload": {"other_id": hidden["decision_id"], "reason": "hidden pair"},
            },
            mcp=False,
        )
        original = store_mod.MAX_CONFLICTS
        store_mod.MAX_CONFLICTS = 1
        try:
            with self.assertRaises(self.LedgerError) as ctx:
                self.store.append_event(
                    {
                        "request_id": _uuid(),
                        "decision_id": visible["decision_id"],
                        "expected_revision": visible["revision"] + 1,
                        "occurred_at": None,
                        "event_type": "conflict_opened",
                        "payload": {"other_id": other["decision_id"], "reason": "would be 33rd"},
                    },
                    mcp=True,
                )
            self.assertEqual(ctx.exception.code, "LIMIT_EXCEEDED")
        finally:
            store_mod.MAX_CONFLICTS = original

    def test_import_accepts_reversed_event_lines(self) -> None:
        rec = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="order sqlite"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        self.store.append_event(
            {
                "request_id": _uuid(),
                "decision_id": rec["decision_id"],
                "expected_revision": rec["revision"],
                "occurred_at": None,
                "event_type": "outcome",
                "payload": _outcome("failed", note="later observation"),
            },
            mcp=False,
        )
        export_path = Path(self.tmp.name) / "order.jsonl"
        self.store.export(export_path, include_local_only=False)
        lines = export_path.read_text(encoding="utf-8").splitlines()
        reversed_path = Path(self.tmp.name) / "order-rev.jsonl"
        reversed_path.write_text("\n".join([lines[0], *reversed(lines[1:])]) + "\n", encoding="utf-8")
        dest = Path(self.tmp.name) / "order-dest"
        dest.mkdir()
        os.chmod(dest, 0o700)
        self.init_ledger(dest, None, "dest-scope", "dest-actor")
        other = self.Store(dest, actor_channel="owner_cli")
        other.import_file(
            reversed_path,
            self.binding["ledger_id"],
            self.binding["scope_id"],
            include_local_only=False,
        )
        got = other.get({"decision_id": rec["decision_id"]}, mcp=True)["record"]
        self.assertEqual(got["outcome"]["status"], "failed")
        other.close()

    def test_import_rejects_impossible_timestamp(self) -> None:
        rec = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="ts sqlite"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        export_path = Path(self.tmp.name) / "ts.jsonl"
        self.store.export(export_path, include_local_only=False)
        text = export_path.read_text(encoding="utf-8")
        match = re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", text)
        self.assertIsNotNone(match)
        bad_path = Path(self.tmp.name) / "ts-bad.jsonl"
        bad_path.write_text(text.replace(match.group(0), "2026-99-99T99:99:99.999Z", 1), encoding="utf-8")
        dest = Path(self.tmp.name) / "ts-dest"
        dest.mkdir()
        os.chmod(dest, 0o700)
        self.init_ledger(dest, None, "dest-scope", "dest-actor")
        other = self.Store(dest, actor_channel="owner_cli")
        with self.assertRaises(self.LedgerError) as ctx:
            other.import_file(
                bad_path,
                self.binding["ledger_id"],
                self.binding["scope_id"],
                include_local_only=False,
            )
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")
        other.close()
        _ = rec

    def test_import_rejects_malformed_tombstone(self) -> None:
        rec = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="tomb sqlite"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        self.store.purge(rec["decision_id"], rec["revision"], self.binding["ledger_id"])
        export_path = Path(self.tmp.name) / "tomb.jsonl"
        self.store.export(export_path, include_local_only=False)
        from context_ledger.contracts import parse_json

        lines = export_path.read_text(encoding="utf-8").splitlines()
        out = [lines[0]]
        found = False
        for line in lines[1:]:
            obj = parse_json(line)
            if obj.get("type") == "tombstone":
                obj["revision"] = True
                obj["purged_at"] = "not-a-time"
                found = True
            out.append(json.dumps(obj, sort_keys=True, separators=(",", ":")))
        self.assertTrue(found)
        bad_path = Path(self.tmp.name) / "tomb-bad.jsonl"
        bad_path.write_text("\n".join(out) + "\n", encoding="utf-8")
        dest = Path(self.tmp.name) / "tomb-dest"
        dest.mkdir()
        os.chmod(dest, 0o700)
        self.init_ledger(dest, None, "dest-scope", "dest-actor")
        other = self.Store(dest, actor_channel="owner_cli")
        with self.assertRaises(self.LedgerError) as ctx:
            other.import_file(
                bad_path,
                self.binding["ledger_id"],
                self.binding["scope_id"],
                include_local_only=False,
            )
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")
        other.close()

    def test_admin_concurrency_one_winner(self) -> None:
        rec = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="concurrent sqlite"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        script = (
            "import sys, json\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from context_ledger.store import Store\n"
            "from context_ledger.contracts import LedgerError\n"
            f"s = Store({str(self.data)!r}, actor_channel='owner_cli')\n"
            "try:\n"
            "    s.append_event({\n"
            "        'request_id': sys.argv[1],\n"
            "        'decision_id': sys.argv[2],\n"
            "        'expected_revision': int(sys.argv[3]),\n"
            "        'occurred_at': None,\n"
            "        'event_type': 'outcome',\n"
            "        'payload': {'status':'failed','note':sys.argv[4],'evidence':[]},\n"
            "    }, mcp=False)\n"
            "    print('ok')\n"
            "except LedgerError as exc:\n"
            "    print(exc.code)\n"
        )
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", script, _uuid(), rec["decision_id"], "1", note],
                cwd=str(ROOT),
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for note in ("a", "b")
        ]
        outs = []
        for proc in procs:
            out, err = proc.communicate(timeout=15)
            outs.append(out.strip())
        self.assertEqual(sorted(outs), ["REVISION_CONFLICT", "ok"])


class McpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.data = Path(cls.tmp.name) / "data"
        cls.data.mkdir()
        os.chmod(cls.data, 0o700)
        wheel = Path(cls.tmp.name) / "wheels"
        wheel.mkdir()
        dl = _run(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                "-r",
                str(ROOT / "requirements.lock"),
                "-d",
                str(wheel),
                "--require-hashes",
                "--disable-pip-version-check",
            ]
        )
        if dl.returncode != 0:
            raise unittest.SkipTest("wheel download failed: " + dl.stderr[-400:])
        setup = _boot(cls.data, "setup", ["--wheelhouse", str(wheel)])
        if setup.returncode != 0:
            raise unittest.SkipTest("offline setup failed: " + setup.stderr[-400:])
        init = _boot(
            cls.data,
            "init",
            ["--scope", "example-project", "--actor", "example-agent"],
        )
        if init.returncode != 0:
            raise unittest.SkipTest("init failed: " + init.stderr[-400:])
        import hashlib

        sha = hashlib.sha256((ROOT / "requirements.lock").read_bytes()).hexdigest()
        runtime = cls.data / "runtime" / sha
        sites = list((runtime / "lib").glob("python*/site-packages"))
        if not sites:
            raise unittest.SkipTest("runtime site-packages missing")
        cls.site = sites[0]
        sys.path.insert(0, str(ROOT))
        sys.path.insert(0, str(cls.site))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def _client(self, mode: str):
        from mcp import Client
        from mcp.client.stdio import StdioServerParameters

        params = StdioServerParameters(
            command=sys.executable,
            args=[str(BOOT), "--data", str(self.data), "serve"],
            cwd=str(ROOT),
            env={**os.environ, "PYTHONPATH": str(ROOT), "PYTHONNOUSERSITE": "1"},
        )
        return Client(params, mode=mode)

    def test_tools_legacy_and_modern(self) -> None:
        import anyio

        async def once(mode: str) -> dict[str, Any]:
            async with self._client(mode) as client:
                tools = await client.list_tools()
                names = sorted(t.name for t in tools.tools)
                by_name = {t.name: t.model_dump(by_alias=True) for t in tools.tools}
                return {"names": names, "by_name": by_name}

        for mode in ("legacy", "2026-07-28"):
            info = anyio.run(once, mode)
            self.assertEqual(info["names"], ["append_event", "find", "get", "record"], mode)
            rec = info["by_name"]["record"].get("inputSchema") or {}
            app = info["by_name"]["append_event"].get("inputSchema") or {}
            record_schema = json.dumps(rec)
            append_schema = json.dumps(app)
            self.assertIn("kind", record_schema)
            self.assertIn("summary", record_schema)
            self.assertNotIn("owner_attested", record_schema)
            self.assertIn("replacement_id", append_schema)
            self.assertIn("opened_event_id", append_schema)
            self.assertIn("conflict_opened", append_schema)
            self.assertNotIn("owner_attested", append_schema)
            defs = rec.get("$defs") or {}
            body = defs.get("McpBodyModel") or {}
            self.assertEqual(body.get("additionalProperties"), False)

    def test_record_find_get_roundtrip(self) -> None:
        import anyio
        from context_ledger.store import Store

        store = Store(self.data, actor_channel="owner_cli")
        rec = store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="mcp roundtrip sqlite"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        store.close()

        async def go() -> None:
            async with self._client("legacy") as client:
                found = await client.call_tool(
                    "find",
                    {
                        "query": "roundtrip sqlite",
                        "entities": [],
                        "constraints": [],
                    },
                )
                raw = found.model_dump(by_alias=True, exclude_none=False)
                text = ""
                if found.content:
                    text = found.content[0].text
                payload = found.structured_content
                if not payload and text:
                    payload = json.loads(text)
                self.assertTrue(payload and payload.get("ok"), raw)
                ids = [m["decision_id"] for m in payload["data"]["matches"]]
                self.assertIn(rec["decision_id"], ids)
                got = await client.call_tool("get", {"decision_id": rec["decision_id"]})
                gpay = got.structured_content or json.loads(got.content[0].text)
                self.assertTrue(gpay.get("ok"), gpay)
                self.assertEqual(gpay["data"]["record"]["decision_id"], rec["decision_id"])

        anyio.run(go)

    def test_offline_setup_marker(self) -> None:
        ready = self.site.parents[2] / ".ready"
        self.assertTrue(ready.is_file(), ready)

    def test_bounded_reader_dup_and_oversize(self) -> None:
        from context_ledger.contracts import MAX_REQUEST_LINE

        proc = subprocess.Popen(
            [sys.executable, str(BOOT), "--data", str(self.data), "serve"],
            cwd=str(ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": str(ROOT), "PYTHONNOUSERSITE": "1"},
        )
        try:
            deadline = time.time() + 8
            proc.stdin.write(b'{"jsonrpc":"2.0","id":1,"id":2}\n')
            proc.stdin.flush()
            line = b""
            while time.time() < deadline and not line:
                if proc.poll() is not None:
                    break
                line = proc.stdout.readline()
            self.assertTrue(line, proc.stderr.read() if proc.poll() is not None else "timeout")
            payload = json.loads(line)
            self.assertEqual(payload["error"]["code"], -32700)
            proc.stdin.write(b"x" * (MAX_REQUEST_LINE + 8) + b"\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
            payload = json.loads(line)
            self.assertEqual(payload["error"]["code"], -32700)
        finally:
            if proc.stdin:
                proc.stdin.close()
            proc.kill()
            proc.wait(timeout=5)
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
