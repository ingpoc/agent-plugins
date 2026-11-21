import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import manage_findings as manager

STAMP = "2026-11-21T00:00:00Z"


def document(groups, findings=None):
    if findings is None:
        findings = [{"id": "ISSUE-01", "status": "in_progress"}]
    return {"findings": findings, "tracking": {"cycle": 1, "round": 1, "plan": {"cycle": 1, "round": 1, "groups": groups}}}


class ReleaseCheckTests(unittest.TestCase):
    def test_lists_in_flight_lanes_and_ignores_settled_ones(self):
        doc = document([
            {"id": "A", "finding_ids": ["ISSUE-01"], "status": "running", "phase": "fix"},
            {"id": "B", "finding_ids": ["ISSUE-02"], "status": "deferred", "phase": "pending"},
            {"id": "C", "finding_ids": ["ISSUE-03"], "status": "verified", "phase": "done"},
        ])
        payload = manager.release_check_payload(doc)
        self.assertFalse(payload["clear"])
        self.assertEqual([lane["id"] for lane in payload["lanes"]], ["A"])

    def test_clear_when_every_lane_is_settled(self):
        doc = document([{"id": "A", "finding_ids": ["ISSUE-01"], "status": "done", "phase": "done"}])
        payload = manager.release_check_payload(doc)
        self.assertTrue(payload["clear"])
        self.assertEqual(payload["lanes"], [])


class ResortFailuresTests(unittest.TestCase):
    def _blocked(self, rows):
        return document([{"id": "A", "finding_ids": ["ISSUE-01"], "status": "blocked", "phase": "fix",
                          "blocked_reason": "Second failure", "failure_history": rows}],
                        findings=[{"id": "ISSUE-01", "status": "blocked"}])

    def test_applies_structured_tool_faults_without_uncounting_code_faults(self):
        doc = self._blocked([
            {"attempt_id": "r1", "source": "review", "reason": "quota ran out", "fault_kind": "quota_out", "at": STAMP},
            {"attempt_id": "r2", "source": "review", "reason": "missing guard", "at": STAMP}])
        preview = manager.resort_failures(doc, {"apply": False})
        self.assertEqual(preview["lanes"][0]["attempts"], ["r1"])
        self.assertEqual(doc["tracking"]["plan"]["groups"][0]["status"], "blocked")
        applied = manager.resort_failures(doc, {"apply": True, "reason": "quota-out is a tool fault", "timestamp": STAMP})
        group = doc["tracking"]["plan"]["groups"][0]
        self.assertTrue(applied["applied"])
        self.assertTrue(group["failure_history"][0]["tool_fault"])
        self.assertFalse(group["failure_history"][1].get("tool_fault"))
        self.assertEqual(group["status"], "running")
        self.assertEqual(doc["findings"][0]["status"], "in_progress")
        self.assertTrue(group["unblock_history"][0]["resort"])

    def test_ambiguous_legacy_incomplete_rows_stay_counted_unless_named(self):
        doc = self._blocked([
            {"attempt_id": "r1", "source": "review", "reason": manager.INCOMPLETE_REVIEW_REASON, "at": STAMP},
            {"attempt_id": "r2", "source": "review", "reason": manager.INCOMPLETE_REVIEW_REASON, "at": STAMP}])
        preview = manager.resort_failures(doc, {"apply": False})
        self.assertEqual(preview["lanes"][0]["attempts"], [])
        self.assertEqual(preview["lanes"][0]["ambiguous"], ["r1", "r2"])
        manager.resort_failures(doc, {"apply": True, "reason": "post-ship sort", "timestamp": STAMP})
        group = doc["tracking"]["plan"]["groups"][0]
        self.assertFalse(any(row.get("tool_fault") for row in group["failure_history"]))
        self.assertEqual(group["status"], "blocked")
        manager.resort_failures(doc, {"apply": True, "reason": "r1 reply had no verdict line", "tool_faults": ["r1"], "timestamp": STAMP})
        self.assertTrue(group["failure_history"][0]["tool_fault"])
        self.assertFalse(group["failure_history"][1].get("tool_fault"))
        self.assertEqual(group["status"], "running")
        with self.assertRaisesRegex(ValueError, "names no ambiguous"):
            manager.resort_failures(doc, {"apply": True, "reason": "x", "tool_faults": ["nope"], "timestamp": STAMP})

    def test_reviewer_text_that_looks_like_a_tool_reason_stays_counted(self):
        doc = self._blocked([
            {"attempt_id": "r1", "source": "review", "reason": "Quota-out: handling is broken", "at": STAMP},
            {"attempt_id": "r2", "source": "review", "reason": "Cut-off: upload truncates", "at": STAMP}])
        self.assertEqual(manager.resort_failures(doc, {"apply": False})["lanes"], [])

    def test_apply_requires_a_reason(self):
        doc = document([{"id": "A", "finding_ids": ["ISSUE-01"], "status": "running", "phase": "fix",
                         "failure_history": [{"attempt_id": "r1", "source": "review", "reason": "x", "fault_kind": "cut_off", "at": STAMP}]}])
        with self.assertRaisesRegex(ValueError, "resort-failures"):
            manager.resort_failures(doc, {"apply": True, "timestamp": STAMP})


class ToolFaultStreakTests(unittest.TestCase):
    def _lane(self):
        return document([{"id": "A", "finding_ids": ["ISSUE-01"], "status": "running", "phase": "fix"}])

    def test_three_incomplete_reviews_in_a_row_block_the_lane_until_unblock(self):
        doc = self._lane()
        for n in range(2):
            manager._record_lane_failure(doc, "ISSUE-01", f"cut-{n}", "review", manager.INCOMPLETE_REVIEW_REASON, tool_fault=True)
        group = doc["tracking"]["plan"]["groups"][0]
        self.assertEqual(group["status"], "running")
        manager._record_lane_failure(doc, "ISSUE-01", "cut-2", "review", manager.INCOMPLETE_REVIEW_REASON, tool_fault=True)
        self.assertEqual(group["status"], "blocked")
        self.assertEqual(group["blocked_reason"], "Repeated tool faults")
        manager.unblock_lane(doc, {"ids": ["ISSUE-01"], "reason": "reviewer fixed", "timestamp": STAMP})
        self.assertEqual(group["status"], "running")
        manager._record_lane_failure(doc, "ISSUE-01", "cut-3", "review", manager.INCOMPLETE_REVIEW_REASON, tool_fault=True)
        self.assertEqual(group["status"], "running")


if __name__ == "__main__":
    unittest.main()
