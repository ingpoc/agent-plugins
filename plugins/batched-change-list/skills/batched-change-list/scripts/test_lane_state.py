import contextlib
import io
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from subprocess import TimeoutExpired

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))
import render
import check_install_sessions
import statechart
import test_bcl
sys.path.pop(0)


class LaneStateTests(unittest.TestCase):
    def lane(self, status="running", **extra):
        return {"id": "A", "finding_ids": ["ISSUE-01"], "status": status,
                "phase": "fix", "codex": {"implementer_session": "impl", **extra}}

    def stale_data(self, pid):
        data = json.loads((ROOT / "examples/findings.json").read_text())
        old = "2026-11-18T08:00:00Z"
        data["tracking"] = {"workflow": {"stage": "review", "updated_at": old,
                                             "now": old, "stale_after_min": 10},
                            "plan": {"cycle": 1, "round": 1, "groups": [{
                                "id": "A", "finding_ids": [data["findings"][0]["id"]],
                                "status": "running", "phase": "fix", "codex": {"job": {
                                    "pid": pid, "phase": "fix", "started_at": old, "heartbeat_at": old}}}]}}
        return data

    def test_job_records_pid_heartbeat_and_exit(self):
        writes = []
        process = MagicMock(pid=90210)
        process.wait.side_effect = [TimeoutExpired(["check"], 60), 0]
        with patch.object(test_bcl.bcl_entrypoint.subprocess, "Popen", return_value=process), \
             patch.object(test_bcl.bcl_entrypoint, "_job_write", side_effect=lambda *args: writes.append(args[-1])), \
             patch.object(test_bcl.bcl_entrypoint, "_utc_now", side_effect=["2026-11-18T09:00:00Z",
                   "2026-11-18T09:01:00Z", "2026-11-18T09:02:00Z"]):
            result = test_bcl.bcl_entrypoint._run_job({}, Path("live"), Path("report"), Path("base"),
                [self.lane()], "fix", ["ISSUE-01"], ["python3", "check.py"])
        self.assertEqual(result, 0)
        jobs = [op["patch"]["codex"]["job"] for batch in writes for op in batch
                if op.get("action") == "update-plan-group"]
        self.assertEqual(len(jobs), 3)
        self.assertEqual(jobs[0]["pid"], 90210)
        self.assertEqual(jobs[1]["heartbeat_at"], "2026-11-18T09:01:00Z")
        self.assertEqual(jobs[-1]["exit_code"], 0)
        self.assertEqual(jobs[-1]["duration_s"], 120)
        self.assertEqual(jobs[-1]["finished_phase_status"], "passed")

    def test_heartbeat_write_conflict_keeps_child_running(self):
        process = MagicMock(pid=90211)
        process.wait.side_effect = [TimeoutExpired(["check"], 60), 0]
        conflict = RuntimeError("live tracker changed during snapshot; retry")
        output = SimpleNamespace(returncode=0, stderr="", stdout="")
        with patch.object(test_bcl.bcl_entrypoint.subprocess, "Popen", return_value=process), \
             patch.object(test_bcl.bcl_entrypoint.subprocess, "run", return_value=output), \
             patch.object(test_bcl.bcl_entrypoint.live_state, "snapshot", side_effect=[None, None, conflict, conflict, conflict, None]) as snapshot, \
             patch.object(test_bcl.bcl_entrypoint.time, "sleep"), \
             patch.object(test_bcl.bcl_entrypoint, "_utc_now", side_effect=["2026-11-18T09:00:00Z",
                   "2026-11-18T09:01:00Z", "2026-11-18T09:02:00Z"]), \
             contextlib.redirect_stderr(io.StringIO()) as errors:
            result = test_bcl.bcl_entrypoint._run_job({}, Path("live"), Path("report"), Path("base"),
                [self.lane()], "fix", ["ISSUE-01"], ["python3", "check.py"])
        self.assertEqual(result, 0)
        self.assertEqual(snapshot.call_count, 6)
        process.terminate.assert_not_called()
        self.assertIn("heartbeat write failed; will retry", errors.getvalue())

    def test_final_write_failure_reports_child_exit_code(self):
        process = MagicMock(pid=90212)
        process.wait.return_value = 7
        with patch.object(test_bcl.bcl_entrypoint.subprocess, "Popen", return_value=process), \
             patch.object(test_bcl.bcl_entrypoint, "_job_write", side_effect=[None, None, OSError("write failed"),
                   OSError("write failed"), OSError("write failed")]), \
             patch.object(test_bcl.bcl_entrypoint.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "child exited with code 7; final tracker write failed"):
                test_bcl.bcl_entrypoint._run_job({}, Path("live"), Path("report"), Path("base"),
                    [self.lane()], "fix", ["ISSUE-01"], ["python3", "check.py"])
        process.terminate.assert_not_called()

    def test_page_not_stale_while_lane_job_alive(self):
        data = self.stale_data(12345)
        with tempfile.TemporaryDirectory() as tmp, patch.object(render.live_state, "_pid_is_running", return_value=True):
            page = render.render(data, Path(tmp), now=render.parse_instant("2026-11-18T10:00:00Z"))
        self.assertIn('class="tag-stale" title="No update for over 10 minutes" hidden', page)

    def test_page_stale_when_job_dead_and_no_update(self):
        data = self.stale_data(12345)
        with tempfile.TemporaryDirectory() as tmp, patch.object(render.live_state, "_pid_is_running", return_value=False):
            page = render.render(data, Path(tmp), now=render.parse_instant("2026-11-18T10:00:00Z"))
        self.assertIn('class="tag-stale" title="No update for over 10 minutes">Stale', page)

    def test_recent_lane_heartbeat_prevents_stale(self):
        data = self.stale_data(12345)
        data["tracking"]["plan"]["groups"][0]["codex"]["job"]["heartbeat_at"] = "2026-11-18T09:59:00Z"
        with tempfile.TemporaryDirectory() as tmp, patch.object(render.live_state, "_pid_is_running", return_value=False):
            page = render.render(data, Path(tmp), now=render.parse_instant("2026-11-18T10:00:00Z"))
        self.assertIn('class="tag-stale" title="No update for over 10 minutes" hidden', page)

    def test_installing_label_only_after_install_starts(self):
        self.assertEqual(render.plan_status({"id": "A", "status": "installing", "finding_ids": []}), "planned")
        self.assertEqual(render.plan_phase({"id": "A", "status": "installing", "phase": "install",
            "finding_ids": []}), "fix")
        self.assertEqual(render.plan_status({"id": "A", "status": "installing", "finding_ids": [],
            "install": {"started_at": "2026-11-18T09:00:00Z"}}), "installing")

    def test_definition_has_review_between_fix_and_install(self):
        loop = render.WF.LOOP
        ids = [stage["id"] for stage in loop]
        self.assertLess(ids.index("plan_fix"), ids.index("review"))
        self.assertLess(ids.index("review"), ids.index("install"))
        self.assertEqual(render.WF.TRANSITIONS["install"], ["review"])

    def test_install_refuses_before_review(self):
        group = self.lane("planned", reviewer_session="reviewer", review_status="pending")
        with self.assertRaisesRegex(ValueError, "review_status='passed'"):
            check_install_sessions.validate_install_sessions({"groups": [group]}, ["ISSUE-01"])

    def test_one_file_lane_skips_review(self):
        group = self.lane("planned", one_file=True, fix_status="passed")
        self.assertEqual(check_install_sessions.validate_review_sessions({"groups": [group]}, ["ISSUE-01"]), ["A"])
        with self.assertRaises(ValueError):
            check_install_sessions.validate_install_sessions({"groups": [group]}, ["ISSUE-01"])
        group["codex"]["review_status"] = "skipped_one_file"
        group["presuite"] = {"status": "passed"}
        self.assertEqual(check_install_sessions.validate_install_sessions({"groups": [group]}, ["ISSUE-01"]), ["A"])

    def test_exited_job_pid_reuse_does_not_prevent_stale(self):
        data = self.stale_data(12345)
        data["tracking"]["plan"]["groups"][0]["codex"]["job"]["exit_code"] = 0
        with tempfile.TemporaryDirectory() as tmp, patch.object(render.live_state, "_pid_is_running", return_value=True):
            page = render.render(data, Path(tmp), now=render.parse_instant("2026-11-18T10:00:00Z"))
        self.assertIn('class="tag-stale" title="No update for over 10 minutes">Stale', page)

    def test_install_requires_passed_presuite(self):
        group = self.lane("review", reviewer_session="reviewer", review_status="passed")
        for suite in (None, {"status": "failed", "exit_code": 1}):
            if suite is not None: group["presuite"] = suite
            with self.subTest(suite=suite), self.assertRaisesRegex(ValueError, "pre-push suite"):
                check_install_sessions.validate_install_sessions({"groups": [group]}, ["ISSUE-01"])

    def test_strip_and_chart_read_review_from_definition(self):
        stage = next(stage for stage in render.WF.LOOP if stage["id"] == "review")
        original = stage["label"]
        stage["label"] = "Peer review"
        try:
            strip = render.render_workflow("review", [], 0, 15, False, 0, 1)
        finally:
            stage["label"] = original
        self.assertIn("Peer review", strip)
        self.assertIn("Pre-push suite", strip)
        definition = statechart.definition()
        next(stage for stage in definition["loop"] if stage["id"] == "review")["label"] = "Peer review"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "workflow_definition.json"
            path.write_text(json.dumps(definition))
            chart = statechart.render(definition_path=path)
        self.assertIn("Peer review", chart)
        self.assertIn('data-id="review"', chart)

    def test_router_has_review_row(self):
        rows = statechart.router((ROOT / "SKILL.md").read_text())
        row = next(row for row in rows if row["step"] == "Review")
        self.assertEqual(row["path"], "review.md")
        self.assertIn("bcl review", row["run"])
        self.assertIn("bcl job", row["run"])


if __name__ == "__main__":
    unittest.main()
