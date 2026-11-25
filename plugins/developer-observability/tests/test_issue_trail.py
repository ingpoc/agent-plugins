import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
sys.path.insert(0, str(RUNTIME))
sys.path.insert(0, str(RUNTIME / "backend"))
from backend.issue_state import build_issues
from backend.round_view import _fold_jobs
from viewer import issues_payload


def _tracker(findings, runs=None, groups=None):
    return {"title": "Agent Plugins PR 121", "findings": findings, "tracking": {
        "repo": "agent-plugins", "pull_request": {"number": 121, "opened_at": "2026-11-24T08:00:00Z"},
        "round": 2, "cycle": 1, "round_history": [{"round": 1, "started_at": "2026-11-24T08:00:00Z",
                                                   "ended_at": "2026-11-24T10:00:00Z"}],
        "round_started_at": "2026-11-24T10:00:00Z",
        "plan": {"round": 2, "cycle": 1, "groups": groups or []},
        "verification_history": runs or []}}


class IssueTrailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        folder = self.state / "agent-plugins"
        folder.mkdir(parents=True)
        (folder / "121.json").write_text(json.dumps(_tracker([
            {"id": "DO-1", "title": "Fixed", "status": "verified_fixed",
             "first_seen": {"at": "2026-11-24T09:00:00Z", "pr": 121, "round": 1, "source": "check"},
             "state_changes": [{"at": "2026-11-24T10:30:00Z", "from": "untested", "to": "in_progress", "source": "tracker_update"},
                               {"at": "2026-11-24T11:05:00Z", "from": "in_progress", "to": "resolved", "source": "tracker_update"}]},
            {"id": "DO-2", "title": "Open", "status": "untested"},
            {"id": "DO-3", "title": "Emitted", "status": "verified_fixed", "created_at": "2026-11-24T09:30:00Z"},
        ], runs=[{"id": "r2-abc-121", "started_at": "2026-11-24T11:00:00Z", "finished_at": "2026-11-24T11:04:00Z",
                  "checks": {"DO-1": {"state": "fixed", "finished_at": "2026-11-24T11:04:00Z", "evidence": "passed"}}}],
           groups=[{"id": "R2-LANE", "status": "verify", "finding_ids": ["DO-1"]}])))
        jobs = self.state / "abc"
        jobs.mkdir()
        rows = [
            {"event": "start", "job_id": "j1", "phase": "fix", "finding_ids": ["DO-1"], "round": 2,
             "at": "2026-11-24T10:20:00Z", "started_at": "2026-11-24T10:20:00Z", "model": "gpt-5.1-codex", "model_version": "2026-11-01"},
            {"event": "finish", "job_id": "j1", "phase": "fix", "finding_ids": ["DO-1"], "round": 2, "at": "2026-11-24T10:40:00Z",
             "finished_at": "2026-11-24T10:40:00Z", "exit_code": 0, "tokens": {"input": 900, "output": 100, "total": 1000},
             "tokens_source": "stamp"},
            {"event": "start", "job_id": "j2", "phase": "review", "group_id": "R2-LANE", "at": "2026-11-24T10:45:00Z",
             "started_at": "2026-11-24T10:45:00Z", "model": "Grok Bot"},
            {"event": "finish", "job_id": "j2", "phase": "review", "group_id": "R2-LANE", "at": "2026-11-24T10:50:00Z",
             "finished_at": "2026-11-24T10:50:00Z", "exit_code": 0, "tokens": {"total": 50}, "tokens_source": "estimated"},
            {"event": "issue_failed_first", "finding_id": "DO-3", "run_id": "r1-x-121", "at": "2026-11-24T09:40:00Z",
             "evidence_ref": "check:r1-x-121:DO-3", "summary": "verify failed", "by_job_id": "j9"},
            {"event": "root_cause_identified", "finding_id": "DO-3", "run_id": "r1-x-121", "at": "2026-11-24T10:10:00Z",
             "evidence_ref": "job:j9", "summary": "stale cache key"},
            {"event": "fix_verified", "finding_id": "DO-3", "run_id": "r2-abc-121", "at": "2026-11-24T11:04:00Z",
             "evidence_ref": "check:r2-abc-121:DO-3", "summary": "passed"},
        ]
        (jobs / "events.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        os.environ["BCL_STATE_DIR"] = str(self.state)

    def tearDown(self):
        os.environ.pop("BCL_STATE_DIR", None)
        self.tmp.cleanup()

    def _issue(self, payload, identifier):
        return next(issue for issue in payload["issues"] if issue["id"] == identifier)

    def _steps(self, issue):
        return {(step["cycle"], step["step"]): step for step in issue["trail"]}

    def test_no_include_adds_nothing(self):
        result = build_issues("agent-plugins", scope="all")
        self.assertEqual(result["include"], [])
        self.assertNotIn("trail", self._issue(result, "DO-1"))
        self.assertNotIn("rollup", result)
        with self.assertRaises(ValueError):
            build_issues("agent-plugins", include="trail,bogus")

    def test_recorded_and_derived_steps_and_durations(self):
        issue = self._issue(build_issues("agent-plugins", scope="all", include="trail"), "DO-1")
        steps = self._steps(issue)
        first, cause, verified = steps[(1, "first_fail")], steps[(1, "root_cause")], steps[(1, "fix_verified")]
        self.assertEqual((first["provenance"], first["source"]), ("recorded", "findings[].first_seen"))
        # Lane plan time is the fallback root cause, marked derived (first job on the lane).
        self.assertEqual(cause["provenance"], "derived")
        self.assertEqual(cause["ts"], "2026-11-24T10:45:00Z")
        self.assertEqual(cause["field_provenance"]["run_id"], "recorded")
        self.assertEqual((verified["provenance"], verified["run_id"]), ("recorded", "r2-abc-121"))
        timing = issue["timing"][0]
        self.assertEqual(timing["ttr"], {"ms": 105 * 60000, "status": "done", "so_far_ms": None, "provenance": "derived"})
        self.assertEqual(timing["ttv"]["provenance"], "recorded")
        self.assertEqual(timing["ttv"]["ms"], 124 * 60000)

    def test_emitted_events_are_recorded_and_never_fold_into_jobs(self):
        issue = self._issue(build_issues("agent-plugins", scope="all", include="trail"), "DO-3")
        steps = self._steps(issue)
        self.assertEqual({step["provenance"] for step in issue["trail"]}, {"recorded"})
        self.assertEqual(steps[(1, "root_cause")]["what"], "Stale cache key")
        self.assertEqual(issue["timing"][0]["ttr"]["ms"], 30 * 60000)
        self.assertEqual({job["job_id"] for job in _fold_jobs()}, {"j1", "j2"})

    def test_open_issue_is_pending_not_null(self):
        issue = self._issue(build_issues("agent-plugins", scope="all", include="trail"), "DO-2")
        steps = self._steps(issue)
        self.assertEqual(steps[(1, "first_fail")]["provenance"], "missing")
        self.assertEqual(steps[(1, "root_cause")]["status"], "pending")
        self.assertEqual(steps[(1, "fix_verified")]["status"], "pending")
        self.assertEqual(issue["timing"][0]["ttv"]["status"], "open")

    def test_jobs_tokens_never_sum_official_and_estimated(self):
        result = issues_payload({"scope": ["all"], "include": ["trail,jobs,rollup"]})
        issue = self._issue(result, "DO-1")
        jobs = {row["job_id"]: row for row in issue["jobs_usage"]}
        self.assertEqual((jobs["j1"]["model"], jobs["j1"]["model_version"]), ("gpt-5.1-codex", "2026-11-01"))
        self.assertEqual((jobs["j1"]["tokens"], jobs["j1"]["tokens_source"], jobs["j1"]["link_provenance"]), (1000, "official", "recorded"))
        self.assertEqual((jobs["j2"]["model"], jobs["j2"]["tokens_source"], jobs["j2"]["link_provenance"]), (None, "estimated", "derived"))
        self.assertEqual(issue["tokens"], {"jobs": 2, "by_source": {"official": 1, "estimated": 1, "none": 0},
                                           "official": 1000, "estimated": 50})
        rows = {row["scope"]: row for row in result["rollup"]}
        self.assertEqual(rows["#121 R2"]["token_sums"], {"official": 1000, "estimated": 50})
        self.assertEqual(rows["#121 R2"]["closed"], 2)

    def test_detail_always_includes_trail_and_jobs_and_keeps_counts(self):
        listing = build_issues("agent-plugins", scope="all")
        detail = build_issues("agent-plugins", scope="all", issue_id="DO-1")
        self.assertTrue(listing["counts"])
        self.assertEqual(set(detail), {"schema_version", "project", "pr", "issue_id", "include", "issues"})
        self.assertEqual(len(detail["issues"]), 1)
        self.assertEqual(detail["include"], ["trail", "jobs"])
        self.assertIn("trail", detail["issues"][0])
        self.assertIn("raw", detail["issues"][0])
        self.assertIn("jobs_usage", detail["issues"][0])
        with self.assertRaises(ValueError):
            build_issues("agent-plugins", issue_id="NOPE-1")

    def test_regression_opens_a_second_cycle_and_inconclusive_does_not(self):
        tracker = self.state / "agent-plugins" / "121.json"
        document = json.loads(tracker.read_text())
        document["findings"][0]["state_changes"].append(
            {"at": "2026-11-24T11:30:00Z", "from": "resolved", "to": "regressed", "source": "owner_rejected"})
        document["tracking"]["verification_history"].append(
            {"id": "r3-def-121", "started_at": "2026-11-24T11:10:00Z", "finished_at": "2026-11-24T11:12:00Z",
             "checks": {"DO-1": {"state": "inconclusive", "finished_at": "2026-11-24T11:12:00Z"}}})
        tracker.write_text(json.dumps(document))
        issue = self._issue(build_issues("agent-plugins", scope="all", include="trail"), "DO-1")
        self.assertEqual(issue["cycle"], 2)
        steps = self._steps(issue)
        self.assertEqual(steps[(2, "regressed")]["ts"], "2026-11-24T11:30:00Z")
        self.assertEqual(steps[(2, "fix_verified")]["status"], "pending")
        self.assertEqual([row["kind"] for row in issue["after"]], ["inconclusive"])

    def test_past_round_replay_cuts_the_trail_at_round_end(self):
        result = build_issues("agent-plugins", pr="121", scope="round:1", include="trail")
        self.assertTrue(result["issues"])
        for issue in result["issues"]:
            for step in issue["trail"]:
                if step["ts"]:
                    self.assertLessEqual(step["ts"][:16], "2026-11-24T10:00")


if __name__ == "__main__":
    unittest.main()
