"""Regression tests for the WF-06 CI and review-policy leftovers."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class WF06RegressionTests(unittest.TestCase):
    def test_origin_ci_does_not_skip_draft_pull_requests(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text()

        self.assertRegex(workflow, r"(?m)^  pull_request:\s*(?:#.*)?$")
        self.assertNotRegex(
            workflow,
            r"(?im)^\s*if\s*:.*github\.event\.pull_request\.draft",
        )

    def test_review_policy_allows_one_round_and_logs_leftover_nits(self) -> None:
        policy = (
            ROOT
            / "plugins/batched-change-list/skills/batched-change-list/references/review.md"
        ).read_text()

        self.assertIn("Use one review round per group", policy)
        self.assertIn(
            "log leftover nits as new issues and do not start a second review",
            policy,
        )


if __name__ == "__main__":
    unittest.main()
