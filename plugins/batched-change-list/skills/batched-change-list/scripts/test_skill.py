import re
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / "SKILL.md"
REFS = SKILL.parent / "references"
STEPS = ("collect.md", "present.md", "plan-fix.md", "install.md", "verify.md", "introspect.md", "gate.md", "report-rollover.md")
SHARED = ("tracker.md", "viewer.md")


class SkillLayoutTests(unittest.TestCase):
    def test_skill_stays_under_line_cap_and_every_step_links_an_existing_file(self):
        text = SKILL.read_text()
        self.assertLessEqual(len(text.splitlines()), 60)
        self.assertLessEqual(len(text.encode()), 3072)
        links = re.findall(r"\[([^\]]+)\]\(([^)]+)\)", text)
        self.assertTrue(links)
        for _label, target in links:
            if target.startswith("http"):
                continue
            path = (SKILL.parent / target).resolve()
            self.assertTrue(path.is_file(), f"missing {target}")
        for step in STEPS:
            self.assertRegex(text, rf"(?m)^- .+: \[{re.escape(step)}\]\(references/{re.escape(step)}\)")
        for name in SHARED:
            self.assertTrue((REFS / name).is_file())
            self.assertIn(f"references/{name}", text)

    def test_feedback_and_failure_contract_is_always_loaded(self):
        text = SKILL.read_text()
        for required in ("new messages throughout the cycle", "record new findings before fixing",
                         "changed requirement invalidates", "last verified outcome", "Never invent missing evidence",
                         "failed script", "stop that lane"):
            self.assertIn(required, text)


if __name__ == "__main__":
    unittest.main()
