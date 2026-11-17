import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import check_introspect as check


class IntrospectCheckTests(unittest.TestCase):
    def payload(self):
        steps = ["ROUTE coding-CLI implementation", "ROUTE coding-CLI review", "parallel group count",
                 "polling", "rework", "install", "verify", "no change needed"]
        sections = {
            "delete": {"items": [{"item": steps[0], "evidence": "The one-line replacement is enough.", "outcome": "fixed inline"}]},
            "simplify": {"none": "No separate simplification."},
            "optimize": {"none": "No optimization warranted."},
            "automate": {"none": "No automation beyond the existing check."},
        }
        return {"steps": steps, "none": steps[1:], "sections": sections}

    def test_out_of_order_sections_are_refused(self):
        payload = self.payload()
        payload["sections"] = {key: payload["sections"][key] for key in ("simplify", "delete", "optimize", "automate")}
        with self.assertRaisesRegex(ValueError, "order"):
            check.validate_introspect(payload["sections"], payload["steps"], payload["none"])

    def test_performed_step_must_be_covered(self):
        payload = self.payload()
        payload["none"].remove("polling")
        with self.assertRaisesRegex(ValueError, "polling"):
            check.validate_introspect(payload["sections"], payload["steps"], payload["none"])

    def test_ordered_sections_cover_orchestration_and_none_steps(self):
        payload = self.payload()
        covered = check.validate_introspect(payload["sections"], payload["steps"], payload["none"])
        self.assertEqual(covered["steps"], payload["steps"])
        self.assertIn("parallel group count", covered["none"])

    def test_script_refuses_missing_coverage_with_one_line(self):
        payload = self.payload()
        payload["none"].remove("polling")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "sections.json"
            source.write_text(json.dumps(payload))
            result = subprocess.run([sys.executable, str(Path(__file__).with_name("check_introspect.py")),
                                     "--sections", str(source)], text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("polling", result.stderr)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
