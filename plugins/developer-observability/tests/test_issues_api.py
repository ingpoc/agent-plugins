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
from viewer import issues_payload


class IssuesAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        folder = self.state / "agent-plugins"
        folder.mkdir(parents=True)
        self.tracker = folder / "121.json"
        self.tracker.write_text(json.dumps({
            "title": "Agent Plugins PR 121",
            "findings": [
                {"id": "DO-1", "title": "Untested", "status": "pending", "created_at": "2026-11-24T10:00:00Z"},
                {"id": "DO-2", "title": "Blocked", "status": "blocked", "failure_history": [
                    {"at": "2026-11-24T11:00:00Z", "source": "review", "reason": "Must fix"}]},
                {"id": "DO-3", "title": "Resolved", "status": "verified", "fixed_at": "2026-11-24T09:00:00Z"},
            ],
            "tracking": {"repo": "agent-plugins", "pull_request": {"number": 121, "opened_at": "2026-11-24T08:00:00Z"},
                         "round": 2, "round_started_at": "2026-11-24T10:00:00Z", "cycle": 1,
                         "gate": {"merge_previous_pr_threshold": 12},
                         "round_history": [{"round": 1, "ended_at": "2026-11-24T10:00:00Z"}],
                         "plan": {"round": 2, "groups": [{"id": "R2-DO2", "status": "blocked", "finding_ids": ["DO-2"]}]}}
        }))
        os.environ["BCL_STATE_DIR"] = str(self.state)

    def tearDown(self):
        os.environ.pop("BCL_STATE_DIR", None)
        self.tmp.cleanup()

    def test_api_defaults_to_now_and_returns_stored_gate_and_lifecycle(self):
        result = build_issues("agent-plugins")
        self.assertEqual(result["pr"], "121")
        self.assertEqual({row["id"] for row in result["issues"]}, {"DO-1", "DO-2"})
        self.assertEqual(result["gate"]["threshold"], 12)
        blocked = next(row for row in result["issues"] if row["id"] == "DO-2")
        self.assertEqual(blocked["state"], "blocked")
        self.assertEqual(blocked["last_seen_at"], "2026-11-24T11:00:00Z")

    def test_get_api_and_shared_scopes_return_same_issue_ids(self):
        via_http = issues_payload({"project": ["agent-plugins"], "pr": ["current"], "scope": ["all"]})
        via_tool_model = build_issues("agent-plugins", scope="all")
        self.assertEqual([row["id"] for row in via_http["issues"]], [row["id"] for row in via_tool_model["issues"]])
        self.assertEqual({row["id"] for row in via_http["issues"]}, {"DO-1", "DO-2", "DO-3"})

    def test_timeframe_is_project_wide_and_filters_by_issue_activity(self):
        result = build_issues("agent-plugins", from_time="2026-11-24T10:30:00+00:00", to_time="2026-11-24T11:30:00+00:00")
        self.assertEqual({row["id"] for row in result["issues"]}, {"DO-2"})
        self.assertEqual(result["issues"][0]["window_reason"], "reoccurred")

    def test_verification_run_checks_enter_the_timeline_and_window(self):
        tracker = json.loads(self.tracker.read_text())
        tracker["tracking"]["verification_history"] = [{"id": "verify-1", "checks": {
            "DO-1": {"state": "fixed", "finished_at": "2026-11-24T10:45:00Z", "summary": "Observed fixed"}}}]
        self.tracker.write_text(json.dumps(tracker))
        result = build_issues("agent-plugins", scope="all")
        issue = next(row for row in result["issues"] if row["id"] == "DO-1")
        self.assertEqual(issue["last_evidence"]["ref"], "check:verify-1:DO-1")
        window = build_issues("agent-plugins", from_time="2026-11-24T10:30:00Z", to_time="2026-11-24T11:00:00Z")
        issue = next(row for row in window["issues"] if row["id"] == "DO-1")
        self.assertEqual(issue["window_reason"], "fixed")


if __name__ == "__main__":
    unittest.main()
