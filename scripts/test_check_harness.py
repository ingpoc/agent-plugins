#!/usr/bin/env python3
"""Harness discovery, allow-list, and fail-closed comet deps."""

from __future__ import annotations

import builtins
import contextlib
import io
import json
import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import check
import check_inventory

MAIN_LAYOUT = """
plugins/agent-computer-use/runtime/voice-cua/tests/test_voice_cua.py
plugins/agent-computer-use/skills/macos-cua/service/Tests/CUAServiceTests/test_cua_client.py
plugins/agent-computer-use/skills/macos-cua/tests/test_bench_rating.py
plugins/agent-computer-use/skills/macos-cua/tests/test_benchmarks.py
plugins/agent-computer-use/skills/macos-cua/tests/test_cli_parser.py
plugins/agent-computer-use/skills/macos-cua/tests/test_compact_mcp.py
plugins/agent-computer-use/skills/macos-cua/tests/test_cua_service_health.py
plugins/agent-computer-use/skills/macos-cua/tests/test_cuaservice_runtime.py
plugins/agent-computer-use/skills/macos-cua/tests/test_fail_feedback.py
plugins/agent-computer-use/skills/macos-cua/tests/test_fast_path.py
plugins/agent-computer-use/skills/macos-cua/tests/test_jev_act.py
plugins/agent-computer-use/skills/macos-cua/tests/test_label_resolve.py
plugins/agent-computer-use/skills/macos-cua/tests/test_live_computer_parity.py
plugins/agent-computer-use/skills/macos-cua/tests/test_live_pointer_isolation.py
plugins/agent-computer-use/skills/macos-cua/tests/test_macos_cua.py
plugins/agent-computer-use/skills/macos-cua/tests/test_plan_contract.py
plugins/agent-computer-use/skills/macos-cua/tests/test_plugin_package.py
plugins/agent-computer-use/skills/macos-cua/tests/test_proof_cache.py
plugins/agent-computer-use/skills/macos-cua/tests/test_secondary_display_regressions.py
plugins/agent-computer-use/skills/macos-cua/tests/test_state_delta.py
plugins/agent-computer-use/skills/macos-cua/tests/test_state_diff.py
plugins/agent-computer-use/skills/macos-cua/tests/test_visible_typing.py
plugins/agent-computer-use/skills/macos-cua/tests/test_visual_focus_lock.py
plugins/comet-control/plugin/comet_control/tests/test_agent_cli.py
plugins/comet-control/plugin/comet_control/tests/test_broker.py
plugins/comet-control/plugin/comet_control/tests/test_broker_script.py
plugins/comet-control/plugin/comet_control/tests/test_browser_use_cdp_bridge.py
plugins/comet-control/plugin/comet_control/tests/test_call_counter_ax.py
plugins/comet-control/plugin/comet_control/tests/test_comet_capabilities.py
plugins/comet-control/plugin/comet_control/tests/test_cua_coexistence.py
plugins/comet-control/plugin/comet_control/tests/test_dashboard_interactions.py
plugins/comet-control/plugin/comet_control/tests/test_developer_diagnostics.py
plugins/comet-control/plugin/comet_control/tests/test_durable_lease_controller.py
plugins/comet-control/plugin/comet_control/tests/test_expect_value_inputs.py
plugins/comet-control/plugin/comet_control/tests/test_form_plan.py
plugins/comet-control/plugin/comet_control/tests/test_handoff_hardening.py
plugins/comet-control/plugin/comet_control/tests/test_lease_driver.py
plugins/comet-control/plugin/comet_control/tests/test_loaded_build_hash.py
plugins/comet-control/plugin/comet_control/tests/test_multi_agent_isolation.py
plugins/comet-control/plugin/comet_control/tests/test_navigation_waiter.py
plugins/comet-control/plugin/comet_control/tests/test_razorpay_bank_popup.py
plugins/comet-control/plugin/comet_control/tests/test_remote_debugging_check.py
plugins/comet-control/plugin/comet_control/tests/test_settle_preflight.py
plugins/comet-control/plugin/comet_control/tests/test_source_contracts.py
plugins/context-ledger/tests/test_contract.py
plugins/context-ledger/tests/test_http_server.py
plugins/context-ledger/tests/test_manifests.py
plugins/context-ledger/tests/test_remote_bridge.py
plugins/context-ledger/tests/test_server_logging.py
plugins/irctc-train-booking/tests/test_pay_gate.py
scripts/test_refresh_cursor_marketplace_index.py
""".split()

PACKAGE = """\
// swift-tools-version: 5.9
import PackageDescription
let package = Package(name: "CUAService", targets: [.testTarget(name: "CUAServiceTests")])
"""


