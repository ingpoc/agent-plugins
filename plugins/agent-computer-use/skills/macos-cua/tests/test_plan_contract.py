import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "plan_contract", ROOT / "scripts" / "plan_contract.py"
)
plan_contract = importlib.util.module_from_spec(SPEC)
sys.modules.setdefault("plan_contract", plan_contract)
SPEC.loader.exec_module(plan_contract)


class PlanContractTests(unittest.TestCase):
    def test_valid_asserted_plan_has_no_errors(self):
        plan = {
            "actions": [
                {"action": "click", "label": "Save"},
                {"action": "key", "keys": "return"},
            ],
            "expect": {"text": "Saved"},
        }
        self.assertEqual(plan_contract.validate_plan(plan), [])

    def test_unknown_action_and_missing_fields_are_reported_before_dispatch(self):
        errors = plan_contract.validate_plan(
            {
                "actions": [
                    {"action": "key"},
                    {"action": "click", "x": 10},
                    {"action": "invented"},
                ]
            }
        )
        self.assertEqual(
            [error["code"] for error in errors],
            [
                "required_field_missing",
                "target_missing",
                "coordinate_pair_incomplete",
                "unknown_action",
            ],
        )

    def test_ambiguous_result_is_not_accepted(self):
        self.assertFalse(plan_contract.result_accepted({"raw": "not-json"}))
        self.assertFalse(plan_contract.result_accepted({"ok": False}))
        self.assertTrue(plan_contract.result_accepted({"ok": True}))
        self.assertTrue(plan_contract.result_accepted({"effect": "confirmed"}))
        self.assertFalse(plan_contract.result_accepted({"effect": "suspected_noop"}))
        self.assertFalse(
            plan_contract.result_accepted({"ok": True, "effect": "suspected_noop"})
        )
        self.assertFalse(
            plan_contract.result_accepted({"effect": "unverifiable"})
        )
        self.assertFalse(
            plan_contract.result_accepted(
                {
                    "ok": True,
                    "path": "operator-proof-screen-coordinate",
                    "route": "global_input",
                }
            )
        )
        self.assertFalse(
            plan_contract.result_accepted(
                {
                    "refusal": {
                        "code": "session_ended",
                        "message": "this session has ended; call start_session explicitly to reuse its label",
                    },
                    "status": "refused",
                }
            )
        )

    def test_perform_action_defaults_name_to_press(self):
        plan = {
            "actions": [{"action": "perform_action", "label": "New Chat"}],
            "expect": {"text": "New chat", "role": "AXHeading"},
        }
        self.assertEqual(plan_contract.validate_plan(plan), [])
        self.assertEqual(plan["actions"][0]["name"], "press")

    def test_existing_defaulted_actions_remain_valid(self):
        self.assertEqual(
            plan_contract.validate_plan(
                {
                    "actions": [
                        {"action": "wait"},
                        {"action": "type"},
                        {"action": "expect"},
                    ]
                }
            ),
            [],
        )

    def test_adjacent_observes_are_rejected_as_redundant(self):
        errors = plan_contract.validate_plan(
            {
                "actions": [
                    {"action": "state"},
                    {"action": "snapshot"},
                ]
            }
        )
        self.assertEqual(
            errors,
            [{"path": "$.actions[1]", "code": "redundant_observe"}],
        )

    def test_cli_action_aliases_normalize_to_plan_actions(self):
        plan = {
            "actions": [
                {
                    "action": "click-label-pointer",
                    "label": "Continue as Gurusharan Gupta",
                }
            ]
        }
        self.assertEqual(plan_contract.validate_plan(plan), [])
        self.assertEqual(plan["actions"][0]["action"], "click")
        self.assertEqual(
            plan_contract.normalize_action_name("double-click"), "double_click"
        )

    def test_text_and_unlabeled_value_expectations_include_ax_values(self):
        state = {
            "tree_markdown": "[1] AXTextArea",
            "elements": [{"element_index": 1, "value": "red AMBER blue"}],
        }

        self.assertTrue(
            plan_contract.evaluate_expectations(
                state, {"text": "red AMBER blue"}
            )[0]
        )
        self.assertTrue(
            plan_contract.evaluate_expectations(
                state, {"value": {"label": "", "equals": "red AMBER blue"}}
            )[0]
        )

    def test_text_expect_ignores_the_acted_control(self):
        state = {
            "tree_markdown": '[21] AXButton "New Chat"\n[62] AXHeading "New chat"',
            "elements": [
                {"element_index": 21, "label": "New Chat"},
                {"element_index": 62, "label": "New chat"},
            ],
        }
        self.assertFalse(
            plan_contract.evaluate_expectations(
                {
                    "tree_markdown": '[21] AXButton "New Chat"',
                    "elements": [{"element_index": 21, "label": "New Chat"}],
                },
                {"text": "New chat"},
                ignore_element_indices=[21],
            )[0]
        )
        self.assertTrue(
            plan_contract.evaluate_expectations(
                state, {"text": "New chat"}, ignore_element_indices=[21]
            )[0]
        )

    def test_compact_state_text_keeps_only_expect_lines(self):
        text = (
            'Window: "WhatsApp"\n'
            '[21] AXButton "New Chat"\n'
            '[24] AXGroup "List of chats"\n'
            '[31] AXTextArea "Compose message"'
        )
        compact = plan_contract.compact_state_text(text, {"text": "Compose message"})
        self.assertIn("Compose message", compact)
        self.assertNotIn("List of chats", compact)

    def test_heading_role_is_required_for_new_chat_popover_proof(self):
        sidebar = {
            "tree_markdown": '[21] AXButton "New Chat"\n[39] AXStaticText value="Message yourself"',
            "elements": [
                {"element_index": 21, "role": "AXButton", "label": "New Chat"},
                {
                    "element_index": 39,
                    "role": "AXStaticText",
                    "label": "You",
                    "value": "Message yourself",
                },
            ],
        }
        expect = {"text": "New chat", "role": "AXHeading"}
        self.assertTrue(
            plan_contract.evaluate_expectations(
                sidebar, {"text": "Message yourself"}, ignore_element_indices=[21]
            )[0]
        )
        self.assertFalse(
            plan_contract.evaluate_expectations(
                sidebar, expect, ignore_element_indices=[21]
            )[0]
        )
        opened = {
            "tree_markdown": '[21] AXButton "New Chat"\n[62] AXHeading "New chat"',
            "elements": [
                {"element_index": 21, "role": "AXButton", "label": "New Chat"},
                {"element_index": 62, "role": "AXHeading", "label": "New chat"},
            ],
        }
        self.assertTrue(
            plan_contract.evaluate_expectations(
                opened, expect, ignore_element_indices=[21]
            )[0]
        )
        compact = plan_contract.compact_state_text(
            '[21] AXButton "New Chat"\n[39] AXStaticText value="Message yourself"\n[62] AXHeading "New chat"',
            expect,
        )
        self.assertIn("AXHeading", compact)
        self.assertNotIn("Message yourself", compact)
        self.assertNotIn("List of chats", compact)

    def test_compact_step_lifts_nested_press_error(self):
        compact = plan_contract.compact_step(
            {
                "step": 1,
                "action": "click",
                "accepted": False,
                "duration_ms": 1800,
                "label": "All Clear",
                "result": {
                    "ok": False,
                    "method": "agent-cursor-glide+native-axpress-fallback",
                    "escalation": {"recommended": "px", "reason": "tree disagrees"},
                    "result": {"error": "AXUIElementPerformAction(AXPress) returned -25204"},
                },
            }
        )
        self.assertIn("25204", compact["error"])
        self.assertEqual(compact["method"], "agent-cursor-glide+native-axpress-fallback")
        self.assertEqual(compact["escalation"]["recommended"], "px")


if __name__ == "__main__":
    unittest.main()
