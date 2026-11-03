"""Samantha's tools report plugin activity through ACU's shared island_activity path."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from voice_cua import plugin_island  # noqa: E402


class FakeActivity:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple] = []

    def call_started(self, plugin, agent, key, in_flight_s=120.0, **extra):
        self.calls.append(("started", plugin, agent, key, in_flight_s, *extra.values()))
        if self.fail:
            raise RuntimeError("island down")
        return True

    def call_finished(self, plugin, agent, key, **extra):
        self.calls.append(("finished", plugin, agent, key, *extra.values()))
        if self.fail:
            raise RuntimeError("island down")
        return True


class PluginIslandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeActivity()
        patcher = mock.patch.object(plugin_island, "_ACTIVITY", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        plugin_island.FAILURES["count"] = 0
        os.environ.pop("VOICE_CUA_PLUGIN_ISLAND", None)

    def test_tools_map_to_the_plugin_they_drive(self) -> None:
        self.assertEqual(plugin_island.plugin_for_tool("cua_state"), "acu")
        self.assertEqual(plugin_island.plugin_for_tool("cua_act"), "acu")
        self.assertEqual(plugin_island.plugin_for_tool("comet_navigate"), "comet-control")
        self.assertIsNone(plugin_island.plugin_for_tool("secrets_get"))
        self.assertIsNone(plugin_island.plugin_for_tool("workflow_save"))

    def test_acu_call_holds_then_finishes_as_samantha(self) -> None:
        plugin_island.tool_started("cua_act")
        plugin_island.tool_finished("cua_act")
        self.assertEqual(self.fake.calls, [
            ("started", "acu", "Samantha", "samantha:acu", 120.0),
            ("finished", "acu", "Samantha", "samantha:acu"),
        ])

    def test_comet_uses_comet_control_and_drops_the_old_comet_app_circle(self) -> None:
        plugin_island.tool_started("comet_click")
        self.assertEqual(self.fake.calls[0][1], "comet-control")
        self.assertEqual(plugin_island.island_app("comet_click", "Comet"), "")
        # A Mac app Samantha drives through ACU keeps its own right-side app circle.
        self.assertEqual(plugin_island.island_app("cua_act", "Finder"), "Finder")

    def test_operated_mac_app_rides_its_own_acu_row(self) -> None:
        plugin_island.tool_started("cua_state", {"app": " Calculator "})
        plugin_island.tool_finished("cua_state", {"app": "Calculator"})
        self.assertEqual(self.fake.calls, [
            ("started", "acu", "Samantha", "samantha:acu:app:Calculator", 120.0, "Calculator"),
            ("finished", "acu", "Samantha", "samantha:acu:app:Calculator", "Calculator"),
        ])

    def test_comet_tools_never_send_an_operated_app(self) -> None:
        self.assertEqual(plugin_island.target_app("comet_click", {"app": "Comet"}), "")
        self.assertEqual(plugin_island.target_app("cua_act", "not a dict"), "")

    def test_other_tools_never_post(self) -> None:
        plugin_island.tool_started("secrets_get")
        plugin_island.tool_finished("secrets_get")
        self.assertEqual(self.fake.calls, [])

    def test_disabled_and_failures_never_raise(self) -> None:
        with mock.patch.dict(os.environ, {"VOICE_CUA_PLUGIN_ISLAND": "0"}):
            plugin_island.tool_started("cua_state")
        self.assertEqual(self.fake.calls, [])
        with mock.patch.object(plugin_island, "_ACTIVITY", FakeActivity(fail=True)):
            plugin_island.tool_started("cua_state")
            plugin_island.tool_finished("cua_state")
        self.assertEqual(plugin_island.FAILURES["count"], 2)
        with mock.patch.object(plugin_island, "_ACTIVITY", None):
            plugin_island.tool_started("cua_state")

    def test_finds_acu_island_activity_from_the_package(self) -> None:
        path = Path(plugin_island.__file__).resolve().parents[4] / "bin" / "island_activity.py"
        self.assertIn(path, plugin_island._candidates())
        self.assertTrue(path.is_file())


if __name__ == "__main__":
    unittest.main()
