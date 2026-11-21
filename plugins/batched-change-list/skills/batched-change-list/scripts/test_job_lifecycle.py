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
    operation = {"action": action, "phase": phase, "ids": fields.pop("ids", ["ISSUE-01"]), "cycle": 1, "round": 1,
                 "timestamp": STAMP, **fields}
    if action == "start-job":
        operation["job"] = {"id": job_id, "plan_sha256": manager.plan_candidate_sha256(plan)}
    else:
        operation.update(job_id=job_id, plan_sha256=manager.plan_candidate_sha256(plan))
    manager.job_transition(doc, operation)


def finish(doc, phase, job_id, code=0, reply=None, timestamp=STAMP):
    transition(doc, "finish-job", phase, job_id, timestamp=timestamp, exit_code=code, reply=reply)


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
        self.assertEqual(manager.parse_review_reply("VERDICT: PASS\nMust-fix: none\nNits: wording only"), ("PASS", "none"))
        self.assertEqual(manager.parse_review_reply("VERDICT: CHANGES\n**Must-fix:**\n1. fix it\nNits: none"), ("CHANGES", "1. fix it"))
        self.assertEqual(manager.parse_review_reply("VERDICT: PASS\nMust-fix: see below\n"), (None, None))

    def test_first_failure_requires_plan_review_and_second_blocks_lane(self):
        doc = document()
        transition(doc, "start-job", "fix", "fix-1")
        finish(doc, "fix", "fix-1", code=1)
        group = doc["tracking"]["plan"]["groups"][0]
        self.assertEqual(len(group["failure_history"]), 1)
        manager._record_lane_failure(doc, "ISSUE-01", "fix-1", "fix", "duplicate")
        self.assertEqual(len(group["failure_history"]), 1)
        with self.assertRaisesRegex(ValueError, "passed plan_review"):
            transition(doc, "start-job", "fix", "fix-2")
        transition(doc, "start-job", "plan_review", "plan-1")
        finish(doc, "plan_review", "plan-1", reply=("VERDICT: PASS\n## Must-fix\nnone\n"
               "## ROOT CAUSE\nmissing guard\n## FAILING TEST\ntest_guard"))
        transition(doc, "start-job", "fix", "fix-2")
        finish(doc, "fix", "fix-2", code=1)
        self.assertEqual(group["status"], "blocked")
        self.assertEqual(doc["findings"][0]["status"], "blocked")

    def test_plan_review_changes_keeps_fix_gate_closed_without_counting_a_failure(self):
        doc = document()
        transition(doc, "start-job", "fix", "fix-1")
        finish(doc, "fix", "fix-1", code=1)
        group = doc["tracking"]["plan"]["groups"][0]
        for n in (1, 2):
            transition(doc, "start-job", "plan_review", f"plan-{n}")
            finish(doc, "plan_review", f"plan-{n}", code=1, reply="VERDICT: CHANGES\n## Must-fix\n1. tighten the plan")
        self.assertNotEqual(group["status"], "blocked")
        self.assertEqual(len(group["failure_history"]), 1)
        with self.assertRaisesRegex(ValueError, "passed plan_review"):
            transition(doc, "start-job", "fix", "fix-2")

    def test_lane_gate_reviews_siblings_and_deduplicates_attempts(self):
        doc = document()
        doc["findings"].append({"id": "ISSUE-02", "status": "in_progress"})
        group = doc["tracking"]["plan"]["groups"][0]
        group["finding_ids"].append("ISSUE-02")
        manager._record_lane_failure(doc, "ISSUE-01", "failed-A", "review", "review failed")
        manager._record_lane_failure(doc, "ISSUE-02", "failed-A", "review", "same lane attempt")
        self.assertEqual(len(group["failure_history"]), 1)
        with self.assertRaisesRegex(ValueError, "passed plan_review"):
            transition(doc, "start-job", "fix", "fix-B", ids=["ISSUE-02"])
        transition(doc, "start-job", "plan_review", "plan-B", ids=["ISSUE-02"])
        with self.assertRaisesRegex(ValueError, "ROOT CAUSE and FAILING TEST"):
            finish(doc, "plan_review", "plan-B", reply="VERDICT: PASS\n## Must-fix\nnone\n## ROOT CAUSE\nwhy")

    def test_plan_review_after_failure_requires_each_named_section(self):
        for reply in (
            "VERDICT: PASS\n## Must-fix\nnone\n## FAILING TEST\ntest_lane",
            "VERDICT: PASS\n## Must-fix\nnone\n## ROOT CAUSE\nwhy",
        ):
            with self.subTest(reply=reply):
                doc = document()
                manager._record_lane_failure(doc, "ISSUE-01", "failed-A", "review", "review failed")
                transition(doc, "start-job", "plan_review", "plan-B")
                with self.assertRaisesRegex(ValueError, "ROOT CAUSE and FAILING TEST"):
                    finish(doc, "plan_review", "plan-B", reply=reply)

    def test_lane_plan_review_binds_sibling_fix_and_second_failure_blocks(self):
        doc = document()
        doc["findings"].append({"id": "ISSUE-02", "status": "in_progress"})
        group = doc["tracking"]["plan"]["groups"][0]
        group["finding_ids"].append("ISSUE-02")
        manager._record_lane_failure(doc, "ISSUE-01", "failed-A", "review", "review failed")
        transition(doc, "start-job", "plan_review", "plan-B", ids=["ISSUE-02"])
        finish(doc, "plan_review", "plan-B", reply=("VERDICT: PASS\n## Must-fix\nnone\n"
               "## ROOT CAUSE\nwhy\n## FAILING TEST\ntest_lane"))
        self.assertEqual(group["plan_review_history"][-1]["after_failure"], "failed-A")
        transition(doc, "start-job", "fix", "fix-B", ids=["ISSUE-02"])
        finish(doc, "fix", "fix-B", code=1)
        self.assertEqual(group["status"], "blocked")

    def test_legacy_sibling_histories_merge_chronologically_and_dedupe(self):
        group = {"id": "A", "failure_history": {
            "ISSUE-01": [{"attempt_id": "later", "at": "2026-11-21T00:00:02Z"},
                         {"attempt_id": "shared", "at": "2026-11-21T00:00:01Z"}],
            "ISSUE-02": [{"attempt_id": "earlier", "at": "2026-11-21T00:00:00Z"},
                         {"attempt_id": "shared", "at": "2026-11-21T00:00:01Z"}]}}
        rows = manager._lane_history(group, "failure_history")
        self.assertEqual([row["attempt_id"] for row in rows], ["earlier", "shared", "later"])

    def test_two_legacy_sibling_failures_block_until_owner_unblock(self):
        doc = document()
        group = doc["tracking"]["plan"]["groups"][0]
        group["failure_history"] = {"ISSUE-01": [{"attempt_id": "one", "at": "2026-11-21T00:00:00Z"}],
                                    "ISSUE-02": [{"attempt_id": "two", "at": "2026-11-21T00:00:01Z"}]}
        group["status"] = "running"
        manager._lane_history(group, "failure_history")
        self.assertEqual(group["status"], "blocked")
        with self.assertRaisesRegex(ValueError, "bcl unblock --reason"):
            transition(doc, "start-job", "fix", "fix-blocked")
        manager.unblock_lane(doc, {"ids": ["ISSUE-01"], "reason": "owner authorized", "tool_faults": [],
                                   "timestamp": "2026-11-21T00:00:02Z"})
        transition(doc, "start-job", "plan_review", "recovery-review")
        finish(doc, "plan_review", "recovery-review", reply=("VERDICT: PASS\n## Must-fix\nnone\n"
               "## ROOT CAUSE\nwhy\n## FAILING TEST\ntest_lane"), timestamp="2026-11-21T00:00:03Z")
        transition(doc, "start-job", "fix", "fix-open")

    def test_unblock_attempt_ids_distinguish_same_second_legacy_failures(self):
        def legacy_lane():
            doc = document()
            doc["findings"].append({"id": "ISSUE-02", "status": "in_progress"})
            group = doc["tracking"]["plan"]["groups"][0]
            group.update(finding_ids=["ISSUE-01", "ISSUE-02"], status="blocked",
                         failure_history={"ISSUE-01": [{"attempt_id": "before-A", "at": STAMP}],
                                          "ISSUE-02": [{"attempt_id": "before-B", "at": STAMP}]})
            manager.unblock_lane(doc, {"ids": ["ISSUE-01"], "reason": "owner authorized", "tool_faults": [],
                                       "timestamp": STAMP})
            return doc, group

        _, group = legacy_lane()
        self.assertEqual(group["unblock_history"][0]["covered_attempts"], ["before-A", "before-B"])
        manager._lane_history(group, "failure_history")
        self.assertEqual(group["status"], "running")

        _, group = legacy_lane()
        group["failure_history"] = {
            "ISSUE-01": [{"attempt_id": "before-A", "at": STAMP}, {"attempt_id": "after-unblock", "at": STAMP}],
            "ISSUE-02": [{"attempt_id": "before-B", "at": STAMP}],
        }
        manager._lane_history(group, "failure_history")
        self.assertEqual(group["status"], "blocked")

    def test_same_second_legacy_failures_require_fresh_plan_review(self):
        doc = document()
        group = doc["tracking"]["plan"]["groups"][0]
        stamp = "2026-11-21T00:00:00Z"
        group["failure_history"] = {"ISSUE-01": [{"attempt_id": "one", "at": stamp}],
                                    "ISSUE-02": [{"attempt_id": "two", "at": stamp}]}
        manager._lane_history(group, "failure_history")
        latest = manager._counted_failures(group["failure_history"])[-1]
        group["plan_review_history"] = [{"job_id": "stale", "after_failure": latest["attempt_id"],
                                          "status": "passed", "at": stamp}]
        group["status"] = "blocked"
        manager.unblock_lane(doc, {"ids": ["ISSUE-01"], "reason": "owner authorized", "tool_faults": [],
                                   "timestamp": "2026-11-21T00:00:01Z"})
        with self.assertRaisesRegex(ValueError, "passed plan_review"):
            transition(doc, "start-job", "fix", "fix-stale")
        transition(doc, "start-job", "plan_review", "fresh")
        finish(doc, "plan_review", "fresh", reply=("VERDICT: PASS\n## Must-fix\nnone\n"
               "## ROOT CAUSE\nwhy\n## FAILING TEST\ntest_lane"), timestamp="2026-11-21T00:00:02Z")
        transition(doc, "start-job", "fix", "fix-fresh")

    def test_incomplete_review_is_recorded_as_a_tool_fault_and_does_not_require_plan_review(self):
        doc = document()
        transition(doc, "start-job", "fix", "fix-1")
        finish(doc, "fix", "fix-1")
        transition(doc, "start-job", "review", "review-1")
        finish(doc, "review", "review-1", reply="cut off mid-sentence")
        group = doc["tracking"]["plan"]["groups"][0]
        self.assertEqual(group["failure_history"][0]["reason"], "Review incomplete: strict verdict or Must-fix section missing")
        self.assertTrue(group["failure_history"][0]["tool_fault"])
        self.assertEqual(group["status"], "running")
        transition(doc, "start-job", "fix", "fix-2")
        self.assertEqual(group["status"], "running")
        self.assertEqual(group["phase"], "fix")

    def test_changes_review_still_counts_as_a_code_failure(self):
        doc = document()
        transition(doc, "start-job", "fix", "fix-1")
        finish(doc, "fix", "fix-1")
        transition(doc, "start-job", "review", "review-1")
        finish(doc, "review", "review-1", reply="VERDICT: CHANGES\n## Must-fix\n- missing guard")
        group = doc["tracking"]["plan"]["groups"][0]
        self.assertFalse(group["failure_history"][0].get("tool_fault"))
        with self.assertRaisesRegex(ValueError, "passed plan_review"):
            transition(doc, "start-job", "fix", "fix-2")


if __name__ == "__main__":
    unittest.main()
