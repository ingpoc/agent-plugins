import importlib.util
import os
from pathlib import Path
import unittest
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "macos-cua.py"
SPEC = importlib.util.spec_from_file_location("macos_cua_visible_typing", SCRIPT)
macos_cua = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(macos_cua)

NATIVE_INPUT_SCRIPT = Path(__file__).parents[1] / "scripts" / "native_input.py"
NATIVE_INPUT_SPEC = importlib.util.spec_from_file_location(
    "macos_cua_native_input_tests", NATIVE_INPUT_SCRIPT
)
native_input = importlib.util.module_from_spec(NATIVE_INPUT_SPEC)
NATIVE_INPUT_SPEC.loader.exec_module(native_input)


class VisibleTypingTests(unittest.TestCase):
    def test_slider_drag_rejects_source_above_accessibility_frame(self):
        observation = {
            "elements": [
                {
                    "element_index": 4,
                    "role": "AXSlider",
                    "frame": {"x": 100, "y": 200, "w": 300, "h": 30},
                }
            ]
        }
        resolve = mock.Mock()
        result = native_input.accessible_slider_drag(
            resolve=resolve,
            ax_value=mock.Mock(),
            pid=10,
            observation=observation,
            source={"x": 120, "y": 190},
            destination={"x": 350, "y": 190},
        )

        self.assertFalse(result["ok"])
        self.assertIn("no accessible slider", result["error"])
        resolve.assert_not_called()

    def test_resolved_text_area_index_bypasses_button_role_filter(self):
        snapshot = {
            "source": "driver_ax",
            "elements": [
                {
                    "element_index": 0,
                    "role": "AXWindow",
                    "frame": {"x": 0, "y": 0, "w": 100, "h": 100},
                },
                {
                    "element_index": 7,
                    "role": "AXTextArea",
                    "label": "Compose message",
                    "frame": {"x": 10, "y": 70, "w": 80, "h": 20},
                },
            ],
        }
        with (
            mock.patch.object(
                macos_cua,
                "find_clickable_index",
                side_effect=AssertionError("field must not be re-resolved as a button"),
            ),
            mock.patch.object(
                macos_cua,
                "operator_update",
                return_value={"ok": True, "state": {"cursor_update_id": "u1"}},
            ),
            mock.patch.object(
                macos_cua, "_wait_for_operator_cursor", return_value={"ok": True}
            ),
            mock.patch.object(
                macos_cua, "click_with_retry", return_value={"ok": True}
            ),
            mock.patch.object(
                macos_cua, "_cleanup_driver_cursors", return_value={"ended": []}
            ),
        ):
            result = macos_cua.click_label_pointer(
                10,
                20,
                "Compose message",
                snapshot_data=snapshot,
                app_name="WhatsApp",
                element_index=7,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["element"], 7)
        self.assertEqual(result["method"], "agent-cursor-glide+ax-click")

    def test_type_text_rejects_newline_before_driver_dispatch(self):
        with mock.patch.object(macos_cua, "call_driver") as driver:
            result = macos_cua.type_text(10, 20, 7, "Hi there\n")

        self.assertFalse(result["ok"])
        self.assertFalse(result["accepted"])
        self.assertEqual(result["error_code"], "newline_may_submit")
        driver.assert_not_called()

    def test_type_text_allows_explicit_newline_override(self):
        with mock.patch.object(
            macos_cua, "call_driver", return_value={"ok": True}
        ) as driver:
            result = macos_cua.type_text(
                10, 20, 7, "line one\nline two", allow_newline=True
            )

        self.assertTrue(result["ok"])
        self.assertEqual(driver.call_args.args[0], "type_text")
        self.assertEqual(driver.call_args.args[1]["text"], "line one\nline two")

    def test_type_label_requires_visible_focus_before_typing(self):
        snapshot = {"elements": [{"element_index": 7, "role": "AXTextArea"}]}
        with (
            mock.patch.object(macos_cua, "snapshot", return_value=snapshot),
            mock.patch.object(macos_cua, "find_field_index", return_value=7),
            mock.patch.object(
                macos_cua,
                "click_label_pointer",
                return_value={"ok": True, "element": 7, "move": {"ok": True}},
            ) as focus,
            mock.patch.object(macos_cua, "press_key", return_value={"ok": True}),
            mock.patch.object(
                macos_cua, "type_text", return_value={"ok": True}
            ) as type_text,
            mock.patch.dict(os.environ, {"MACOS_CUA_FAST": "1"}),
        ):
            result = macos_cua.type_label_action(
                10,
                20,
                "Compose message",
                "Hi there",
                app_name="WhatsApp",
            )

        self.assertTrue(result["ok"])
        focus.assert_called_once_with(
            10,
            20,
            "Compose message",
            50,
            snapshot_data=snapshot,
            app_name="WhatsApp",
            element_index=7,
        )
        type_text.assert_called_once_with(
            10, 20, 7, "Hi there", allow_newline=False
        )

    def test_type_label_stops_when_visible_focus_fails(self):
        with (
            mock.patch.object(macos_cua, "snapshot", return_value={"elements": []}),
            mock.patch.object(macos_cua, "find_field_index", return_value=7),
            mock.patch.object(
                macos_cua,
                "click_label_pointer",
                return_value={"ok": False, "error": "cursor acknowledgement timed out"},
            ),
            mock.patch.object(macos_cua, "press_key") as press_key,
            mock.patch.object(macos_cua, "type_text") as type_text,
        ):
            result = macos_cua.type_label_action(
                10,
                20,
                "Compose message",
                "Hi there",
                app_name="WhatsApp",
            )

        self.assertFalse(result["ok"])
        self.assertIn("visible agent cursor", result["error"])
        press_key.assert_not_called()
        type_text.assert_not_called()

    def test_batched_labeled_typing_uses_visible_cursor_by_default(self):
        snapshot = {
            "tree_markdown": '[7] AXTextArea "Compose message" value="Hi there"',
            "elements": [
                {
                    "element_index": 7,
                    "role": "AXTextArea",
                    "label": "Compose message",
                    "value": "Hi there",
                }
            ],
        }
        with (
            mock.patch.object(macos_cua, "snapshot", return_value=snapshot),
            mock.patch.object(macos_cua, "find_field_index", return_value=7),
            mock.patch.object(
                macos_cua,
                "click_label_pointer",
                return_value={"ok": True, "element": 7, "move": {"ok": True}},
            ) as focus,
            mock.patch.object(
                macos_cua, "type_text", return_value={"ok": True}
            ) as type_text,
            mock.patch.object(
                macos_cua, "_cleanup_driver_cursors", return_value={"ended": []}
            ),
            mock.patch.object(macos_cua, "operator_update", return_value={"ok": True}),
        ):
            macos_cua.run_actions(
                10,
                20,
                {
                    "allow_unverified": True,
                    "capture": "never",
                    "settle_ms": 0,
                    "actions": [
                        {
                            "action": "type",
                            "label": "Compose message",
                            "text": "Hi there",
                        }
                    ],
                },
                app_name="WhatsApp",
            )

        focus.assert_called_once()
        self.assertEqual(focus.call_args.kwargs["app_name"], "WhatsApp")
        self.assertFalse(focus.call_args.kwargs["prepare_cursor"])
        self.assertEqual(focus.call_args.kwargs["element_index"], 7)
        type_text.assert_called_once_with(
            10,
            20,
            7,
            "Hi there",
            x=None,
            y=None,
            delivery_mode="background",
            allow_newline=False,
        )

    def test_first_pointer_step_uses_fresh_driver_state(self):
        snapshot = {
            "tree_markdown": '[2] AXButton "Clear"',
            "elements": [
                {"element_index": 2, "role": "AXButton", "label": "Clear"}
            ],
        }
        with (
            mock.patch.object(macos_cua, "snapshot", return_value=snapshot) as state,
            mock.patch.object(
                macos_cua, "resolve_clickable_index", return_value=(2, None)
            ),
            mock.patch.object(
                macos_cua,
                "click_label_pointer",
                return_value={"ok": True, "element": 2, "move": {"ok": True}},
            ) as click,
            mock.patch.object(
                macos_cua, "_cleanup_driver_cursors", return_value={"ended": []}
            ),
            mock.patch.object(macos_cua, "operator_update", return_value={"ok": True}),
        ):
            macos_cua.run_actions(
                10,
                20,
                {
                    "allow_unverified": True,
                    "capture": "never",
                    "settle_ms": 0,
                    "actions": [{"action": "click", "label": "Clear"}],
                },
                app_name="Calculator",
            )

        state.assert_called()
        self.assertIs(click.call_args.kwargs["snapshot_data"], snapshot)


if __name__ == "__main__":
    unittest.main()
