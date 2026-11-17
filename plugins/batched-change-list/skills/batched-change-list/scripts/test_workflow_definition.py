import re
import unittest
from unittest.mock import patch
from pathlib import Path

import render
import workflow_definition as workflow

SKILL = Path(__file__).resolve().parents[1] / "SKILL.md"
REFERENCES = SKILL.parent / "references"
SCRIPTS = Path(__file__).resolve().parent

class WorkflowDefinitionTests(unittest.TestCase):
    def test_viewer_stage_order_comes_from_definition(self):
        page = render.render_workflow("gate", [], 0, 15, False, 0, 1)
        self.assertEqual(re.findall(r'data-stage="([^"]+)"', page), [stage["id"] for stage in workflow.STAGES])

    def test_every_required_check_has_a_script_test_and_triggered_reference(self):
        reference_for_check = {"install": "install.md", "introspect": "introspect.md", "gate": "gate.md", "merge_gate": "gate.md"}
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
            if check_name:
                with self.subTest(stage=stage["id"]), patch.object(workflow, "run_check", return_value="ok") as run:
                    self.assertEqual(workflow.run_stage_check(stage["id"], "fixture"), "ok")
                    run.assert_called_once_with(check_name, "fixture")

    def test_gate_thresholds_are_definition_driven_and_keep_default_rollover(self):
        self.assertEqual(workflow.DEFAULT_ROLLOVER_THRESHOLD, 15)
        self.assertEqual(workflow.MERGE_PREVIOUS_PR_THRESHOLD, 16)
        count = lambda n: {"findings": [{"id": str(i), "status": "verified_fixed"} for i in range(n)], "tracking": {}}
        self.assertEqual(workflow.run_stage_check("report_rollover", count(15)), (15, 15, True))
        self.assertEqual(workflow.run_stage_check("merged", count(15)), (15, 16, False))
        self.assertEqual(workflow.run_stage_check("merged", count(16)), (16, 16, True))

if __name__ == "__main__": unittest.main()
