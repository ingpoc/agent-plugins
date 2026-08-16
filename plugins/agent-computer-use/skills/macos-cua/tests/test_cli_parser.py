import argparse
import importlib.util
import io
import unittest
from contextlib import redirect_stderr
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "cli_parser", ROOT / "scripts" / "cli_parser.py"
)
cli_parser = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli_parser)


class CliParserTests(unittest.TestCase):
    def setUp(self):
        self.parser = cli_parser.build_parser(
            cua_session="fixture-session", key_codes={"w": 13, "return": 36}
        )

    def test_command_inventory_is_stable(self):
        subparsers = next(
            action
            for action in self.parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        self.assertEqual(
            set(subparsers.choices),
            {
                "status", "reset", "apps", "displays", "ensure-display",
                "focus", "snap", "state", "click", "click-point", "click-desktop",
                "double-click", "perform-action", "drag", "type", "type-text",
                "key", "hold-key", "scroll", "right-click", "set-value",
                "select-text", "find", "click-label", "click-label-pointer",
                "type-label", "list-buttons", "run", "cursor", "operator",
            },
        )

    def test_representative_defaults_and_types_are_preserved(self):
        state = self.parser.parse_args(["state", "Calculator", "--compact"])
        self.assertEqual(state.max_elements, 120)
        self.assertTrue(state.compact)
        point = self.parser.parse_args(
            ["click-point", "Calculator", "10.5", "20", "--foreground"]
        )
        self.assertEqual((point.x, point.y), (10.5, 20.0))
        self.assertTrue(point.foreground)
        self.assertEqual(point.button, "left")
        self.assertFalse(point.preserve_pointer)
        right = self.parser.parse_args(
            ["click-point", "Calculator", "10", "20", "--button", "right"]
        )
        self.assertEqual(right.button, "right")
        desktop = self.parser.parse_args(
            ["click-desktop", "3480", "220", "--button", "right"]
        )
        self.assertEqual((desktop.x, desktop.y), (3480.0, 220.0))
        self.assertEqual(desktop.button, "right")
        preserved = self.parser.parse_args(
            ["click-desktop", "3480", "220", "--preserve-pointer"]
        )
        self.assertTrue(preserved.preserve_pointer)
        state_default = self.parser.parse_args(["state", "Calculator"])
        self.assertFalse(state_default.foreground)
        self.assertEqual(cli_parser.AX_FOREGROUND_COMMANDS, frozenset())
        apps = self.parser.parse_args(
            ["apps", "--query", "ScreenContinuity", "--running"]
        )
        self.assertEqual(apps.query, "ScreenContinuity")
        self.assertTrue(apps.running)
        cursor = self.parser.parse_args(["cursor", "status"])
        self.assertEqual(cursor.session, "fixture-session")

    def test_filter_apps_query_and_running_shrink_payload(self):
        payload = {
            "apps": [
                {
                    "name": "Comet",
                    "bundle_id": "ai.perplexity.comet",
                    "running": True,
                    "pid": 1,
                },
                {
                    "name": "iPhone Mirroring",
                    "bundle_id": "com.apple.ScreenContinuity",
                    "running": True,
                    "pid": 2,
                },
                {
                    "name": "Chess",
                    "bundle_id": "com.apple.Chess",
                    "running": False,
                    "pid": None,
                },
            ]
        }
        filtered = cli_parser.filter_apps(
            payload, query="ScreenContinuity", running=True
        )
        self.assertEqual(filtered["match_count"], 1)
        self.assertEqual(filtered["apps"][0]["bundle_id"], "com.apple.ScreenContinuity")
        self.assertTrue(filtered["running_only"])
        running = cli_parser.filter_apps(payload, running=True)
        self.assertEqual(running["match_count"], 2)

    def test_right_click_point_suggests_button_flag(self):
        hint = cli_parser.suggest_command(
            "right-click-point",
            ["click-point", "click-desktop", "right-click"],
        )
        self.assertIsNotNone(hint)
        self.assertIn("click-point", hint)
        self.assertIn("--button right", hint)
        self.assertIn("click-desktop", hint)

    def test_unknown_command_error_prints_hint(self):
        stderr = io.StringIO()
        with self.assertRaises(SystemExit) as raised, redirect_stderr(stderr):
            self.parser.parse_args(["right-click-point", "1", "2"])
        self.assertEqual(raised.exception.code, 2)
        text = stderr.getvalue()
        self.assertIn("invalid choice: 'right-click-point'", text)
        self.assertIn("hint:", text)
        self.assertIn("--button right", text)


if __name__ == "__main__":
    unittest.main()
