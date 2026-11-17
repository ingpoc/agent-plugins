import re
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import render
import workflow_definition as workflow

SKILL = Path(__file__).resolve().parents[1] / "SKILL.md"
REFERENCES = SKILL.parent / "references"
SCRIPTS = Path(__file__).resolve().parent


class WorkflowDefinitionTests(unittest.TestCase):
    def test_viewer_stage_order_and_skill_loop_come_from_definition(self):
        page = render.render_workflow("gate", [], 0, 15, False, 0, 1)
        page_stages = re.findall(r'data-stage="([^"]+)"', page)
        self.assertEqual(page_stages, [stage["id"] for stage in workflow.STAGES])
        skill_line = next(line for line in SKILL.read_text().splitlines() if line.startswith("Workflow: "))
        self.assertEqual(skill_line, workflow.skill_loop_line())

    def test_skill_loop_generator_writes_the_machine_defined_line(self):
        text = SKILL.read_text()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "SKILL.md"
            target.write_text(text)
            workflow.write_skill_loop(target)
            generated = next(line for line in target.read_text().splitlines() if line.startswith("Workflow: "))
        self.assertEqual(generated, workflow.skill_loop_line())

    def test_every_required_check_has_a_script_test_and_triggered_reference(self):
        reference_for_check = {"install": "install.md", "introspect": "introspect.md", "gate": "gate.md"}
        for name, check in workflow.REQUIRED_CHECKS.items():
            with self.subTest(name=name):
                self.assertTrue((SCRIPTS / check["file"]).is_file())
                self.assertTrue((SCRIPTS / f"test_{check['module']}.py").is_file())
                self.assertIn(check["file"], (REFERENCES / reference_for_check[name]).read_text())

    def test_each_declared_transition_has_one_enforced_prerequisite_set(self):
        for stage, requires in workflow.TRANSITIONS.items():
            if isinstance(requires, dict):
                with self.subTest(stage=stage), self.assertRaisesRegex(ValueError, "requires"):
                    workflow.require_transition(stage, [requires["when_reached"]])
                workflow.require_transition(stage, [requires["when_reached"], *requires["requires"]])
                workflow.require_transition(stage, [])
            else:
                with self.subTest(stage=stage), self.assertRaisesRegex(ValueError, "requires"):
                    workflow.require_transition(stage, [])
                workflow.require_transition(stage, requires)

    def test_stage_checks_dispatch_through_each_declared_check(self):
        for stage in workflow.STAGES:
            check_name = stage.get("check")
            if not check_name:
                continue
            with self.subTest(stage=stage["id"]), patch.object(workflow, "run_check", return_value="ok") as run:
                self.assertEqual(workflow.run_stage_check(stage["id"], "fixture"), "ok")
                run.assert_called_once_with(check_name, "fixture")


if __name__ == "__main__":
    unittest.main()
