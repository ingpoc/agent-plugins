#!/usr/bin/env python3
"""Unit tests for act failure taxonomy. No live apps."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("fail_feedback", ROOT / "scripts" / "fail_feedback.py")
ff = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ff)


class FailFeedbackTests(unittest.TestCase):
    def test_classify_target_missing(self):
        reason = ff.classify_failure(
            dispatched=False,
            verified=False,
            before_text='[0] AXWindow "X"',
            after_text='[0] AXWindow "X"',
            results=[{"ok": False, "error": "Label not found"}],
            error="Label not found",
        )
        self.assertEqual(reason, ff.TARGET_MISSING)

    def test_classify_stale_id(self):
        reason = ff.classify_failure(
            dispatched=False,
            verified=False,
            before_text="x",
            after_text="x",
            results=[{"ok": False, "error": "element index 12 is stale / detached"}],
            error="element index 12 is stale / detached",
        )
        self.assertEqual(reason, ff.STALE_ID)

    def test_classify_expect_unverified(self):
        reason = ff.classify_failure(
            dispatched=True,
            verified=False,
            before_text='value="a"',
            after_text='value="b"',
            results=[{"ok": True}],
            error="completion_unverified: ...",
        )
        self.assertEqual(reason, ff.EXPECT_UNVERIFIED)

    def test_classify_observation_incomplete(self):
        reason = ff.classify_failure(
            dispatched=False,
            verified=False,
            before_text="",
            after_text="",
            results=[],
            error="empty CUAService state",
        )
        self.assertEqual(reason, ff.OBSERVATION_INCOMPLETE)

    def test_format_is_compact_and_typed(self):
        tree = '[0] AXWindow "Calc"\n' + "\n".join(
            f'  [{i}] AXButton "B{i}"' for i in range(1, 40)
        ) + '\n  [40] AXStaticText value="7"'
        text = ff.format_failure_text(
            reason=ff.TARGET_MISSING,
            arguments={"label": "Nope", "expect": "9"},
            before_text=tree,
            after_text=tree,
            results=[{"ok": False, "error": "Label not found", "method": "ax-press"}],
            error="Label not found",
        )
        self.assertIn("reason: target_missing", text)
        self.assertLess(len(text), len(tree) // 2)
        self.assertNotIn('AXButton "B20"', text)

    def test_apply_mutates_payload(self):
        after = '[0] AXWindow "W"\n  [1] AXButton "Save"\n  [2] AXStaticText value="draft"'
        payload = {
            "ok": False,
            "verified": False,
            "dispatched": True,
            "error": "completion_unverified: ...",
            "error_type": "completion_unverified",
            "results": [{"ok": True, "method": "ax-press"}],
            "text": after,
        }
        out = ff.apply_failure_feedback(
            payload,
            arguments={"label": "Save", "expect": "published"},
            before_text=after.replace("draft", "old"),
            after_text=after,
        )
        self.assertEqual(out["error_type"], ff.EXPECT_UNVERIFIED)
        self.assertEqual(out["failure"]["reason"], ff.EXPECT_UNVERIFIED)
        self.assertTrue(out["full_text_omitted"])
        self.assertIn("reason: expect_unverified", out["text"])
        self.assertIn("Save", out["text"])


if __name__ == "__main__":
    unittest.main()
