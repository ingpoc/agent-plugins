#!/usr/bin/env python3
"""Unit graders for ACU session friction encoded in fast_path.py."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "macos_cua_fast_path", ROOT / "scripts" / "fast_path.py"
)
fast_path = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fast_path)


class FastPathTests(unittest.TestCase):
    def test_lint_source_is_green_on_owner(self):
        checks = fast_path.lint_source(ROOT)
        failed = [item for item in checks if not item["ok"]]
        self.assertEqual(failed, [])
        self.assertTrue(
            all(
                "calculator" not in item["name"].lower()
                and "whatsapp" not in item["name"].lower()
                for item in checks
            )
        )

    def test_reset_app_fixture_requires_app_name(self):
        spec = importlib.util.spec_from_file_location(
            "macos_cua_bench_mcp_runtime", ROOT / "scripts" / "bench_mcp_runtime.py"
        )
        bench = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bench)
        with self.assertRaises(ValueError):
            bench.reset_app_fixture("  ")

    def test_desktop_global_click_is_not_an_outcome(self):
        errors = fast_path.grade_click_result(
            {
                "ok": True,
                "path": "operator-proof-screen-coordinate",
                "route": "global_input",
                "effect": "unverifiable",
            }
        )
        self.assertIn("forbidden_click_path", [item["code"] for item in errors])
        self.assertIn("dispatch_is_not_outcome", [item["code"] for item in errors])

    def test_cgevent_click_null_point_is_not_an_outcome(self):
        errors = fast_path.grade_click_result(
            {"ok": True, "method": "cgevent-click", "point": {"x": None, "y": None}}
        )
        self.assertIn("nonfinite_click_point", [item["code"] for item in errors])
        ok = fast_path.grade_click_result(
            {"ok": True, "method": "cgevent-click", "point": {"x": 1.5, "y": 2.5}}
        )
        self.assertNotIn("nonfinite_click_point", [item["code"] for item in ok])

    def test_ax_text_click_requires_verified_selection(self):
        errors = fast_path.grade_click_result(
            {
                "ok": True,
                "path": "verified-screen-point+native-ax-text",
                "result": {
                    "ok": True,
                    "verified": True,
                    "path": "native_ax_range_for_position",
                },
            },
            text_target=True,
        )
        self.assertEqual(errors, [])
        missing = fast_path.grade_click_result(
            {"ok": True, "path": "verified-screen-point+native-pid-mouse"},
            text_target=True,
        )
        self.assertIn("text_click_missing_ax_range", [item["code"] for item in missing])

    def test_readme_publish_rejects_cold_or_failed_suite(self):
        self.assertTrue(
            fast_path.can_publish_readme({"ok": True, "repeat": 1, "ratings": {"overall": 8}})
        )
        self.assertTrue(
            fast_path.can_publish_readme(
                {"ok": False, "repeat": 5, "ratings": {"overall": 8}}
            )
        )
        self.assertEqual(
            fast_path.can_publish_readme(
                {"ok": True, "repeat": 5, "ratings": {"overall": 8.0}}
            ),
            [],
        )

    def test_tool_trace_fails_probe_state_and_cross_app_observe(self):
        errors = fast_path.grade_tool_trace(
            [
                {"name": "state", "arguments": {"app": "AppA"}},
                {"name": "act", "arguments": {"app": "AppA", "plan": {"actions": [{"action": "click", "label": "X"}]}}, "result": {"verified": True}},
                {"name": "verify", "arguments": {"app": "AppA", "expect": "X"}},
                {"name": "state", "arguments": {"app": "AppB"}},
                {"name": "act", "arguments": {"app": "AppB", "plan": {"actions": [{"action": "click", "label": "Y"}]}}},
            ]
        )
        codes = {item["code"] for item in errors}
        self.assertIn("pre_act_state_before_act", codes)
        self.assertIn("redundant_verify_after_verified_act", codes)
        self.assertIn("cross_app_observe_between_acts", codes)

    def test_tool_trace_allows_act_first_across_apps(self):
        errors = fast_path.grade_tool_trace(
            [
                {
                    "name": "act",
                    "arguments": {
                        "app": "AppA",
                        "plan": {
                            "actions": [
                                {"action": "click", "label": "1"},
                                {"action": "click", "label": "2"},
                            ]
                        },
                    },
                    "result": {"verified": True},
                },
                {
                    "name": "act",
                    "arguments": {
                        "app": "AppB",
                        "plan": {"actions": [{"action": "click", "label": "Go"}]},
                    },
                    "result": {"verified": True},
                },
            ]
        )
        self.assertEqual(errors, [])

    def test_tool_trace_fails_unbatched_same_app_acts(self):
        errors = fast_path.grade_tool_trace(
            [
                {"name": "act", "arguments": {"app": "AppA", "label": "1"}},
                {"name": "act", "arguments": {"app": "AppA", "label": "2"}},
                {"name": "act", "arguments": {"app": "AppA", "label": "3"}},
            ]
        )
        self.assertIn("granular_unbatched_acts", [item["code"] for item in errors])

    def test_session_shape_grades_redundant_verify_and_probe_state(self):
        errors = fast_path.grade_acu_session(
            {
                "metrics": {
                    "tools": {"verify": 2},
                    "verified_acts": 2,
                    "pre_act_observe_same_app": 3,
                    "cross_app_observe_between_acts": 2,
                }
            }
        )
        self.assertEqual(
            {item["code"] for item in errors},
            {
                "redundant_verify_after_verified_act",
                "pre_act_state_before_act",
                "cross_app_observe_between_acts",
            },
        )

    def test_acu_session_grades_mined_cursor_waste(self):
        errors = fast_path.grade_acu_session(
            {
                "metrics": {
                    "tools": {"start_session": 11, "end_session": 3, "act": 25, "state": 30},
                    "actions": 25,
                    "action_batches": 0,
                    "max_action_batch": 1,
                    "repeated_observations": 7,
                }
            }
        )
        self.assertEqual(
            {item["code"] for item in errors},
            {
                "unpaired_start_session",
                "repeated_observe",
                "granular_unbatched_acts",
            },
        )

    def test_unrecovered_driver_session_is_not_an_outcome(self):
        ended = {
            "refusal": {
                "code": "session_ended",
                "message": "this session has ended; call start_session explicitly to reuse its label",
            },
            "status": "refused",
        }
        self.assertIn(
            "driver_session_dropped",
            [item["code"] for item in fast_path.grade_click_result(ended)],
        )
        self.assertEqual(
            fast_path.grade_click_result({**ended, "session_recovered": True}),
            [],
        )
        session_errors = fast_path.grade_acu_session({"error": ended})
        self.assertIn("driver_session_dropped", [item["code"] for item in session_errors])

    def test_container_axpress_without_descendant_is_not_an_outcome(self):
        failed = {
            "ok": False,
            "error": "AXUIElementPerformAction(AXPress) returned -25206",
        }
        self.assertIn(
            "container_press_without_descendant",
            [
                item["code"]
                for item in fast_path.grade_click_result(failed, role="AXRow")
            ],
        )
        recovered = {
            "ok": True,
            "pressed_descendant": True,
            "requested_element": 47,
            "element": 49,
        }
        self.assertEqual(
            fast_path.grade_click_result(recovered, role="AXRow"),
            [],
        )
        self.assertEqual(fast_path.grade_click_result(failed, role="AXButton"), [])
        descendant_miss = {
            "ok": False,
            "error": "AXUIElementPerformAction(AXPress) returned -25206",
            "pressed_descendant": True,
            "method": "agent-cursor-glide+native-axpress-fallback",
        }
        self.assertIn(
            "container_press_without_ax_frame_hid",
            [
                item["code"]
                for item in fast_path.grade_click_result(
                    descendant_miss, role="AXRow"
                )
            ],
        )
        hid = {
            "ok": True,
            "method": "agent-cursor-glide+ax-frame-hid",
            "result": {"ok": True, "accepted": True, "path": "native_hid_mouse"},
        }
        self.assertEqual(fast_path.grade_click_result(hid, role="AXRow"), [])

    def test_pid_only_post_hid_is_not_dual_post(self):
        pid_only = """
    private func postHid(_ event: CGEvent?, to pid: pid_t) -> Bool {
        guard let event else { return false }
        event.postToPid(pid)
        return true
    }

    private func postHidGlobal(_ event: CGEvent?) {
        event?.post(tap: .cghidEventTap)
    }
"""
        self.assertFalse(fast_path.hid_dual_posts_same_helper(pid_only))
        self.assertIn("postToPid", fast_path.post_hid_helper_body(pid_only))
        self.assertNotIn("cghidEventTap", fast_path.post_hid_helper_body(pid_only))

    def test_dual_post_same_helper_fails(self):
        # Real-world: postToPid then cghid for the same event doubles glyphs.
        dual = """
    private func postHid(_ event: CGEvent?, to pid: pid_t) -> Bool {
        guard let event else { return false }
        event.postToPid(pid)
        event.post(tap: .cghidEventTap)
        return true
    }
"""
        self.assertTrue(fast_path.hid_dual_posts_same_helper(dual))


if __name__ == "__main__":
    unittest.main()
