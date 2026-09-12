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
        self.assertEqual(state_delta.action_state_text(before, after), after)

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

    def test_long_single_character_edit_is_bounded(self):
        old = "a" * 10000
        new = old[:5000] + "b" + old[5001:]
        before = tree(f'  [1] AXTextArea value="{old}"')
        after = tree(f'  [8] AXTextArea value="{new}"')
        result = state_delta.action_state_text(before, after)
        self.assertLess(len(result), 600)
        self.assertIn("[8] AXTextArea: text changed", result)
        self.assertIn("old[5000:5001] -> new[5000:5001]", result)
        self.assertIn("length 10000 -> 10000", result)
        self.assertIn("excerpt, not full value", result)
        self.assertEqual(state_delta._parse(after)[1][0][0], after.split("\n", 1)[1])

    def test_multiline_quotes_and_unicode(self):
        old = 'hello "friend"\n' + "🌍é" * 300
        new = old[:200] + "🙂" + old[200:]
        result = state_delta.action_state_text(
            tree(f'  [1] AXTextArea value="{old}" {{1,2 3x4}}'),
            tree(f'  [9] AXTextArea value="{new}" {{2,3 3x4}}'))
        self.assertLess(len(result), 600)
        self.assertIn("old[200:200] -> new[200:201]", result)
        self.assertIn("🙂", result)
        self.assertIn("[9] AXTextArea", result)

    def test_deletion_reports_empty_replacement(self):
        old = "x" * 300 + "DELETE" + "y" * 300
        new = "x" * 300 + "y" * 300
        result = state_delta.action_state_text(
            tree(f'  [1] AXTextArea value="{old}"'),
            tree(f'  [2] AXTextArea value="{new}"'))
        self.assertIn("old[300:306] -> new[300:300]", result)
        self.assertIn("length 606 -> 600", result)

    def test_ambiguous_node_shaped_continuation_falls_back(self):
        before = tree(CONTEXT, '  [1] AXTextArea value="start\n  [2] AXButton fake\nend"')
        after = before.replace("start", "changed")
        self.assertEqual(state_delta.action_state_text(before, after), after)
        quoted = tree(CONTEXT, '  [1] AXTextArea value="start"\n  [2] AXTextArea value="fake\nend"')
        self.assertEqual(state_delta.action_state_text(quoted, quoted.replace("start", "changed")),
                         quoted.replace("start", "changed"))
        malformed = tree(CONTEXT, '  [1] AXTextArea value="unterminated')
        self.assertEqual(state_delta.action_state_text(before, malformed), malformed)

    def test_short_multiline_has_escaped_excerpt_with_context(self):
        before = tree('  [0] AXStaticText "' + 'x' * 340 + '"', '  [1] AXTextArea value="one\ntwo"')
        after = tree('  [0] AXStaticText "' + 'x' * 340 + '"', '  [7] AXTextArea value="one\nthree"')
        result = state_delta.action_state_text(before, after)
        self.assertIn("text changed", result)
        self.assertIn('one\\nthree', result)

    def test_tiny_unchanged_state_never_expands(self):
        after = '[1] AXButton'
        self.assertEqual(state_delta.action_state_text(after, after), after)

    def test_empty_and_long_delta_return_full_current_state(self):
        after = tree('  [9] AXButton "A"')
        self.assertEqual(state_delta.action_state_text("", after), after)
        before = tree('  [1] AXButton "Old"')
        self.assertEqual(state_delta.action_state_text(before, after), after)


if __name__ == "__main__":
    unittest.main()
