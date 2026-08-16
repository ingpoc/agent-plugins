"""Handoff hardening: one response per command, reclaim, cua_slice, controller drain."""

from __future__ import annotations

import importlib.util
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

WIP_ROOT = Path(__file__).resolve().parents[3]
LEASE_DRIVER = WIP_ROOT / "skills" / "comet-control" / "scripts" / "lease_driver.py"
CTRL = WIP_ROOT / "skills" / "comet-control" / "scripts" / "durable_lease_controller.py"
CUA_SLICE = WIP_ROOT / "skills" / "comet-control" / "scripts" / "cua_slice.py"
SERVICE_WORKER = (
    WIP_ROOT / "plugin" / "comet_control" / "extension" / "service_worker.js"
)


class HandoffHardeningTests(unittest.TestCase):
    def test_page_context_emits_handoff_hint_helper(self) -> None:
        source = SERVICE_WORKER.read_text()
        self.assertIn("detectNativeOverlayHandoffHint", source)
        self.assertIn("handoff_hint", source)
        self.assertIn("oauth_popup_or_native_overlay", source)

    def test_controller_has_status_and_closeout_drain(self) -> None:
        source = CTRL.read_text()
        self.assertIn("def cmd_status", source)
        self.assertIn('event == "closeout"', source)

    def test_failed_run_does_not_emit_second_stdout_event(self) -> None:
        source = LEASE_DRIVER.read_text()
        branch = source.split('if command == "run":', 1)[1].split(
            'elif command == "native_handoff":', 1
        )[0]
        self.assertIn('run_event["diagnostic"]', branch)
        self.assertNotIn('"event": "run_diagnostic"', branch)
        self.assertEqual(branch.count("reply("), 1)

    def test_native_handoff_requests_same_session_reclaim(self) -> None:
        source = LEASE_DRIVER.read_text()
        branch = source.split('elif command == "native_handoff":', 1)[1].split(
            'elif command == "sessions":', 1
        )[0]
        self.assertIn('"reclaim": True', branch)

    def test_extension_reclaims_orphan_native_dialog_for_same_session(self) -> None:
        source = SERVICE_WORKER.read_text()
        self.assertIn("sameSession", source)
        self.assertIn("reclaimed", source)
        self.assertIn("message.reclaim !== false", source)

    def test_controller_skips_legacy_run_diagnostic_lines(self) -> None:
        source = CTRL.read_text()
        self.assertIn('"run_diagnostic"', source)
        # Still skips renew/heartbeat
        self.assertIn('"renew"', source)

    def test_cua_slice_releases_claim_in_finally(self) -> None:
        source = CUA_SLICE.read_text()
        self.assertIn("def _release(", source)
        self.assertIn("finally:", source)
        self.assertIn("--release-claim", source)
        self.assertIn("comet_control_resume", source)
        self.assertIn("range(8)", source)  # drain desync / reclaim wait
        self.assertNotIn("leaseToken", source)
        self.assertNotIn("lease_token", source)

    def test_cua_slice_module_loads(self) -> None:
        spec = importlib.util.spec_from_file_location("cua_slice", CUA_SLICE)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        self.assertTrue(callable(mod.main))

    def test_cua_slice_uses_broker_attested_browser_pid(self) -> None:
        spec = importlib.util.spec_from_file_location("cua_slice", CUA_SLICE)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        probe = SimpleNamespace(stdout=json.dumps({"broker": {"browser_pid": 82356}}))
        with patch.object(mod.subprocess, "run", return_value=probe):
            self.assertEqual(mod._browser_pid(), 82356)


if __name__ == "__main__":
    unittest.main()
