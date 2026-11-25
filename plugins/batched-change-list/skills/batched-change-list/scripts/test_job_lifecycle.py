import copy
import json
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
    def test_parallel_lane_receipt_does_not_stale_a_running_jobs_finish(self):
        doc = document()
        doc["findings"].append({"id": "ISSUE-02", "status": "in_progress"})
        doc["tracking"]["plan"]["groups"].append(
            {"id": "B", "finding_ids": ["ISSUE-02"], "status": "planned"})
        transition(doc, "start-job", "fix", "job-a", ids=["ISSUE-01"])
        transition(doc, "start-job", "fix", "job-b", ids=["ISSUE-02"])
        finish(doc, "fix", "job-a", code=0)
        transition(doc, "finish-job", "fix", "job-b", ids=["ISSUE-02"], exit_code=0)
        jobs = [group["codex"]["job"] for group in doc["tracking"]["plan"]["groups"]]
        self.assertEqual([job["exit_code"] for job in jobs], [0, 0])

    def test_stale_lane_plan_cannot_finish_as_passed(self):
        doc = document()
        transition(doc, "start-job", "fix", "job-a")
        doc["tracking"]["plan"]["groups"][0]["files"] = ["changed.py"]
        with self.assertRaisesRegex(ValueError, "stale fix completion"):
            finish(doc, "fix", "job-a", code=0)

    def test_second_plan_review_waits_for_a_standalone_filing(self):
        doc = document()
        transition(doc, "start-job", "plan_review", "plan-1")
        finish(doc, "plan_review", "plan-1", reply="VERDICT: CHANGES\n## Must-fix\n1. revise the plan")
        for n in range(2, 8):
            with self.assertRaisesRegex(ValueError, "filed as standalone findings"):
                transition(doc, "start-job", "plan_review", f"plan-{n}")
        group = doc["tracking"]["plan"]["groups"][0]
        self.assertEqual(len(group["plan_review_history"]), 1)
        item = {"id": "ISSUE-02", "title": "Remaining plan review item", "status": "open"}
        manager.place_new_finding(doc["findings"], item, new=True)
        transition(doc, "start-job", "plan_review", "plan-8")

    def test_fourth_post_fix_review_waits_for_a_standalone_filing(self):
        doc = document()
        group = doc["tracking"]["plan"]["groups"][0]
        group.update(status="review", phase="review", codex={"fix_status": "passed"})
        group["failure_history"] = [
            {"attempt_id": f"review-{n}", "source": "review", "at": f"2026-11-21T00:00:0{n}Z"}
            for n in range(1, 4)]
        group["failure_history"].append({"attempt_id": "fault", "source": "review", "tool_fault": True,
                                         "at": "2026-11-21T00:00:04Z"})
        group["unblock_history"] = [{"owner_override": True, "covered_attempts": ["review-1", "review-2", "review-3"],
                                     "at": "2026-11-21T00:00:05Z"}]
        with self.assertRaisesRegex(ValueError, "remaining review items to be filed"):
            transition(doc, "start-job", "review", "review-4")
        manager.place_new_finding(doc["findings"], {"id": "ISSUE-02", "title": "Remaining review item",
            "status": "open"}, new=True)
        transition(doc, "start-job", "review", "review-4")

    def test_review_lane_membership_is_immutable_but_standalone_add_works(self):
        before = {"groups": [{"id": "A", "phase": "review", "finding_ids": ["ISSUE-01"]}]}
        after = {"groups": [{"id": "A", "phase": "review", "finding_ids": ["ISSUE-01", "ISSUE-02"]}]}
        with self.assertRaisesRegex(ValueError, "cannot add findings to lane A"):
            manager._refuse_review_lane_membership(before, after)
        # A replan that leaves review may replace membership (runtime identity reset).
        replaced = {"groups": [{"id": "A", "phase": "planned", "finding_ids": ["ISSUE-02"]}]}
        manager._refuse_review_lane_membership(before, replaced)
        findings = [{"id": "ISSUE-01", "title": "Existing", "status": "open"}]
        item = {"id": "ISSUE-02", "title": "Standalone follow-up", "status": "open"}
        self.assertTrue(manager.place_new_finding(findings, item, new=True))
        self.assertIn("created_at", item)

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
        finish(doc, "fix", "fix-1")
        transition(doc, "start-job", "review", "review-1")
        finish(doc, "review", "review-1", reply="VERDICT: CHANGES\n## Must-fix\n1. guard the lane")
        group = doc["tracking"]["plan"]["groups"][0]
        self.assertEqual(len(group["failure_history"]), 1)
        manager._record_lane_failure(doc, "ISSUE-01", "review-1", "review", "duplicate")
        self.assertEqual(len(group["failure_history"]), 1)
        with self.assertRaisesRegex(ValueError, "passed plan_review"):
            transition(doc, "start-job", "fix", "fix-2")
        transition(doc, "start-job", "plan_review", "plan-1")
        finish(doc, "plan_review", "plan-1", reply=("VERDICT: PASS\n## Must-fix\nnone\n"
               "## ROOT CAUSE\nmissing guard\n## FAILING TEST\ntest_guard"), timestamp="2026-11-21T00:00:01Z")
        transition(doc, "start-job", "fix", "fix-2")
        finish(doc, "fix", "fix-2")
        transition(doc, "start-job", "review", "review-2")
        finish(doc, "review", "review-2", reply="VERDICT: CHANGES\n## Must-fix\n1. still unguarded",
               timestamp="2026-11-21T00:00:02Z")
        self.assertEqual(group["status"], "blocked")
        self.assertEqual(doc["findings"][0]["status"], "blocked")

    def test_failed_fix_job_is_an_uncounted_attempt_and_three_in_a_row_block(self):
        doc = document()
        group = doc["tracking"]["plan"]["groups"][0]
        transition(doc, "start-job", "fix", "fix-1")
        finish(doc, "fix", "fix-1", code=1)
        row = group["failure_history"][-1]
        self.assertEqual((row["source"], row["tool_fault"], row["fault_kind"]), ("fix", True, "fix_job_exit"))
        self.assertEqual(manager._counted_failures(group["failure_history"]), [])
        self.assertEqual(group["phase"], "fix")
        transition(doc, "start-job", "fix", "fix-2")
        finish(doc, "fix", "fix-2", code=1)
        self.assertNotEqual(group["status"], "blocked")
        transition(doc, "start-job", "fix", "fix-3")
        finish(doc, "fix", "fix-3", code=1)
        self.assertEqual((group["status"], group["blocked_reason"]), ("blocked", "Repeated tool faults"))

    def test_passed_fix_resets_the_failed_job_streak(self):
        doc = document()
        group = doc["tracking"]["plan"]["groups"][0]
        for n in (1, 2):
            transition(doc, "start-job", "fix", f"fix-{n}")
            finish(doc, "fix", f"fix-{n}", code=1)
        transition(doc, "start-job", "fix", "fix-3")
        finish(doc, "fix", "fix-3")
        self.assertEqual(group["codex"]["tool_fault_streak"], 0)
        transition(doc, "start-job", "fix", "fix-4")
        finish(doc, "fix", "fix-4", code=1)
        self.assertNotEqual(group["status"], "blocked")

    def test_plan_review_changes_keeps_fix_gate_closed_without_counting_a_failure(self):
        doc = document()
        manager._record_lane_failure(doc, "ISSUE-01", "review-1", "review", "review failed")
        group = doc["tracking"]["plan"]["groups"][0]
        for n in (1, 2):
            transition(doc, "start-job", "plan_review", f"plan-{n}")
            finish(doc, "plan_review", f"plan-{n}", code=1, reply="VERDICT: CHANGES\n## Must-fix\n1. tighten the plan")
            if n == 1:
                manager.place_new_finding(doc["findings"], {"id": "ISSUE-02", "title": "Plan follow-up",
                    "status": "open"}, new=True)
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
        finish(doc, "fix", "fix-B")
        transition(doc, "start-job", "review", "review-B", ids=["ISSUE-02"])
        finish(doc, "review", "review-B", reply="VERDICT: CHANGES\n## Must-fix\n1. still broken",
               timestamp="2026-11-21T00:00:02Z")
        self.assertEqual(group["status"], "blocked")

    def _second_review_failure(self, reply):
        doc = document()
        group = doc["tracking"]["plan"]["groups"][0]
        manager._record_lane_failure(doc, "ISSUE-01", "review-1", "review", "first review failed")
        transition(doc, "start-job", "plan_review", "plan-1")
        finish(doc, "plan_review", "plan-1", reply=("VERDICT: PASS\n## Must-fix\nnone\n"
               "## ROOT CAUSE\nwhy\n## FAILING TEST\ntest_lane"), timestamp="2026-11-21T00:00:01Z")
        transition(doc, "start-job", "fix", "fix-2")
        finish(doc, "fix", "fix-2")
        transition(doc, "start-job", "review", "review-2")
        finish(doc, "review", "review-2", reply=reply, timestamp="2026-11-21T00:00:02Z")
        self.assertEqual(group["status"], "blocked")
        manager.unblock_lane(doc, {"ids": ["ISSUE-01"], "reason": "owner authorized", "tool_faults": [],
                                   "timestamp": "2026-11-21T00:00:03Z"})
        return doc, group

    def test_review_answering_root_cause_replaces_the_second_plan_review(self):
        doc, group = self._second_review_failure("VERDICT: CHANGES\n## Must-fix\n1. guard the lane\n"
                                                 "## ROOT CAUSE\nguard missing\n## FAILING TEST\ntest_guard")
        self.assertTrue(group["failure_history"][-1]["plan_answered"])
        with self.assertRaisesRegex(ValueError, "filed as standalone findings"):
            transition(doc, "start-job", "plan_review", "plan-2")
        transition(doc, "start-job", "fix", "fix-3")
        self.assertEqual(len(group["plan_review_history"]), 1)

    def test_review_without_root_cause_still_needs_a_plan_review(self):
        doc, group = self._second_review_failure("VERDICT: CHANGES\n## Must-fix\n1. guard the lane")
        self.assertNotIn("plan_answered", group["failure_history"][-1])
        with self.assertRaisesRegex(ValueError, "passed plan_review"):
            transition(doc, "start-job", "fix", "fix-3")
        manager.place_new_finding(doc["findings"], {"id": "ISSUE-02", "title": "Plan follow-up",
            "status": "open"}, new=True)
        transition(doc, "start-job", "plan_review", "plan-2")
        finish(doc, "plan_review", "plan-2", reply=("VERDICT: PASS\n## Must-fix\nnone\n"
               "## ROOT CAUSE\nwhy\n## FAILING TEST\ntest_lane"), timestamp="2099-01-01T00:00:00Z")
        transition(doc, "start-job", "fix", "fix-3")

    def test_first_failure_still_needs_its_plan_review_even_when_answered(self):
        doc = document()
        transition(doc, "start-job", "fix", "fix-1")
        finish(doc, "fix", "fix-1")
        transition(doc, "start-job", "review", "review-1")
        finish(doc, "review", "review-1", reply=("VERDICT: CHANGES\n## Must-fix\n1. x\n"
               "## ROOT CAUSE\nwhy\n## FAILING TEST\ntest_lane"))
        with self.assertRaisesRegex(ValueError, "passed plan_review"):
            transition(doc, "start-job", "fix", "fix-2")
        transition(doc, "start-job", "plan_review", "plan-1")

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
        manager.place_new_finding(doc["findings"], {"id": "ISSUE-02", "title": "Fresh follow-up",
            "status": "open"}, new=True)
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
        self.assertEqual(group["status"], "review")
        self.assertEqual(group["phase"], "review")
        with self.assertRaisesRegex(ValueError, "stays in review after a tool-fault review"):
            transition(doc, "start-job", "fix", "fix-2")
        transition(doc, "start-job", "review", "review-2")
        self.assertEqual(group["status"], "review")
        self.assertEqual(group["phase"], "review")

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

    def test_changes_verdict_without_must_fix_section_still_counts(self):
        doc = document()
        transition(doc, "start-job", "fix", "fix-1")
        finish(doc, "fix", "fix-1")
        transition(doc, "start-job", "review", "review-1")
        finish(doc, "review", "review-1", reply="VERDICT: CHANGES\n- must-fix 0\n")
        group = doc["tracking"]["plan"]["groups"][0]
        self.assertFalse(group["failure_history"][0].get("tool_fault"))
        self.assertNotIn("incomplete", group["failure_history"][0]["reason"].lower())
        with self.assertRaisesRegex(ValueError, "passed plan_review"):
            transition(doc, "start-job", "fix", "fix-2")


    def test_nonzero_reviewer_exit_with_a_pass_reply_is_an_uncounted_tool_fault(self):
        doc = document()
        transition(doc, "start-job", "fix", "fix-1")
        finish(doc, "fix", "fix-1")
        transition(doc, "start-job", "review", "review-1")
        finish(doc, "review", "review-1", code=3, reply="VERDICT: PASS\n## Must-fix\nnone\n")
        group = doc["tracking"]["plan"]["groups"][0]
        row = group["failure_history"][0]
        self.assertTrue(row["tool_fault"])
        self.assertEqual(row["fault_kind"], "runner_exit")
        self.assertEqual(row["reason"], "Reviewer exited 3")
        self.assertEqual(manager._counted_failures(group["failure_history"]), [])
        self.assertNotEqual(group["codex"]["review_status"], "passed")

    def test_repeated_plan_review_cut_offs_block_the_lane_without_counting(self):
        doc = document()
        manager._record_lane_failure(doc, "ISSUE-01", "review-1", "review", "review failed")
        group = doc["tracking"]["plan"]["groups"][0]
        for n in (1, 2):
            transition(doc, "start-job", "plan_review", f"plan-{n}")
            finish(doc, "plan_review", f"plan-{n}", reply="partial plan review with no verdict")
        self.assertNotEqual(group["status"], "blocked")
        transition(doc, "start-job", "plan_review", "plan-3")
        finish(doc, "plan_review", "plan-3", code=1, reply="")
        self.assertEqual(group["status"], "blocked")
        self.assertEqual(group["blocked_reason"], "Repeated tool faults")
        self.assertEqual(len(manager._counted_failures(group["failure_history"])), 1)


    def test_tool_fault_after_an_owner_unblock_does_not_reblock_the_lane(self):
        doc = document()
        group = doc["tracking"]["plan"]["groups"][0]
        for n in (1, 2):
            manager._record_lane_failure(doc, "ISSUE-01", f"bug-{n}", "review", "real bug")
        self.assertEqual(group["status"], "blocked")
        manager.unblock_lane(doc, {"ids": ["ISSUE-01"], "reason": "owner approved", "timestamp": STAMP})
        manager._record_lane_failure(doc, "ISSUE-01", "cut-1", "review", manager.INCOMPLETE_REVIEW_REASON, tool_fault=True)
        self.assertEqual(group["status"], "running")
        manager._record_lane_failure(doc, "ISSUE-01", "bug-3", "review", "another real bug")
        self.assertEqual(group["status"], "blocked")
        self.assertEqual(group["blocked_reason"], "Second failure")

    def test_tool_faults_separated_by_a_clean_job_do_not_block(self):
        doc = document()
        group = doc["tracking"]["plan"]["groups"][0]
        transition(doc, "start-job", "fix", "fix-1")
        finish(doc, "fix", "fix-1")
        for n in (1, 2):
            transition(doc, "start-job", "review", f"cut-{n}")
            finish(doc, "review", f"cut-{n}", reply="partial review")
            transition(doc, "start-job", "review", f"pass-{n}")
            finish(doc, "review", f"pass-{n}", reply="VERDICT: PASS\n## Must-fix\nnone\n")
        transition(doc, "start-job", "review", "cut-3")
        finish(doc, "review", "cut-3", reply="partial review")
        self.assertNotEqual(group["status"], "blocked")
        self.assertEqual(group["codex"]["tool_fault_streak"], 1)

    def test_tool_fault_review_leaves_the_lane_in_review(self):
        doc = document()
        transition(doc, "start-job", "fix", "fix-1")
        finish(doc, "fix", "fix-1")
        transition(doc, "start-job", "review", "review-1")
        finish(doc, "review", "review-1", code=3, reply="VERDICT: PASS\n## Must-fix\nnone\n")
        group = doc["tracking"]["plan"]["groups"][0]
        self.assertEqual(group["status"], "review")
        self.assertEqual(group["phase"], "review")
        self.assertEqual(group["codex"]["fix_status"], "passed")
        self.assertTrue(group["failure_history"][0]["tool_fault"])
        transition(doc, "start-job", "review", "review-2")
        finish(doc, "review", "review-2", reply="VERDICT: CHANGES\n## Must-fix\n- add the guard")
        self.assertEqual(group["status"], "running")
        self.assertEqual(group["phase"], "fix")
        self.assertFalse(group["failure_history"][1].get("tool_fault"))

    def test_running_job_accepts_its_own_step_report_and_refuses_a_foreign_job(self):
        doc = document()
        transition(doc, "start-job", "fix", "job-a")
        manager.job_transition(doc, {"action": "report-step", "phase": "fix", "ids": ["ISSUE-01"],
                                     "cycle": 1, "round": 1, "timestamp": "2026-11-21T00:01:00Z",
                                     "job_id": "job-a", "step": "Committed first test",
                                     "result": "ok", "commit": "abc1234"})
        job = doc["tracking"]["plan"]["groups"][0]["codex"]["job"]
        self.assertEqual(job["latest_step"]["step"], "Committed first test")
        self.assertEqual(job["latest_step"]["result"], "ok")
        self.assertEqual(job["latest_step"]["commit"], "abc1234")
        self.assertEqual(job["steps"][0]["step"], "Committed first test")
        self.assertIn("Committed first test", doc["tracking"]["workflow"]["now"])
        with self.assertRaisesRegex(ValueError, "refused"):
            manager.job_transition(doc, {"action": "report-step", "phase": "fix", "ids": ["ISSUE-01"],
                                         "cycle": 1, "round": 1, "timestamp": "2026-11-21T00:02:00Z",
                                         "job_id": "job-other", "step": "not mine"})
        finish(doc, "fix", "job-a")
        with self.assertRaisesRegex(ValueError, "refused"):
            manager.job_transition(doc, {"action": "report-step", "phase": "fix", "ids": ["ISSUE-01"],
                                         "cycle": 1, "round": 1, "timestamp": "2026-11-21T00:03:00Z",
                                         "job_id": "job-a", "step": "too late"})

    def test_finished_jobs_are_kept_for_the_lane_timeline(self):
        doc = document()
        transition(doc, "start-job", "fix", "job-a")
        finish(doc, "fix", "job-a")
        transition(doc, "start-job", "review", "job-b")
        finish(doc, "review", "job-b", reply="VERDICT: PASS\n## Must-fix\nnone\n")
        jobs = manager.lane_jobs(doc["tracking"]["plan"]["groups"][0])
        self.assertEqual([job["id"] for job in jobs], ["job-a", "job-b"])
        self.assertEqual(jobs[0]["phase"], "fix")
        self.assertEqual(jobs[1]["phase"], "review")

    def test_failed_job_keeps_a_first_error_line_and_output_pointer(self):
        doc = document()
        transition(doc, "start-job", "fix", "job-a")
        manager.job_transition(doc, {"action": "finish-job", "phase": "fix", "ids": ["ISSUE-01"],
                                     "cycle": 1, "round": 1, "timestamp": STAMP, "job_id": "job-a",
                                     "plan_sha256": manager.plan_candidate_sha256(doc["tracking"]["plan"]),
                                     "exit_code": 2, "first_error_line": "AssertionError: guard missing",
                                     "output_path": "/tmp/jobs/job-a.log"})
        job = doc["tracking"]["plan"]["groups"][0]["codex"]["job"]
        self.assertEqual(job["first_error_line"], "AssertionError: guard missing")
        self.assertEqual(job["output_path"], "/tmp/jobs/job-a.log")
        self.assertTrue(job["output_retained"])

    def test_unblock_validates_every_id_before_writing(self):
        doc = document()
        group = doc["tracking"]["plan"]["groups"][0]
        group.update(status="blocked", phase="fix", blocked_reason="Second failure")
        before = json.dumps(doc, sort_keys=True)
        with self.assertRaisesRegex(ValueError, "covered by current plan"):
            manager.unblock_lane(doc, {"ids": ["ISSUE-01", "ISSUE-MISSING"], "reason": "owner approved",
                                       "timestamp": STAMP})
        self.assertEqual(json.dumps(doc, sort_keys=True), before)

    def test_start_job_resumes_when_pid_is_dead_without_exit_code(self):
        from unittest.mock import patch
        doc = document()
        transition(doc, "start-job", "fix", "fix-1")
        manager.job_transition(doc, {"action": "attach-job", "phase": "fix", "ids": ["ISSUE-01"],
                                     "cycle": 1, "round": 1, "timestamp": STAMP, "job_id": "fix-1",
                                     "pid": 424242})
        group = doc["tracking"]["plan"]["groups"][0]
        with patch.object(manager.live_state, "_pid_is_running", return_value=False):
            transition(doc, "start-job", "fix", "fix-2")
        history = group["codex"]["jobs"]
        closed = next(job for job in history if job["id"] == "fix-1")
        self.assertIn("exit_code", closed)
        self.assertEqual(closed["finished_phase_status"], "interrupted")
        self.assertEqual(group["codex"]["job"]["id"], "fix-2")
        self.assertNotIn("exit_code", group["codex"]["job"])

    def test_renderer_and_start_job_share_lane_job_running(self):
        from unittest.mock import patch
        import render as renderer
        doc = document()
        transition(doc, "start-job", "fix", "fix-1")
        manager.job_transition(doc, {"action": "attach-job", "phase": "fix", "ids": ["ISSUE-01"],
                                     "cycle": 1, "round": 1, "timestamp": STAMP, "job_id": "fix-1",
                                     "pid": 424242})
        group = doc["tracking"]["plan"]["groups"][0]
        with patch.object(manager.live_state, "_pid_is_running", return_value=True), \
             patch.object(renderer.live_state, "_pid_is_running", return_value=True):
            self.assertTrue(renderer.lane_job_running(group))
            with self.assertRaisesRegex(ValueError, "already has a running"):
                transition(doc, "start-job", "fix", "fix-2")
        with patch.object(manager.live_state, "_pid_is_running", return_value=False), \
             patch.object(renderer.live_state, "_pid_is_running", return_value=False):
            self.assertFalse(renderer.lane_job_running(group))
            transition(doc, "start-job", "fix", "fix-3")
        self.assertEqual(group["codex"]["job"]["id"], "fix-3")

    def test_review_tool_fault_leaves_lane_in_review_and_allows_rerun(self):
        doc = document()
        transition(doc, "start-job", "fix", "fix-1")
        finish(doc, "fix", "fix-1")
        transition(doc, "start-job", "review", "review-1")
        finish(doc, "review", "review-1", code=1, reply="")
        group = doc["tracking"]["plan"]["groups"][0]
        self.assertEqual(group["phase"], "review")
        self.assertEqual(group["status"], "review")
        self.assertTrue(group["failure_history"][0]["tool_fault"])
        self.assertEqual(group["failure_history"][0]["fault_kind"], "runner_exit")
        with self.assertRaisesRegex(ValueError, "stays in review after a tool-fault review"):
            transition(doc, "start-job", "fix", "fix-2")
        transition(doc, "start-job", "review", "review-2")
        self.assertEqual(group["phase"], "review")
        self.assertEqual(group["codex"]["job"]["id"], "review-2")



if __name__ == "__main__":
    unittest.main()
