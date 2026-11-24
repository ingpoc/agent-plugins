import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
sys.path.insert(0, str(RUNTIME))
sys.path.insert(0, str(RUNTIME / "backend"))
from backend.issue_state import apply_cascade, build_issues, lifecycle_state, parse_observed, select_scope
from viewer import issues_payload


def _tracker(pr, round_number, cycle, findings, *, merged=None, history=None, plan_groups=None,
             unresolved=None, source_pr=None, runs=None, gate=None, extra_tracking=None):
    tracking = {
        "repo": "agent-plugins",
        "pull_request": {"number": int(pr), "label": f"PR #{pr}", "opened_at": "2026-11-24T08:00:00Z" if str(pr) == "121" else None},
        "round": round_number,
        "cycle": cycle,
        "round_history": history or [],
        "plan": {"round": round_number, "cycle": cycle, "groups": plan_groups or []},
    }
    if merged:
        tracking["merged"] = merged
    if unresolved is not None:
        tracking["rollover"] = {"source_pr": source_pr or 120, "unresolved_ids": unresolved}
    if runs:
        tracking["verification_history"] = runs
    if gate:
        tracking["gate"] = gate
    if extra_tracking:
        tracking.update(extra_tracking)
    return {"title": f"Agent Plugins PR {pr}", "findings": findings, "tracking": tracking}


class IssuesAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        folder = self.state / "agent-plugins"
        folder.mkdir(parents=True)
        self.folder = folder
        self.tracker = folder / "121.json"
        self.tracker.write_text(json.dumps(_tracker(121, 2, 1, [
            {"id": "DO-1", "title": "Untested", "status": "pending", "created_at": "2026-11-24T10:00:00Z"},
            {"id": "DO-2", "title": "Blocked", "status": "blocked", "failure_history": [
                {"at": "2026-11-24T11:00:00Z", "source": "review", "reason": "Must fix"}]},
            {"id": "DO-3", "title": "Resolved", "status": "verified", "fixed_at": "2026-11-24T09:00:00Z"},
        ], history=[{"round": 1, "ended_at": "2026-11-24T10:00:00Z"}],
           plan_groups=[{"id": "R2-DO2", "status": "blocked", "finding_ids": ["DO-2"]}],
           gate={"merge_previous_pr_threshold": 12})))
        os.environ["BCL_STATE_DIR"] = str(self.state)

    def tearDown(self):
        os.environ.pop("BCL_STATE_DIR", None)
        self.tmp.cleanup()

    def test_api_defaults_to_now_and_returns_stored_gate_and_lifecycle(self):
        result = build_issues("agent-plugins")
        self.assertEqual(result["pr"], "121")
        self.assertEqual({row["id"] for row in result["issues"]}, {"DO-1", "DO-2", "DO-3"})
        self.assertEqual(result["gate"]["threshold"], 12)
        blocked = next(row for row in result["issues"] if row["id"] == "DO-2")
        self.assertEqual(blocked["state"], "blocked")
        self.assertEqual(blocked["last_seen_at"], "2026-11-24T11:00:00Z")
        resolved = next(row for row in result["issues"] if row["id"] == "DO-3")
        self.assertEqual(resolved["state"], "resolved")

    def test_get_api_and_shared_scopes_return_same_issue_ids(self):
        via_http = issues_payload({"project": ["agent-plugins"], "pr": ["current"], "scope": ["all"]})
        via_tool_model = build_issues("agent-plugins", scope="all")
        self.assertEqual([row["id"] for row in via_http["issues"]], [row["id"] for row in via_tool_model["issues"]])
        self.assertEqual({row["id"] for row in via_http["issues"]}, {"DO-1", "DO-2", "DO-3"})

    def test_timeframe_is_project_wide_and_filters_by_issue_activity(self):
        result = build_issues("agent-plugins", from_time="2026-11-24T10:30:00+00:00", to_time="2026-11-24T11:30:00+00:00")
        self.assertEqual({row["id"] for row in result["issues"]}, {"DO-2"})
        self.assertEqual(result["issues"][0]["window_reason"], "reoccurred")
        self.assertIsNotNone(result["window"])
        self.assertEqual(result["window"]["kind"], "fixed")
        self.assertIsInstance(result["quiet_excluded"], int)

    def test_verification_run_checks_enter_the_timeline_and_window(self):
        tracker = json.loads(self.tracker.read_text())
        tracker["tracking"]["verification_history"] = [{"id": "verify-1", "installed_at": "2026-11-24T10:30:00Z", "checks": {
            "DO-1": {"state": "fixed", "finished_at": "2026-11-24T10:45:00Z", "summary": "Observed fixed"}}}]
        self.tracker.write_text(json.dumps(tracker))
        result = build_issues("agent-plugins", scope="all")
        issue = next(row for row in result["issues"] if row["id"] == "DO-1")
        self.assertEqual(issue["last_evidence"]["ref"], "check:verify-1:DO-1")
        window = build_issues("agent-plugins", from_time="2026-11-24T10:30:00Z", to_time="2026-11-24T11:00:00Z")
        issue = next(row for row in window["issues"] if row["id"] == "DO-1")
        self.assertEqual(issue["window_reason"], "fixed")
        run = next(row for row in result["runs"] if row.get("id") == "verify-1")
        self.assertEqual(run["started_at"], "2026-11-24T10:30:00Z")
        self.assertEqual(run["ended_at"], "2026-11-24T10:45:00Z")

    def test_viewer_import_path_does_not_need_backend_on_sys_path(self):
        """The loopback viewer only adds runtime/ to sys.path."""
        backend = str(RUNTIME / "backend")
        restored = [path for path in sys.path if path == backend]
        sys.path[:] = [path for path in sys.path if path != backend]
        self.addCleanup(lambda: [sys.path.insert(0, path) for path in restored])
        via_http = issues_payload({"project": ["agent-plugins"], "pr": ["current"], "scope": ["all"]})
        self.assertEqual({row["id"] for row in via_http["issues"]}, {"DO-1", "DO-2", "DO-3"})

    def test_state_negation_and_relative_window(self):
        result = build_issues("agent-plugins", scope="all", state="!resolved")
        self.assertEqual({row["id"] for row in result["issues"]}, {"DO-1", "DO-2"})
        relative = build_issues("agent-plugins", from_time="now-6h", to_time="now")
        self.assertEqual(relative["window"]["kind"], "sliding")
        self.assertEqual(relative["window"]["requested_from"], "now-6h")


