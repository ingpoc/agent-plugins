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
os.environ["LEDGER_AB_DATE"] = "2026-10-17"  # pre-A/B-window day: find on; A/B tests pin their own days

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
        "scope": "fleet",
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


def _require_sentence_embedding(test: unittest.TestCase) -> None:
    """Off darwin, a skip is not proof of select or feedback. On darwin, fail."""
    if sys.platform != "darwin":
        test.skipTest("not proof of select or feedback off darwin")
    try:
        import objc

        objc.loadBundle(
            "NaturalLanguage",
            bundle_path="/System/Library/Frameworks/NaturalLanguage.framework",
            module_globals={},
        )
        embedding = objc.lookUpClass("NLEmbedding").sentenceEmbeddingForLanguage_("en")
    except Exception as exc:
        test.fail(f"sentence embedding unavailable: {exc}")
    if embedding is None:
        test.fail("sentence embedding unavailable")


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
        cwd = server.get("cwd")
        if _collection_root() is not None:
            self.assertEqual(command, "sh")
            self.assertIsNone(cwd)
            self.assertEqual(server["args"][0], "-c")
            self.assertNotIn("${", server["args"][1])
            self.assertEqual(
                server["args"][2:],
                [
                    "context-ledger-mcp",
                    "${PLUGIN_ROOT}",
                    "${CURSOR_PLUGIN_ROOT}",
                    "${CLAUDE_PLUGIN_ROOT}",
                    "serve",
                ],
            )
            self.assertEqual(
                server["env"],
                {"CONTEXT_LEDGER_MCP_CLIENT_TOKEN": "${env:CONTEXT_LEDGER_MCP_TOKEN}"},
            )
            self.assertTrue(launcher.is_file())
            return
        if ".cursor" in ROOT.parts and Path(command).is_absolute():
            self.assertTrue(Path(command).is_file())
            if cwd is not None and Path(str(cwd)).is_absolute():
                self.assertEqual(cwd, str(ROOT))
            return
        self.assertIn(command, ("python", "python3", "./bin/context-ledger-mcp", "sh"))
        self.assertNotIn("${", command)
        self.assertFalse(command.startswith("/") and command.count("/") == 1)
        self.assertNotIn(" ", command)
        if command == "./bin/context-ledger-mcp":
            self.assertTrue(launcher.is_file())
            self.assertIn(cwd, ("${PLUGIN_ROOT}", "./"))
        elif command == "sh":
            self.assertIsNone(cwd)
        elif cwd is not None:
            self.assertIn(cwd, ("${PLUGIN_ROOT}", "./"))

    def test_skill_frontmatter(self) -> None:
        text = (ROOT / "skills/context-ledger/SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        self.assertIn("name: context-ledger", text)
        self.assertIn("find", text)
        self.assertIn("append_event", text)
        self.assertIn("read/write-capable Context Ledger subagent", text)
        self.assertIn("references/lookup-subagent.md", text)
        text = (ROOT / "skills/context-ledger/references/lookup-subagent.md").read_text(encoding="utf-8")
        self.assertIn("answer these four yourself before adding anything", text)
        for question in (
            "Is this a decision or a lesson?",
            "Which future task will apply it?",
            "Would a `find` for what that task would type return it?",
            "Is it already recorded, and does it carry its `scope`?",
        ):
            self.assertIn(question, text)

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
        claude = json.loads((collection / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
        claude_names = [p["name"] for p in claude["plugins"]]
        expected = sorted(path.name for path in (collection / "plugins").iterdir()
                          if (path / ".claude-plugin/plugin.json").is_file())
        self.assertEqual(sorted(claude_names), expected)

    def test_cursor_package_override(self) -> None:
        collection = _collection_root()
        if collection is None:
            self.skipTest("collection catalogs are not part of an installed package")
        data = json.loads((collection / ".cursor-plugin/marketplace.json").read_text(encoding="utf-8"))
        entry = next(plugin for plugin in data["plugins"] if plugin["name"] == "context-ledger")
        self.assertNotIn("mcpServers", entry)
        root_plugin = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
        override = json.loads((ROOT / ".cursor-plugin/plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(
            override,
            {
                "name": "context-ledger",
                "version": root_plugin["version"],
                "description": root_plugin["description"],
                "mcpServers": "./.cursor-plugin/mcp.json",
            },
        )
        cursor_mcp = json.loads((ROOT / ".cursor-plugin/mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(set(cursor_mcp), {"mcpServers"})
        cursor_server = cursor_mcp["mcpServers"]["context-ledger"]
        self.assertEqual(cursor_server["type"], "stdio")
        self.assertEqual(cursor_server["command"], "sh")
        self.assertNotIn("cwd", cursor_server)
        self.assertEqual(cursor_server["args"][0], "-c")
        self.assertEqual(
            cursor_server["args"][2:],
            ["context-ledger-mcp", "${CURSOR_PLUGIN_ROOT}", "serve"],
        )
        self.assertNotIn("${", cursor_server["args"][1])
        self.assertNotIn("|| true", cursor_server["args"][1])
        self.assertIn("ver=" + root_plugin["version"], cursor_server["args"][1])
        self.assertEqual(
            cursor_server["env"],
            {"CONTEXT_LEDGER_MCP_CLIENT_TOKEN": "${env:CONTEXT_LEDGER_MCP_TOKEN}"},
        )
        self.assertEqual(
            (ROOT / ".cursor-plugin/mcp.json").read_text(encoding="utf-8").count(root_plugin["version"]),
            1,
        )
        self.assertEqual((ROOT / "mcp.json").read_text(encoding="utf-8").count(root_plugin["version"]), 1)
        claude_mcp = json.loads((ROOT / ".claude-plugin/mcp.json").read_text(encoding="utf-8"))
        self.assertNotIn("env", claude_mcp["mcpServers"]["context-ledger"])
        self.assertNotIn("${env:", (ROOT / ".claude-plugin/mcp.json").read_text(encoding="utf-8"))
        portable = json.loads((ROOT / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]["context-ledger"]
        self.assertEqual(portable["command"], "sh")
        self.assertIsNone(portable.get("cwd"))
        self.assertNotIn("${", portable["command"])
        self.assertNotIn("${", portable["args"][1])
        self.assertEqual(
            portable["args"][2:],
            [
                "context-ledger-mcp",
                "${PLUGIN_ROOT}",
                "${CURSOR_PLUGIN_ROOT}",
                "${CLAUDE_PLUGIN_ROOT}",
                "serve",
            ],
        )
        self.assertEqual(portable["env"], cursor_mcp["mcpServers"]["context-ledger"]["env"])
        self.assertEqual(portable["args"][-1], "serve")
        self.assertIn("ver=" + root_plugin["version"], portable["args"][1])

    def test_relative_stdio_requires_cursor_override(self) -> None:
        collection = _collection_root()
        if collection is None:
            self.skipTest("collection scripts/check.py is not part of an installed package")
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "agent_plugins_collection_check",
            collection / "scripts" / "check.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        schema = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
        mcp_schema = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
        axes = ("Reliability", "Robustness", "Context efficiency", "Speed", "Efficiency")

        def build(directory: Path, override: str | None) -> Path:
            root = directory / "demo"
            root.mkdir()
            (root / "plugin.json").write_text(
                json.dumps(
                    {
                        "$schema": schema,
                        "name": "demo",
                        "version": "0.0.1",
                        "description": "fixture",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            skill = root / "skills" / "demo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("---\nname: demo\ndescription: fixture\n---\n\n", encoding="utf-8")
            (root / "AGENTS.md").write_text("# demo\n", encoding="utf-8")
            (root / "README.md").write_text(
                "".join(f"### {axis}\n\nunmeasured\n\n" for axis in axes),
                encoding="utf-8",
            )
            (root / "mcp.json").write_text(
                json.dumps(
                    {
                        "$schema": mcp_schema,
                        "mcpServers": {
                            "demo": {"type": "stdio", "command": "./bin/x", "cwd": "./"}
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            if override is None:
                return root
            dest = root / ".cursor-plugin"
            dest.mkdir()
            (dest / "plugin.json").write_text(
                json.dumps(
                    {
                        "name": "demo",
                        "version": "0.0.1",
                        "description": "fixture",
                        "mcpServers": "./.cursor-plugin/mcp.json",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            command = "./bin/x" if override == "relative" else "${CURSOR_PLUGIN_ROOT}/bin/x"
            (dest / "mcp.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "demo": {
                                "type": "stdio",
                                "command": command,
                                "cwd": "${CURSOR_PLUGIN_ROOT}",
                            }
                        }
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            return root

        with tempfile.TemporaryDirectory() as tmp:
            errors = module.validate_plugin(build(Path(tmp), None))
        self.assertTrue(
            any("Cursor resolves ./ against the workspace -> ENOENT" in error for error in errors),
            errors,
        )
        with tempfile.TemporaryDirectory() as tmp:
            errors = module.validate_plugin(build(Path(tmp), "relative"))
        self.assertTrue(errors, errors)
        self.assertTrue(any("command must be" in error for error in errors), errors)
        with tempfile.TemporaryDirectory() as tmp:
            errors = module.validate_plugin(build(Path(tmp), "ok"))
        self.assertEqual(errors, [])

    def test_plugin_version(self) -> None:
        plugin = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
        init = (ROOT / "context_ledger/__init__.py").read_text(encoding="utf-8")
        self.assertEqual(plugin["version"], "0.1.36")
        self.assertIn('__version__ = "0.1.36"', init)
        skill = (ROOT / "skills/context-ledger/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("description: Use only for critical, recurring, research or debugging tasks, or to save an owner override", skill)
        self.assertIn("Simple tasks skip it. Short timeout, fail open.", skill)
        self.assertNotIn("Guru", skill)
        self.assertNotIn("Every new task", skill)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        for axis in ("Reliability", "Robustness", "Context efficiency", "Speed", "Efficiency"):
            self.assertIn(axis, readme)
        self.assertGreaterEqual(readme.count("unmeasured"), 5)
        self.assertIn("macOS arm64", readme)
        self.assertIn("release-blocked", readme)
        self.assertNotIn("supported: Ubuntu", readme)
        self.assertIn("Release-blocked until run", agents)
        page = (ROOT / "ui/ledger-v2.html").read_text(encoding="utf-8")
        self.assertIn('version:"0.1.36"', page)
        self.assertIn("ui/initialize", page)
        self.assertNotIn("<script src", page)
        self.assertNotIn("https://", page)
        self.assertNotIn("http://", page.replace('"http://www.w3.org/2000/svg"', ""))  # SVG namespace, not a fetch


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
        env.pop("CONTEXT_LEDGER_DATA", None)
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

    def test_implicit_plugin_data_does_not_override_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            plugin_data = Path(tmp) / "host-data"
            env = {**os.environ, "HOME": str(home), "PLUGIN_DATA": str(plugin_data)}
            env.pop("CONTEXT_LEDGER_DATA", None)
            proc = subprocess.run(
                [sys.executable, str(BOOT), "doctor"],
                cwd=str(ROOT), env=env, capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertTrue((home / ".context-ledger").is_dir())
            self.assertFalse(plugin_data.exists())

            env["CONTEXT_LEDGER_DATA"] = str(plugin_data)
            proc = subprocess.run(
                [sys.executable, str(BOOT), "doctor"],
                cwd=str(ROOT), env=env, capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertTrue(plugin_data.is_dir())

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
            self.assertTrue(one["data"]["gate"])
            self.assertTrue(one["data"]["save"])
            text = agents.read_text(encoding="utf-8")
            self.assertIn("**Ledger lookup**", text)
            self.assertIn("before the first step of a new task, load `context-ledger`", text)
            self.assertIn("**Ledger gate**", text)
            self.assertIn("answer the four questions in the skill", text)
            self.assertIn("decide alone which entries to add", text)
            self.assertIn("**Ledger save**", text)
            self.assertLess(text.index("**Ledger lookup**"), text.index("## AFTER"))
            self.assertLess(text.index("## AFTER"), text.index("**Ledger gate**"))
            self.assertLess(text.index("**Ledger gate**"), text.index("**Ledger save**"))
            self.assertLess(text.index("**Ledger save**"), text.index("**Durable-learning"))
            second = _boot(data, "ensure-global-triggers", ["--agents-md", str(agents)])
            self.assertEqual(second.returncode, 0, second.stderr)
            two = json.loads(second.stderr.strip())
            self.assertFalse(two["data"]["added"])
            self.assertEqual(agents.read_text(encoding="utf-8"), text)

    def test_replaces_stale_marked_lines(self) -> None:
        fixture = (
            "# Global AGENTS.md\n\n"
            "## BEFORE\n\n"
            "- **Ledger lookup** → `context-ledger` if history or precedent could change next decision; else skip\n\n"
            "## AFTER\n\n"
            "- **Ledger gate** → stale gate wording; else skip\n"
            "- **Ledger save** → `context-ledger` if settled decision, useful failure, or material update; else skip\n"
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
            text = agents.read_text(encoding="utf-8")
            self.assertIn("before the first step of a new task, load `context-ledger`", text)
            self.assertIn("Do not store the problem", text)
            self.assertIn("decide alone which entries to add", text)
            self.assertLess(text.index("**Ledger gate**"), text.index("**Ledger save**"))
            self.assertNotIn("settled decision", text)
            self.assertNotIn("stale gate wording", text)
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

        def call(command: str, body: dict, *, code: int = 0) -> dict:
            proc = subprocess.run(
                [sys.executable, str(helper), command, "--json", "-"],
                input=json.dumps(body), text=True, capture_output=True, env=env,
            )
            self.assertEqual(proc.returncode, code, proc.stdout + proc.stderr)
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
        receipt = self.store.append_event(
            {
                "request_id": _uuid(),
                "decision_id": decision_id,
                "expected_revision": 2,
                "occurred_at": None,
                "event_type": "applied",
                "payload": {"task_id": task_id, "decision_ids": [decision_id]},
            },
            mcp=False,
        )
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
        # The outcome names the applied event and the verdict: without them the loop's check reads
        # nothing the tier counts, which is the whole point of the producer writing them.
        self.assertEqual(rec["outcome"]["applied_event_id"], receipt["event_id"])
        self.assertEqual(rec["outcome"]["helpful"], True)

        duplicate = call("closeout", {"task_id": task_id, "outcomes": [{"id": decision_id,
            "applied": True, "status": "success", "note": "once", "evidence_ref": "session://first"},
            {"id": decision_id, "applied": True, "status": "failed", "note": "twice",
             "evidence_ref": "session://second"}]}, code=1)
        self.assertEqual(duplicate, {"written": [], "write": "unavailable"})
        self.assertEqual(call("closeout", {"outcomes": [], "candidate": {
            "summary": "unreviewed", "reason": "repeatable", "evidence_ref": "session://test"}},
            code=1),
            {"written": [], "write": "unavailable"})
        self.assertEqual(self.store.get({"decision_id": decision_id}, mcp=True)["record"]["revision"], 4)

    def test_closeout_without_the_applied_link_writes_nothing(self) -> None:
        """The item asserts the decision was applied. With no receipt the claim is unverifiable."""
        helper = ROOT / "skills/context-ledger/scripts/lookup.py"
        env = {**os.environ, "CONTEXT_LEDGER_DATA": str(self.data)}
        created = self.store.record(
            {"request_id": _uuid(), "occurred_at": None,
             "body": _body(summary="Closeout needs a receipt"), "outcome": _outcome("pending")},
            mcp=False,
        )
        decision_id = created["decision_id"]
        proc = subprocess.run(
            [sys.executable, str(helper), "closeout", "--json", "-"],
            input=json.dumps({"task_id": _uuid(), "outcomes": [{"id": decision_id,
                "applied": True, "status": "success", "note": "no receipt",
                "evidence_ref": "session://no-receipt"}]}),
            text=True, capture_output=True, env=env,
        )
        # A write that wrote nothing is a failure the agent must fix, so it exits 1 like `feedback`;
        # the payload still carries the need and the fix.
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        body = json.loads(proc.stdout)
        self.assertEqual(body["written"], [])
        self.assertEqual(body["write"], "unavailable")
        self.assertEqual(body["need"], "applied_event_id")
        self.assertIn("--applied", body["fix"])
        # Nothing was written, and the reason names the fix rather than only refusing.
        rec = self.store.get({"decision_id": decision_id}, mcp=True)["record"]
        self.assertEqual(rec["revision"], 1)
        self.assertNotIn("helpful", rec["outcome"])

    def test_a_negative_closeout_needs_its_effect_and_carries_it(self) -> None:
        helper = ROOT / "skills/context-ledger/scripts/lookup.py"
        env = {**os.environ, "CONTEXT_LEDGER_DATA": str(self.data)}
        created = self.store.record(
            {"request_id": _uuid(), "occurred_at": None,
             "body": _body(summary="A decision that misled"), "outcome": _outcome("pending")},
            mcp=False,
        )
        decision_id = created["decision_id"]
        task_id = _uuid()
        self.store.append_event(
            {"request_id": _uuid(), "decision_id": decision_id, "expected_revision": 1,
             "occurred_at": None, "event_type": "applied",
             "payload": {"task_id": task_id, "decision_ids": [decision_id]}},
            mcp=False,
        )
        empty_effect = subprocess.run(
            [sys.executable, str(helper), "closeout", "--json", "-"],
            input=json.dumps({"task_id": task_id, "outcomes": [{"id": decision_id,
                "applied": True, "status": "failed", "note": "the action backfired",
                "evidence_ref": "session://misled", "helpful": True, "effect": "misled"}]}),
            text=True, capture_output=True, env=env,
        )
        # effect/reason describe a decision that did not help, so they go only with helpful: false.
        self.assertEqual(json.loads(empty_effect.stdout), {"written": [], "write": "unavailable"})
        stated = subprocess.run(
            [sys.executable, str(helper), "closeout", "--json", "-"],
            input=json.dumps({"task_id": task_id, "outcomes": [{"id": decision_id,
                "applied": True, "status": "failed", "note": "the action backfired",
                "evidence_ref": "session://misled", "helpful": False, "effect": "misled",
                "reason": "it undid the earlier fix"}]}),
            text=True, capture_output=True, env=env,
        )
        self.assertEqual(json.loads(stated.stdout)["written"], [decision_id])
        outcome = self.store.get({"decision_id": decision_id}, mcp=True)["record"]["outcome"]
        self.assertEqual(outcome["status"], "failed")
        self.assertEqual(outcome["helpful"], False)
        self.assertEqual(outcome["effect"], "misled")
        self.assertEqual(outcome["reason"], "it undid the earlier fix")

    def test_a_derived_negative_observation_keeps_the_note_as_its_reason(self) -> None:
        helper = ROOT / "skills/context-ledger/scripts/lookup.py"
        env = {**os.environ, "CONTEXT_LEDGER_DATA": str(self.data)}
        created = self.store.record(
            {"request_id": _uuid(), "occurred_at": None,
             "body": _body(summary="A task that did not succeed"), "outcome": _outcome("pending")},
            mcp=False,
        )
        decision_id = created["decision_id"]
        task_id = _uuid()
        self.store.append_event(
            {"request_id": _uuid(), "decision_id": decision_id, "expected_revision": 1,
             "occurred_at": None, "event_type": "applied",
             "payload": {"task_id": task_id, "decision_ids": [decision_id]}},
            mcp=False,
        )
        proc = subprocess.run(
            [sys.executable, str(helper), "closeout", "--json", "-"],
            input=json.dumps({"task_id": task_id, "outcomes": [{"id": decision_id,
                "applied": True, "status": "inconclusive", "note": "the run proved nothing",
                "evidence_ref": "session://inconclusive"}]}),
            text=True, capture_output=True, env=env,
        )
        self.assertEqual(json.loads(proc.stdout)["written"], [decision_id])
        outcome = self.store.get({"decision_id": decision_id}, mcp=True)["record"]["outcome"]
        # The item asserts application; with no verdict of its own, an inconclusive task did not help.
        self.assertEqual(outcome["helpful"], False)
        self.assertEqual(outcome["effect"], "not_helped")
        self.assertEqual(outcome["reason"], "the run proved nothing")

    def _applied_for(self, decision_id: str, task_id: str) -> str:
        return self.store.append_event(
            {
                "request_id": _uuid(),
                "decision_id": decision_id,
                "expected_revision": 1,
                "occurred_at": None,
                "event_type": "applied",
                "payload": {"task_id": task_id, "decision_ids": [decision_id]},
            },
            mcp=False,
        )["event_id"]

    def test_feedback_writes_the_verdict_and_the_receipt_it_names(self) -> None:
        """The feedback producer is the closeout producer: same verdict, same applied link."""
        helped = self.store.record(
            {"request_id": _uuid(), "occurred_at": None,
             "body": _body(summary="A decision that helped"),
             "outcome": _outcome("pending")},
            mcp=False,
        )["decision_id"]
        misled = self.store.record(
            {"request_id": _uuid(), "occurred_at": None,
             "body": _body(summary="A decision that misled"),
             "outcome": _outcome("pending")},
            mcp=False,
        )["decision_id"]
        task_id = _uuid()
        receipts = {d: self._applied_for(d, task_id) for d in (helped, misled)}
        body = self._lookup("feedback", {"task_id": task_id, "provided": [
            {"id": helped, "helped": True},
            {"id": misled, "helped": False, "effect": "misled", "issue": "it undid the earlier fix"},
        ]})
        self.assertEqual(body["outcomes"], [helped, misled])
        good = self.store.get({"decision_id": helped}, mcp=True)["record"]["outcome"]
        self.assertEqual(good["status"], "success")
        self.assertEqual(good["helpful"], True)
        self.assertEqual(good["applied_event_id"], receipts[helped])
        bad = self.store.get({"decision_id": misled}, mcp=True)["record"]["outcome"]
        self.assertEqual(bad["status"], "inconclusive", "helped: false is not a failed decision")
        self.assertEqual(bad["helpful"], False)
        self.assertEqual(bad["effect"], "misled")
        self.assertEqual(bad["reason"], "it undid the earlier fix")
        self.assertEqual(bad["applied_event_id"], receipts[misled])
        rejected = self._lookup("feedback", {"task_id": task_id, "provided": [
            {"id": helped, "helped": True, "effect": "misled"},
        ]}, ok=False)
        self.assertEqual(rejected, {"written": [], "write": "unavailable"})

    def test_a_plain_helped_false_feedback_stays_unlinked_and_unjudged(self) -> None:
        """No effect means no verdict, and an applied outcome needs helpful, so it cannot cite the link."""
        decision_id = self.store.record(
            {"request_id": _uuid(), "occurred_at": None,
             "body": _body(summary="A decision that did not help"),
             "outcome": _outcome("pending")},
            mcp=False,
        )["decision_id"]
        task_id = _uuid()
        self._applied_for(decision_id, task_id)
        self._lookup("feedback", {"task_id": task_id, "provided": [
            {"id": decision_id, "helped": False, "issue": "not what this task needed"},
        ]})
        outcome = self.store.get({"decision_id": decision_id}, mcp=True)["record"]["outcome"]
        self.assertEqual(outcome["status"], "inconclusive")
        self.assertNotIn("helpful", outcome)
        self.assertNotIn("applied_event_id", outcome)

    def test_the_internal_actor_keeps_plumbing_reads_out_of_the_callers(self) -> None:
        decision_id = self.store.record(
            {"request_id": _uuid(), "occurred_at": None,
             "body": _body(summary="Internal reads"), "outcome": _outcome("pending")},
            mcp=False,
        )["decision_id"]
        task_id = _uuid()
        self._applied_for(decision_id, task_id)
        body = self._lookup("closeout", {"task_id": task_id, "outcomes": [{"id": decision_id,
            "applied": True, "status": "success", "note": "done",
            "evidence_ref": "session://internal"}]})
        self.assertEqual(body["written"], [decision_id])
        by_caller = self.store.access_report()["by_caller"]
        self.assertIn("ledger-internal", by_caller)
        # The revision read and the confidence re-read are plumbing, one access row each.
        self.assertGreaterEqual(by_caller["ledger-internal"]["get"], 2)
        self.assertNotIn("unnamed", by_caller)

    def test_the_helper_bills_every_read_it_makes_to_a_named_actor(self) -> None:
        """`unnamed` is not a bucket: the sweep is plumbing, and the answer is the agent's lookup."""
        _require_sentence_embedding(self)
        ids = [
            self.store.record(
                {"request_id": _uuid(), "occurred_at": None,
                 "body": _body(summary=f"Helper actor record {n}"),
                 "outcome": _outcome("pending")},
                mcp=False,
            )["decision_id"]
            for n in range(3)
        ]
        helper = ROOT / "skills/context-ledger/scripts/lookup.py"
        env = {**os.environ, "CONTEXT_LEDGER_DATA": str(self.data)}
        self._lookup("select", {"situation": "Helper actor record 1 happens",
                                "need": "Run helper actor record 1"})
        appended = self._lookup("append", {"decision_id": ids[1], "event_type": "outcome",
                                           "payload": _outcome("inconclusive", note="actor probe")})
        self.assertEqual(appended["written"], [ids[1]])
        for args in (["find", "--query", "helper actor record"], ["get", "--id", ids[0]]):
            proc = _run([sys.executable, str(helper), *args], env=env)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        by_caller = self.store.access_report()["by_caller"]
        self.assertNotIn("unnamed", by_caller)
        actor = self.store.binding["actor_id"]
        self.assertIn(actor, by_caller, "a read the agent asked for names the binding's actor")
        self.assertGreaterEqual(by_caller[actor]["find"], 1)
        self.assertGreaterEqual(by_caller[actor]["get"], 2, "the selected decision and the explicit get")
        self.assertGreaterEqual(by_caller["ledger-internal"]["get"], 3,
                                "the candidate sweep and the append revision read")

    def test_an_over_long_caller_reason_is_rejected_not_trimmed(self) -> None:
        decision_id = self.store.record(
            {"request_id": _uuid(), "occurred_at": None,
             "body": _body(summary="A reason over the limit"), "outcome": _outcome("pending")},
            mcp=False,
        )["decision_id"]
        task_id = _uuid()
        self._applied_for(decision_id, task_id)
        before = self.store.get({"decision_id": decision_id}, mcp=True)["record"]["revision"]
        body = self._lookup("closeout", {"task_id": task_id, "outcomes": [{
            "id": decision_id, "applied": True, "status": "failed", "note": "the action backfired",
            "evidence_ref": "session://reason", "helpful": False, "effect": "not_helped",
            "reason": " ".join(f"w{n}" for n in range(30))}]}, ok=False)
        self.assertEqual(body["need"], "reason")
        self.assertIn("20 words", body["fix"])
        # Nothing was written, so the reason the caller wrote is not silently replaced by 20 words.
        rec = self.store.get({"decision_id": decision_id}, mcp=True)["record"]
        self.assertEqual(rec["revision"], before)
        self.assertNotIn("helpful", rec["outcome"])

    def test_an_over_long_feedback_issue_is_rejected_with_the_limit(self) -> None:
        decision_id = self.store.record(
            {"request_id": _uuid(), "occurred_at": None,
             "body": _body(summary="A feedback issue over the limit"), "outcome": _outcome("pending")},
            mcp=False,
        )["decision_id"]
        task_id = _uuid()
        self._applied_for(decision_id, task_id)
        body = self._lookup("feedback", {"task_id": task_id, "provided": [{
            "id": decision_id, "helped": False, "effect": "not_helped",
            "issue": " ".join(f"w{n}" for n in range(30))}]}, ok=False)
        self.assertEqual(body["need"], "reason")
        self.assertIn("20 words", body["fix"])

    def test_a_long_note_still_gives_a_reason_when_the_caller_gave_none(self) -> None:
        """The note is the caller's prose for the task, so the derived reason is cut, not fatal."""
        decision_id = self.store.record(
            {"request_id": _uuid(), "occurred_at": None,
             "body": _body(summary="A note over the limit"), "outcome": _outcome("pending")},
            mcp=False,
        )["decision_id"]
        task_id = _uuid()
        self._applied_for(decision_id, task_id)
        note = " ".join(f"note{n}" for n in range(30))
        body = self._lookup("closeout", {"task_id": task_id, "outcomes": [{
            "id": decision_id, "applied": True, "status": "failed", "note": note,
            "evidence_ref": "session://note", "helpful": False, "effect": "not_helped"}]})
        self.assertEqual(body["written"], [decision_id])
        rec = self.store.get({"decision_id": decision_id}, mcp=True)["record"]
        self.assertEqual(rec["outcome"]["reason"], " ".join(f"note{n}" for n in range(20)))

    def test_a_judged_feedback_item_without_the_receipt_writes_nothing(self) -> None:
        decision_id = self.store.record(
            {"request_id": _uuid(), "occurred_at": None,
             "body": _body(summary="A verdict with no receipt"), "outcome": _outcome("pending")},
            mcp=False,
        )["decision_id"]
        body = self._lookup("feedback", {"task_id": _uuid(), "provided": [
            {"id": decision_id, "helped": True}]}, ok=False)
        self.assertEqual(body["write"], "unavailable")
        self.assertEqual(body["need"], "applied_event_id")
        self.assertIn("--applied", body["fix"])
        rec = self.store.get({"decision_id": decision_id}, mcp=True)["record"]
        self.assertEqual(rec["revision"], 1, "an unlinkable verdict is not stored")

    def test_a_closeout_item_that_denies_applied_names_the_way_out(self) -> None:
        decision_id = self.store.record(
            {"request_id": _uuid(), "occurred_at": None,
             "body": _body(summary="Considered but not applied"), "outcome": _outcome("pending")},
            mcp=False,
        )["decision_id"]
        task_id = _uuid()
        self._applied_for(decision_id, task_id)
        before = self.store.get({"decision_id": decision_id}, mcp=True)["record"]["revision"]
        body = self._lookup("closeout", {"task_id": task_id, "outcomes": [{
            "id": decision_id, "applied": False, "status": "success", "note": "considered only",
            "evidence_ref": "session://considered"}]}, ok=False)
        self.assertEqual(body["need"], "applied: true")
        self.assertIn("feedback", body["fix"])
        rec = self.store.get({"decision_id": decision_id}, mcp=True)["record"]
        self.assertEqual(rec["revision"], before)

    def test_the_helper_producer_goes_through_the_loop_for_a_positive_and_a_negative(self) -> None:
        helper = ROOT / "skills/context-ledger/scripts/lookup.py"
        loop = ROOT / "scripts/ledger_loop.py"
        env = {
            **os.environ,
            "CONTEXT_LEDGER_DATA": str(self.data),
            "LEDGER_AB_DATE": "2026-10-17",
            "LEDGER_LOOP_LOG": str(self.data / "ledger-loop" / "starts.jsonl"),
            "LEDGER_TASK_LOG": str(self.data / "ledger-loop" / "tasks.jsonl"),
        }

        def helper_call(command: str, body: dict) -> dict:
            proc = subprocess.run(
                [sys.executable, str(helper), command, "--json", "-"],
                input=json.dumps(body), text=True, capture_output=True, env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            return json.loads(proc.stdout)

        good = helper_call("record", {"body": _body(summary="Read the failing call first"),
                                      "outcome": _outcome("pending")})["written"][0]
        bad = helper_call("record", {"body": _body(summary="Retry the tool after a timeout"),
                                     "outcome": _outcome("pending")})["written"][0]
        task_id = _uuid()
        started = _run([sys.executable, str(loop), "start", task_id, "--bot", "box", "--query",
                        "the tool stalled", "--found", f"{good},{bad}"], env=env)
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        # Close writes the receipts first: that is the link the producer's outcome has to name.
        linked = _run([sys.executable, str(loop), "close", task_id, "--applied", good,
                       "--applied", bad], env=env)
        self.assertEqual(linked.returncode, 1, linked.stdout + linked.stderr)
        self.assertIn("no outcome for applied", linked.stderr)
        closed = helper_call("closeout", {"task_id": task_id, "outcomes": [
            {"id": good, "applied": True, "status": "success", "note": "the fix landed",
             "evidence_ref": "session://positive"},
            {"id": bad, "applied": True, "status": "failed", "note": "the retry masked the cause",
             "evidence_ref": "session://negative", "helpful": False, "effect": "misled",
             "reason": "it hid the failing call"},
        ]})
        self.assertEqual(closed["written"], [good, bad])
        done = _run([sys.executable, str(loop), "close", task_id, "--applied", good,
                     "--applied", bad], env=env)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("PASS", done.stdout)

    def _lookup(self, command: str, body: dict, *, ok: bool = True, env: dict | None = None) -> dict:
        helper = ROOT / "skills/context-ledger/scripts/lookup.py"
        run_env = {**os.environ, "CONTEXT_LEDGER_DATA": str(self.data), **(env or {})}
        proc = subprocess.run(
            [sys.executable, str(helper), command, "--json", "-"],
            input=json.dumps(body),
            text=True,
            capture_output=True,
            env=run_env,
        )
        if ok:
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        else:
            self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def test_lookup_task_ids_reach_access_logs_and_reject_invalid_ids(self) -> None:
        _require_sentence_embedding(self)
        did, _ = self._seed_situation_and_success()
        helper = ROOT / "skills/context-ledger/scripts/lookup.py"
        task = _uuid()
        env = {**os.environ, "CONTEXT_LEDGER_DATA": str(self.data), "LEDGER_AB_DATE": "2026-10-17"}
        for command in (
            ["find", "--query", "plugin live client", "--task-id", task],
            ["get", "--id", did, "--task-id", task],
        ):
            proc = subprocess.run([sys.executable, str(helper), *command], env=env, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(json.loads(proc.stderr)["task_id"], task)
        self._lookup("select", {
            "task_id": task, "situation": "A plugin change is about to ship with only unit tests as proof",
            "need": "What has to happen before the change goes to main", "query": "plugin live client",
        }, env=env)
        rows = self.store.open().execute(
            "select tool, task_id from access_log where caller=?", (self.binding["actor_id"],)
        ).fetchall()
        self.assertEqual([(row["tool"], row["task_id"]) for row in rows],
                         [("find", task), ("get", task), ("get", task)])
        before = self.store.open().execute("select count(*) from access_log").fetchone()[0]
        for command in (["find", "--query", "plugin"], ["get", "--id", did]):
            proc = subprocess.run([sys.executable, str(helper), *command, "--task-id", "invalid"],
                                  env=env, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 2)
            self.assertIn("--task-id", proc.stderr)
        invalid = self._lookup("select", {
            "task_id": "invalid", "situation": "Plugin change", "need": "Live proof",
        }, env=env, ok=False)
        self.assertEqual(invalid["lookup"], "unavailable")
        self.assertEqual(self.store.open().execute("select count(*) from access_log").fetchone()[0], before)

    def _seed_situation_and_success(self) -> tuple[str, str]:
        pending = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(
                    summary="Live client pass before main",
                    situation="A plugin change is about to ship with only unit tests as proof",
                    action="Run a live end-to-end pass on the installed client before the change goes to main",
                    applicability="A plugin change is about to ship with only unit tests as proof",
                    entities=["helper-selection"],
                    constraints=[],
                    evidence=[],
                ),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        success = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(
                    summary="Refresh the marketplace badge",
                    situation="Cursor is showing a plugin version that should change",
                    action="Commit matching plugin.json and version, then refresh the marketplace so the badge is the indexed commit",
                    applicability="Cursor is showing a plugin version that should change",
                    entities=["helper-selection"],
                    constraints=[],
                    evidence=[],
                ),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        revision = 1
        for _ in range(3):
            self.store.append_event(
                {
                    "request_id": _uuid(),
                    "decision_id": success["decision_id"],
                    "expected_revision": revision,
                    "occurred_at": None,
                    "event_type": "outcome",
                    "payload": _outcome(
                        "success",
                        note="badge matched the indexed commit",
                        task_id=_uuid(),
                    ),
                },
                mcp=False,
            )
            revision += 1
        recorded = self.store.get({"decision_id": success["decision_id"]}, mcp=True)["record"]
        self.assertEqual(recorded["outcome"]["status"], "success")
        self.assertGreater(recorded["confidence"]["successes"], 0)
        waiting = self.store.get({"decision_id": pending["decision_id"]}, mcp=True)["record"]
        self.assertEqual(waiting["outcome"]["status"], "pending")
        self.assertEqual(waiting["confidence"]["successes"], 0)
        return pending["decision_id"], success["decision_id"]

    def test_select_fits_situation_ahead_of_higher_success(self) -> None:
        _require_sentence_embedding(self)
        pending_id, success_id = self._seed_situation_and_success()
        live = self._lookup(
            "select",
            {
                "situation": "About to change a plugin and the only proof so far is the automated suite",
                "need": "What has to happen before that change goes to main",
            },
        )
        badge = self._lookup(
            "select",
            {
                "situation": "Shipping a plugin version badge into the marketplace index",
                "need": "What has to happen so the badge matches the indexed commit",
            },
        )
        self.assertEqual(live["decision"]["id"], pending_id)
        self.assertIsInstance(live["distance"], float)
        self.assertEqual(live["decision"]["outcome"], "pending")
        self.assertIsNone(live["decision"]["flag"])
        self.assertEqual(live["decision"]["approval"], "none")
        self.assertEqual(badge["decision"]["id"], success_id)
        self.assertEqual(badge["decision"]["outcome"], "success")
        self.assertIsNone(badge["decision"]["flag"])
        self.assertEqual(badge["decision"]["approval"], "none")
        self.assertNotEqual(live["decision"]["id"], badge["decision"]["id"])
        unrelated = self._lookup(
            "select",
            {
                "situation": "Booking a train ticket for tomorrow morning",
                "need": "Which coach and payment method to use",
            },
        )
        self.assertIn(unrelated["decision"]["id"], {pending_id, success_id})
        self.assertIsInstance(unrelated["distance"], float)
        self.assertGreater(unrelated["distance"], live["distance"])
        found = self.store.find(
            {
                "query": "marketplace badge",
                "entities": ["helper-selection"],
                "constraints": [],
                "limit": 3,
                "offset": 0,
            },
            mcp=True,
        )
        self.assertEqual(found["matches"][0]["decision_id"], success_id)

    def test_select_reports_whether_its_nearest_record_is_grounded(self) -> None:
        """The embedding always returns a nearest record. Verdict says whether it is evidence."""
        _require_sentence_embedding(self)
        pending_id, success_id = self._seed_situation_and_success()
        situation = "About to change a plugin and the only proof so far is the automated suite"
        need = "What has to happen before that change goes to main"
        prose = self._lookup("select", {"situation": situation, "need": need})
        self.assertEqual(prose["decision"]["id"], pending_id)
        self.assertIn(prose["verdict"], {"grounded", "ungrounded"})
        self.assertGreater(prose["grounding"]["terms"], 0)
        self.assertIsNone(prose["grounding"]["query"])
        unrelated = self._lookup(
            "select",
            {
                "situation": "Booking a train ticket for tomorrow morning",
                "need": "Which coach and payment method to use",
            },
        )
        self.assertIn(unrelated["decision"]["id"], {pending_id, success_id})
        self.assertEqual(unrelated["verdict"], "ungrounded")
        self.assertFalse(unrelated["grounding"]["lexical_ok"])
        # The same situation with terse keywords is the reading the record's own text supports.
        terse = self._lookup(
            "select", {"situation": situation, "need": need, "query": "plugin change proof"}
        )
        self.assertEqual(terse["decision"]["id"], pending_id)
        self.assertEqual(terse["verdict"], "grounded")
        self.assertTrue(terse["grounding"]["lexical_ok"])
        self.assertEqual(terse["grounding"]["query"], "plugin change proof")
        badge = self._lookup(
            "select",
            {
                "situation": "Shipping a plugin version badge into the marketplace index",
                "need": "What has to happen so the badge matches the indexed commit",
                "query": "marketplace badge version commit",
            },
        )
        self.assertEqual(badge["decision"]["id"], success_id)
        self.assertEqual(badge["verdict"], "grounded")

    def test_select_does_not_surface_a_record_find_hides(self) -> None:
        """Selection reads candidates itself, so it has to apply find's eligibility: a record
        scoped to another project must not come back through the semantic route."""
        _require_sentence_embedding(self)
        situation = "A Codex login was rejected for the pinned model"
        need = "Which login the fallback reviewer uses instead"
        hidden = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(
                    summary="Fallback reviewer login for a rejected Codex login",
                    situation=situation,
                    action=need,
                    applicability=situation,
                    entities=["helper-selection"],
                    constraints=[],
                    evidence=[],
                    scope="project:some-other-project",
                ),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        visible = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(
                    summary="Route a rejected Codex login to the API-key fallback reviewer",
                    situation=situation,
                    action="Use the API-key login for the fallback reviewer",
                    applicability=situation,
                    entities=["helper-selection"],
                    constraints=[],
                    evidence=[],
                    scope="fleet",
                ),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        found = self.store.find(
            {"query": "fallback reviewer login", "entities": [], "constraints": [], "limit": 3, "offset": 0},
            mcp=True,
        )
        ids = [m["decision_id"] for m in found["matches"]]
        self.assertIn(visible["decision_id"], ids)
        self.assertNotIn(hidden["decision_id"], ids, "find must hide another project's record")
        got = self._lookup("select", {"situation": situation, "need": need})
        self.assertEqual(got["decision"]["id"], visible["decision_id"],
                         "select must not surface a record find hides")

    def test_select_is_off_on_an_a_b_off_day(self) -> None:
        """The A/B treatment is the agent's lookup. find is off on an even day inside the window,
        so the semantic route must be off with it, or the trial has no control arm."""
        _require_sentence_embedding(self)
        self._seed_situation_and_success()
        body = {"situation": "A plugin change is about to ship with only unit tests as proof",
                "need": "What has to happen before that change goes to main"}
        off = self._lookup("select", body, env={"LEDGER_AB_DATE": "2026-10-18"})
        self.assertEqual(off.get("arm"), "off")
        self.assertIsNone(off["decision"])
        on = self._lookup("select", body, env={"LEDGER_AB_DATE": "2026-10-19"})
        self.assertNotEqual(on.get("arm"), "off")
        self.assertIsNotNone(on["decision"])

    def test_feedback_reuses_similar_precedent_and_records_a_missing_decision(self) -> None:
        _require_sentence_embedding(self)
        pending_id, success_id = self._seed_situation_and_success()

        def receipt(decision_id: str, task_id: str) -> None:
            """A judged item needs the applied receipt it names (the seed left this one at revision 2)."""
            rec = self.store.get({"decision_id": decision_id}, mcp=True)["record"]
            self.store.append_event({
                "request_id": _uuid(), "decision_id": decision_id,
                "expected_revision": rec["revision"], "occurred_at": None,
                "event_type": "applied",
                "payload": {"task_id": task_id, "decision_ids": [decision_id]},
            }, mcp=False)

        task_id = _uuid()
        receipt(success_id, task_id)
        similar = self._lookup(
            "feedback",
            {
                "task_id": task_id,
                "provided": [{"id": success_id, "helped": True, "issue": ""}],
                "would_have_helped": {
                    "situation": "A plugin change is about to ship and unit tests are the only proof",
                    "need": "Run a live end-to-end pass on the installed client before main",
                },
            },
        )
        self.assertEqual(similar["similar"], pending_id)
        self.assertEqual(similar["written"], [])
        quiet_task = _uuid()
        receipt(success_id, quiet_task)
        quiet = self._lookup(
            "feedback",
            {
                "task_id": quiet_task,
                "provided": [{"id": success_id, "helped": True}],
            },
        )
        self.assertIsNone(quiet["similar"])
        self.assertEqual(quiet["written"], [])
        missing = {
            "situation": "A funnel publish is down and find must fail closed until that same publish is restored",
            "need": "Stop only the publish job, prove find fails, and restore that job before exiting",
        }
        created = self._lookup(
            "feedback",
            {
                "task_id": _uuid(),
                "provided": [{
                    "id": pending_id,
                    "helped": False,
                    "issue": "The provided decisions left the publish outage unresolved",
                }],
                "would_have_helped": missing,
            },
        )
        self.assertIsNone(created["similar"])
        self.assertEqual(len(created["written"]), 1)
        new_id = created["written"][0]
        saved = self.store.get({"decision_id": new_id}, mcp=True)["record"]
        self.assertEqual(saved["body"]["applicability"], missing["situation"])
        self.assertEqual(saved["body"]["action"], missing["need"])
        self.assertEqual(saved["outcome"]["status"], "pending")
        again = self._lookup(
            "feedback",
            {
                "task_id": _uuid(),
                "provided": [{"id": new_id, "helped": False, "issue": "still unresolved"}],
                "would_have_helped": missing,
            },
        )
        self.assertIsNone(again["similar"])
        self.assertEqual(len(again["written"]), 1)
        self.assertNotEqual(again["written"][0], new_id)
        self.assertIn(pending_id, saved["body"]["rationale"])
        self.assertIn(
            "The provided decisions left the publish outage unresolved",
            saved["body"]["rationale"],
        )
        marked = self.store.get({"decision_id": pending_id}, mcp=True)["record"]
        self.assertEqual(marked["outcome"]["status"], "inconclusive")

    def test_feedback_records_a_different_action_and_rejects_bad_input_first(self) -> None:
        pending_id, success_id = self._seed_situation_and_success()
        success_revision = self.store.get({"decision_id": success_id}, mcp=True)["record"]["revision"]
        pending_revision = self.store.get({"decision_id": pending_id}, mcp=True)["record"]["revision"]
        duplicate = self._lookup(
            "feedback",
            {
                "task_id": _uuid(),
                "provided": [
                    {"id": success_id, "helped": True},
                    {"id": success_id, "helped": False, "issue": "again"},
                ],
            },
            ok=False,
        )
        malformed = self._lookup(
            "feedback",
            {
                "task_id": _uuid(),
                "provided": [
                    {"id": success_id, "helped": True},
                    {"id": "not-a-uuid", "helped": False},
                ],
            },
            ok=False,
        )
        incomplete = self._lookup(
            "feedback",
            {
                "task_id": _uuid(),
                "provided": [{"id": pending_id, "helped": False, "issue": "gap"}],
                "would_have_helped": {
                    "situation": "A plugin change is about to ship with only unit tests as proof",
                },
            },
            ok=False,
        )
        unknown = self._lookup(
            "feedback",
            {
                "task_id": _uuid(),
                "provided": [
                    {"id": success_id, "helped": True},
                    {"id": _uuid(), "helped": False, "issue": "missing"},
                ],
            },
            ok=False,
        )
        self.assertEqual(unknown, {"written": [], "write": "unavailable"})
        self.assertEqual(duplicate, {"written": [], "write": "unavailable"})
        self.assertEqual(malformed, {"written": [], "write": "unavailable"})
        self.assertEqual(incomplete, {"written": [], "write": "unavailable"})
        self.assertEqual(
            self.store.get({"decision_id": success_id}, mcp=True)["record"]["revision"],
            success_revision,
        )
        self.assertEqual(
            self.store.get({"decision_id": pending_id}, mcp=True)["record"]["revision"],
            pending_revision,
        )
        _require_sentence_embedding(self)
        issue = "Unit tests were the wrong gate"
        created = self._lookup(
            "feedback",
            {
                "task_id": _uuid(),
                "provided": [{"id": pending_id, "helped": False, "issue": issue}],
                "would_have_helped": {
                    "situation": "A plugin change is about to ship and unit tests are the only proof",
                    "need": "Do not ship; revert the change",
                },
            },
        )
        self.assertIsNone(created["similar"])
        self.assertEqual(len(created["written"]), 1)
        saved = self.store.get({"decision_id": created["written"][0]}, mcp=True)["record"]
        self.assertEqual(saved["body"]["action"], "Do not ship; revert the change")
        self.assertIn(pending_id, saved["body"]["rationale"])
        self.assertIn(issue, saved["body"]["rationale"])
        marked = self.store.get({"decision_id": pending_id}, mcp=True)["record"]
        self.assertEqual(marked["outcome"]["status"], "inconclusive")
        found = self.store.find(
            {
                "query": "marketplace badge",
                "entities": ["helper-selection"],
                "constraints": [],
                "limit": 3,
                "offset": 0,
            },
            mcp=True,
        )
        self.assertEqual(found["matches"][0]["decision_id"], success_id)

    def test_feedback_excludes_the_decision_that_did_not_help(self) -> None:
        _require_sentence_embedding(self)
        pending_id, success_id = self._seed_situation_and_success()
        need = "Run a live end-to-end pass on the staging server before the change goes to main"
        created = self._lookup(
            "feedback",
            {
                "task_id": _uuid(),
                "provided": [{
                    "id": pending_id,
                    "helped": False,
                    "issue": "The live pass has to run on the staging server",
                }],
                "would_have_helped": {
                    "situation": "A plugin change is about to ship and unit tests are the only proof",
                    "need": need,
                },
            },
        )
        self.assertIsNone(created["similar"])
        self.assertNotIn(pending_id, created.get("similar") or "")
        self.assertEqual(created["written"], [created["written"][0]])
        self.assertNotEqual(created["written"][0], pending_id)
        self.assertNotEqual(created["written"][0], success_id)
        saved = self.store.get({"decision_id": created["written"][0]}, mcp=True)["record"]
        self.assertEqual(saved["body"]["action"], need)
        marked = self.store.get({"decision_id": pending_id}, mcp=True)["record"]
        self.assertEqual(marked["outcome"]["status"], "inconclusive")
        found = self.store.find(
            {
                "query": "marketplace badge",
                "entities": ["helper-selection"],
                "constraints": [],
                "limit": 3,
                "offset": 0,
            },
            mcp=True,
        )
        self.assertEqual(found["matches"][0]["decision_id"], success_id)

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

        def link(tid: str) -> str:
            """The receipt the loop writes before an outcome can name it."""
            revision = self.store.get({"decision_id": decision_id}, mcp=False)["record"]["revision"]
            return self.store.append_event(
                {"request_id": _uuid(), "decision_id": decision_id,
                 "expected_revision": revision, "occurred_at": None, "event_type": "applied",
                 "payload": {"task_id": tid, "decision_ids": [decision_id]}},
                mcp=False,
            )["event_id"]

        applied_event = link(task_id)
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
                            "sha256": None, "state": "unverified"}],
                        "applied_event_id": applied_event, "helpful": True},
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

        second = _uuid()
        link(second)
        inconclusive = call({"task_id": second, "outcomes": [{
            "id": decision_id, "applied": True, "status": "inconclusive",
            "note": "Outcome cannot be attributed yet", "evidence_ref": "session://task-b"}]})
        self.assertEqual(inconclusive["feedback"][0]["confidence"], {
            "score": 0.0, "successes": 0, "failures": 1,
            "inconclusive": 1, "sample_count": 1})
        third = _uuid()
        link(third)
        next_task = call({"task_id": third, "outcomes": [{
            "id": decision_id, "applied": True, "status": "success",
            "note": "Applied and helped again", "evidence_ref": "session://task-c"}]})
        expected = {"score": 0.5, "successes": 1, "failures": 1,
                    "inconclusive": 1, "sample_count": 2}
        self.assertEqual(next_task["feedback"][0]["confidence"], expected)
        record = self.store.get({"decision_id": decision_id}, mcp=True)["record"]
        self.assertEqual(record["confidence"], expected)
        found = self.store.find({"query": "Apply known decision", "entities": [],
                                 "constraints": [], "limit": 3, "offset": 0}, mcp=True)
        # v2 find is compact: confidence lives on get, find only names the hit.
        self.assertEqual(found["matches"][0]["decision_id"], decision_id)
        self.assertNotIn("confidence", found["matches"][0])

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

    def test_failure_ranks_below_success_without_task_id(self) -> None:
        ok_rec = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="alpha ok", entities=["same-ent"]),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        self.store.append_event(
            {
                "request_id": _uuid(),
                "decision_id": ok_rec["decision_id"],
                "expected_revision": ok_rec["revision"],
                "occurred_at": None,
                "event_type": "outcome",
                "payload": _outcome("success", note="ok"),
            },
            mcp=False,
        )
        pending = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="alpha pending", entities=["same-ent"]),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        fail = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="alpha fail", entities=["same-ent"]),
                "outcome": _outcome("failed", note="failed"),
            },
            mcp=False,
        )
        found = self.store.find(
            {"query": "alpha", "entities": ["same-ent"], "constraints": []},
            mcp=True,
        )
        ids = [m["decision_id"] for m in found["matches"]]
        self.assertEqual(ids, [ok_rec["decision_id"], pending["decision_id"], fail["decision_id"]])
        self.assertIsNone(found["matches"][0].get("flag"))
        self.assertEqual(found["matches"][2]["flag"], "status failed")
        got = self.store.get({"decision_id": ok_rec["decision_id"]}, mcp=True)["record"]
        self.assertEqual(got["outcome"]["status"], "success")
        self.assertNotIn("task_id", got["outcome"])
        self.assertEqual(got["confidence"]["successes"], 0)
        self.assertEqual(got["confidence"]["sample_count"], 0)

    def test_superseded_ranks_below_pending_with_flag(self) -> None:
        old = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="beta old", entities=["beta-ent"]),
                "outcome": _outcome("success", note="ok"),
            },
            mcp=False,
        )
        replacement = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="beta new", entities=["beta-ent"]),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        self.store.append_event(
            {
                "request_id": _uuid(),
                "decision_id": old["decision_id"],
                "expected_revision": old["revision"],
                "occurred_at": None,
                "event_type": "superseded",
                "payload": {"replacement_id": replacement["decision_id"], "reason": "replaced"},
            },
            mcp=False,
        )
        found = self.store.find(
            {
                "query": "beta",
                "entities": ["beta-ent"],
                "constraints": [],
                "include_superseded": True,
            },
            mcp=True,
        )
        ids = [m["decision_id"] for m in found["matches"]]
        self.assertEqual(ids[0], replacement["decision_id"])
        self.assertEqual(ids[-1], old["decision_id"])
        self.assertEqual(
            found["matches"][-1]["flag"],
            f"superseded_by {replacement['decision_id']}",
        )

    def test_find_limit_above_three_names_max(self) -> None:
        with self.assertRaises(self.LedgerError) as ctx:
            self.store.find(
                {"query": "alpha", "entities": [], "constraints": [], "limit": 4},
                mcp=True,
            )
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")
        self.assertEqual(ctx.exception.message, "find limit max is 3")

    def test_find_names_path_when_ledger_file_removed_after_startup(self) -> None:
        self.store.open()
        path = self.store.db_path
        self.assertIsNotNone(self.store.conn)
        path.unlink()
        with self.assertRaises(self.LedgerError) as ctx:
            self.store.find(
                {"query": "startup", "entities": [], "constraints": []},
                mcp=True,
            )
        self.assertEqual(ctx.exception.code, "UNAVAILABLE")
        self.assertTrue(ctx.exception.retryable)
        self.assertEqual(ctx.exception.message, f"ledger unreachable at {path}")
        fresh = self.Store(self.data, actor_channel="owner_cli")
        with self.assertRaises(self.LedgerError) as startup:
            fresh.open()
        self.assertEqual(startup.exception.code, "UNAVAILABLE")
        self.assertTrue(startup.exception.retryable)
        self.assertEqual(startup.exception.message, f"ledger unreachable at {path}")

    def test_corrupt_ledger_after_startup_is_not_unreachable(self) -> None:
        self.store.open()
        self.store.db_path.write_bytes(b"not a sqlite database")
        with self.assertRaises(self.LedgerError) as ctx:
            self.store.find(
                {"query": "startup", "entities": [], "constraints": []},
                mcp=True,
            )
        self.assertEqual(ctx.exception.code, "CORRUPT_LEDGER")

    def test_abandoned_and_inconclusive_rank_below_pending(self) -> None:
        pending = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="band pending", entities=["rank-band"]),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        abandoned = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="band abandoned", entities=["rank-band"]),
                "outcome": _outcome("abandoned", note="left"),
            },
            mcp=False,
        )
        failed = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="band failed", entities=["rank-band"]),
                "outcome": _outcome("failed", note="no"),
            },
            mcp=False,
        )
        inconclusive = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="marker inconclusive", entities=["rank-incon"]),
                "outcome": _outcome("inconclusive", note="unsure"),
            },
            mcp=False,
        )
        pending_other = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="marker still pending", entities=["rank-incon"]),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        ranked = self.store.find(
            {"query": "band", "entities": ["rank-band"], "constraints": []},
            mcp=True,
        )
        self.assertEqual(
            [item["decision_id"] for item in ranked["matches"]],
            [pending["decision_id"], abandoned["decision_id"], failed["decision_id"]],
        )
        other = self.store.find(
            {"query": "marker", "entities": ["rank-incon"], "constraints": [], "limit": 2},
            mcp=True,
        )
        self.assertEqual(
            [item["decision_id"] for item in other["matches"]],
            [pending_other["decision_id"], inconclusive["decision_id"]],
        )

    def test_find_health_retries_once_and_names_path(self) -> None:
        calls = {"n": 0}

        def boom() -> None:
            calls["n"] += 1
            raise self.LedgerError("UNAVAILABLE")

        self.store.probe = boom  # type: ignore[method-assign]
        self.store._reachable = False
        with self.assertRaises(self.LedgerError) as ctx:
            self.store.find(
                {"query": "alpha", "entities": [], "constraints": []},
                mcp=True,
            )
        self.assertEqual(calls["n"], 2)
        self.assertEqual(ctx.exception.code, "UNAVAILABLE")
        self.assertIn(str(self.store.db_path), ctx.exception.message or "")
        self.assertIn("ledger unreachable at", ctx.exception.message or "")

    def test_find_probes_before_each_call(self) -> None:
        calls = {"n": 0}
        real = self.store.probe

        def wrapped() -> None:
            calls["n"] += 1
            real()

        self.store.probe = wrapped  # type: ignore[method-assign]
        args = {"query": "alpha", "entities": [], "constraints": []}
        self.store.find(args, mcp=True)
        self.store.find(args, mcp=True)
        self.assertEqual(calls["n"], 2)

    def test_applied_event_is_queryable_and_links_outcome(self) -> None:
        task_id = _uuid()
        rec = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="apply me sqlite"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        other = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="also applied sqlite"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        applied = self.store.append_event(
            {
                "request_id": _uuid(),
                "decision_id": rec["decision_id"],
                "expected_revision": rec["revision"],
                "occurred_at": None,
                "event_type": "applied",
                "payload": {
                    "task_id": task_id,
                    "decision_ids": [rec["decision_id"], other["decision_id"]],
                },
            },
            mcp=True,
        )
        with self.assertRaises(self.LedgerError) as ctx:
            self.store.append_event(
                {
                    "request_id": _uuid(),
                    "decision_id": rec["decision_id"],
                    "expected_revision": applied["revision"],
                    "occurred_at": None,
                    "event_type": "applied",
                    "payload": {"task_id": task_id, "decision_ids": [other["decision_id"]]},
                },
                mcp=True,
            )
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")
        closed = self.store.append_event(
            {
                "request_id": _uuid(),
                "decision_id": rec["decision_id"],
                "expected_revision": applied["revision"],
                "occurred_at": None,
                "event_type": "outcome",
                "payload": _outcome(
                    "success",
                    note="applied then checked",
                    applied_event_id=applied["event_id"],
                    helpful=True,
                ),
            },
            mcp=True,
        )
        self.assertEqual(closed["revision"], applied["revision"] + 1)
        got = self.store.get({"decision_id": rec["decision_id"]}, mcp=True)["record"]
        self.assertEqual(
            [event["event_type"] for event in got["events"]],
            ["applied", "outcome"],
        )
        self.assertEqual(got["events"][0]["payload"]["task_id"], task_id)
        self.assertEqual(got["events"][0]["payload"]["decision_ids"][1], other["decision_id"])
        self.assertEqual(got["events"][1]["payload"]["applied_event_id"], applied["event_id"])
        self.assertNotIn("task_id", got["events"][1]["payload"])
        self.assertEqual(got["confidence"]["sample_count"], 0)
        with self.assertRaises(self.LedgerError) as ctx:
            self.store.append_event(
                {
                    "request_id": _uuid(),
                    "decision_id": other["decision_id"],
                    "expected_revision": other["revision"],
                    "occurred_at": None,
                    "event_type": "outcome",
                    "payload": _outcome(
                        "success",
                        note="wrong link",
                        applied_event_id=applied["event_id"],
                        helpful=True,
                    ),
                },
                mcp=True,
            )
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")

    def test_get_drops_oldest_trace_events_when_the_cap_is_exceeded(self) -> None:
        from context_ledger.contracts import MAX_GET_RESPONSE, canonical_json

        rec = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="trace cap sqlite"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        note = "n" * 480
        revision = rec["revision"]
        for _ in range(20):
            appended = self.store.append_event(
                {
                    "request_id": _uuid(),
                    "decision_id": rec["decision_id"],
                    "expected_revision": revision,
                    "occurred_at": None,
                    "event_type": "outcome",
                    "payload": _outcome("success", note=note),
                },
                mcp=True,
            )
            revision = appended["revision"]
        got = self.store.get({"decision_id": rec["decision_id"]}, mcp=True)["record"]
        encoded = canonical_json({"record": got}).encode("utf-8")
        self.assertLessEqual(len(encoded), MAX_GET_RESPONSE)
        self.assertTrue(got["trace_continued"])
        self.assertLess(len(got["events"]), 20)
        self.assertEqual(got["events"][-1]["payload"]["note"], note)
        self.assertEqual(got["events"][-1]["revision"], revision)
        self.assertEqual(got["outcome"]["note"], note)
        self.assertEqual(got["revision"], revision)

    def test_applied_refs_are_checked_and_hidden_on_mcp_read(self) -> None:
        rec = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="visible apply sqlite"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        hidden = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="local apply sqlite", sensitivity="local_only"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        missing = _uuid()
        with self.assertRaises(self.LedgerError) as ctx:
            self.store.append_event(
                {
                    "request_id": _uuid(),
                    "decision_id": rec["decision_id"],
                    "expected_revision": rec["revision"],
                    "occurred_at": None,
                    "event_type": "applied",
                    "payload": {
                        "task_id": _uuid(),
                        "decision_ids": [rec["decision_id"], missing],
                    },
                },
                mcp=True,
            )
        self.assertEqual(ctx.exception.code, "UNAVAILABLE")
        with self.assertRaises(self.LedgerError) as ctx:
            self.store.append_event(
                {
                    "request_id": _uuid(),
                    "decision_id": rec["decision_id"],
                    "expected_revision": rec["revision"],
                    "occurred_at": None,
                    "event_type": "applied",
                    "payload": {
                        "task_id": _uuid(),
                        "decision_ids": [rec["decision_id"], hidden["decision_id"]],
                    },
                },
                mcp=True,
            )
        self.assertEqual(ctx.exception.code, "UNAVAILABLE")
        applied = self.store.append_event(
            {
                "request_id": _uuid(),
                "decision_id": rec["decision_id"],
                "expected_revision": rec["revision"],
                "occurred_at": None,
                "event_type": "applied",
                "payload": {
                    "task_id": _uuid(),
                    "decision_ids": [rec["decision_id"], hidden["decision_id"]],
                },
            },
            mcp=False,
        )
        owner = self.store.get({"decision_id": rec["decision_id"]}, mcp=False)["record"]
        mcp_view = self.store.get({"decision_id": rec["decision_id"]}, mcp=True)["record"]
        owner_ids = owner["events"][0]["payload"]["decision_ids"]
        mcp_ids = mcp_view["events"][0]["payload"]["decision_ids"]
        self.assertEqual(owner["events"][0]["event_id"], applied["event_id"])
        self.assertEqual(owner_ids, [rec["decision_id"], hidden["decision_id"]])
        self.assertEqual(mcp_ids, [rec["decision_id"]])
        self.assertNotIn("trace_continued", mcp_view)

    def test_default_export_drops_local_only_applied_ids(self) -> None:
        visible = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="export visible sqlite"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        hidden = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="export hidden sqlite", sensitivity="local_only"),
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
                "event_type": "applied",
                "payload": {
                    "task_id": _uuid(),
                    "decision_ids": [visible["decision_id"], hidden["decision_id"]],
                },
            },
            mcp=False,
        )
        export_path = Path(self.tmp.name) / "no-local.jsonl"
        self.store.export(export_path, include_local_only=False)
        text = export_path.read_text(encoding="utf-8")
        self.assertNotIn(hidden["decision_id"], text)
        self.assertNotIn(visible["decision_id"], text)
        data2 = Path(self.tmp.name) / "import-no-local"
        data2.mkdir()
        os.chmod(data2, 0o700)
        self.init_ledger(data2, None, "dest-scope", "dest-actor")
        other = self.Store(data2, actor_channel="owner_cli")
        other.import_file(
            export_path,
            self.binding["ledger_id"],
            self.binding["scope_id"],
            include_local_only=False,
        )
        for decision_id in (visible["decision_id"], hidden["decision_id"]):
            with self.assertRaises(self.LedgerError) as ctx:
                other.get({"decision_id": decision_id}, mcp=False)
            self.assertEqual(ctx.exception.code, "UNAVAILABLE")
        other.close()

    def test_export_import_flag_combinations(self) -> None:
        plain = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="plain export sqlite"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        hidden = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="hidden export sqlite", sensitivity="local_only"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        linked = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="linked export sqlite"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        rival = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="rival export sqlite"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        self.store.append_event(
            {
                "request_id": _uuid(),
                "decision_id": linked["decision_id"],
                "expected_revision": linked["revision"],
                "occurred_at": None,
                "event_type": "applied",
                "payload": {
                    "task_id": _uuid(),
                    "decision_ids": [linked["decision_id"], hidden["decision_id"]],
                },
            },
            mcp=False,
        )
        self.store.append_event(
            {
                "request_id": _uuid(),
                "decision_id": rival["decision_id"],
                "expected_revision": rival["revision"],
                "occurred_at": None,
                "event_type": "conflict_opened",
                "payload": {"other_id": hidden["decision_id"], "reason": "same claim"},
            },
            mcp=False,
        )
        files = {}
        for flag in (False, True):
            path = Path(self.tmp.name) / f"combo-{int(flag)}.jsonl"
            self.store.export(path, include_local_only=flag)
            files[flag] = path
        default_text = files[False].read_text(encoding="utf-8")
        self.assertIn(plain["decision_id"], default_text)
        for dropped in (hidden["decision_id"], linked["decision_id"], rival["decision_id"]):
            self.assertNotIn(dropped, default_text)
        full_text = files[True].read_text(encoding="utf-8")
        for kept in (
            plain["decision_id"],
            hidden["decision_id"],
            linked["decision_id"],
            rival["decision_id"],
        ):
            self.assertIn(kept, full_text)

        def fresh(name: str):
            data = Path(self.tmp.name) / name
            data.mkdir()
            os.chmod(data, 0o700)
            self.init_ledger(data, None, "dest-scope", "dest-actor")
            return self.Store(data, actor_channel="owner_cli")

        def present(store, decision_id: str) -> bool:
            try:
                store.get({"decision_id": decision_id}, mcp=False)
            except self.LedgerError as exc:
                self.assertEqual(exc.code, "UNAVAILABLE")
                return False
            return True

        n = 0
        for first in (False, True):
            for second in (False, True):
                dest = fresh(f"combo-{int(first)}-{int(second)}")
                for flag in (first, first, second, second):
                    dest.import_file(
                        files[flag],
                        self.binding["ledger_id"],
                        self.binding["scope_id"],
                        include_local_only=flag,
                    )
                self.assertTrue(present(dest, plain["decision_id"]))
                wants_private = first or second
                self.assertEqual(present(dest, hidden["decision_id"]), wants_private)
                self.assertEqual(present(dest, linked["decision_id"]), wants_private)
                self.assertEqual(present(dest, rival["decision_id"]), wants_private)
                if wants_private:
                    got = dest.get({"decision_id": linked["decision_id"]}, mcp=False)["record"]
                    applied = next(ev for ev in got["events"] if ev["event_type"] == "applied")
                    self.assertEqual(
                        applied["payload"]["decision_ids"],
                        [linked["decision_id"], hidden["decision_id"]],
                    )
                again = fresh(f"reexport-{n}")
                n += 1
                replay = Path(self.tmp.name) / f"replay-{n}.jsonl"
                dest.export(replay, include_local_only=wants_private)
                again.import_file(
                    replay,
                    dest.binding["ledger_id"],
                    dest.binding["scope_id"],
                    include_local_only=wants_private,
                )
                again.import_file(
                    replay,
                    dest.binding["ledger_id"],
                    dest.binding["scope_id"],
                    include_local_only=wants_private,
                )
                self.assertTrue(present(again, plain["decision_id"]))
                self.assertEqual(present(again, hidden["decision_id"]), wants_private)
                dest.close()
                again.close()

    def test_browse_lists_recent_model_safe_decisions(self) -> None:
        visible = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="browse visible sqlite", entities=["ledger-v1"]),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        hidden = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="browse hidden sqlite", sensitivity="local_only"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        listed = self.store.browse({}, mcp=True)
        ids = [row["decision_id"] for row in listed["matches"]]
        self.assertIn(visible["decision_id"], ids)
        self.assertNotIn(hidden["decision_id"], ids)
        self.assertEqual(listed["matches"][0]["entities"], ["ledger-v1"])
        narrowed = self.store.browse({"query": "visible", "entities": ["ledger-v1"]}, mcp=True)
        self.assertEqual([row["decision_id"] for row in narrowed["matches"]], [visible["decision_id"]])
        owner = self.store.browse({}, mcp=False)
        self.assertIn(hidden["decision_id"], [row["decision_id"] for row in owner["matches"]])
        replacement = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="browse replacement sqlite"),
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
                "event_type": "superseded",
                "payload": {"replacement_id": replacement["decision_id"], "reason": "replaced"},
            },
            mcp=False,
        )
        active = [row["decision_id"] for row in self.store.browse({}, mcp=True)["matches"]]
        self.assertNotIn(visible["decision_id"], active)
        self.assertIn(replacement["decision_id"], active)
        wide = [row["decision_id"] for row in self.store.browse({"include_superseded": True}, mcp=True)["matches"]]
        self.assertIn(visible["decision_id"], wide)
        missed = self.store.browse({"query": "no-such-token"}, mcp=True)
        self.assertEqual(missed["matches"], [])
        self.assertFalse(missed["has_more"])
        capped = self.store.browse({"limit": 1, "include_superseded": True}, mcp=False)
        self.assertEqual(len(capped["matches"]), 1)
        self.assertTrue(capped["has_more"])
        with self.assertRaises(self.LedgerError) as ctx:
            self.store.browse({"limit": 25}, mcp=True)
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")

    def test_browse_hides_local_only_links_like_get(self) -> None:
        visible = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="browse public superseded sqlite"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        hidden = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="browse private replacement sqlite", sensitivity="local_only"),
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
                "event_type": "superseded",
                "payload": {"replacement_id": hidden["decision_id"], "reason": "private replacement"},
            },
            mcp=False,
        )
        got = self.store.get({"decision_id": visible["decision_id"]}, mcp=True)["record"]
        self.assertIsNone(got.get("replacement_id"))
        listed = self.store.browse({"include_superseded": True}, mcp=True)["matches"]
        card = next(row for row in listed if row["decision_id"] == visible["decision_id"])
        self.assertNotIn(hidden["decision_id"], json.dumps(card))
        self.assertEqual(card["flag"], "superseded")
        self.assertFalse(card["has_replacement"])
        owner = self.store.browse({"include_superseded": True}, mcp=False)["matches"]
        owner_card = next(row for row in owner if row["decision_id"] == visible["decision_id"])
        self.assertEqual(owner_card["flag"], f"superseded_by {hidden['decision_id']}")
        rival = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="browse public conflict sqlite"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        self.store.append_event(
            {
                "request_id": _uuid(),
                "decision_id": rival["decision_id"],
                "expected_revision": rival["revision"],
                "occurred_at": None,
                "event_type": "conflict_opened",
                "payload": {"other_id": hidden["decision_id"], "reason": "hidden clash"},
            },
            mcp=False,
        )
        rival_got = self.store.get({"decision_id": rival["decision_id"]}, mcp=True)["record"]
        self.assertEqual(rival_got["conflicts"], [])
        rival_card = next(
            row
            for row in self.store.browse({}, mcp=True)["matches"]
            if row["decision_id"] == rival["decision_id"]
        )
        self.assertFalse(rival_card["has_conflict"])
        self.assertNotIn(hidden["decision_id"], json.dumps(rival_card))
        owner_rival = next(
            row
            for row in self.store.browse({}, mcp=False)["matches"]
            if row["decision_id"] == rival["decision_id"]
        )
        self.assertTrue(owner_rival["has_conflict"])

    def test_browse_offset_reaches_the_next_page(self) -> None:
        ids = []
        for index in range(13):
            rec = self.store.record(
                {
                    "request_id": _uuid(),
                    "occurred_at": None,
                    "body": _body(summary=f"paged shared {index}", entities=["paged-shared"]),
                    "outcome": _outcome("pending"),
                },
                mcp=False,
            )
            ids.append(rec["decision_id"])
        query = {
            "query": "paged shared",
            "entities": ["paged-shared"],
            "limit": 12,
        }
        seen = []
        offset = 0
        pages = 0
        while True:
            page = self.store.browse({**query, "offset": offset}, mcp=True)
            self.assertGreater(len(page["matches"]), 0)
            seen.extend(row["decision_id"] for row in page["matches"])
            pages += 1
            if not page["has_more"]:
                break
            offset += len(page["matches"])
            self.assertLess(pages, 5)
        self.assertGreater(pages, 1)
        self.assertEqual(sorted(seen), sorted(ids))
        self.assertEqual(len(set(seen)), 13)
        with self.assertRaises(self.LedgerError) as ctx:
            self.store.browse({"offset": -1}, mcp=True)
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")
        with self.assertRaises(self.LedgerError) as ctx:
            self.store.browse({"offset": 20001}, mcp=True)
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")

    def test_purge_of_applied_ref_still_imports(self) -> None:
        kept = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="kept apply sqlite"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        dropped = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="dropped apply sqlite"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        self.store.append_event(
            {
                "request_id": _uuid(),
                "decision_id": kept["decision_id"],
                "expected_revision": kept["revision"],
                "occurred_at": None,
                "event_type": "applied",
                "payload": {
                    "task_id": _uuid(),
                    "decision_ids": [kept["decision_id"], dropped["decision_id"]],
                },
            },
            mcp=False,
        )
        self.store.purge(dropped["decision_id"], dropped["revision"], self.binding["ledger_id"])
        after = self.store.get({"decision_id": kept["decision_id"]}, mcp=False)["record"]
        self.assertEqual(after["events"][0]["payload"]["decision_ids"], [kept["decision_id"]])
        export_path = Path(self.tmp.name) / "purged-apply.jsonl"
        self.store.export(export_path, include_local_only=False)
        data2 = Path(self.tmp.name) / "import-after-purge"
        data2.mkdir()
        os.chmod(data2, 0o700)
        self.init_ledger(data2, None, "dest-scope", "dest-actor")
        other = self.Store(data2, actor_channel="owner_cli")
        imported = other.import_file(
            export_path,
            self.binding["ledger_id"],
            self.binding["scope_id"],
            include_local_only=False,
        )
        self.assertGreaterEqual(imported["imported_events"], 1)
        got = other.get({"decision_id": kept["decision_id"]}, mcp=True)["record"]
        self.assertEqual(got["events"][0]["payload"]["decision_ids"], [kept["decision_id"]])
        extra = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="later import sqlite"),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        extra_path = Path(self.tmp.name) / "later.jsonl"
        self.store.export(extra_path, include_local_only=False)
        again = other.import_file(
            extra_path,
            self.binding["ledger_id"],
            self.binding["scope_id"],
            include_local_only=False,
        )
        self.assertGreaterEqual(again["imported_events"], 1)
        self.assertEqual(
            other.get({"decision_id": extra["decision_id"]}, mcp=True)["record"]["decision_id"],
            extra["decision_id"],
        )
        other.close()

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

    def test_outcome_null_task_id_is_absent(self) -> None:
        from context_ledger.contracts import validate_outcome

        parsed = validate_outcome({"status": "pending", "note": "", "evidence": [], "task_id": None})
        self.assertNotIn("task_id", parsed)
        rec = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(summary="null task id"),
                "outcome": _outcome("pending", task_id=None),
            },
            mcp=False,
        )
        got = self.store.get({"decision_id": rec["decision_id"]}, mcp=True)
        self.assertNotIn("task_id", got["record"]["outcome"])
        self.assertEqual(got["record"]["outcome"]["status"], "pending")

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
        self.assertTrue(found["has_more"])

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

    def test_recall_check_ranks_without_writing(self) -> None:
        _require_sentence_embedding(self)
        near_situation = "Shipping the catalog change"
        near_need = "Run the installed example before merge"
        near = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(
                    summary="Catalog example",
                    situation=near_situation,
                    action=near_need,
                    applicability=near_situation,
                ),
                "outcome": _outcome("pending"),
            },
            mcp=False,
        )
        far = self.store.record(
            {
                "request_id": _uuid(),
                "occurred_at": None,
                "body": _body(
                    summary="Chart palette",
                    situation="Picking chart color",
                    action="Keep palette file",
                    rationale="Use swatches",
                    applicability="Picking chart color",
                ),
                "outcome": _outcome("success"),
            },
            mcp=False,
        )
        near_id = near["decision_id"]
        far_id = far["decision_id"]
        fixture = Path(self.tmp.name) / "recall.json"
        fixture.write_text(
            json.dumps(
                {
                    "cases": [
                        {
                            "name": "near",
                            "situation": near_situation,
                            "need": near_need,
                            "target_id": near_id,
                        },
                        {
                            "name": "far",
                            "situation": near_situation,
                            "need": near_need,
                            "target_id": far_id,
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        db = self.store.db_path
        before = db.stat()
        proc = _run(
            [sys.executable, str(ROOT / "scripts" / "recall_check.py"), "--fixture", str(fixture)],
            env={"CONTEXT_LEDGER_DATA": str(self.data)},
        )
        after = db.stat()
        self.assertEqual(before.st_ino, after.st_ino)
        self.assertEqual(before.st_size, after.st_size)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        by_name = {row["name"]: row for row in payload["cases"]}
        self.assertEqual(by_name["near"]["select_rank"], 1)
        self.assertEqual(by_name["far"]["select_rank"], 2)
        self.assertEqual(payload["select_top1"], "1/2")
        self.assertEqual(by_name["near"]["find_rank"], 1)
        self.assertIsNone(by_name["far"]["find_rank"])


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
            self.assertEqual(info["names"], ["append_event", "browse", "find", "get", "ledger_activity", "ledger_query", "ledger_stats", "record", "save"], mode)
            ui = ((info["by_name"]["browse"].get("_meta") or {}).get("ui") or {})
            self.assertEqual(ui.get("resourceUri"), "ui://context-ledger/ledger-v2.html", mode)
            self.assertEqual(ui.get("visibility"), ["model", "app"], mode)
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
            self.assertIn("applied", append_schema)
            self.assertIn("applied_event_id", append_schema)
            self.assertIn("decision_ids", append_schema)
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


class CursorDestTests(unittest.TestCase):
    def test_doctor_rejects_relative_plugin_token_dest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / ".cursor/plugins/cache/other-market/context-ledger/deadbeef"
            dest.mkdir(parents=True)
            (dest / "mcp.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "context-ledger": {
                                "type": "stdio",
                                "command": "./bin/context-ledger-mcp",
                                "args": ["--data", "${PLUGIN_DATA}", "serve"],
                                "cwd": "${PLUGIN_ROOT}",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            data = root / "data"
            data.mkdir()
            proc = _run(
                [sys.executable, str(BOOT), "--data", str(data), "doctor"],
                env={"CONTEXT_LEDGER_CURSOR_HOME": str(root)},
            )
            self.assertEqual(proc.returncode, 2, proc.stderr)
            payload = json.loads(proc.stderr)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"]["code"], "CURSOR_DEST")
            issues = payload["error"]["dests"][0]["issues"]
            self.assertIn("relative_command", issues)
            self.assertIn("literal_plugin_token", issues)

    def test_rewrite_discovers_cache_hash_and_clears_problems(self) -> None:
        sys.path.insert(0, str(ROOT))
        from context_ledger.cursor_dest import candidate_dests, cursor_dest_problems, rewrite_dest

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / ".cursor/plugins/cache/ingpoc-agent-plugins/context-ledger/abc123"
            launcher = dest / "bin" / "context-ledger-mcp"
            launcher.parent.mkdir(parents=True)
            launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            launcher.chmod(0o644)
            (dest / "mcp.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "context-ledger": {
                                "type": "stdio",
                                "command": "./bin/context-ledger-mcp",
                                "args": ["serve"],
                                "cwd": "${PLUGIN_ROOT}",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            found = candidate_dests(root)
            self.assertEqual(found, [dest])
            self.assertTrue(cursor_dest_problems([dest]))
            data = root / "ledger-data"
            result = rewrite_dest(dest, data_dir=data)
            self.assertTrue(result["ok"], result)
            server = json.loads((dest / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]["context-ledger"]
            self.assertEqual(server["command"], str(launcher.resolve()))
            self.assertEqual(server["cwd"], "./")
            self.assertEqual(server["args"], ["--data", str(data.resolve()), "serve"])
            self.assertEqual(cursor_dest_problems([dest]), [])
            self.assertTrue(os.access(launcher, os.X_OK))

    def test_cursor_plugin_root_command_is_valid(self) -> None:
        sys.path.insert(0, str(ROOT))
        from context_ledger.cursor_dest import cursor_dest_problems, rewrite_dest

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "dest"
            launcher = dest / "bin" / "context-ledger-mcp"
            launcher.parent.mkdir(parents=True)
            launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            launcher.chmod(0o755)

            def write(command: str, cwd: str) -> None:
                (dest / "mcp.json").write_text(
                    json.dumps(
                        {
                            "mcpServers": {
                                "context-ledger": {
                                    "type": "stdio",
                                    "command": command,
                                    "args": ["serve"],
                                    "cwd": cwd,
                                }
                            }
                        }
                    ),
                    encoding="utf-8",
                )

            write("${CURSOR_PLUGIN_ROOT}/bin/context-ledger-mcp", "${CURSOR_PLUGIN_ROOT}")
            self.assertEqual(cursor_dest_problems([dest]), [])
            write("${CURSOR_PLUGIN_ROOT}/bin/context-ledger-mcp", "./")
            self.assertEqual(cursor_dest_problems([dest]), [])
            write("./bin/context-ledger-mcp", "./")
            self.assertIn("relative_command", cursor_dest_problems([dest])[0]["issues"])
            write("${PLUGIN_ROOT}/bin/context-ledger-mcp", "${PLUGIN_ROOT}")
            issues = cursor_dest_problems([dest])[0]["issues"]
            self.assertIn("literal_plugin_token", issues)
            self.assertIn("relative_command", issues)
            write("${CURSOR_PLUGIN_ROOT}/bin/context-ledger-mcp", "${PLUGIN_DATA}")
            issues = cursor_dest_problems([dest])[0]["issues"]
            self.assertIn("literal_plugin_token", issues)
            self.assertIn("cwd", issues)
            write("${CURSOR_PLUGIN_ROOT}/bin/other", "${CURSOR_PLUGIN_ROOT}")
            self.assertIn("relative_command", cursor_dest_problems([dest])[0]["issues"])
            write("${CURSOR_PLUGIN_ROOT}/bin/context-ledger-mcp", "${CURSOR_PLUGIN_ROOT}")
            result = rewrite_dest(dest, data_dir=root / "data")
            self.assertTrue(result["ok"], result)
            server = json.loads((dest / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]["context-ledger"]
            self.assertEqual(server["command"], str(launcher.resolve()))
            self.assertEqual(server["cwd"], "./")
            self.assertEqual(cursor_dest_problems([dest]), [])

    def test_shipped_sh_resolver_override_is_healthy(self) -> None:
        sys.path.insert(0, str(ROOT))
        from context_ledger.cursor_dest import cursor_dest_problems

        self.assertEqual(cursor_dest_problems([ROOT]), [])

    def test_cursor_override_dest_and_legacy_rewrite(self) -> None:
        sys.path.insert(0, str(ROOT))
        from context_ledger.cursor_dest import cursor_dest_problems, rewrite_dest

        def tree(dest: Path) -> dict:
            snap = {}
            for path in sorted(dest.rglob("*")):
                if path.is_file():
                    mode = stat.S_IMODE(path.stat().st_mode)
                    snap[str(path.relative_to(dest))] = (path.read_bytes(), mode)
            return snap

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "override"
            (dest / "bin").mkdir(parents=True)
            launcher = dest / "bin" / "context-ledger-mcp"
            launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            launcher.chmod(0o755)
            (dest / "mcp.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "context-ledger": {
                                "type": "stdio",
                                "command": "./bin/context-ledger-mcp",
                                "args": ["serve"],
                                "cwd": "./",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            cursor = dest / ".cursor-plugin"
            cursor.mkdir()
            (cursor / "plugin.json").write_text(
                json.dumps(
                    {
                        "name": "context-ledger",
                        "version": "0.1.14",
                        "mcpServers": "./.cursor-plugin/mcp.json",
                    }
                ),
                encoding="utf-8",
            )
            (cursor / "mcp.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "context-ledger": {
                                "type": "stdio",
                                "command": "${CURSOR_PLUGIN_ROOT}/bin/context-ledger-mcp",
                                "args": ["serve"],
                                "cwd": "${CURSOR_PLUGIN_ROOT}",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            before = tree(dest)
            self.assertEqual(cursor_dest_problems([dest]), [])
            result = rewrite_dest(dest, data_dir=root / "unused-data")
            self.assertTrue(result["ok"], result)
            self.assertEqual(result.get("mode"), "cursor_override")
            self.assertEqual(tree(dest), before)
            self.assertFalse((root / "unused-data").exists())

            (cursor / "mcp.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "context-ledger": {
                                "type": "stdio",
                                "command": "./bin/context-ledger-mcp",
                                "args": ["serve"],
                                "cwd": "${CURSOR_PLUGIN_ROOT}",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            broken = tree(dest)
            problems = cursor_dest_problems([dest])
            self.assertIn("relative_command", problems[0]["issues"])
            result = rewrite_dest(dest, data_dir=root / "unused-data")
            self.assertFalse(result["ok"])
            self.assertEqual(tree(dest), broken)

            (cursor / "plugin.json").write_text(
                json.dumps({"mcpServers": "./.cursor-plugin/missing.json"}),
                encoding="utf-8",
            )
            problems = cursor_dest_problems([dest])
            self.assertIn("override_missing", problems[0]["issues"])
            missing = tree(dest)
            result = rewrite_dest(dest, data_dir=root / "unused-data")
            self.assertFalse(result["ok"])
            self.assertEqual(tree(dest), missing)

            legacy = root / "legacy"
            legacy_launcher = legacy / "bin" / "context-ledger-mcp"
            legacy_launcher.parent.mkdir(parents=True)
            legacy_launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            legacy_launcher.chmod(0o644)
            (legacy / "mcp.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "context-ledger": {
                                "type": "stdio",
                                "command": "./bin/context-ledger-mcp",
                                "args": ["serve"],
                                "cwd": "./",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            data = root / "ledger-data"
            result = rewrite_dest(legacy, data_dir=data)
            self.assertTrue(result["ok"], result)
            self.assertNotIn("mode", result)
            server = json.loads((legacy / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]["context-ledger"]
            self.assertEqual(server["command"], str(legacy_launcher.resolve()))
            self.assertEqual(server["cwd"], "./")
            self.assertEqual(server["args"], ["--data", str(data.resolve()), "serve"])
            self.assertEqual(cursor_dest_problems([legacy]), [])

    def test_tunnel_trap_initializes_fifo(self) -> None:
        text = (ROOT / "scripts/remote/tunnel.sh").read_text(encoding="utf-8")
        self.assertIn('FIFO=""', text)
        self.assertIn("${FIFO:-}", text)
        proc = subprocess.run(
            ["sh", "-n", str(ROOT / "scripts/remote/tunnel.sh")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
