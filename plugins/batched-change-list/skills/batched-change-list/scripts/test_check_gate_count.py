import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import check_gate_count as check
import render


class GateCountTests(unittest.TestCase):
    def test_uses_the_renderer_gate_definition(self):
        data = {"findings": [], "tracking": {}}
        self.assertEqual(check.gate_count(data), render.rollover_gate(data["findings"], data["tracking"]))

    def test_merge_gate_count_uses_the_merge_threshold(self):
        def data(count):
            return {"findings": [{"id": f"ISSUE-{i}", "status": "verified_fixed"} for i in range(count)], "tracking": {}}
        self.assertEqual(check.merge_gate_count(data(15)), (15, 16, False))
        self.assertEqual(check.merge_gate_count(data(16)), (16, 16, True))

    def test_script_reports_count_from_a_temp_tracker(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "tracker.json"
            data.write_text(json.dumps({"findings": [], "tracking": {}}))
            result = subprocess.run([sys.executable, str(Path(__file__).with_name("check_gate_count.py")),
                                     "--data", str(data)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Gate count: 0 of 15; met=true", result.stdout)

    def test_invalid_tracker_fails_loudly(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "tracker.json"
            data.write_text("{}")
            result = subprocess.run([sys.executable, str(Path(__file__).with_name("check_gate_count.py")),
                                     "--data", str(data)], text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
