import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import manage_findings as manager

STAMP = "2026-11-24T06:20:00Z"
REJECTED = "ERROR: The 'gpt-6.1-terra' model is not supported when using Codex with a ChatGPT account.\nmodel_not_found\n"


def document():
    return {"findings": [{"id": "ISSUE-01", "status": "in_progress"}],
            "tracking": {"cycle": 1, "round": 1,
                         "plan": {"cycle": 1, "round": 1,
                                  "groups": [{"id": "A", "finding_ids": ["ISSUE-01"], "status": "planned"}]}}}


def transition(doc, action, phase, job_id, **fields):
    plan = doc.get("tracking", {}).get("plan", {})
    operation = {"action": action, "phase": phase, "ids": fields.pop("ids", ["ISSUE-01"]), "cycle": 1, "round": 1,
                 "timestamp": STAMP, **fields}
    if action == "start-job":
        operation["job"] = {"id": job_id, "plan_sha256": manager.plan_candidate_sha256(plan)}
    else:
        operation.update(job_id=job_id, plan_sha256=manager.plan_candidate_sha256(plan))
    manager.job_transition(doc, operation)
    return doc


def group(doc):
    return doc["tracking"]["plan"]["groups"][0]


class ReviewerFallbackTests(unittest.TestCase):
    def test_rejected_model_text_is_named(self):
        self.assertTrue(manager.reviewer_model_rejected(REJECTED))
        self.assertFalse(manager.reviewer_model_rejected("VERDICT: CHANGES\n## Must-fix\n- a test\n"))
        self.assertFalse(manager.reviewer_model_rejected(""))

    def _pass_fix(self, doc):
        transition(doc, "start-job", "fix", "fix-1")
        transition(doc, "finish-job", "fix", "fix-1", exit_code=0)

    def test_rejected_reviewer_stays_in_review_and_records_substitution(self):
        doc = document()
        self._pass_fix(doc)
        transition(doc, "start-job", "review", "review-1")
        transition(doc, "finish-job", "review", "review-1", exit_code=1, reply=REJECTED,
                   fault_kind="model_rejected", substitution="Grok Bot subagent review")
        lane = group(doc)
        self.assertEqual(lane["phase"], "review")
        self.assertEqual(lane["status"], "review")
        self.assertEqual(lane["codex"]["fix_status"], "passed")
        job = lane["codex"]["job"]
        self.assertEqual(job["fault_kind"], "model_rejected")
        self.assertEqual(job["model_substitution"], "Grok Bot subagent review")
        self.assertEqual(lane["codex"]["reviewer_model"], "Grok Bot subagent review")
        rows = manager._lane_history(lane, "failure_history")
        self.assertTrue(rows and rows[-1].get("tool_fault") and rows[-1].get("fault_kind") == "model_rejected")
        self.assertEqual(len(manager._counted_failures(rows)), 0)

    def test_second_review_starts_without_a_noop_fix(self):
        doc = document()
        self._pass_fix(doc)
        transition(doc, "start-job", "review", "review-1")
        transition(doc, "finish-job", "review", "review-1", exit_code=1, reply=REJECTED,
                   fault_kind="model_rejected", substitution="Grok Bot subagent review")
        transition(doc, "start-job", "review", "review-2")
        transition(doc, "finish-job", "review", "review-2", exit_code=0,
                   reply="VERDICT: PASS\n## Must-fix\nnone\n")
        lane = group(doc)
        self.assertEqual(lane["codex"]["review_status"], "passed")
        self.assertEqual(lane["phase"], "review")

    def test_ordinary_runner_exit_still_returns_to_fix(self):
        doc = document()
        self._pass_fix(doc)
        transition(doc, "start-job", "review", "review-1")
        transition(doc, "finish-job", "review", "review-1", exit_code=1, reply="codex crashed\n")
        lane = group(doc)
        self.assertEqual(lane["phase"], "fix")
        self.assertEqual(lane["status"], "running")


if __name__ == "__main__":
    unittest.main()
