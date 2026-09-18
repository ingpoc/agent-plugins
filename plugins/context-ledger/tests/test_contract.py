"""Contract tests for context-ledger. Table-driven stdlib unittest."""

from __future__ import annotations

import json
import os
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
CREATOR = Path.home() / ".agents/skills/agent-plugin-creator/scripts/create_agent_plugin.py"
FIXTURE = ROOT / "tests" / "scenario_fixture.json"


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


class PackageTests(unittest.TestCase):
    def test_creator_validate(self) -> None:
        proc = _run([sys.executable, str(CREATOR), "--validate", str(ROOT)])
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_skill_frontmatter(self) -> None:
        text = (ROOT / "skills/context-ledger/SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        self.assertIn("name: context-ledger", text)
        self.assertIn("find", text)
        self.assertIn("append_event", text)

    def test_catalogs_and_plugins_row(self) -> None:
        collection = ROOT.parent.parent
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

    def test_readme_unmeasured_and_exercised_os(self) -> None:
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

        async def once(mode: str) -> list[str]:
            async with self._client(mode) as client:
                tools = await client.list_tools()
                return sorted(t.name for t in tools.tools)

        for mode in ("legacy", "2026-07-28"):
            names = anyio.run(once, mode)
            self.assertEqual(names, ["append_event", "find", "get", "record"], mode)

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
