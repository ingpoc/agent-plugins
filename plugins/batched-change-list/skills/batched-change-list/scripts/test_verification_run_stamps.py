import unittest
from datetime import datetime, timezone

import manage_findings


class VerificationRunStampTests(unittest.TestCase):
    def test_start_and_complete_record_run_window_pr_and_round(self):
        document = {
            "findings": [{"id": "DO-4", "title": "Viewer", "status": "untested"}],
            "tracking": {
                "pull_request": {"number": 121, "label": "PR #121"},
                "round": 2,
                "plan": {"round": 2, "groups": []},
            },
        }
        manage_findings.verification_action(document, document["findings"], {
            "action": "start-verification",
            "run": {
                "id": "r2-test",
                "build": "0.1.37",
                "revision": "abc1234",
                "installed_at": "2026-11-24T08:32:00Z",
                "install_evidence": "install receipt",
            },
        })
        run = document["tracking"]["verification_run"]
        self.assertEqual(run["run_id"], "r2-test")
        self.assertEqual(run["pr"], 121)
        self.assertEqual(run["round"], 2)
        self.assertEqual(run["started_at"], "2026-11-24T08:32:00Z")
        self.assertIsNone(run.get("ended_at"))
        run["checks"]["DO-4"] = {
            "state": "fixed",
            "finished_at": "2026-11-24T08:50:00Z",
            "updated_at": "2026-11-24T08:50:00Z",
        }
        manage_findings.verification_action(document, document["findings"], {"action": "complete-verification", "run_id": "r2-test"})
        self.assertEqual(run["status"], "complete")
        self.assertEqual(run["ended_at"], "2026-11-24T08:50:00Z")

    def test_annotate_does_not_invent_times_when_called_without_ending(self):
        run = {"id": "old-run"}
        manage_findings.annotate_verification_run(run, {"round": 1, "pull_request": {"number": 120}})
        self.assertEqual(run["run_id"], "old-run")
        self.assertEqual(run["pr"], 120)
        self.assertEqual(run["round"], 1)
        self.assertTrue(run.get("started_at"))
        self.assertIsNone(run.get("ended_at"))


if __name__ == "__main__":
    unittest.main()
