import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import check_introspect as check


class IntrospectCheckTests(unittest.TestCase):
    def tracking(self):
        return {"round": 1, "cycle": 1, "stages_reached": ["plan_fix", "install", "verify"],
                "plan": {"round": 1, "cycle": 1, "groups": [
                    {"id": "A", "codex": {"implementer_session": "writer", "reviewer_session": "reviewer"},
                     "verify": {"command": "python3 -m unittest test_a.py"}},
                    {"id": "B", "codex": {"implementer_session": "writer-b", "one_file": True},
                     "verify": {"command": "python3 -m unittest test_b.py"}},
                ]}}

    def payload(self, tracking=None):
        steps = [*check.performed_round_steps(tracking or self.tracking()), "polling", "rework", "tests"]
        sections = {
            "delete": {"items": [{"item": steps[0], "evidence": "The one-line replacement is enough.", "outcome": "fixed inline", "proof": "python3 test_hooks.py"}]},
            "simplify": {"none": "No separate simplification."},
            "optimize": {"none": "No optimization warranted."},
            "automate": {"none": "No automation beyond the existing check."},
        }
        return {"steps": steps, "none": steps[1:], "sections": sections}

    def test_draft_uses_performed_round_steps_and_requires_review(self):
        draft = check.draft_introspect(self.tracking())
        self.assertEqual(draft["steps"], check.performed_round_steps(self.tracking()))
        self.assertEqual(draft["none"], [])
        self.assertEqual(list(draft["sections"]), list(check.SECTION_ORDER))
        self.assertTrue(all(section["none"].startswith("Review performed steps")
                            for section in draft["sections"].values()))
        with self.assertRaisesRegex(ValueError, "lack section or none coverage"):
            check.validate_introspect(draft["sections"], draft["steps"], draft["none"], self.tracking())

    def test_performed_steps_preserve_current_round_history(self):
        tracking = self.tracking()
        tracking["introspect_history"] = [{"cycle": 1, "round": 1,
            "result": "recorded", "at": "2026-11-18T00:00:00Z", "steps": ["manual polling"],
            "none": ["manual polling"], "sections": {name: {"none": "Reviewed."}
                for name in ("delete", "simplify", "optimize", "automate")}}]
        self.assertIn("manual polling", check.performed_round_steps(tracking))

    def test_out_of_order_sections_are_refused(self):
        payload = self.payload()
        payload["sections"] = {key: payload["sections"][key] for key in ("simplify", "delete", "optimize", "automate")}
        with self.assertRaisesRegex(ValueError, "order"):
            check.validate_introspect(payload["sections"], payload["steps"], payload["none"], self.tracking())

    def test_omitted_tracker_recorded_orchestration_step_is_refused(self):
        tracking = self.tracking()
        steps = ["Stage: Verify"]
        sections = {name: {"none": "Reviewed."} for name in ("delete", "simplify", "optimize", "automate")}
        with self.assertRaisesRegex(ValueError, "Parallel group count"):
            check.validate_introspect(sections, steps, steps, tracking)

    def test_broad_labels_cannot_omit_recorded_test_commands(self):
        tracking = self.tracking()
        steps = [step for step in check.performed_round_steps(tracking)
                 if not step.startswith("Test command:")]
        sections = {name: {"none": "Reviewed."} for name in ("delete", "simplify", "optimize", "automate")}
        with self.assertRaisesRegex(ValueError, "Test command: A"):
            check.validate_introspect(sections, steps, steps, tracking)

    def test_performed_step_must_be_covered(self):
        payload = self.payload()
        payload["none"].remove("polling")
        with self.assertRaisesRegex(ValueError, "polling"):
            check.validate_introspect(payload["sections"], payload["steps"], payload["none"], self.tracking())

    def test_ordered_sections_cover_orchestration_and_none_steps(self):
        payload = self.payload()
        covered = check.validate_introspect(payload["sections"], payload["steps"], payload["none"], self.tracking())
        self.assertEqual(covered["steps"], payload["steps"])
        self.assertIn("Parallel group count: 2", covered["none"])
        self.assertIn("Codex write: A", covered["steps"])
        self.assertIn("Codex review: A", covered["steps"])
        self.assertIn("Test command: A: python3 -m unittest test_a.py", covered["steps"])
        self.assertIn("Test command: B: python3 -m unittest test_b.py", covered["steps"])

    def test_script_refuses_missing_coverage_with_one_line(self):
        payload = self.payload()
        payload["none"].remove("polling")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "sections.json"
            tracker = Path(directory) / "tracker.json"
            source.write_text(json.dumps(payload))
            tracker.write_text(json.dumps({"tracking": self.tracking()}))
            result = subprocess.run([sys.executable, str(Path(__file__).with_name("check_introspect.py")),
                                     "--data", str(tracker), "--sections", str(source)], text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("polling", result.stderr)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
