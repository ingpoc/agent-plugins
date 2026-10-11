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

if __name__ == "__main__":
    unittest.main()
