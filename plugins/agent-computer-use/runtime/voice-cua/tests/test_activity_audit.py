"""Tool-call audit trail: plan shape is logged, dictated content never is."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from voice_cua import activity_log  # noqa: E402


class PlanShape(unittest.TestCase):
    def test_steps_keep_ops_targets_and_drop_typed_text(self) -> None:
        shape = activity_log.plan_shape({
            "app": "TextEdit",
            "steps": [
                {"op": "key", "key": "cmd+n"},
                {"op": "type_text", "text": "my secret diary entry"},
                {"op": "click", "label": "Bold"},
                {"op": "set_value", "element": 9, "value": "hunter2"},
            ],
        })
        self.assertEqual(shape, ["op:key cmd+n", "op:type_text text=21ch", "op:click label=Bold", "op:set_value el=9 value=7ch"])
        self.assertNotIn("diary", json.dumps(shape))
        self.assertNotIn("hunter2", json.dumps(shape))

    def test_single_op_and_state(self) -> None:
        self.assertEqual(activity_log.plan_shape({"app": "Calculator", "op": "click", "label": "3"}), ["op:click label=3"])
        self.assertEqual(activity_log.plan_shape({"app": "Finder", "query": "Downloads"}, tool="cua_state"), ["op:state query=9ch"])

    def test_tool_rows_carry_plan_hint_and_failed_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "a.jsonl"
            with patch.dict(os.environ, {"VOICE_CUA_ACTIVITY_LOG": str(log)}):
                activity_log.log_tool_start("cua_act", {"app": "TextEdit", "steps": [{"op": "type_text", "text": "abc"}]})
                activity_log.log_tool_result("cua_act", {
                    "ok": False, "error_type": "stale_id", "hint": "Ids changed: retry by label, not element id.",
                    "failure": {"completed_steps": 1, "failed_step": 2},
                }, app="TextEdit")
            start, end = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(start["plan"], ["op:type_text text=3ch"])
        self.assertEqual(end["failed_step"], 2)
        self.assertIn("retry by label", end["hint"])


if __name__ == "__main__":
    unittest.main()
