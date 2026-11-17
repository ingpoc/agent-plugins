import re
import tempfile
import unittest
from pathlib import Path
import workflow_definition
import statechart

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

    def test_generated_statechart_is_current_and_stale_or_missing_fails_shape_check(self):
        chart = SKILL.parent / "statechart.html"
        self.assertTrue(statechart.is_current(chart), "run python3 scripts/statechart.py to regenerate the chart")
        rendered = statechart.render()
        self.assertIn("<h2>Current</h2>", rendered)
        self.assertIn("<h2>After trigger</h2>", rendered)
        changed_skill = SKILL.read_bytes().replace(b"Workflow: Collect and Present once", b"Workflow: Collect and Present initially", 1)
        changed_chart = statechart.render(changed_skill)
        self.assertNotEqual(changed_chart, rendered)
        self.assertFalse(statechart.is_current(chart, changed_chart), "non-link SKILL.md edits must stale the chart")
        with tempfile.TemporaryDirectory() as temporary:
            stale = Path(temporary) / "statechart.html"
            self.assertFalse(statechart.is_current(stale, rendered))
            stale.write_text("stale", encoding="utf-8")
            self.assertFalse(statechart.is_current(stale, rendered))


if __name__ == "__main__":
    unittest.main()
