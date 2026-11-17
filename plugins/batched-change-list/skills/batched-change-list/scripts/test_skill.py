import re
import unittest
from pathlib import Path
import workflow_definition

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

    def test_router_body_shrinks_and_loop_line_matches_workflow_definition(self):
        text = SKILL.read_text()
        body = text.split("---", 2)[2].lstrip("\n")
        self.assertLessEqual(len(body.encode()), 2589)
        lines = [line for line in body.splitlines() if line.startswith("Workflow: ")]
        self.assertEqual(lines, [workflow_definition.skill_loop_line()])


if __name__ == "__main__":
    unittest.main()
