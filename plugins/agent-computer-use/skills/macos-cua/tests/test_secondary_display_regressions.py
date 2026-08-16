import importlib.util
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "macos-cua.py"
SPEC = importlib.util.spec_from_file_location("macos_cua_secondary", SCRIPT)
macos_cua = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(macos_cua)


class LabelResolutionRegressionTests(unittest.TestCase):
    def test_menu_label_does_not_match_window_title_substring(self):
        state = {
            "tree_markdown": (
                "[1] AXWindow ExampleApp Window\n"
                "[2] AXMenuBarItem Window\n"
            ),
            "elements": [
                {
                    "element_index": 1,
                    "role": "AXWindow",
                    "label": "ExampleApp Window",
                },
                {
                    "element_index": 2,
                    "role": "AXMenuBarItem",
                    "label": "Window",
                },
            ],
        }

        self.assertEqual(macos_cua.find_clickable_index(state, "Window"), 2)

    def test_window_title_is_not_a_clickable_label_fallback(self):
        state = {
            "tree_markdown": "[1] AXWindow ExampleApp Window\n",
            "elements": [
                {
                    "element_index": 1,
                    "role": "AXWindow",
                    "label": "ExampleApp Window",
                }
            ],
        }

        self.assertIsNone(macos_cua.find_clickable_index(state, "Window"))


def _driver_click_params(call_mock):
    """Return params from the real click call (ignore cursor-cleanup traffic)."""
    for args, _kwargs in call_mock.call_args_list:
        if args and args[0] == "click":
            return args[1]
    raise AssertionError(f"no click call in {call_mock.call_args_list!r}")


