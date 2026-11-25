#!/usr/bin/env python3
"""Verify and merge clear leftover install.running and an active verification run."""
import sys
import unittest
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
