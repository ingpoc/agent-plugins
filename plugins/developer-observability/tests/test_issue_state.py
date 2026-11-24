import unittest
from datetime import datetime, timezone
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
from backend.issue_state import first_seen_for, lifecycle_state, select_scope, window_reason


class IssueStateTests(unittest.TestCase):
    def test_lifecycle_progression_and_regression(self):
        self.assertEqual(lifecycle_state({"status": "pending"}), "untested")
        self.assertEqual(lifecycle_state({"status": "running"}), "in_progress")
        self.assertEqual(lifecycle_state({"status": "pending"}, [{"kind": "review_failed", "at": "2026-11-24T10:00:00Z"}]), "blocked")
        self.assertEqual(lifecycle_state({"status": "parked"}), "parked")
        self.assertEqual(lifecycle_state({"status": "untested"}, lane={"status": "planned", "phase": "fix"}), "untested")
        self.assertEqual(lifecycle_state({"status": "untested"}, lane={"status": "parked", "phase": "fix"}), "untested")
        self.assertEqual(lifecycle_state({"status": "untested"}, lane={"status": "deferred", "phase": "deferred"}), "parked")
        self.assertEqual(lifecycle_state({"status": "verified"}), "resolved")
        self.assertEqual(lifecycle_state({"status": "verified", "resolved_at": "2026-11-24T10:00:00Z"},
                                         [{"kind": "fail", "at": "2026-11-24T10:01:00Z"}]), "regressed")
        self.assertEqual(lifecycle_state({"status": "verified", "state_changes": [
            {"at": "2026-11-24T10:01:00Z", "from": "resolved", "to": "regressed", "source": "owner_rejected"}]}), "regressed")

    def test_now_hides_resolved_and_round_and_state_filters_apply(self):
        issues = [{"id": "a", "state": "resolved", "round": 2, "current_round": 2},
                  {"id": "b", "state": "blocked", "origin": "new", "round": 2, "current_round": 2},
                  {"id": "c", "state": "untested", "origin": "carried", "round": 1, "current_round": 2}]
        self.assertEqual({row["id"] for row in select_scope(issues)}, {"a", "b", "c"})
        self.assertEqual(select_scope(issues)[0]["id"], "b")
        self.assertEqual({row["id"] for row in select_scope(issues, "now")}, {"a", "b", "c"})
        self.assertEqual({row["id"] for row in select_scope(issues, "all")}, {"a", "b", "c"})
        self.assertEqual({row["id"] for row in select_scope(issues, "all", state="!resolved")}, {"b", "c"})
        self.assertEqual({row["id"] for row in select_scope(issues, "round:current")}, {"a", "b"})
        self.assertEqual([row["id"] for row in select_scope(issues, "all", state="carried")], ["c"])

    def test_window_reason_distinguishes_new_regressed_and_quiet(self):
        start = datetime(2026, 11, 24, 10, tzinfo=timezone.utc)
        end = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)
        self.assertEqual(window_reason({"first_seen": {"at": "2026-11-24T10:30:00Z"}}, start, end), "new")
        self.assertEqual(window_reason({"events": [{"kind": "pass", "at": "2026-11-24T09:00:00Z"},
                                                     {"kind": "fail", "at": "2026-11-24T11:00:00Z"}]}, start, end), "regressed")
        self.assertIsNone(window_reason({"events": [{"kind": "job", "at": "2026-11-24T08:00:00Z"}]}, start, end))

    def test_inconclusive_is_never_a_regression(self):
        start, end = "2026-11-24T10:00:00Z", "2026-11-24T12:00:00Z"
        issue = {"events": [
            {"kind": "pass", "at": "2026-11-24T08:00:00Z"},
            {"kind": "inconclusive", "at": "2026-11-24T11:00:00Z", "summary": "not enough evidence"},
        ]}
        self.assertEqual(window_reason(issue, start, end), "inconclusive")
        fail = {"events": [
            {"kind": "pass", "at": "2026-11-24T08:00:00Z"},
            {"kind": "fail", "at": "2026-11-24T11:00:00Z"},
        ]}
        self.assertEqual(window_reason(fail, start, end), "regressed")

    def test_first_seen_is_the_earliest_time_across_sources(self):
        finding = {
            "id": "DO-4",
            "created_at": "2026-11-24T07:10:26Z",
            "first_seen": {"at": "2026-11-24T07:10:26Z", "source": "created_at", "pr": 121},
            "observed": "24 Nov 2026 16:19 IST. Guru approved the v3 calm design.",
        }
        seen = first_seen_for(finding, 121, 2)
        # Structured 12:40 IST beats the later 16:19 observed rewrite.
        self.assertTrue((seen["at"] or "").startswith("2026-11-24T07:10:26"))
        self.assertNotEqual(seen["source"], "observed_text")
        finding["observed"] = "24 Nov 2026 12:37 IST, first visit on #120"
        seen = first_seen_for(finding, 121, 2)
        self.assertTrue("07:07" in (seen["at"] or "") or "12:37" in (seen["at"] or ""))

if __name__ == "__main__":
    unittest.main()