def _layout(root: Path) -> None:
    for rel in MAIN_LAYOUT:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "import unittest\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n",
            encoding="utf-8",
        )
    package = root / "plugins/agent-computer-use/skills/macos-cua/service/Package.swift"
    package.write_text(PACKAGE, encoding="utf-8")
    decoy = root / "plugins/agent-computer-use/skills/macos-cua/service/.build/checkouts/vendor/test_vendor.py"
    decoy.parent.mkdir(parents=True, exist_ok=True)
    decoy.write_text("import unittest\n", encoding="utf-8")


class InventoryTests(unittest.TestCase):
    def test_discover_roots_follow_tests_segment(self) -> None:
        rows = {
            "plugins/context-ledger/tests/test_contract.py": "plugins/context-ledger/tests",
            "plugins/comet-control/plugin/comet_control/tests/test_broker.py": (
                "plugins/comet-control/plugin/comet_control/tests"
            ),
            "plugins/agent-computer-use/skills/macos-cua/tests/test_macos_cua.py": (
                "plugins/agent-computer-use/skills/macos-cua/tests"
            ),
            "plugins/agent-computer-use/runtime/voice-cua/tests/test_voice_cua.py": (
                "plugins/agent-computer-use/runtime/voice-cua/tests"
            ),
        }
        for rel, start in rows.items():
            found = check_inventory.discover_start(Path(rel))
            self.assertEqual(found.as_posix(), start, rel)
        singleton = Path(
            "plugins/agent-computer-use/skills/macos-cua/service/Tests/CUAServiceTests/test_cua_client.py"
        )
        self.assertIsNone(check_inventory.discover_start(singleton))

    def test_audit_matches_inventory_on_main_layout(self) -> None:
        self.assertEqual(len(MAIN_LAYOUT), 51)
        with self._tmp() as root:
            _layout(root)
            allow = check_inventory.load_allowlist(check_inventory.DEFAULT_ALLOWLIST)
            darwin = check_inventory.audit(root, allowlist=allow, platform="darwin")
            linux = check_inventory.audit(root, allowlist=allow, platform="linux")
        self.assertEqual(darwin["python"], {"found": 51, "run": 51, "skipped_allowed": 0, "skipped_unallowed": 0})
        self.assertEqual(darwin["swift"], {"found": 1, "run": 1, "skipped_allowed": 0, "skipped_unallowed": 0})
        self.assertEqual(darwin["harness_skips"], [])
        self.assertEqual(linux["python"]["found"], 51)
        self.assertEqual(linux["python"]["run"], 50)
        self.assertEqual(linux["python"]["skipped_allowed"], 1)
        self.assertEqual(linux["python"]["skipped_unallowed"], 0)
        self.assertEqual(linux["swift"], {"found": 1, "run": 0, "skipped_allowed": 1, "skipped_unallowed": 0})
        self.assertEqual(linux["harness_skips"], [])

    def test_allowlist_rejects_bad_entries(self) -> None:
        cases = [
            {"version": 1, "unittest_skips": [{"id": "a.b", "reason": "short"}]},
            {"version": 1, "unittest_skips": [{"id": "a.b", "reason": "long enough reason"}, {"id": "a.b", "reason": "long enough reason"}]},
            {"version": 1, "harness_skips": [{"id": "swift:x", "reason": "long enough reason", "when": "rm -rf /"}]},
            {"version": 2, "unittest_skips": []},
        ]
        for data in cases:
            with self._tmp() as root:
                path = root / "allow.json"
                path.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(check_inventory.AllowlistError):
                    check_inventory.load_allowlist(path)

    def test_unlisted_unittest_skip_fails(self) -> None:
        with self._tmp() as root:
            sample = root / "test_sample_skip.py"
            sample.write_text(
                textwrap.dedent(
                    """\
                    import unittest
                    class Sample(unittest.TestCase):
                        def test_skips(self):
                            self.skipTest("intentional in the sample only")
                    """
                ),
                encoding="utf-8",
            )
            empty = root / "empty.json"
            empty.write_text(json.dumps({"version": 1, "unittest_skips": [], "harness_skips": []}), encoding="utf-8")
            denied = self._run(empty, sample)
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn("unallowed unittest skip test_sample_skip.Sample.test_skips", denied.stderr)
            allowed = root / "allowed.json"
            allowed.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "unittest_skips": [
                            {
                                "id": "test_sample_skip.Sample.test_skips",
                                "reason": "sample skip is the allow-list fixture",
                            }
                        ],
                        "harness_skips": [],
                    }
                ),
                encoding="utf-8",
            )
            accepted = self._run(allowed, sample)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)

    def test_harness_skip_of_every_case_fails(self) -> None:
        with self._tmp() as root:
            sample = root / "test_only_skip.py"
            sample.write_text(
                "import unittest\nclass Only(unittest.TestCase):\n    def test_one(self):\n        pass\n",
                encoding="utf-8",
            )
            allow = root / "allow.json"
            allow.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "unittest_skips": [],
                        "harness_skips": [
                            {
                                "id": "test_only_skip.Only.test_one",
                                "reason": "fixture proves an empty kept suite is not a pass",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            denied = self._run(allow, sample)
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn("unittest harness skipped every test", denied.stderr)

    def test_main_only_scripts_are_not_passing_runs(self) -> None:
        with self._tmp() as root:
            script = root / "plugins" / "demo" / "tests" / "test_live_only.py"
            script.parent.mkdir(parents=True)
            script.write_text(
                "def main():\n    return 0\n\nif __name__ == '__main__':\n    raise SystemExit(main())\n",
                encoding="utf-8",
            )
            unit = root / "plugins" / "demo" / "tests" / "test_real.py"
            unit.write_text(
                "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n",
                encoding="utf-8",
            )
            self.assertEqual(
                [path.name for path in check_inventory.python_test_files(root)],
                ["test_real.py"],
            )
            report = check_inventory.audit(root, platform="linux", env={})
            self.assertEqual(report["script_files"], ["plugins/demo/tests/test_live_only.py"])
            self.assertEqual(report["python"]["found"], 1)
            empty = root / "empty.json"
            empty.write_text(
                json.dumps({"version": 1, "unittest_skips": [], "harness_skips": []}),
                encoding="utf-8",
            )
            denied = self._run(empty, script)
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn("unittest load collected zero tests", denied.stderr)

    def test_repo_live_scripts_stay_out_of_unittest_runs(self) -> None:
        rels = {
            run.path.relative_to(ROOT).as_posix() for run in check_inventory.python_runs(ROOT)
        }
        for rel in (
            "plugins/agent-computer-use/skills/macos-cua/tests/test_live_computer_parity.py",
        ):
            self.assertNotIn(rel, rels)
            self.assertIn(rel, check_inventory.script_files(ROOT))

    def test_comet_missing_deps_not_allowlisted(self) -> None:
        text = check_inventory.DEFAULT_ALLOWLIST.read_text(encoding="utf-8")
        self.assertNotIn("websockets", text)
        self.assertNotIn("Pillow", text)
        self.assertNotIn('"PIL"', text)
        real = builtins.__import__

        def block(name, *args, **kwargs):
            if name in ("websockets", "PIL"):
                raise ImportError(name)
            return real(name, *args, **kwargs)

        builtins.__import__ = block
        try:
            code = check.run_tests(ROOT / "plugins" / "comet-control")
        finally:
            builtins.__import__ = real
        self.assertNotEqual(code, 0)

    def test_integration_suites_are_opt_in_and_reported(self) -> None:
        with self._tmp() as root:
            suite = root / "plugins" / "demo" / "tests" / "integration_live.py"
            suite.parent.mkdir(parents=True)
            suite.write_text(
                'INTEGRATION_REQUIRES = "macOS DemoService"\n'
                "import unittest\nclass L(unittest.TestCase):\n    def test_live(self):\n        pass\n",
                encoding="utf-8",
            )
            unit = root / "plugins" / "demo" / "tests" / "test_unit.py"
            unit.write_text(
                "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n",
                encoding="utf-8",
            )
            self.assertEqual(
                [path.name for path in check_inventory.python_test_files(root)], ["test_unit.py"]
            )
            suites = check_inventory.integration_suites(root)
            self.assertEqual([(s.path.name, s.requires) for s in suites], [("integration_live.py", "macOS DemoService")])
            report = check_inventory.audit(root, platform="linux", env={})
            self.assertEqual(report["integration_suites"], ["plugins/demo/tests/integration_live.py"])
            self.assertEqual(report["python"]["found"], 1)
            plugins = [root / "plugins" / "demo"]
            env = {k: v for k, v in os.environ.items() if k != check_inventory.INTEGRATION_ENV}
            out = io.StringIO()
            with (
                mock.patch.object(check, "ROOT", root),
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch.object(check, "_exec_python") as exec_python,
                contextlib.redirect_stdout(out),
            ):
                self.assertEqual(check.run_integration(plugins), 0)
            exec_python.assert_not_called()
            self.assertIn(
                "integration: skipped plugins/demo/tests/integration_live.py, needs macOS DemoService",
                out.getvalue(),
            )
            with (
                mock.patch.object(check, "ROOT", root),
                mock.patch.dict(os.environ, {check_inventory.INTEGRATION_ENV: "1"}),
                mock.patch.object(check, "_exec_python", return_value=1) as exec_python,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(check.run_integration(plugins), 1)
            run = exec_python.call_args.args[0]
            self.assertEqual((run.path, run.mode), (suite, "module"))
            suite.write_text(
                "import unittest\nclass L(unittest.TestCase):\n    def test_live(self):\n        pass\n",
                encoding="utf-8",
            )
            err = io.StringIO()
            with (
                mock.patch.object(check, "ROOT", root),
                contextlib.redirect_stderr(err),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(check.run_integration(plugins), 1)
            self.assertIn("must declare INTEGRATION_REQUIRES", err.getvalue())

    def test_integration_skip_under_strict_runner_fails(self) -> None:
        with self._tmp() as root:
            sample = root / "integration_sample.py"
            sample.write_text(
                'INTEGRATION_REQUIRES = "nothing real"\n'
                "import unittest\nclass L(unittest.TestCase):\n    def test_live(self):\n"
                '        self.skipTest("service missing")\n',
                encoding="utf-8",
            )
            empty = root / "empty.json"
            empty.write_text(json.dumps({"version": 1, "unittest_skips": [], "harness_skips": []}), encoding="utf-8")
            denied = self._run(empty, sample)
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn("unallowed unittest skip", denied.stderr)

    def test_repo_cua_client_live_cases_are_an_integration_suite(self) -> None:
        service = ROOT / "plugins/agent-computer-use/skills/macos-cua/service/Tests/CUAServiceTests"
        unit = service / "test_cua_client.py"
        live = service / "integration_cua_client.py"
        self.assertNotIn("SkipTest", unit.read_text(encoding="utf-8"))
        self.assertNotIn("skipTest", unit.read_text(encoding="utf-8"))
        rels = {run.path for run in check_inventory.python_runs(ROOT)}
        self.assertIn(unit, rels)
        self.assertNotIn(live, rels)
        suites = {s.path: s.requires for s in check_inventory.integration_suites(ROOT)}
        self.assertIn("macOS CUAService", suites[live])

    def _run(self, allowlist: Path, sample: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "run_strict.py"),
                "--allowlist",
                str(allowlist),
                "-q",
                "module",
                str(sample),
            ],
            cwd=sample.parent,
            capture_output=True,
            text=True,
        )

    def _tmp(self):
        return tempfile_cm()


class tempfile_cm:
    def __enter__(self) -> Path:
        import tempfile

        self._dir = tempfile.TemporaryDirectory()
        return Path(self._dir.name)

    def __exit__(self, *exc) -> None:
        self._dir.cleanup()


class HookEnvironmentTests(unittest.TestCase):
    def test_each_child_has_private_activity_log_even_with_production_override(self):
        from types import SimpleNamespace
        run = SimpleNamespace(path=ROOT / "scripts/test_check_harness.py", cwd=ROOT,
                              mode="module", start=None)
        seen = []
        def child(_cmd, *, cwd, env):
            path = Path(env["VOICE_CUA_ACTIVITY_LOG"])
            self.assertNotEqual(str(path), "/production/activity.jsonl")
            self.assertTrue(path.parent.is_dir())
            path.write_text("fixture")
            seen.append(path)
            return SimpleNamespace(returncode=0)
        with mock.patch.object(check.subprocess, "run", side_effect=child):
            for _ in range(2):
                self.assertEqual(check._exec_python(run, {"VOICE_CUA_ACTIVITY_LOG": "/production/activity.jsonl"}), 0)
        self.assertNotEqual(seen[0], seen[1])
        self.assertTrue(all(not path.exists() for path in seen))

    def test_child_tests_preserve_home_but_drop_hook_repository_overrides(self):
        env = dict(os.environ, GIT_DIR="parent", GIT_INDEX_FILE="parent-index",
                   GIT_WORK_TREE="parent-tree", FIXTURE_SETTING="keep")
        result = check.test_environment(env)
        for key in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE"):
            self.assertNotIn(key, result)
        self.assertEqual(result["HOME"], os.environ["HOME"])
        self.assertEqual(result["FIXTURE_SETTING"], "keep")

    def test_swift_dependency_commands_receive_isolated_environment(self):
        package = ROOT/"plugins/agent-computer-use/skills/macos-cua/service"
        with mock.patch.object(check.sys, "platform", "darwin"), \
             mock.patch.object(check.check_inventory, "swift_packages", return_value=[package]), \
             mock.patch.dict(os.environ, {"GIT_DIR":"parent", "GIT_INDEX_FILE":"parent-index"}), \
             mock.patch.object(check.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            self.assertEqual(check.run_swift([ROOT/"plugins/agent-computer-use"]),0)
        self.assertNotIn("GIT_DIR", run.call_args.kwargs["env"])
        self.assertNotIn("GIT_INDEX_FILE", run.call_args.kwargs["env"])


if __name__ == "__main__":
    unittest.main()
