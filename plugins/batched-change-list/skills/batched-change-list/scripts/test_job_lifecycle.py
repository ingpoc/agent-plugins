import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import manage_findings as manager

STAMP = "2026-11-21T00:00:00Z"


def document():
    return {"findings": [{"id": "ISSUE-01", "status": "in_progress"}],
            "tracking": {"cycle": 1, "round": 1,
                         "plan": {"cycle": 1, "round": 1,
                                  "groups": [{"id": "A", "finding_ids": ["ISSUE-01"], "status": "planned"}]}}}


def transition(doc, action, phase, job_id, **fields):
    plan = doc.get("tracking", {}).get("plan", {})
    operation = {"action": action, "phase": phase, "ids": ["ISSUE-01"], "cycle": 1, "round": 1,
                 "timestamp": STAMP, **fields}
    if action == "start-job":
        operation["job"] = {"id": job_id, "plan_sha256": manager.plan_candidate_sha256(plan)}
    else:
        operation.update(job_id=job_id, plan_sha256=manager.plan_candidate_sha256(plan))
    manager.job_transition(doc, operation)


def finish(doc, phase, job_id, code=0, reply=None):
    transition(doc, "finish-job", phase, job_id, exit_code=code, reply=reply)


class JobLifecycleTests(unittest.TestCase):
    def test_fix_requires_a_recorded_plan_group_and_review_requires_passed_fix(self):
        no_plan = {"findings": [{"id": "ISSUE-01"}], "tracking": {}}
        with self.assertRaisesRegex(ValueError, "current recorded plan"):
            transition(no_plan, "start-job", "fix", "fix-1")
        doc = document()
        with self.assertRaisesRegex(ValueError, "requires lane A to finish fix"):
            transition(doc, "start-job", "review", "review-1")

    def test_presuite_waits_for_review_and_new_fix_invalidates_review_and_suite(self):
        doc = document()
        with self.assertRaisesRegex(ValueError, "presuite requires"):
            transition(doc, "start-job", "presuite", "suite-early")
        transition(doc, "start-job", "fix", "fix-1")
        finish(doc, "fix", "fix-1")
        transition(doc, "start-job", "review", "review-1")
        finish(doc, "review", "review-1", reply="VERDICT: PASS\n## Must-fix\nnone")
        transition(doc, "start-job", "presuite", "suite-1")
        finish(doc, "presuite", "suite-1")
        transition(doc, "start-job", "fix", "fix-2")
        group = doc["tracking"]["plan"]["groups"][0]
        self.assertNotIn("review_status", group["codex"])
        self.assertNotIn("presuite", group)

    def test_review_verdict_requires_exact_first_line_and_must_fix_section(self):
        for reply in ("note\nVERDICT: PASS\n## Must-fix\nnone", "VERDICT: PASS", "VERDICT: PASS\n## Must-fix\nissue"):
            self.assertEqual(manager.parse_review_reply(reply), (None, None))
        self.assertEqual(manager.parse_review_reply("VERDICT: PASS\n## Must-fix\nnone"), ("PASS", "none"))
        self.assertEqual(manager.parse_review_reply("VERDICT: CHANGES\n## Must-fix\n- fix it"), ("CHANGES", "- fix it"))

    def test_first_failure_requires_plan_review_and_second_blocks_lane(self):
        doc = document()
        transition(doc, "start-job", "fix", "fix-1")
        finish(doc, "fix", "fix-1", code=1)
        group = doc["tracking"]["plan"]["groups"][0]
        self.assertEqual(len(group["failure_history"]["ISSUE-01"]), 1)
        manager._record_lane_failure(doc, "ISSUE-01", "fix-1", "fix", "duplicate")
        self.assertEqual(len(group["failure_history"]["ISSUE-01"]), 1)
        with self.assertRaisesRegex(ValueError, "passed plan_review"):
            transition(doc, "start-job", "fix", "fix-2")
        transition(doc, "start-job", "plan_review", "plan-1")
        finish(doc, "plan_review", "plan-1", reply=("VERDICT: PASS\n## Must-fix\nnone\n"
               "## ROOT CAUSE\nmissing guard\n## FAILING TEST\ntest_guard"))
        transition(doc, "start-job", "fix", "fix-2")
        finish(doc, "fix", "fix-2", code=1)
        self.assertEqual(group["status"], "blocked")
        self.assertEqual(doc["findings"][0]["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
