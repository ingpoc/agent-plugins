import re
import hashlib
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
        self.assertLessEqual(len(text.splitlines()), 90)
        self.assertLessEqual(len(text.encode()), 3072)
        self.assertRegex(text, r"(?m)^\| When \| Load \|")
        self.assertLessEqual(sum(line.startswith("|") and not re.match(r"\|\s*:?-+", line) and not line.lower().startswith("| when") for line in text.splitlines()), 5)
        for path in REFS.glob("*.md"):
            self.assertIn(f"references/{path.name}", text, f"unnamed reference {path.name}")
        for name in SHARED:
            self.assertTrue((REFS / name).is_file())

    def test_numbered_path_tracks_workflow_definition_and_names_check_scripts(self):
        text = SKILL.read_text()
        numbered = re.findall(r"(?m)^\d+\. (.+)$", text)
        expected = [step["label"] for step in workflow_definition.STAGES[:7]] + ["Report & roll over / Merge"]
        self.assertEqual(numbered, expected)
        for name in ("check_gate_count.py", "check_install_sessions.py", "check_introspect.py"):
            self.assertIn(name, text)

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
        self.assertRegex(rendered, r'<html lang="en" data-source-hash="[0-9a-f]{64}">')
        digest = hashlib.sha256(b"SKILL.md\n" + SKILL.read_bytes() + b"".join(path.name.encode() + b"\n" + path.read_bytes() for path in sorted(REFS.glob("*.md")))).hexdigest()
        self.assertIn(f'data-source-hash="{digest}"', rendered)
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
