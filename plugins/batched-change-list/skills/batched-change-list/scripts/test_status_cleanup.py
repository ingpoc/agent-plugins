#!/usr/bin/env python3
"""Verify and merge clear leftover install.running and an active verification run."""
import sys
import unittest
import copy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import manage_findings as manager


STAMP = "2026-11-25T16:00:00Z"
MERGE_SHA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def fixture(verified_already=False):
    finding = {"id": "ISSUE-01", "status": "in_progress" if not verified_already else "verified_fixed",
               "title": "Demo", "source": "Agent reported", "observed": "seen", "recommendation": "fix"}
    group = {
        "id": "R-DEMO",
        "finding_ids": ["ISSUE-01"],
        "status": "verifying" if not verified_already else "verified",
        "phase": "verify",
        "install": {"status": "running", "started_at": "2026-11-25T15:33:11Z"},
        "verify": {"command": "/tmp/verify-demo.sh"},
    }
    plan = {"cycle": 15, "round": 3, "groups": [group]}
    return {
        "findings": [finding],
        "tracking": {
            "cycle": 15, "round": 3, "plan": plan,
            "verification_run": {
                "id": "pr123-r2-736a6ccd",
                "status": "active",
                "round": 2,
                "checks": {"ISSUE-01": {"state": "pending"}},
            },
        },
    }


