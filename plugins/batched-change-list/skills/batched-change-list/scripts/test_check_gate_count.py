import json
import contextlib
import importlib.util
from importlib.machinery import SourceFileLoader
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import check_gate_count as check
import manage_findings
import render
import test_bcl


class GateCountTests(unittest.TestCase):
    def tracker(self, count):
        return {"findings": [{"id": f"ISSUE-{i}", "status": "verified_fixed"} for i in range(count)], "tracking": {}}

    def test_rollover_waits_for_merge_threshold(self):
        self.assertEqual(check.rollover_gate_count(self.tracker(15)), (15, 16, False))
        self.assertEqual(check.rollover_gate_count(self.tracker(16)), (16, 16, True))

    def test_report_gate_unchanged_at_default_threshold(self):
        self.assertEqual(check.gate_count(self.tracker(15)), (15, 15, True))
        data = self.tracker(14)
        data["findings"].append({"id": "ISSUE-open", "status": "untested"})
        self.assertEqual(check.gate_count(data), (14, 15, False))

    def test_rollover_no_open_exception_removed(self):
        data = {"findings": [], "tracking": {}}
        self.assertTrue(check.gate_count(data)[2])
        self.assertFalse(check.verified_gate_count(data)[2])
        self.assertFalse(check.rollover_gate_count(data)[2])
        with self.assertRaisesRegex(ValueError, "rollover gate not met"):
            manage_findings.require_rollover_gate([], {}, False, "rollover")
        self.assertIsNone(manage_findings.require_rollover_gate([], {}, False, "set-stage"))

    def test_status_shows_report_and_rollover_gates(self):
        bcl_path = test_bcl.BCL
        loader = SourceFileLoader("bcl_status_test", str(bcl_path))
        spec = importlib.util.spec_from_loader("bcl_status_test", loader)
        bcl = importlib.util.module_from_spec(spec)
        loader.exec_module(bcl)
        with tempfile.TemporaryDirectory() as directory:
            data_path = Path(directory) / "tracker.json"
            data_path.write_text(json.dumps(self.tracker(15)))
            output = io.StringIO()
            with patch.object(bcl.live_state, "locate", return_value=("test", {"id": "test"}, data_path)), \
                 patch.object(bcl, "_ensure_viewer"), \
                 patch.object(sys, "argv", [str(bcl_path), "--data", str(data_path), "status"]), \
                 contextlib.redirect_stdout(output):
                bcl.main()
        status = json.loads(output.getvalue())
        self.assertEqual(status["report_gate"], {"fixed": 15, "threshold": 15, "met": True})
        self.assertEqual(status["verified_gate"], {"fixed": 15, "threshold": 15, "met": True})
        self.assertEqual(status["rollover_gate"], {"fixed": 15, "threshold": 16, "met": False})

    def test_status_keeps_empty_report_exception_separate_from_verified_gate(self):
        bcl_path = test_bcl.BCL
        loader = SourceFileLoader("bcl_empty_status_test", str(bcl_path))
        spec = importlib.util.spec_from_loader("bcl_empty_status_test", loader)
        bcl = importlib.util.module_from_spec(spec)
        loader.exec_module(bcl)
        with tempfile.TemporaryDirectory() as directory:
            data_path = Path(directory) / "tracker.json"
            data_path.write_text(json.dumps({"findings": [], "tracking": {}}))
            output = io.StringIO()
            with patch.object(bcl.live_state, "locate", return_value=("test", {"id": "test"}, data_path)), \
                 patch.object(bcl, "_ensure_viewer"), \
                 patch.object(sys, "argv", [str(bcl_path), "--data", str(data_path), "status"]), \
                 contextlib.redirect_stdout(output):
                bcl.main()
        status = json.loads(output.getvalue())
        self.assertTrue(status["report_gate"]["met"])
        self.assertEqual(status["verified_gate"], {"fixed": 0, "threshold": 15, "met": False})
        self.assertEqual(status["rollover_gate"], {"fixed": 0, "threshold": 16, "met": False})

    def test_status_current_stage_matches_active_lanes(self):
        bcl_path = test_bcl.BCL
        loader = SourceFileLoader("bcl_lane_status_test", str(bcl_path))
        spec = importlib.util.spec_from_loader("bcl_lane_status_test", loader)
        bcl = importlib.util.module_from_spec(spec)
        loader.exec_module(bcl)
        data = self.tracker(0)
        data["tracking"] = {"workflow": {"stage": "plan_fix"}, "round": 1,
                            "plan": {"round": 1, "groups": [
                                {"id": "A", "status": "review"},
                                {"id": "B", "status": "review"}]}}
        with tempfile.TemporaryDirectory() as directory:
            data_path = Path(directory) / "tracker.json"
            data_path.write_text(json.dumps(data))
            output = io.StringIO()
            with patch.object(bcl.live_state, "locate", return_value=("test", {"id": "test"}, data_path)), \
                 patch.object(bcl, "_ensure_viewer"), \
                 patch.object(sys, "argv", [str(bcl_path), "--data", str(data_path), "status"]), \
                 contextlib.redirect_stdout(output):
                bcl.main()
        status = json.loads(output.getvalue())
        self.assertEqual(status["stage"], "review")
        self.assertEqual(status["recorded_stage"], "plan_fix")

    def test_entry_point_rollover_boundaries_and_force(self):
        default = render.WF.DEFAULT_ROLLOVER_THRESHOLD
        merge = render.WF.MERGE_PREVIOUS_PR_THRESHOLD
        self.assertGreater(merge, default)
        for entry in ("manager", "bcl"):
            for count, force, succeeds in ((default, False, False), (merge, False, True), (default, True, True)):
                with self.subTest(entry=entry, count=count, force=force):
                    fixture = test_bcl.BclTests("test_verify_failure_reason_has_checked_expected_and_observed")
                    fixture.call = lambda stage, *args: fixture.call_without_viewer(stage)
                    try:
                        fixture.setUp()
                        live = Path(json.loads((fixture.state / "registry.json").read_text())["example/project/123"]["live"])
                        doc = fixture.merge_ready(json.loads(live.read_text()))
                        doc["findings"] = (doc["findings"] * ((count // len(doc["findings"])) + 1))[:count]
                        for index, item in enumerate(doc["findings"]): item["id"] = f"ISSUE-{index + 1:03}"
                        raw = json.dumps(doc).encode()
                        live.write_bytes(raw); fixture.data.write_bytes(raw)
                        if entry == "manager":
                            op = [{"action": "rollover", "pull_request": {"label": "PR #124", "url": "https://github.com/example/project/pull/124"}, "worktree": "next"}]
                            args = [sys.executable, str(Path(__file__).with_name("manage_findings.py")), "batch", str(live), "-"]
                            if force: args.extend(["--force", "--reason", "test override"])
                            result = subprocess.run(args, input=json.dumps(op), env=fixture.env, text=True, capture_output=True)
                        else:
                            args = ["--new-pr", "124", "--worktree", "next"]
                            if force: args.extend(["--force", "--reason", "test override"])
                            result = fixture.call_without_viewer("rollover", *args)
                        self.assertEqual(result.returncode == 0, succeeds, result.stderr)
                        records = json.loads((fixture.state / "registry.json").read_text())
                        self.assertEqual("example/project/124" in records, succeeds)
                        if not succeeds: self.assertIn("rollover gate not met", result.stderr)
                        if force:
                            new = json.loads(Path(records["example/project/124"]["live"]).read_text())
                            self.assertEqual(new["tracking"]["rollover"]["forced_gate_overrides"][-1]["action"], "rollover")
                    finally:
                        fixture.doCleanups()

    def test_bcl_report_at_default_and_no_open_exception(self):
        default = render.WF.DEFAULT_ROLLOVER_THRESHOLD
        for count, open_finding in ((default, True), (0, False)):
            with self.subTest(count=count, open_finding=open_finding):
                fixture = test_bcl.BclTests("test_verify_failure_reason_has_checked_expected_and_observed")
                fixture.call = lambda stage, *args: fixture.call_without_viewer(stage)
                try:
                    fixture.setUp()
                    live = Path(json.loads((fixture.state / "registry.json").read_text())["example/project/123"]["live"])
                    doc = fixture.merge_ready(json.loads(live.read_text()))
                    template = doc["findings"][0]
                    doc["findings"] = [{**template, "id": f"ISSUE-{i:03}"} for i in range(count)]
                    if open_finding: doc["findings"].append({**template, "id": "OPEN-01", "status": "untested"})
                    raw = json.dumps(doc).encode()
                    live.write_bytes(raw); fixture.data.write_bytes(raw)
                    result = fixture.call_without_viewer("report")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(live.read_text())["tracking"]["workflow"]["stage"], "report_rollover")
                finally:
                    fixture.doCleanups()

    def test_report_gate_keeps_no_open_exception_separate_from_ui_gate(self):
        data = {"findings": [], "tracking": {}}
        self.assertEqual(check.gate_count(data), (0, 15, True))
        self.assertEqual(check.verified_gate_count(data), (0, 15, False))

    def test_gate_waits_for_current_round_introspect_history_after_install(self):
        data = {"findings": [], "tracking": {"cycle": 2, "round": 3,
            "stages_reached": ["install"]}}
        self.assertEqual(check.gate_count(data), (0, 15, False))
        data["tracking"]["introspect_history"] = [{"cycle": 2, "round": 3,
            "result": "reviewed", "at": "2026-11-18T00:00:00Z", "steps": ["Verify"],
            "none": ["Verify"], "sections": {name: {"none": "Reviewed."}
                for name in ("delete", "simplify", "optimize", "automate")}}]
        self.assertEqual(check.gate_count(data), (0, 15, True))

    def test_merge_gate_count_uses_the_merge_threshold(self):
        def data(count):
            return {"findings": [{"id": f"ISSUE-{i}", "status": "verified_fixed"} for i in range(count)], "tracking": {}}
        self.assertEqual(check.merge_gate_count(data(15)), (15, 16, False))
        self.assertEqual(check.merge_gate_count(data(16)), (16, 16, True))

    def test_documented_gate_count_command_keeps_report_exception(self):
        self.assertEqual(check.gate_count({"findings": [], "tracking": {}}), (0, 15, True))
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "tracker.json"
            for count, expected in ((15, "Merge gate: 15 of 16; met=false"),
                                    (16, "Merge gate: 16 of 16; met=true")):
                tracker = {"findings": [{"id": f"ISSUE-{i}", "status": "verified_fixed"}
                                         for i in range(count)], "tracking": {}}
                data.write_text(json.dumps(tracker))
                result = subprocess.run([sys.executable, str(Path(__file__).with_name("check_gate_count.py")),
                                         "--data", str(data)], text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected, result.stdout)

    def test_invalid_tracker_fails_loudly(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "tracker.json"
            data.write_text("{}")
            result = subprocess.run([sys.executable, str(Path(__file__).with_name("check_gate_count.py")),
                                     "--data", str(data)], text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
