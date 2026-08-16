import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Load runtime_vision with minimal stubs for injected names it does not need at import.
SPEC = importlib.util.spec_from_file_location(
    "runtime_vision", ROOT / "scripts" / "runtime_vision.py"
)
mod = importlib.util.module_from_spec(SPEC)
sys.modules["runtime_vision"] = mod
SPEC.loader.exec_module(mod)


class LabelResolveTests(unittest.TestCase):
    def test_single_primary_wins_over_static_duplicate(self):
        elements = [
            {"element_index": 1, "role": "AXStaticText", "label": "Continue as Ada"},
            {"element_index": 2, "role": "AXButton", "label": "Continue as Ada"},
        ]
        idx = mod._match_label(elements, "Continue as Ada", mod.CLICK_ROLES)
        self.assertEqual(idx, 2)

    def test_two_primary_exact_matches_are_ambiguous(self):
        elements = [
            {"element_index": 1, "role": "AXButton", "label": "Continue as Ada"},
            {"element_index": 2, "role": "AXLink", "label": "Continue as Ada"},
        ]
        with self.assertRaises(mod.AmbiguousLabelError):
            mod._match_label(elements, "Continue as Ada", mod.CLICK_ROLES)

    def test_resolve_clickable_index_returns_error_dict(self):
        snap = {
            "elements": [
                {"element_index": 1, "role": "AXButton", "label": "Save"},
                {"element_index": 2, "role": "AXButton", "label": "Save"},
            ]
        }
        idx, err = mod.resolve_clickable_index(snap, "Save")
        self.assertIsNone(idx)
        self.assertEqual(err["error_code"], "ambiguous_label")


if __name__ == "__main__":
    unittest.main()
