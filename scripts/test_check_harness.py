#!/usr/bin/env python3
"""Harness discovery, allow-list, and fail-closed comet deps."""

from __future__ import annotations

import builtins
import json
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

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
            "plugins/agent-computer-use/skills/macos-cua/tests/test_live_pointer_isolation.py",
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


if __name__ == "__main__":
    unittest.main()
