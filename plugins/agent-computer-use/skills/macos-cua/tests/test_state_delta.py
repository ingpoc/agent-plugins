#!/usr/bin/env python3
"""Unit tests for action-local AX state deltas. No live apps."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("state_delta", ROOT / "scripts" / "state_delta.py")
state_delta = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(state_delta)


def tree(*lines: str) -> str:
    return 'Window: "Mail"\n' + "\n".join(lines)


CONTEXT = '  [0] AXStaticText value="' + "x" * 300 + '"'


class StateDeltaTests(unittest.TestCase):
    def test_reindexing_and_geometry_only_are_unchanged(self):
        before = tree('  [2] AXButton "Send" {1,2 30x20}')
        after = tree('  [9] AXButton "Send" {50,60 30x20}')
        self.assertIn("No changes during action", state_delta.action_state_text(before, after))

    def test_added_and_removed_lines_keep_only_current_ids(self):
        before = tree(CONTEXT, '  [2] AXButton "Archive"', '  [3] AXButton "Delete"')
        after = tree(CONTEXT, '  [8] AXButton "Archive"', '  [9] AXButton "Reply"')
        result = state_delta.action_state_text(before, after)
        self.assertIn('-   AXButton "Delete"', result)
        self.assertIn('~   [9] AXButton "Reply"', result)
        self.assertNotIn('[3]', result)

    def test_duplicate_labels_remain_positionally_distinct(self):
        before = tree(CONTEXT, '  [1] AXButton "Open"', '  [2] AXButton "Open"')
        after = tree(CONTEXT, '  [4] AXButton "Open"', '  [5] AXButton "Open"', '  [6] AXButton "Open"')
        result = state_delta.action_state_text(before, after)
        self.assertIn('+   [6] AXButton "Open"', result)

    def test_real_value_change_preserves_full_value(self):
        before = tree(CONTEXT, '  [1] AXTextArea value="draft"')
        after = tree(CONTEXT, '  [7] AXTextArea value="final message"')
        result = state_delta.action_state_text(before, after)
        self.assertIn('value="final message"', result)
        self.assertIn('-   AXTextArea value="draft"', result)

    def test_window_switch_and_malformed_input_return_full_current_state(self):
        before = tree('  [1] AXButton "Send"')
        switched = 'Window: "Notes"\n  [2] AXButton "Send"'
        malformed = 'Window: "Mail"\n  [2] AXTextArea value="line one\nline two"'
        self.assertEqual(state_delta.action_state_text(before, switched), switched)
        self.assertEqual(state_delta.action_state_text(before, malformed), malformed)

    def test_empty_and_long_delta_return_full_current_state(self):
        after = tree('  [9] AXButton "A"')
        self.assertEqual(state_delta.action_state_text("", after), after)
        before = tree('  [1] AXButton "Old"')
        self.assertEqual(state_delta.action_state_text(before, after), after)


if __name__ == "__main__":
    unittest.main()
