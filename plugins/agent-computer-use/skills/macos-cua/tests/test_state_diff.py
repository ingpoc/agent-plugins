#!/usr/bin/env python3
"""Unit tests for Codex-style AX state diffs. No live apps."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "state_diff", ROOT / "scripts" / "state_diff.py"
)
state_diff = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(state_diff)


class StateDiffTests(unittest.TestCase):
    def test_reports_added_removed_changed(self):
        previous = (
            'Window: "WhatsApp"\n'
            '  [30] AXTextArea "Compose message"\n'
            '  [46] AXButton "Voice message"'
        )
        current = (
            'Window: "WhatsApp"\n'
            '  [30] AXTextArea "Compose message" value="token"\n'
            '  [46] AXButton "Send"'
        )
        payload = state_diff.diff_text(previous, current)
        self.assertEqual(payload["added"], 0)
        self.assertEqual(payload["removed"], 0)
        self.assertEqual(payload["changed"], 2)
        self.assertIn("~", payload["text"])
        self.assertIn("Send", payload["text"])

    def test_apply_first_call_keeps_full_text(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(state_diff, "CACHE", Path(directory)):
                first = state_diff.apply("WhatsApp", 1, "Compose", "full tree", enabled=True)
                second = state_diff.apply("WhatsApp", 1, "Compose", "full tree\n  [2] AXButton", enabled=True)
        self.assertFalse(first["diff"])
        self.assertEqual(first["text"], "full tree")
        self.assertTrue(second["diff"])
        self.assertIn("+", second["text"])


if __name__ == "__main__":
    unittest.main()
