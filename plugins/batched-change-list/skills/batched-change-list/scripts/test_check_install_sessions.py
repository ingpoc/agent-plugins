import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import check_install_sessions as check


class InstallSessionTests(unittest.TestCase):
    def group(self, identifier="A", **codex):
        return {"id": identifier, "finding_ids": [identifier], "codex": codex, "presuite": {"status": "passed"}}

    def test_review_requires_implementer(self):
        with self.assertRaisesRegex(ValueError, "implementer_session"):
            check.validate_review_sessions({"groups": [self.group()]}, ["A"])

    def test_install_requires_reviewer_for_multifile_and_unmarked_groups(self):
        for codex in ({"implementer_session": "session"},
                      {"implementer_session": "session", "one_file": False}):
            with self.subTest(codex=codex), self.assertRaisesRegex(ValueError, "reviewer_session"):
                check.validate_review_sessions({"groups": [self.group(**codex)]}, ["A"])

    def test_one_file_group_may_skip_reviewer(self):
        groups = check.validate_review_sessions(
            {"groups": [self.group(implementer_session="writer", one_file=True)]}, ["A"])
        self.assertEqual(groups, ["A"])

    def test_both_sessions_allow_install(self):
        groups = check.validate_install_sessions(
            {"groups": [self.group(implementer_session="writer", reviewer_session="reviewer",
                                    review_status="passed")]}, ["A"])
        self.assertEqual(groups, ["A"])

    def test_reviewer_session_without_passing_review_refuses_install(self):
        for status in (None, "pending", "failed"):
            codex = {"implementer_session": "writer", "reviewer_session": "reviewer"}
            if status is not None:
                codex["review_status"] = status
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, "review_status='passed'"):
                check.validate_install_sessions({"groups": [self.group(**codex)]}, ["A"])

    def test_one_file_does_not_bypass_a_failed_review_that_was_run(self):
        group = self.group(implementer_session="writer", reviewer_session="reviewer",
                           review_status="failed", one_file=True)
        with self.assertRaisesRegex(ValueError, "review_status='passed'"):
            check.validate_install_sessions({"groups": [group]}, ["A"])

    def test_unselected_group_does_not_block_install(self):
        plan = {"groups": [
            self.group("A", implementer_session="writer", reviewer_session="reviewer", review_status="passed"),
            self.group("B"),
        ]}
        self.assertEqual(check.validate_install_sessions(plan, ["A"]), ["A"])

    def test_invalid_schema_is_refused(self):
        for codex in ({"implementer_session": " "}, {"one_file": 1}, {"review_status": "complete"}):
            with self.subTest(codex=codex), self.assertRaises(ValueError):
                check.validate_group_codex(self.group(**codex))

    def test_script_reports_failure_in_one_line(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "tracker.json"
            data.write_text(json.dumps({"tracking": {"plan": {"groups": [self.group()]}}}))
            result = subprocess.run([sys.executable, str(Path(__file__).with_name("check_install_sessions.py")),
                                     "--data", str(data), "--ids", "A"], text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("review_status='passed'", result.stderr)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