class CascadeAndAccuracyTests(unittest.TestCase):
    """P1-1/2/3 and cascade resets: counts are checked against the fixture trackers."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        folder = self.state / "agent-plugins"
        folder.mkdir(parents=True)
        r6_run = {"id": "r6-828e2f90-120", "installed_at": "2026-11-24T06:55:44Z", "status": "interrupted", "checks": {
            "BCL-121": {"state": "fixed", "finished_at": "2026-11-24T06:58:00Z", "summary": "fixed"},
            "DO-2": {"state": "fixed", "finished_at": "2026-11-24T06:58:10Z", "summary": "fixed"},
            "DO-3": {"state": "fixed", "finished_at": "2026-11-24T06:58:20Z", "summary": "fixed"},
            "PR120-r5-codex-reviewer-model-fallback": {"state": "fixed", "finished_at": "2026-11-24T06:58:30Z", "summary": "fixed"},
        }}
        findings_120 = [
            {"id": "BCL-121", "title": "BCL-121", "status": "verified_fixed", "created_at": "2026-11-20T00:00:00Z",
             "observed": "20 Nov 2026 10:00 IST"},
            {"id": "DO-2", "title": "DO-2", "status": "verified_fixed", "created_at": "2026-11-20T00:00:00Z",
             "observed": "20 Nov 2026 10:00 IST"},
            {"id": "DO-3", "title": "DO-3", "status": "verified_fixed", "created_at": "2026-11-20T00:00:00Z",
             "observed": "20 Nov 2026 10:00 IST"},
            {"id": "PR120-r5-codex-reviewer-model-fallback", "title": "Fallback", "status": "verified_fixed",
             "created_at": "2026-11-23T00:00:00Z", "observed": "23 Nov 2026 10:00 IST"},
            {"id": "PR120-r6-gate-denominator", "title": "Gate", "status": "untested",
             "created_at": "2026-11-24T06:20:00Z", "observed": "24 Nov 2026 11:50 IST"},
            {"id": "PR120-r6-stale-job-finish", "title": "Stale job", "status": "untested",
             "created_at": "2026-11-24T07:02:02Z", "observed": "24 Nov 2026, 12:08–12:10 IST, Asia/Calcutta."},
            {"id": "PR120-r6-collect-job-coverage", "title": "Coverage", "status": "untested",
             "created_at": "2026-11-24T07:02:02Z", "observed": "24 Nov 2026, 12:27–12:30 IST, Asia/Calcutta."},
            {"id": "DO-4", "title": "Viewer v3", "status": "in_progress", "created_at": "2026-11-24T07:10:26Z",
             "observed": "24 Nov 2026 12:37 IST"},
            {"id": "BCL-171", "title": "Collector", "status": "blocked",
             "observed": "22 Nov 2026 18:54 IST, Guru rule and read-only audit",
             "last_seen": {"at": "2026-11-24T04:43:44Z", "source": "review"},
             "failure_history": [{"at": "2026-11-24T04:43:44Z", "source": "review", "reason": "review failed at /Users/gurusharan/code/worktrees/agent-plugins/demo/collect.py:69"}]},
            {"id": "BCL-106", "title": "106", "status": "blocked", "observed": "20 Nov 2026 23:10 IST",
             "failure_history": [{"at": "2026-11-24T04:34:33Z", "source": "review", "reason": "failed"}]},
            {"id": "OLD-1", "title": "Quiet", "status": "untested", "observed": "14 Nov 2026 10:00 IST",
             "created_at": "2026-11-14T04:30:00Z"},
        ]
        (folder / "120.json").write_text(json.dumps(_tracker(
            120, 7, 12, findings_120,
            merged={"commit_sha": "9fc50e95", "merged_at": "2026-11-24T09:08:28Z"},
            history=[
                {"round": 5, "fixed": 10, "ended_at": "2026-11-24T06:19:20Z"},
                {"round": 6, "fixed": 14, "ended_at": "2026-11-24T07:07:37Z"},
            ],
            plan_groups=[{"id": "R7-DOVIEW", "status": "parked", "finding_ids": ["DO-4"]}],
            runs=[r6_run],
        )))
        unresolved = ["DO-4", "DO-1", "DO-6", "DO-8", "PR120-r6-stale-job-finish",
                      "PR120-r6-collect-job-coverage", "BCL-171", "BCL-106"]
        findings_121 = [
            {"id": "DO-4", "title": "Viewer v3", "status": "failing",
             "created_at": "2026-11-24T07:10:26Z",
             "first_seen": {"at": "2026-11-24T07:10:26Z", "source": "created_at", "pr": 121, "round": 2},
             "observed": "24 Nov 2026 16:19 IST Asia/Calcutta. Guru approved the v3 calm design.",
             "verification": "Guru rejected the page: it is not understandable, so the earlier sign-off is withdrawn.",
             "verification_evidence": [{"observed": "The mid-afternoon review of 24 November 2026 concluded the page is ready to ship."}],
             "last_seen": {"at": "2026-11-24T11:44:38Z", "source": "review"}},
            {"id": "DO-12", "title": "Tracker fields", "status": "untested",
             "created_at": "2026-11-24T12:00:00Z", "observed": "24 Nov 2026 17:30 IST"},
            {"id": "DO-8", "title": "DO-8", "status": "untested",
             "created_at": "2026-11-24T08:04:16Z", "observed": "24 Nov 2026 13:30 IST Asia/Calcutta."},
            {"id": "DO-6", "title": "DO-6", "status": "untested",
             "created_at": "2026-11-24T08:04:16Z", "observed": "24 Nov 2026 13:30 IST Asia/Calcutta."},
            {"id": "DO-1", "title": "DO-1", "status": "untested",
             "observed": "22 Nov 2026 19:08 IST, round 1 introspect of PR 120"},
            {"id": "DO-5", "title": "DO-5", "status": "verified_fixed",
             "created_at": "2026-11-24T08:04:16Z", "observed": "24 Nov 2026 13:30 IST"},
            {"id": "DO-7", "title": "DO-7", "status": "verified_fixed",
             "created_at": "2026-11-24T08:04:16Z", "observed": "24 Nov 2026 13:30 IST"},
            {"id": "PR120-r6-gate-denominator", "title": "Gate", "status": "verified_fixed",
             "created_at": "2026-11-24T06:20:00Z", "observed": "24 Nov 2026 11:50 IST"},
            {"id": "PR120-r6-stale-job-finish", "title": "Stale job", "status": "untested",
             "created_at": "2026-11-24T07:02:02Z", "observed": "24 Nov 2026, 12:08–12:10 IST, Asia/Calcutta."},
            {"id": "PR120-r6-collect-job-coverage", "title": "Coverage", "status": "untested",
             "created_at": "2026-11-24T07:02:02Z", "observed": "24 Nov 2026, 12:27–12:30 IST, Asia/Calcutta."},
            {"id": "BCL-171", "title": "Collector", "status": "blocked",
             "observed": "22 Nov 2026 18:54 IST, Guru rule and read-only audit",
             "last_seen": {"at": "2026-11-24T04:43:44Z", "source": "review"},
             "failure_history": [
                 {"at": "2026-11-24T04:43:44Z", "source": "review",
                  "reason": "review failed at /Users/gurusharan/code/worktrees/agent-plugins/demo/collect.py:69"}]},
            {"id": "BCL-106", "title": "106", "status": "blocked", "observed": "20 Nov 2026 23:10 IST",
             "failure_history": [{"at": "2026-11-24T04:34:33Z", "source": "review", "reason": "failed"}]},
        ]
        (folder / "121.json").write_text(json.dumps(_tracker(
            121, 2, 13, findings_121,
            history=[{"round": 1, "fixed": 3, "ended_at": "2026-11-24T10:31:36Z"}],
            plan_groups=[
                {"id": "R2-DO4", "status": "planned", "phase": "fix", "finding_ids": ["DO-4", "DO-12"],
                 "failure_history": [
                     {"attempt_id": "a", "source": "review", "reason": "Review incomplete", "at": "2026-11-24T11:43:38Z"},
                     {"attempt_id": "b", "source": "review", "reason": "Must-fix remaining", "at": "2026-11-24T11:44:38Z"},
                 ]},
                {"id": "R2-DO1", "status": "deferred", "phase": "deferred", "finding_ids": ["DO-1"], "parked_to": 3},
            ],
            unresolved=unresolved, source_pr=120,
            gate={"merge_previous_pr_threshold": 16},
        )))
        self.trackers = {120: json.loads((folder / "120.json").read_text()),
                         121: json.loads((folder / "121.json").read_text())}
        os.environ["BCL_STATE_DIR"] = str(self.state)

    def tearDown(self):
        os.environ.pop("BCL_STATE_DIR", None)
        self.tmp.cleanup()

    def _ids(self, pr):
        return {row["id"] for row in self.trackers[pr]["findings"]}

    def test_p1_round_lens_resolves_any_pr_and_counts_match_tracker(self):
        via_scope = build_issues("agent-plugins", pr="120", scope="round:6")
        via_round = build_issues("agent-plugins", pr="120", round_id=6)
        via_http = issues_payload({"project": ["agent-plugins"], "pr": ["120"], "round": ["6"]})
        for result in (via_scope, via_round, via_http):
            self.assertEqual(result["pr"], "120")
            self.assertEqual(result["round"], 6)
            ids = [row["id"] for row in result["issues"]]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertGreater(len(ids), 0)
            self.assertTrue(set(ids) <= self._ids(120))
            self.assertNotIn("DO-5", ids)
            self.assertNotIn("DO-8", ids)
            counts = result["counts"]["by_state"]
            self.assertEqual(sum(counts.values()), len(result["issues"]))
            self.assertEqual(counts, {name: sum(issue["state"] == name for issue in result["issues"])
                                      for name in counts})
        self.assertEqual({row["id"] for row in via_scope["issues"]}, {row["id"] for row in via_http["issues"]})
        r6 = next(row for row in via_http["rounds"] if str(row.get("pr")) == "120" and row.get("round") == 6)
        self.assertEqual(r6["fixed"], 4)
        self.assertEqual(r6["found"], 4)
        self.assertEqual(r6["regressed"], 0)

    def test_p1_timeframe_dedupes_by_latest_pr_and_fills_window(self):
        result = build_issues(
            "agent-plugins",
            from_time="2026-11-24T09:30:00+05:30",
            to_time="2026-11-24T12:45:00+05:30",
        )
        ids = [row["id"] for row in result["issues"]]
        self.assertEqual(len(ids), len(set(ids)), ids)
        by_id = {row["id"]: row for row in result["issues"]}
        # Latest PR wins for ids present in both trackers.
        for identifier in ("PR120-r6-stale-job-finish", "PR120-r6-collect-job-coverage", "BCL-171", "BCL-106"):
            self.assertEqual(str(by_id[identifier]["pr"]), "121")
        self.assertIsNotNone(result["window"])
        self.assertEqual(result["window"]["kind"], "fixed")
        self.assertIsNotNone(result["window"]["from"])
        self.assertIsNotNone(result["window"]["to"])
        self.assertIsInstance(result["window"]["rounds"], list)
        self.assertIsInstance(result["window"]["test_runs"], list)
        self.assertIsInstance(result["quiet_excluded"], int)
        self.assertEqual(result["quiet_excluded"] + len(result["issues"]),
                         len({row["id"] for row in build_issues("agent-plugins", scope="all")["issues"]}))
        counts = result["counts"]
        self.assertEqual(sum(counts["by_state"].values()), len(result["issues"]))
        self.assertEqual(counts["by_reason"]["regressed"],
                         sum(issue.get("window_reason") == "regressed" for issue in result["issues"]))
        self.assertEqual(counts["by_reason"]["reoccurred"],
                         sum(issue.get("window_reason") == "reoccurred" for issue in result["issues"]))

    def test_p1_origin_and_regression_follow_the_tracker(self):
        result = build_issues("agent-plugins")
        issues = {row["id"]: row for row in result["issues"]}
        do4 = issues["DO-4"]
        self.assertEqual(do4["status_raw"], "failing")
        self.assertEqual(do4["origin"], "carried")
        self.assertEqual(do4["state"], "regressed")
        self.assertTrue(do4["needs_you"])
        self.assertEqual(issues["DO-4"]["events"] and issues["DO-4"]["attempts"], 2)
        unresolved = set(self.trackers[121]["tracking"]["rollover"]["unresolved_ids"])
        present = unresolved & set(issues)
        for identifier in present:
            self.assertEqual(issues[identifier]["origin"], "carried", identifier)
        self.assertEqual(issues["DO-8"]["state"], "untested")
        self.assertIsNone(issues["DO-8"].get("lane"))
        self.assertEqual(issues["DO-1"]["state"], "parked")
        self.assertEqual(issues["BCL-171"]["first_seen"]["source"], "observed_text")
        self.assertTrue((issues["BCL-171"]["first_seen"]["at"] or "").startswith("2026-11-22T18:54"))
        summary = (issues["BCL-171"].get("last_evidence") or {}).get("summary") or ""
        self.assertNotIn("/Users/", summary)
        self.assertEqual(result["counts"]["by_state"]["regressed"],
                         sum(issue["state"] == "regressed" for issue in result["issues"]))
        self.assertEqual(issues["DO-4"]["id"], result["issues"][0]["id"])

    def test_cascade_resets_pr_and_round_and_defaults_to_latest(self):
        lists = build_issues("agent-plugins")["lists"]
        prs = lists["prs"]
        self.assertEqual([row["pr"] for row in prs], ["121", "120"])
        self.assertEqual(prs[0]["status"], "open")
        self.assertEqual(prs[0]["latest_round"], 2)
        self.assertEqual(prs[0]["open"], sum(issue["state"] != "resolved" for issue in build_issues("agent-plugins")["issues"]))
        self.assertEqual(prs[1]["status"], "merged")
        self.assertEqual(prs[1]["latest_round"], 7)
        changed_pr = apply_cascade(lists, "agent-plugins", "121", 2, {"pr": "120"})
        self.assertEqual(changed_pr, {"project": "agent-plugins", "pr": "120", "round": 7})
        changed_round = apply_cascade(lists, "agent-plugins", "120", 7, {"round": 6})
        self.assertEqual(changed_round["pr"], "120")
        self.assertEqual(changed_round["round"], 6)
        other = {"projects": lists["projects"] + [{"id": "other", "label": "Other"}],
                 "prs": lists["prs"] + [{"project": "other", "pr": "3", "latest_round": 1, "status": "open"}]}
        changed_project = apply_cascade(other, "agent-plugins", "121", 2, {"project": "other"})
        self.assertEqual(changed_project, {"project": "other", "pr": "3", "round": 1})
        defaulted = build_issues("agent-plugins", pr="120")
        self.assertEqual(defaulted["pr"], "120")
        self.assertEqual(defaulted["round"], 7)
        self.assertTrue(defaulted["issues"])
        self.assertEqual(defaulted["counts"]["by_state"],
                         {name: sum(issue["state"] == name for issue in defaulted["issues"])
                          for name in defaulted["counts"]["by_state"]})

    def test_observed_parser_and_lifecycle_helpers(self):
        stamp = parse_observed("22 Nov 2026 18:54 IST, Guru rule")
        self.assertEqual(stamp.isoformat(), "2026-11-22T18:54:00+05:30")
        self.assertEqual(lifecycle_state({"status": "pending"}), "untested")
        self.assertEqual(lifecycle_state({"status": "running"}), "in_progress")
        self.assertEqual(select_scope(
            [{"id": "a", "state": "resolved", "round": 2, "current_round": 2},
             {"id": "b", "state": "blocked", "origin": "new", "round": 2, "current_round": 2}],
            "now",
        )[0]["id"], "b")
        both = {row["id"] for row in select_scope(
            [{"id": "a", "state": "resolved"}, {"id": "b", "state": "blocked"}], "now")}
        self.assertEqual(both, {"a", "b"})

    def test_last_test_run_window_matches_counts_and_rejects_unrecorded_fallback(self):
        result = build_issues("agent-plugins", pr="120", run="last")
        self.assertEqual(result["run"]["id"], "r6-828e2f90-120")
        self.assertTrue(result["run"]["recorded"])
        self.assertEqual(result["window"]["kind"], "run")
        from backend.issue_state import instant
        self.assertEqual(instant(result["window"]["from"]), instant(result["run"]["started_at"]))
        self.assertIsNotNone(result["window"]["from"])
        self.assertIsNotNone(result["window"]["to"])
        # Window is the recorded run, not a 7-day fallback.
        from datetime import datetime, timezone
        start = datetime.fromisoformat(result["window"]["from"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(result["window"]["to"].replace("Z", "+00:00"))
        self.assertLess((end - start).total_seconds(), 7 * 24 * 3600)
        self.assertEqual(sum(result["counts"]["by_state"].values()), len(result["issues"]))
        self.assertEqual(result["counts"]["by_reason"]["fixed"], 4)
        self.assertEqual(result["counts"]["by_reason"]["new"], sum(i.get("window_reason") == "new" for i in result["issues"]))
        self.assertEqual(len({row["id"] for row in result["issues"]}), len(result["issues"]))
        via_http = issues_payload({"project": ["agent-plugins"], "pr": ["120"], "run": ["last"]})
        self.assertEqual({row["id"] for row in via_http["issues"]}, {row["id"] for row in result["issues"]})
        self.assertEqual(via_http["counts"], result["counts"])
        by_id = build_issues("agent-plugins", run="r6-828e2f90-120")
        self.assertEqual({row["id"] for row in by_id["issues"]}, {row["id"] for row in result["issues"]})

        # A run with no timestamps must not invent a 7-day window.
        tracker = json.loads((self.state / "agent-plugins" / "121.json").read_text())
        tracker["tracking"].setdefault("verification_history", []).append({"id": "no-times-run"})
        (self.state / "agent-plugins" / "121.json").write_text(json.dumps(tracker))
        missing = build_issues("agent-plugins", run="no-times-run")
        self.assertEqual(missing["issues"], [])
        self.assertFalse(missing["run"]["recorded"])
        self.assertIsNone(missing["window"]["from"])
        self.assertIsNone(missing["window"]["to"])
        self.assertEqual(missing["counts"]["by_state"]["resolved"], 0)


    def test_p1a_run_lists_every_check_and_inconclusive_is_not_regressed(self):
        tracker = json.loads((self.state / "agent-plugins" / "120.json").read_text())
        tracker["tracking"].setdefault("verification_history", []).append({
            "id": "r7-acu-demo-120",
            "pr": 120,
            "round": 7,
            "installed_at": "2026-11-24T08:31:56Z",
            "ended_at": "2026-11-24T08:50:24Z",
            "status": "complete",
            "checks": {
                "BCL-121": {"state": "inconclusive", "finished_at": "2026-11-24T08:40:00Z", "summary": "run stopped"},
                "DO-2": {"state": "inconclusive", "finished_at": "2026-11-24T08:41:00Z", "summary": "run stopped"},
                "DO-3": {"state": "fixed", "finished_at": "2026-11-24T08:42:00Z", "summary": "fixed"},
                "PR120-r5-codex-reviewer-model-fallback": {"state": "inconclusive", "finished_at": "2026-11-24T08:43:00Z",
                                                           "summary": "run stopped"},
                "DO-4": {"state": "not_fixed", "finished_at": "2026-11-24T08:44:00Z", "summary": "still failing"},
            },
        })
        (self.state / "agent-plugins" / "120.json").write_text(json.dumps(tracker))
        result = build_issues("agent-plugins", pr="120", run="last")
        self.assertEqual(result["run"]["id"], "r7-acu-demo-120")
        ids = [row["id"] for row in result["issues"]]
        self.assertEqual(len(ids), 5)
        self.assertEqual(len(set(ids)), 5)
        by_reason = result["counts"]["by_reason"]
        self.assertEqual(result["counts"]["tested"], 5)
        self.assertEqual(result["counts"]["fixed"], 1)
        self.assertEqual(result["counts"]["inconclusive"], 3)
        self.assertEqual(by_reason["inconclusive"], 3)
        self.assertEqual(by_reason["fixed"], 1)
        self.assertEqual(by_reason.get("regressed") or 0, 1)
        self.assertTrue(all(row["window_reason"] != "regressed" or row["id"] == "DO-4" for row in result["issues"]))
        # Window / run counts match state= filters on the same set.
        self.assertEqual(result["counts"]["by_state"]["regressed"],
                         sum(issue["state"] == "regressed" for issue in result["issues"]))
        filtered = build_issues("agent-plugins", pr="120", run="r7-acu-demo-120", state="regressed")
        self.assertEqual(len(filtered["issues"]), result["counts"]["by_state"]["regressed"])
        self.assertTrue(all(issue["state"] == "regressed" for issue in filtered["issues"]))

    def test_p1b_first_seen_is_earliest_across_prs_and_timeline_is_sorted(self):
        result = build_issues("agent-plugins")
        do4 = next(row for row in result["issues"] if row["id"] == "DO-4")
        stamp = do4["first_seen"]["at"]
        from backend.issue_state import instant
        first = instant(stamp)
        self.assertIsNotNone(first)
        self.assertEqual(first.astimezone(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M"),
                         "2026-11-24T07:07")
        instants = []
        for event in do4["events"]:
            value = instant(event["at"])
            self.assertIsNotNone(value, event)
            instants.append(value)
        self.assertEqual(instants, sorted(instants))
        # The 16:19 rewritten note must not become first seen.
        self.assertFalse((stamp or "").startswith("2026-11-24T16:19") or "10:49" in (stamp or ""))

    def test_p2a_untested_planned_sibling_stays_untested(self):
        result = build_issues("agent-plugins")
        issues = {row["id"]: row for row in result["issues"]}
        do12 = issues["DO-12"]
        self.assertEqual(do12["status_raw"], "untested")
        self.assertEqual(do12["state"], "untested")
        self.assertFalse(do12["needs_you"])
        self.assertEqual(do12["lane"]["id"], "R2-DO4")
        self.assertEqual(issues["DO-4"]["state"], "regressed")
        self.assertEqual(result["counts"]["by_state"]["untested"],
                         sum(issue["state"] == "untested" for issue in result["issues"]))
        self.assertEqual(result["counts"]["by_state"]["parked"],
                         sum(issue["state"] == "parked" for issue in result["issues"]))

    def test_p2c_live_rounds_and_runs_follow_the_source_pr(self):
        result = build_issues("agent-plugins")
        rounds = result["rounds"]
        live = [row for row in rounds if row.get("live")]
        self.assertTrue(all(str(row.get("pr")) == "121" for row in live), live)
        self.assertTrue(all(row.get("round") == 2 for row in live if str(row.get("pr")) == "121"))
        r7 = next(row for row in rounds if str(row.get("pr")) == "120" and row.get("round") == 7)
        self.assertFalse(r7.get("live"))
        self.assertIsNotNone(r7.get("ended_at"))
        # Default last run on the open PR has no run yet.
        missing = build_issues("agent-plugins", pr="121", run="last")
        self.assertIsNone(missing["run"])
        self.assertEqual(missing["run_note"], "no #121 run yet")
        self.assertEqual(missing["issues"], [])
        self.assertIsNotNone(missing["last_other_run"])
        self.assertEqual(str(missing["last_other_run"]["pr"]), "120")
        # Runs copied or hosted elsewhere still keep their source PR from the id suffix.
        from backend.issue_state import run_source_pr
        self.assertEqual(run_source_pr({"id": "r7-acu-0.2.163-11517de1-120"}, 121), "120")



if __name__ == "__main__":
    unittest.main()
