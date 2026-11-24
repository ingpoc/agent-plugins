import json
import re
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
DEFINITION = json.loads((SKILL / "scripts/workflow_definition.json").read_text(encoding="utf-8"))


class ReferenceTests(unittest.TestCase):
    def test_docs_do_not_advertise_retired_commands(self):
        docs = [SKILL.parent.parent / "README.md", SKILL / "SKILL.md",
                *(SKILL / "references").rglob("*.md")]
        command = re.compile(r"\bbcl(?:\s+--data\s+\S+)?\s+sync-main\b")
        self.assertRegex("bcl --data FINDINGS.json sync-main", command)
        self.assertIsNone(command.search("bcl merge --into ID"))
        self.assertIsNone(command.search("bcl merged; merging branches"))
        for path in docs:
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(command.search(text), f"{path} advertises a retired command")
        readme = docs[0].read_text(encoding="utf-8")
        stage_list = readme.split("**Workflow command.**", 1)[1].split(". Successful stages", 1)[0]
        self.assertNotRegex(stage_list, r"\bsync-main\b", "README stage list advertises a retired command")

    def test_references_have_no_literal_gate_numbers(self):
        values = (DEFINITION["gate"]["default_threshold"], DEFINITION["gate"]["merge_previous_pr_threshold"])
        gate_terms = re.compile(r"\b(?:verified|gate|merge|count|threshold)\b", re.I)
        for path in [*(p for p in (SKILL / "references").rglob("*") if p.is_file()), SKILL / "SKILL.md"]:
            text = path.read_text(encoding="utf-8")
            for value in values:
                for match in re.finditer(rf"\b{value}\b", text):
                    excerpt = text[max(0, match.start() - 80):match.end() + 80]
                    self.assertIsNone(gate_terms.search(excerpt), f"{path.relative_to(SKILL)} contains literal gate value {value}")

    def test_plan_fix_lets_small_docs_items_share_round(self):
        text = (SKILL / "references/plan-fix.md").read_text(encoding="utf-8").lower()
        for phrase in ("docs-only", "chart-only", "no code dependency", "no shared file", "nearest group",
                       "4 open lanes", "named test", "verify"):
            self.assertIn(phrase, text)

    def test_review_keeps_quota_out_fallback_order(self):
        text = (SKILL / "references/review.md").read_text(encoding="utf-8").lower()
        text = text[text.find("quota-out fallback"):]
        codex, opencode, owner = text.find("codex first"), text.find("free opencode"), text.find("ask the owner")
        self.assertTrue(0 <= codex < opencode < owner, "review.md must give the quota-out order Codex, OpenCode free, owner")

    def test_review_runs_one_pre_push_suite_per_lane(self):
        text = (SKILL / "references/review.md").read_text(encoding="utf-8").lower()
        for phrase in ("once per lane, right before push", "scripts/check.py", "only the lane's tests",
                       "share one job"):
            self.assertIn(phrase, text)
        self.assertNotIn("unittest discover", text)

    def test_review_does_not_make_version_or_changelog_a_must_fix(self):
        text = (SKILL / "references/review.md").read_text(encoding="utf-8").lower()
        self.assertEqual(
            text.count("the plugin version and changelog belong to integration, not to a lane-review must-fix."),
            1,
        )


if __name__ == "__main__":
    unittest.main()
