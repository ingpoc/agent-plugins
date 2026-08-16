import importlib.util
import json
from pathlib import Path
import unittest


SKILL = Path(__file__).resolve().parents[1]
RUNNER = SKILL / "scripts" / "run_benchmarks.py"
CONTRACT = SKILL / "references" / "entry-contract.json"


def load_runner():
    spec = importlib.util.spec_from_file_location("macos_cua_run_benchmarks", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BenchmarkSuiteTests(unittest.TestCase):
    def test_suite_rows_are_complete(self):
        data = json.loads(CONTRACT.read_text())
        names = [row["name"] for row in data["suite"]]
        self.assertEqual(
            names,
            [
                "calculator-8x8",
                "folder-downloads",
                "textedit-right-click",
                "whatsapp-new-chat",
            ],
        )
        runner = load_runner()
        for row in data["suite"]:
            missing = [key for key in runner.REQUIRED if key not in row]
            self.assertEqual(missing, [], row.get("name"))
        whatsapp = next(row for row in data["suite"] if row["name"] == "whatsapp-new-chat")
        self.assertIn("no send", whatsapp["pass_signal"])
        self.assertIn("AXHeading", whatsapp["pass_signal"])

    def test_visible_ax_text_strips_bidi_marks(self):
        runner = load_runner()
        self.assertEqual(runner.visible_ax_text("\u200e64"), "64")
        self.assertEqual(runner.visible_ax_text("8\u00d78"), "8\u00d78")

    def test_score_weights_visibility_speed_and_accuracy_equally(self):
        runner = load_runner()
        row = {
            "budget_seconds": 25,
            "bytes_budget": 12000,
            "pointer_required": True,
            "max_step_ms": 2500,
        }
        criteria = runner.score(
            row,
            {
                "readback": True,
                "cursor_visible": True,
                "robust": True,
                "asserted_batch": True,
                "output_bytes": 400,
                "duration_s": 6.1,
                "max_step_ms": 1346,
            },
        )
        self.assertEqual(set(criteria), set(runner.CRITERIA))
        self.assertTrue(all(criteria.values()))
        failed = runner.score(
            row,
            {
                "readback": True,
                "cursor_visible": False,
                "robust": True,
                "asserted_batch": True,
                "output_bytes": 12001,
                "duration_s": 26,
                "max_step_ms": 8000,
            },
        )
        self.assertFalse(failed["context_efficiency"])
        self.assertFalse(failed["speed"])
        self.assertFalse(failed["visibility"])
        self.assertTrue(failed["accuracy"])
        silent = runner.score(
            row,
            {
                "readback": True,
                "cursor_visible": False,
                "robust": True,
                "asserted_batch": True,
                "output_bytes": 400,
                "duration_s": 6.1,
                "max_step_ms": 400,
            },
        )
        self.assertFalse(silent["visibility"])
        self.assertTrue(silent["accuracy"])
        self.assertTrue(silent["speed"])
        untimed = runner.score(
            row,
            {
                "readback": True,
                "cursor_visible": True,
                "robust": True,
                "asserted_batch": True,
                "output_bytes": 400,
                "duration_s": 6.1,
                "max_step_ms": 0,
            },
        )
        self.assertFalse(untimed["speed"])

    def test_steps_show_cursor_requires_glide_method(self):
        runner = load_runner()
        self.assertTrue(
            runner.steps_show_cursor(
                [
                    {
                        "action": "click",
                        "method": "agent-cursor-glide+native-axpress-fallback",
                    }
                ]
            )
        )
        self.assertFalse(
            runner.steps_show_cursor(
                [{"action": "click", "method": "native_ax", "path": "native_ax"}]
            )
        )


if __name__ == "__main__":
    unittest.main()
