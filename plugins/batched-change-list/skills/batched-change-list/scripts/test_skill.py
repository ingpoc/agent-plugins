import re
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / "SKILL.md"
REFS = SKILL.parent / "references"
STEPS = ("Collect", "Present", "Plan & fix", "Install", "Verify", "Introspect", "Gate", "Report & roll over")
SHARED = ("tracker.md", "viewer.md")


class SkillLayoutTests(unittest.TestCase):
    def test_skill_stays_under_line_cap_and_every_step_links_an_existing_file(self):
        text = SKILL.read_text()
        self.assertLessEqual(len(text.splitlines()), 60)
        links = re.findall(r"\[([^\]]+)\]\(([^)]+)\)", text)
        self.assertTrue(links)
        for _label, target in links:
            if target.startswith("http"):
                continue
            path = (SKILL.parent / target).resolve()
            self.assertTrue(path.is_file(), f"missing {target}")
        for step in STEPS:
            self.assertIn(f"| {step} |", text)
        for name in SHARED:
            self.assertTrue((REFS / name).is_file())
            self.assertIn(f"references/{name}", text)


if __name__ == "__main__":
    unittest.main()
