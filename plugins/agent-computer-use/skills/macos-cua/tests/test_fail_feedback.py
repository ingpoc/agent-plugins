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
    def test_missing_after_state_discards_old_ids_and_deduplicates_payload(self):
        out = ff.apply_failure_feedback(
            {"ok": False, "dispatched": True, "verified": False,
             "error": "completion_unverified", "expect": "done",
             "results": [{"ok": True}, {"ok": False, "error": "completion_unverified"}]},
            arguments={"label": "OK", "expect": "done"},
            before_text='[0] AXWindow "Dialog"\n[1] AXButton "OK"', after_text="",
        )
        self.assertEqual(out["error_type"], ff.OBSERVATION_INCOMPLETE)
        self.assertNotIn('[1]', out["text"])
        self.assertEqual(out["failure"]["completed_steps"], 1)
        self.assertEqual(out["failure"]["failed_step"], 2)
        self.assertEqual(out["failure"]["observation"]["state"], "empty")
        for key in ("error", "results", "expect"):
            self.assertNotIn(key, out)

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

    def test_failure_facts_keep_expected_target_and_near_miss_without_tree_dump(self):
        tree = '[0] AXWindow "Editor — report"\n[1] AXButton "Save draft"\n[2] AXButton "Cancel"'
        out = ff.apply_failure_feedback(
            {"ok": False, "dispatched": False, "verified": False,
             "error": "AX target lookup failed", "results": [
                 {"ok": False, "error": "Label not found", "method": "ax-press"}]},
            arguments={"app": "TextEdit", "steps": [{"op": "click", "label": "Save"}]},
            before_text=tree, after_text=tree,
        )
        failure = out["failure"]
        self.assertEqual(failure["expected_target"], {"op": "click", "label": "Save", "app": "TextEdit"})
        self.assertEqual(failure["found_target"]["method"], "ax-press")
        self.assertEqual(failure["observation"]["match_count"], 0)
        self.assertEqual(failure["observation"]["near_miss_count"], 1)
        self.assertTrue(any('AXButton "Save draft"' in line
                            for line in failure["observation"]["candidates"]))
        serialized = str(failure) + out["text"]
        self.assertNotIn('AXButton "Cancel"', serialized)
        self.assertNotIn('AXButton "Cancel"', out["text"])
        self.assertIn("AX target lookup failed", failure["error"])

    def test_missing_label_error_names_the_step_with_that_label(self):
        err = "RPC error -32602: Label not found: Multiply. elementCount=16 axTrusted=true."
        out = ff.apply_failure_feedback(
            {"ok": False, "dispatched": False, "verified": False, "error": err, "failed_step": 1,
             "results": [{"ok": False, "error": err}]},
            arguments={"app": "Calculator", "steps": [
                {"op": "click", "label": "All Clear"}, {"op": "click", "label": "1"},
                {"op": "click", "label": "Multiply"}]},
            before_text='[0] AXWindow "Calculator"', after_text='[0] AXWindow "Calculator"',
        )
        self.assertEqual(out["failure"]["failed_step"], 3)
        self.assertEqual(out["failure"]["expected_target"]["label"], "Multiply")

    def test_failure_target_uses_reported_failed_step(self):
        out = ff.apply_failure_feedback(
            {"ok": False, "dispatched": False, "verified": False,
             "error": "Label not found: Multiply",
             "results": [{"ok": True}, {"ok": False, "failed_step": 1,
                        "error": "Label not found"}]},
            arguments={"app": "Calculator", "steps": [
                {"op": "click", "label": "Multiply"},
                {"op": "click", "label": "9"},
            ]},
            before_text='[0] AXWindow "Calculator"', after_text='[0] AXWindow "Calculator"',
        )
        self.assertEqual(out["failure"]["expected_target"]["label"], "Multiply")

    def test_credential_target_redacts_typed_and_observed_values(self):
        sentinel = "password-observation-sentinel-987654321"
        tree = f'[0] AXWindow "Login"\n[1] AXTextField "Password" value="{sentinel}"'
        out = ff.apply_failure_feedback(
            {"ok": False, "dispatched": False, "verified": False,
             "error": "AX target lookup failed", "results": [
                 {"ok": False, "error": "Password field not found", "value": sentinel}]},
            arguments={"app": "Browser", "steps": [{"op": "type_text", "label": "Password",
                                                        "text": sentinel, "expect": sentinel}]},
            before_text=tree, after_text=tree,
        )
        self.assertNotIn(sentinel, str(out))
        self.assertEqual(out["failure"]["expected_target"]["text"], "[redacted]")
        self.assertEqual(out["failure"]["found_target"]["value"], "[redacted]")
        self.assertEqual(out["failure"]["observation"]["candidates"], [])

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
        self.assertNotIn("reason", out["failure"])
        # The settled observation is summarized without returning the full AX tree.
        self.assertTrue(out["full_text_omitted"])
        self.assertIn("settled observation:", out["text"])
        self.assertNotIn('AXStaticText value="draft"', out["text"])
        self.assertIn("reason: expect_unverified", out["text"])
        self.assertIn("Save", out["text"])

    def test_expect_unverified_logs_short_expected_and_readback_for_plan(self):
        out = ff.apply_failure_feedback(
            {"ok": False, "verified": False, "dispatched": True,
             "error": "completion_unverified", "results": [{"ok": True}]},
            arguments={
                "app": "Calculator",
                "steps": [{"op": "click", "label": label} for label in (
                    "All Clear", "9", "9", "Multiply", "9", "9", "Equals",
                    "All Clear", "8", "8", "Multiply", "8", "8", "Equals",
                    "All Clear", "7", "7", "Multiply", "7", "7", "Equals",
                )],
                "expect": "5929",
            },
            before_text='[0] AXWindow "Calculator"',
            after_text='[0] AXWindow "Calculator"\n[1] AXStaticText value="5,929"',
        )
        self.assertEqual(out["failure"]["expectation_check"], {
            "expected": "5929",
            "found": ['[1] AXStaticText value="5,929"'],
        })


if __name__ == "__main__":
    unittest.main()
