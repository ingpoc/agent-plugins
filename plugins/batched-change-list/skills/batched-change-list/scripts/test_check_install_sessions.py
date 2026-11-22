import unittest

import check_install_sessions as check


class InstallSessionTests(unittest.TestCase):
    def group(self, identifier="A", **codex):
        return {"id": identifier, "finding_ids": [identifier], "codex": codex, "presuite": {"status": "passed"}}

    def test_review_requires_implementer(self):
        with self.assertRaisesRegex(ValueError, "implementer_session"):
            check.validate_review_sessions({"groups": [self.group()]}, ["A"])

    def test_review_starts_without_reviewer_session(self):
        groups = check.validate_review_sessions(
            {"groups": [self.group(implementer_session="writer", fix_status="passed")]}, ["A"])
        self.assertEqual(groups, ["A"])

    def test_one_file_group_may_skip_reviewer(self):
        groups = check.validate_review_sessions(
            {"groups": [self.group(implementer_session="writer", fix_status="passed", one_file=True)]}, ["A"])
        self.assertEqual(groups, ["A"])

    def test_invalid_schema_is_refused(self):
        for codex in ({"implementer_session": " "}, {"one_file": 1}, {"review_status": "complete"}):
            with self.subTest(codex=codex), self.assertRaises(ValueError):
                check.validate_group_codex(self.group(**codex))


if __name__ == "__main__":
    unittest.main()