class SecondaryDisplayClickRegressionTests(unittest.TestCase):
    def test_stage_manager_proxy_recaptures_exact_ax_screen_region(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "capture.png"
            raw = {"screenshot_file_path": str(output)}
            geometry = {
                "identity": {
                    "method": "stage-manager-exact-ax-window-id-override"
                },
                "expected": {"x": 2680, "y": 407, "width": 230, "height": 408},
            }

            def capture(command, **_kwargs):
                output.write_bytes(
                    b"\x89PNG\r\n\x1a\n"
                    + b"\0" * 8
                    + (460).to_bytes(4, "big")
                    + (816).to_bytes(4, "big")
                )
                self.assertIn("-R2680,407,230,408", command)
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with mock.patch.object(macos_cua.subprocess, "run", side_effect=capture):
                result = macos_cua._recapture_stage_manager_region(raw, geometry)

        self.assertEqual(result["screenshot_width"], 460)
        self.assertEqual(result["screenshot_height"], 816)
        self.assertEqual(result["capture_source"], "foreground_ax_screen_region")

    def test_region_recapture_is_restricted_to_verified_stage_manager_identity(self):
        with mock.patch.object(macos_cua.subprocess, "run") as run:
            result = macos_cua._recapture_stage_manager_region(
                {"screenshot_file_path": "/tmp/unused.png"},
                {
                    "identity": {"method": "exact-quartz-window-id"},
                    "expected": {"x": 0, "y": 0, "width": 100, "height": 100},
                },
            )
        self.assertIsNone(result)
        run.assert_not_called()

    def test_matching_quartz_and_ax_frames_keep_driver_window_local_route(self):
        frame = {"x": 1920, "y": 20, "width": 1880, "height": 1000}
        with (
            mock.patch.object(
                macos_cua, "_quartz_window_bounds", return_value=(frame, None)
            ),
            mock.patch.object(
                macos_cua, "_logical_ax_window_frame", return_value=frame
            ),
            mock.patch.object(
                macos_cua, "call_driver", return_value={"ok": True}
            ) as call,
        ):
            result = macos_cua.click_point(10, 20, 747, 412.5)

        self.assertTrue(result["ok"])
        self.assertTrue(result["user_interruptive"])
        self.assertFalse(result["isolated_pointer"])
        params = _driver_click_params(call)
        self.assertEqual(params["window_id"], 20)
        self.assertEqual(params["x"], 747.0)
        self.assertEqual(params["y"], 412.5)

    def test_stage_manager_thumbnail_uses_logical_ax_screen_mapping(self):
        quartz = {"x": -1200, "y": 800, "width": 400, "height": 220}
        logical = {"x": 1920, "y": 20, "width": 1880, "height": 1000}
        fresh = {
            "screenshot_width": 1494,
            "screenshot_height": 825,
            "screenshot_file_path": "/tmp/app.png",
        }
        with (
            mock.patch.object(
                macos_cua, "_quartz_window_bounds", return_value=(quartz, None)
            ),
            mock.patch.object(
                macos_cua, "_logical_ax_window_frame", return_value=logical
            ),
            mock.patch.object(macos_cua, "snapshot", return_value=fresh),
            mock.patch.object(
                macos_cua, "call_driver", return_value={"ok": True}
            ) as call,
        ):
            result = macos_cua.click_point(10, 20, 747, 412.5)

        params = _driver_click_params(call)
        self.assertTrue(result["ok"])
        self.assertEqual(result["path"], "logical-ax-screen-coordinate")
        self.assertNotIn("window_id", params)
        self.assertEqual(params["pid"], 10)
        self.assertAlmostEqual(params["x"], 2860.0)
        self.assertAlmostEqual(params["y"], 520.0)

    def test_stage_manager_mapping_fails_closed_for_stale_out_of_bounds_point(self):
        quartz = {"x": -1200, "y": 800, "width": 400, "height": 220}
        logical = {"x": 1920, "y": 20, "width": 1880, "height": 1000}
        with (
            mock.patch.object(
                macos_cua, "_quartz_window_bounds", return_value=(quartz, None)
            ),
            mock.patch.object(
                macos_cua, "_logical_ax_window_frame", return_value=logical
            ),
            mock.patch.object(
                macos_cua,
                "snapshot",
                return_value={"screenshot_width": 1494, "screenshot_height": 825},
            ),
            mock.patch.object(macos_cua, "call_driver") as call,
        ):
            result = macos_cua.click_point(10, 20, 1600, 900)

        self.assertFalse(result["ok"])
        self.assertIn("outside", result["error"])
        call.assert_not_called()

    def test_foreground_recovery_fronts_window_before_screen_coordinate_route(self):
        recovery = {
            "x": 2860.0,
            "y": 520.0,
            "screenshot": {"width": 1494, "height": 825},
            "quartz_frame": {"x": -1200, "y": 800, "width": 400, "height": 220},
            "logical_ax_frame": {
                "x": 1920,
                "y": 20,
                "width": 1880,
                "height": 1000,
            },
            "quartz_error": None,
        }
        prepared = {"ok": True, "method": "driver-foreground"}
        with (
            mock.patch.object(
                macos_cua, "_logical_pixel_target", side_effect=[recovery, recovery]
            ),
            mock.patch.object(
                macos_cua,
                "bring_resolved_window_to_front",
                return_value=prepared,
            ) as foreground,
            mock.patch.object(
                macos_cua, "call_driver", return_value={"ok": True}
            ) as call,
        ):
            result = macos_cua.click_point(
                10, 20, 747, 412.5, delivery_mode="foreground"
            )

        foreground.assert_called_once_with(10, 20)
        self.assertEqual(result["foreground_prepared"], prepared)
        self.assertEqual(_driver_click_params(call)["delivery_mode"], "background")

    def test_explicit_modal_window_disables_main_window_recovery(self):
        with mock.patch.object(
            macos_cua, "call_driver", return_value={"ok": True}
        ) as call:
            macos_cua.click_point(
                10,
                99,
                100,
                50,
                logical_frame_recovery=False,
            )

        params = _driver_click_params(call)
        self.assertEqual(params["window_id"], 99)
        self.assertEqual(params["x"], 100.0)
        self.assertEqual(params["y"], 50.0)


if __name__ == "__main__":
    unittest.main()
