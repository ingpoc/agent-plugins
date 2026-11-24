import contextlib
import copy
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
import manage_findings
import statechart
import test_bcl
sys.path.pop(0)


class LaneStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.live = Path(self.tmp.name) / "live.json"
        self.live.write_text(json.dumps({"tracking": {"plan": {"cycle": 1, "round": 1,
            "groups": [self.lane()]}}}))

    def lane(self, status="running", **extra):
        return {"id": "A", "finding_ids": ["ISSUE-01"], "status": status,
                "phase": "fix", "codex": {"implementer_session": "impl", **extra}}

    def test_blocked_display_uses_stored_reason(self):
        group = {"id": "A", "status": "blocked", "blocked_reason": "Waiting for owner"}
        self.assertEqual(render.lane_display(group, 1)["copy"], "Blocked · Waiting for owner")

    def test_deferred_alias_is_parked_and_uses_stored_target_round(self):
        group = {"id": "A", "status": "deferred", "parked_to": 4}
        self.assertEqual(render.plan_status(group), "parked")
        self.assertEqual(render.lane_display(group, 1)["copy"], "Parked → Round 4")
        manage_findings.validate_plan_group_fields(group, 1)
        self.assertEqual(group["status"], "parked")

    def test_set_plan_carries_runtime_block_reason(self):
        requested = [{"id": "A", "finding_ids": ["ISSUE-01"], "status": "blocked",
                      "blocked_reason": "Plan supplied reason"}]
        previous = {"groups": [{"id": "A", "finding_ids": ["ISSUE-01"],
                                "status": "blocked", "phase": "fix",
                                "blocked_reason": "Runtime owner dependency"}]}
        manage_findings.carry_plan_runtime_state(requested, previous, True)
        self.assertEqual(requested[0]["blocked_reason"], "Runtime owner dependency")

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
        def write_job(*args):
            operations = args[-1]
            writes.append(copy.deepcopy(operations))
            if operations[0]["action"] == "finish-job":
                document = json.loads(self.live.read_text())
                document["tracking"]["plan"]["groups"][0]["codex"] = {
                    "job": {"finished_phase_status": "passed"}}
                self.live.write_text(json.dumps(document))
        process = MagicMock(pid=90210)
        waits = {"n": 0}
        clock = {"t": 0.0}
        def wait(*_args, **_kwargs):
            waits["n"] += 1
            if waits["n"] == 1:
                clock["t"] += 60
                raise TimeoutExpired(["check"], 60)
            return 0
        process.wait.side_effect = wait
        process.poll.side_effect = lambda: 0 if waits["n"] >= 2 else None
        with patch.object(test_bcl.bcl_entrypoint.subprocess, "Popen", return_value=process), \
             patch.object(test_bcl.bcl_entrypoint, "_group_pids", return_value=set()), \
             patch.object(test_bcl.bcl_entrypoint.time, "monotonic", side_effect=lambda: clock["t"]), \
             patch.object(test_bcl.bcl_entrypoint, "_job_write", side_effect=write_job), \
             patch.object(test_bcl.bcl_entrypoint, "_utc_now", side_effect=["2026-11-18T09:00:00Z",
                   "2026-11-18T09:00:30Z", "2026-11-18T09:01:00Z", "2026-11-18T09:02:00Z"]):
            result = test_bcl.bcl_entrypoint._run_job({}, self.live, Path("report"), Path("base"),
                [self.lane()], "fix", ["ISSUE-01"], ["python3", "check.py"])
        self.assertEqual(result, 0)
        self.assertEqual([batch[0]["action"] for batch in writes],
                         ["start-job", "attach-job", "heartbeat-job", "finish-job"])
        self.assertEqual(writes[0][0]["job"]["heartbeat_at"], "2026-11-18T09:00:00Z")
        self.assertEqual(writes[1][0]["pid"], 90210)
        self.assertEqual(writes[2][0]["timestamp"], "2026-11-18T09:01:00Z")
        self.assertEqual(writes[-1][0]["exit_code"], 0)

    def test_heartbeat_write_conflict_keeps_child_running(self):
        process = MagicMock(pid=90211)
        waits = {"n": 0}
        clock = {"t": 0.0}
        def wait(*_args, **_kwargs):
            waits["n"] += 1
            if waits["n"] == 1:
                clock["t"] += 60
                raise TimeoutExpired(["check"], 60)
            return 0
        process.wait.side_effect = wait
        process.poll.side_effect = lambda: 0 if waits["n"] >= 2 else None
        conflict = RuntimeError("live tracker changed during snapshot; retry")
        output = SimpleNamespace(returncode=0, stderr="", stdout="")
        with patch.object(test_bcl.bcl_entrypoint.subprocess, "Popen", return_value=process), \
             patch.object(test_bcl.bcl_entrypoint, "_group_pids", return_value=set()), \
             patch.object(test_bcl.bcl_entrypoint.subprocess, "run", return_value=output), \
             patch.object(test_bcl.bcl_entrypoint.live_state, "snapshot", side_effect=[None, None, conflict, conflict, conflict, None]) as snapshot, \
             patch.object(test_bcl.bcl_entrypoint.time, "sleep"), \
             patch.object(test_bcl.bcl_entrypoint.time, "monotonic", side_effect=lambda: clock["t"]), \
             patch.object(test_bcl.bcl_entrypoint, "_utc_now", side_effect=["2026-11-18T09:00:00Z",
                   "2026-11-18T09:00:30Z", "2026-11-18T09:01:00Z", "2026-11-18T09:02:00Z"]), \
             contextlib.redirect_stderr(io.StringIO()) as errors:
            result = test_bcl.bcl_entrypoint._run_job({}, self.live, Path("report"), Path("base"),
                [self.lane()], "fix", ["ISSUE-01"], ["python3", "check.py"])
        self.assertEqual(result, 1)  # The mocked write path never records the finish receipt.
        self.assertEqual(snapshot.call_count, 6)
        process.terminate.assert_not_called()
        self.assertIn("heartbeat write failed; will retry", errors.getvalue())

    def test_final_write_failure_reports_child_exit_code(self):
        process = MagicMock(pid=90212)
        process.wait.return_value = 7
        process.poll.return_value = 7
        with patch.object(test_bcl.bcl_entrypoint.subprocess, "Popen", return_value=process), \
             patch.object(test_bcl.bcl_entrypoint, "_group_pids", return_value=set()), \
             patch.object(test_bcl.bcl_entrypoint, "_job_write", side_effect=[None, None, OSError("write failed"),
                   OSError("write failed"), OSError("write failed")]), \
             patch.object(test_bcl.bcl_entrypoint.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "child exited with code 7; final tracker write failed"):
                test_bcl.bcl_entrypoint._run_job({}, self.live, Path("report"), Path("base"),
                    [self.lane()], "fix", ["ISSUE-01"], ["python3", "check.py"])
        process.terminate.assert_not_called()

    def test_page_stale_when_live_lane_heartbeat_is_overdue(self):
        data = self.stale_data(12345)
        with tempfile.TemporaryDirectory() as tmp, patch.object(render.live_state, "_pid_is_running", return_value=True):
            page = render.render(data, Path(tmp), now=render.parse_instant("2026-11-18T10:00:00Z"))
        self.assertIn('class="tag-stale" title="No update for over 10 minutes">Stale', page)

    def test_stale_hidden_when_job_dead_and_no_update(self):
        data = self.stale_data(12345)
        with tempfile.TemporaryDirectory() as tmp, patch.object(render.live_state, "_pid_is_running", return_value=False):
            page = render.render(data, Path(tmp), now=render.parse_instant("2026-11-18T10:00:00Z"))
        self.assertIn('class="tag-stale" title="No update for over 10 minutes" hidden', page)

    def test_dead_unfinished_review_job_renders_interrupted(self):
        data = self.stale_data(12345)
        group = data["tracking"]["plan"]["groups"][0]
        group.update(status="review", phase="review")
        group["codex"]["job"]["phase"] = "review"
        with tempfile.TemporaryDirectory() as tmp, patch.object(render.live_state, "_pid_is_running", return_value=False):
            page = render.render(data, Path(tmp), now=render.parse_instant("2026-11-18T10:00:00Z"))
        self.assertIn('class="grp interrupted"', page)
        self.assertIn("Interrupted · resume at review", page)
        self.assertNotIn("In review · ISSUE-01", page)

    def test_dead_unfinished_presuite_job_renders_interrupted(self):
        data = self.stale_data(12345)
        group = data["tracking"]["plan"]["groups"][0]
        group.update(status="review", phase="review", presuite={"status":"running"})
        group["codex"]["job"]["phase"] = "presuite"
        with tempfile.TemporaryDirectory() as tmp, patch.object(render.live_state, "_pid_is_running", return_value=False):
            page = render.render(data, Path(tmp), now=render.parse_instant("2026-11-18T10:00:00Z"))
        self.assertIn('class="grp interrupted"', page)
        self.assertIn("Interrupted · resume at presuite", page)
        self.assertNotIn("In review · ISSUE-01", page)

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

    def install_ready(self, group):
        document = {"findings": [{"id": "ISSUE-01"}], "tracking": {"plan": {"groups": [group]}}}
        payload = manage_findings.release_check_payload(document, ["ISSUE-01"], require_presuite=True)
        return payload["clear"] and payload["included"] == [{"id": "A", "finding_ids": ["ISSUE-01"]}]

    def test_install_readiness_refuses_before_review(self):
        group = self.lane("review", fix_status="passed", review_status="pending")
        group.update(phase="review", presuite={"status": "passed"})
        self.assertFalse(self.install_ready(group))

    def test_one_file_lane_skips_review(self):
        group = self.lane("planned", one_file=True, fix_status="passed")
        self.assertEqual(check_install_sessions.validate_review_sessions({"groups": [group]}, ["ISSUE-01"]), ["A"])
        group.update(status="review", phase="review")
        self.assertFalse(self.install_ready(group))
        group["codex"]["review_status"] = "skipped_one_file"
        group["presuite"] = {"status": "passed"}
        self.assertTrue(self.install_ready(group))

    def test_stale_hidden_for_completed_receipt_with_live_pid(self):
        data = self.stale_data(12345)
        data["tracking"]["plan"]["groups"][0]["codex"]["job"]["exit_code"] = 0
        with tempfile.TemporaryDirectory() as tmp, patch.object(render.live_state, "_pid_is_running", return_value=True):
            page = render.render(data, Path(tmp), now=render.parse_instant("2026-11-18T10:00:00Z"))
        self.assertIn('class="tag-stale" title="No update for over 10 minutes" hidden', page)

    def test_dead_unfinished_fix_job_renders_as_interrupted_and_resume_needed(self):
        data = self.stale_data(87654321)
        with tempfile.TemporaryDirectory() as tmp, patch.object(render.live_state, "_pid_is_running", return_value=False):
            page = render.render(data, Path(tmp), now=render.parse_instant("2026-11-18T10:00:00Z"))
            status = render.lane_issue_status(data["tracking"]["plan"]["groups"][0], 1)
        self.assertEqual(status["copy"], "Interrupted · resume at fix · A")
        self.assertIn('class="grp interrupted"', page)
        self.assertIn('Interrupted · resume at fix', page)
        self.assertNotIn('Fixing · ISSUE-01', page)

    def test_install_requires_passed_presuite(self):
        group = self.lane("review", fix_status="passed", review_status="passed")
        group["phase"] = "review"
        for suite in (None, {"status": "failed", "exit_code": 1}, {"status": "running"}):
            group.pop("presuite", None)
            if suite is not None: group["presuite"] = suite
            with self.subTest(suite=suite):
                self.assertFalse(self.install_ready(group))
        group["presuite"] = {"status": "passed"}
        self.assertTrue(self.install_ready(group))

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



    def test_renderer_reads_lane_copy_tally_and_step_chips_from_the_lane_table(self):
        group = {"id": "A", "finding_ids": ["ISSUE-01"], "status": "installing", "phase": "install",
                 "install": {"started_at": "2026-11-18T08:00:00Z"}, "codex": {}}
        self.assertEqual(render.lane_display(group, 1)["copy"], "In install · A")
        with patch.dict(render.LANE["status_copy"], {"installing": "Shipping"}), \
                patch.dict(render.LANE, {"tally": (("installing", "shipping now", ""),)}):
            self.assertEqual(render.lane_display(group, 1)["copy"], "Shipping · A")
            page = render.render_plan({"findings": []}, {}, {"round": 1, "groups": [group]}, 1, None)
            self.assertIn("<b>1</b> shipping now", page)
        deferred = {"id": "B", "finding_ids": ["ISSUE-02"], "status": "deferred", "parked_to": 2,
                    "codex": {"review_status": "changes_requested"}}
        self.assertEqual(render.lane_display(deferred, 1)["copy"], "Parked → Round 2")
        with patch.dict(render.LANE["groups"], {"busy": frozenset()}):
            self.assertNotIn('class="spinner"', render.render_plan({"findings": []}, {}, {"round": 1, "groups": [group]}, 1, None))

if __name__ == "__main__":
    unittest.main()
