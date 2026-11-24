import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
sys.path.insert(0, str(RUNTIME))
sys.path.insert(0, str(RUNTIME / "backend"))
from backend.issue_state import build_issues
from backend.issue_trail import _plain, build_trail


def _t(pr, findings, groups=None, carried=None, runs=None, merged=None):
    tracking = {"repo": "agent-plugins", "pull_request": {"number": pr, "opened_at": "2026-11-20T08:00:00Z",
                                                          **({"merged_at": merged, "state": "merged"} if merged else {})},
                "round": 1, "cycle": 1, "round_started_at": "2026-11-20T08:00:00Z",
                "round_history": [], "plan": {"round": 1, "cycle": 1, "groups": groups or []},
                "verification_history": runs or []}
    if carried:
        tracking["carried_gates"] = carried
    return {"title": f"Agent Plugins PR {pr}", "findings": findings, "tracking": tracking}


class TrailUnitTests(unittest.TestCase):
    END = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)

    def test_past_view_leaves_a_later_verify_pending(self):
        issue = {"id": "DO-2", "title": "Thing", "state": "resolved", "events": [], "raw": {}}
        later = [{"kind": "pass", "at": "2026-11-24T12:00:00Z", "ref": "check:r7:DO-2"}]
        result = build_trail(issue, [], [], "120.json", end_at=self.END, full_events=later)
        verify = [s for s in result["trail"] if s["step"] == "fix_verified"][0]
        self.assertEqual(verify["status"], "pending")
        self.assertEqual(verify["note"], "not yet at round end")

    def test_status_only_fallback_when_nothing_is_dated(self):
        issue = {"id": "DO-2", "title": "Thing", "state": "resolved", "events": [], "raw": {}}
        result = build_trail(issue, [], [], "120.json", end_at=self.END, full_events=[])
        verify = [s for s in result["trail"] if s["step"] == "fix_verified"][0]
        self.assertEqual((verify["status"], verify["provenance"]), ("done", "missing"))

    def test_later_inconclusive_has_neutral_wording(self):
        issue = {"id": "DO-2", "title": "Thing", "state": "resolved", "raw": {}, "events": [
            {"kind": "fail", "at": "2026-11-24T08:00:00Z"}, {"kind": "pass", "at": "2026-11-24T09:00:00Z", "ref": "check:r6:DO-2"},
            {"kind": "inconclusive", "at": "2026-11-24T09:30:00Z", "ref": "check:r7:DO-2"}]}
        result = build_trail(issue, [], [], "120.json")
        self.assertEqual([a["text"] for a in result["after"]], ["Verify inconclusive after the pass"])
        self.assertNotIn("did not reopen", json.dumps(result))

    def test_plain_summary_drops_markdown_paths_and_title_repeats(self):
        text = "1. [`collect.py:91`](/Users/x/plugins/collect.py:91) — The helper uses merged diagnosis freshness to decide"
        self.assertEqual(_plain(text), "Helper uses merged diagnosis freshness")
        self.assertIsNone(_plain("`a/b.py:3`"))
        issue = {"id": "DO-12", "title": "Viewer shows wrong count", "state": "untested", "raw": {"title": "Viewer shows wrong count"},
                 "first_seen": {"at": "2026-11-24T08:00:00Z", "source": "observed_text"}, "events": []}
        first = build_trail(issue, [], [], "121.json")["trail"][0]
        self.assertIsNone(first["what"])


class CarryAndCreditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        folder = self.state / "agent-plugins"
        folder.mkdir(parents=True)
        lane = {"id": "R4-171", "status": "parked", "finding_ids": ["BCL-171"], "failure_history": [
            {"at": "2026-11-22T16:51:14Z", "reason": "Review failed", "source": "review"},
            {"at": "2026-11-22T17:10:59Z", "reason": "Review failed again", "source": "review"}]}
        (folder / "120.json").write_text(json.dumps(_t(120, [
            {"id": "BCL-171", "title": "Collect never reads findings", "status": "blocked",
             "first_seen": {"at": "2026-11-22T13:24:00Z", "pr": 120, "round": 1, "source": "observed_text"}},
            {"id": "DO-2", "title": "Second", "status": "verified_fixed",
             "first_seen": {"at": "2026-11-21T09:00:00Z", "pr": 120, "round": 1, "source": "check"}}],
            groups=[lane], runs=[{"id": "r1-x-120", "installed_at": "2026-11-21T10:00:00Z", "finished_at": "2026-11-21T11:00:00Z",
                                  "install_evidence": {"log": "x" * 500},
                                  "checks": {"DO-2": {"state": "fixed", "finished_at": "2026-11-21T10:30:00Z", "evidence": "ok"}}}],
            merged="2026-11-23T09:00:00Z")))
        (folder / "121.json").write_text(json.dumps(_t(121, [
            {"id": "BCL-171", "title": "Collect never reads findings", "status": "in_progress",
             "first_seen": {"at": "2026-11-22T13:24:00Z", "pr": 120, "round": 1, "source": "legacy_gap"}}],
            carried=[lane])))
        jobs = self.state / "abc"
        jobs.mkdir()
        rows = [{"event": "start", "job_id": "j6", "phase": "fix", "repo": "/w/bcl120-r6-do2",
                 "at": "2026-11-21T09:30:00Z", "started_at": "2026-11-21T09:30:00Z", "model": "Grok Bot"},
                {"event": "finish", "job_id": "j6", "phase": "fix", "repo": "/w/bcl120-r6-do2", "exit_code": 0,
                 "at": "2026-11-21T09:50:00Z", "finished_at": "2026-11-21T09:50:00Z"}]
        (jobs / "events.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        self.env = {"BCL_STATE_DIR": str(self.state)}

    def tearDown(self):
        self.tmp.cleanup()

    def _build(self, **kw):
        return build_issues("agent-plugins", state_dir=str(self.state), **kw)

    def test_carried_issue_keeps_failure_history_without_moving_counts(self):
        plain = self._build(pr="121", scope="now")
        full = self._build(pr="121", scope="now", include="trail")
        self.assertEqual(plain["counts"], full["counts"])
        issue = [i for i in full["issues"] if i["id"] == "BCL-171"][0]
        self.assertEqual(issue["state"], "in_progress")
        self.assertEqual(issue["attempts"], 2)
        self.assertEqual(issue["last_seen_at"], "2026-11-22T17:10:59Z")
        first = issue["trail"][0]
        self.assertEqual((first["ts"], first["provenance"]), ("2026-11-22T16:51:14Z", "recorded"))
        self.assertIn("120.json", first["evidence_url"])

    def test_job_is_credited_through_its_lane_name(self):
        issue = [i for i in self._build(pr="120", scope="all", include="jobs")["issues"] if i["id"] == "DO-2"][0]
        self.assertEqual([(j["job_id"], j["link_provenance"]) for j in issue["jobs_usage"]], [("j6", "derived")])
        self.assertIsNone(issue["jobs_usage"][0]["model"])
        self.assertEqual(issue["jobs_usage"][0]["harness"], "Grok Bot")
        cause = [step for step in self._build(pr="120", scope="all", include="trail")["issues"] if step["id"] == "DO-2"][0]["trail"][1]
        self.assertEqual((cause["ts"], cause["provenance"], cause["source"]), ("2026-11-21T09:30:00Z", "derived", "first job on lane R6-DO2"))

    def test_list_payload_drops_heavy_fields_and_detail_keeps_them(self):
        listed = self._build(pr="120", scope="all", include="trail,jobs,rollup")
        self.assertTrue(listed["runs"])
        for run in listed["runs"]:
            self.assertNotIn("checks", run)
            self.assertNotIn("install_evidence", run)
        for issue in listed["issues"]:
            self.assertNotIn("raw", issue)
            self.assertNotIn("jobs", issue)
            self.assertIn("trail", issue)
        detail = self._build(pr="120", scope="all", issue_id="DO-2")
        self.assertEqual(detail["issues"][0]["raw"]["id"], "DO-2")
        self.assertIn("jobs", detail["issues"][0])


if __name__ == "__main__":
    unittest.main()