class StatusCleanupTests(unittest.TestCase):
    def test_prior_fixed_finding_is_not_verified_by_another_lane(self):
        doc = fixture()
        doc['findings'].append({'id': 'OLD', 'status': 'verified_fixed', 'verification_refs': ['earlier run']})
        doc['tracking']['verification_run']['checks']['OLD'] = {'state': 'pending'}
        manager.finish_verification(doc, {'ids': ['ISSUE-01'],
            'plan_sha256': manager.plan_candidate_sha256(doc['tracking']['plan']),
            'command': '/tmp/verify-demo.sh', 'timestamp': STAMP})
        checks = doc['tracking']['verification_run']['checks']
        self.assertEqual(checks['ISSUE-01']['state'], 'fixed')
        self.assertEqual(checks['ISSUE-01']['observed_at'], STAMP)
        self.assertIn('/tmp/verify-demo.sh', checks['ISSUE-01']['evidence'])
        self.assertEqual(checks['OLD']['state'], 'inconclusive')

    def test_merge_does_not_infer_current_proof_from_history(self):
        doc = fixture(verified_already=True)
        manager.clear_finished_running_state(doc, STAMP, merged=True)
        self.assertEqual(doc['tracking']['verification_run']['checks']['ISSUE-01']['state'], 'inconclusive')

    def test_repair_restores_historical_lane_without_claiming_current_proof(self):
        doc = fixture(verified_already=True)
        group = doc['tracking']['plan']['groups'][0]
        group.update(status='blocked', blocked_reason='Prerequisite verification changed')
        group['verify']['status'] = 'verified'
        doc['findings'][0]['verification_refs'] = ['historical evidence']
        run = doc['tracking']['verification_run']
        run['status'] = 'complete'
        run['checks']['ISSUE-01'] = {'state': 'inconclusive', 'previous_checks': [
            {'state': 'fixed', 'evidence': 'Official verify recorded this issue as verified.'}]}
        operation = {'ids': ['R-DEMO'], 'reason': 'Restore historical lane after false current-run result'}
        before = copy.deepcopy(doc['tracking']['plan'])
        manager.repair_false_verify_blocks(doc, operation)
        manager.refuse_lane_moves(before, doc['tracking']['plan'], operation['ids'])
        self.assertEqual(group['status'], 'verified')
        self.assertEqual(run['checks']['ISSUE-01']['state'], 'inconclusive')
        self.assertEqual(group['repair_history'][0]['run_id'], run['id'])

    def test_repair_refuses_actual_failed_check_and_dependencies(self):
        doc = fixture(verified_already=True)
        group = doc['tracking']['plan']['groups'][0]
        group.update(status='blocked', blocked_reason='Prerequisite verification changed')
        group['verify']['status'] = 'verified'
        doc['findings'][0]['verification_refs'] = ['historical evidence']
        run = doc['tracking']['verification_run']
        run['status'] = 'complete'
        run['checks']['ISSUE-01'] = {'state': 'not_fixed', 'previous_checks': [
            {'state': 'fixed', 'evidence': 'Official verify recorded this issue as verified.'}]}
        operation = {'ids': ['R-DEMO'], 'reason': 'Do not restore a real failed check'}
        with self.assertRaises(ValueError):
            manager.repair_false_verify_blocks(doc, operation)
        self.assertEqual(group['status'], 'blocked')
        run['checks']['ISSUE-01']['state'] = 'inconclusive'
        run['checks']['ISSUE-01']['previous_checks'].append({'state': 'not_fixed'})
        with self.assertRaises(ValueError):
            manager.repair_false_verify_blocks(doc, operation)
        run['checks']['ISSUE-01']['previous_checks'].pop()
        doc['tracking']['plan']['groups'].append({'id': 'dependent', 'depends_on': ['R-DEMO']})
        with self.assertRaises(ValueError):
            manager.repair_false_verify_blocks(doc, operation)

    def test_correct_false_generated_check_accepts_new_observation(self):
        doc = fixture(verified_already=True)
        item = doc['findings'][0]
        item.update(verification_refs=['earlier proof'], verification='earlier proof')
        run = doc['tracking']['verification_run']
        run.update(build='fixture', revision='fixture', installed_at='2026-11-25T15:00:00Z', status='complete')
        run['checks']['ISSUE-01'] = {'state': 'fixed', 'evidence': 'Official verify recorded this issue as verified.'}
        manager.verification_action(doc, doc['findings'], {'action': 'correct-check', 'run_id': run['id'],
            'id': 'ISSUE-01', 'outcome': 'inconclusive', 'observed_at': STAMP,
            'reason': 'Checked: run record; Expected: current evidence; Observed: no issue check ran; Verdict because: inconclusive.'})
        self.assertEqual(item['status'], 'verified_fixed')
        self.assertEqual(item['verification'], 'earlier proof')
        self.assertEqual(run['checks']['ISSUE-01']['state'], 'inconclusive')
        self.assertEqual(run['checks']['ISSUE-01']['observed_at'], STAMP)
    def test_unrelated_pending_check_stays_pending_while_another_lane_is_open(self):
        doc = fixture()
        doc['findings'].append({'id': 'OLD', 'status': 'verified_fixed', 'verification_refs': ['earlier run']})
        doc['tracking']['verification_run']['checks']['OLD'] = {'state': 'pending'}
        doc['findings'].append({'id': 'NEXT', 'status': 'in_progress'})
        doc['tracking']['plan']['groups'].append({'id': 'NEXT', 'finding_ids': ['NEXT'], 'status': 'planned', 'phase': 'fix'})
        manager.finish_verification(doc, {'ids': ['ISSUE-01'],
            'plan_sha256': manager.plan_candidate_sha256(doc['tracking']['plan']),
            'command': '/tmp/verify-demo.sh', 'timestamp': STAMP})
        self.assertEqual(doc['tracking']['verification_run']['checks']['OLD']['state'], 'pending')

    def test_verify_clears_install_running(self):
        doc = fixture()
        plan_sha = manager.plan_candidate_sha256(doc["tracking"]["plan"])
        manager.finish_verification(doc, {
            "ids": ["ISSUE-01"],
            "plan_sha256": plan_sha,
            "command": "/tmp/verify-demo.sh",
            "timestamp": STAMP,
        })
        group = doc["tracking"]["plan"]["groups"][0]
        self.assertEqual(group["status"], "verified")
        self.assertNotEqual((group.get("install") or {}).get("status"), "running")
        self.assertEqual(group["install"]["status"], "complete")

    def test_merge_does_not_leave_verification_run_active(self):
        doc = fixture(verified_already=True)
        manager.clear_finished_running_state(doc, STAMP, merged=True)
        run = doc["tracking"].get("verification_run") or {}
        self.assertNotEqual(run.get("status"), "active")
        install = doc["tracking"]["plan"]["groups"][0].get("install") or {}
        self.assertNotEqual(install.get("status"), "running")
        # A merged list may archive the run, but it must not stay active.
        if "verification_run" in doc["tracking"]:
            self.assertIn(run.get("status"), {"complete", "stopped", "interrupted"})


    def test_verify_settles_leftover_pending_checks(self):
        doc = fixture()
        doc["findings"].append({"id": "DO-1", "status": "untested", "title": "Other",
                                "source": "Agent reported", "observed": "seen", "recommendation": "later"})
        doc["tracking"]["verification_run"]["checks"]["DO-1"] = {"state": "pending"}
        plan_sha = manager.plan_candidate_sha256(doc["tracking"]["plan"])
        manager.finish_verification(doc, {
            "ids": ["ISSUE-01"],
            "plan_sha256": plan_sha,
            "command": "/tmp/verify-demo.sh",
            "timestamp": STAMP,
        })
        run = doc["tracking"]["verification_run"]
        self.assertEqual(run["checks"]["ISSUE-01"]["state"], "fixed")
        self.assertEqual(run["checks"]["DO-1"]["state"], "inconclusive")
        self.assertNotEqual(run.get("status"), "active")
        self.assertEqual(run.get("status"), "complete")
