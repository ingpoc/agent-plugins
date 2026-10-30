import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


PLUGIN_ROOT = Path(__file__).resolve().parents[3]


class PluginPackageTests(unittest.TestCase):
    def test_plugin_manifest_is_agent_plugins_1(self):
        data = json.loads((PLUGIN_ROOT / "plugin.json").read_text())
        self.assertEqual(
            data["$schema"],
            "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
        )
        self.assertEqual(data["name"], "agent-computer-use")
        extra = set(data) - {
            "$schema",
            "name",
            "version",
            "description",
            "author",
            "homepage",
            "repository",
            "license",
            "keywords",
            "extensions",
        }
        self.assertEqual(extra, set())
        self.assertNotIn("logo", data)
        cursor_plugin = json.loads((PLUGIN_ROOT / ".cursor-plugin/plugin.json").read_text())
        self.assertEqual(
            cursor_plugin,
            {
                "name": "agent-computer-use",
                "version": data["version"],
                "description": data["description"],
                "mcpServers": "./.cursor-plugin/mcp.json",
            },
        )
        cursor_mcp = json.loads((PLUGIN_ROOT / ".cursor-plugin/mcp.json").read_text())
        self.assertEqual(
            cursor_mcp,
            {
                "mcpServers": {
                    "agent-computer-use": {
                        "type": "stdio",
                        "command": "${CURSOR_PLUGIN_ROOT}/bin/agent-computer-use-mcp",
                        "cwd": "${CURSOR_PLUGIN_ROOT}",
                    }
                }
            },
        )
        self.assertFalse((PLUGIN_ROOT / ".codex-plugin").exists())
        self.assertTrue((PLUGIN_ROOT / "AGENTS.md").is_file())
        readme = (PLUGIN_ROOT / "README.md").read_text()
        for axis in (
            "Reliability",
            "Robustness",
            "Context efficiency",
            "Speed",
            "Efficiency",
        ):
            self.assertIn(f"### {axis}", readme)

    def test_skill_orders_ax_first_then_fallback(self):
        skill = (PLUGIN_ROOT / "skills/macos-cua/SKILL.md").read_text()
        fast = (
            PLUGIN_ROOT / "skills/macos-cua/references/fast-workflow.md"
        ).read_text()
        architecture = (
            PLUGIN_ROOT / "skills/macos-cua/references/architecture.md"
        ).read_text()
        # Thin SKILL: critical path + bans + load map only (SkillReducer).
        self.assertIn("## Critical path", skill)
        self.assertIn("## Hard bans", skill)
        self.assertIn("## Load map", skill)
        self.assertIn("Act-first", skill)
        self.assertIn("Encode friction", skill)
        self.assertIn("Clear", skill)
        self.assertIn("All Clear", skill)
        self.assertIn("references/fast-workflow.md", skill)
        self.assertIn("references/architecture.md", skill)
        self.assertNotIn("## Architecture boundary", skill)
        self.assertNotIn("## Fast workflow", skill)
        # Moved detail lives in references.
        self.assertIn("Best first, then fallback", fast)
        self.assertIn("Two wall clocks", fast)
        self.assertIn("fails the old trace", fast)
        self.assertIn("ax_timeout", fast)
        self.assertLess(fast.find("Best first"), fast.find("Fallback, in order"))
        self.assertIn("CUAService", architecture)
        troubleshooting = (
            PLUGIN_ROOT / "skills/macos-cua/references/troubleshooting.md"
        ).read_text()
        self.assertIn("Current-tree first", troubleshooting)
        self.assertNotIn("refresh only on miss", troubleshooting)
        self.assertIn("Last resort after AX miss", troubleshooting)
        self.assertNotIn("preflight:true", skill)
        self.assertNotIn("likeminded.md", skill)
        self.assertNotIn("one compact `state`", skill)
        self.assertNotIn("start_session → `state` / `act` / `verify`", skill)
        self.assertNotIn("one compact `state`", troubleshooting)
        agents = (PLUGIN_ROOT / "AGENTS.md").read_text()
        self.assertNotIn("one compact `state`", agents)
        self.assertNotIn("Two wall clocks", agents)
        self.assertNotIn("Encode friction", agents)

    def test_mcp_uses_packaged_relative_command(self):
        config = json.loads((PLUGIN_ROOT / "mcp.json").read_text())
        server = config["mcpServers"]["agent-computer-use"]
        self.assertEqual(
            config["$schema"],
            "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
        )
        self.assertEqual(server["type"], "stdio")
        command = server["command"]
        self.assertTrue(
            command == "./bin/agent-computer-use-mcp"
            or command.endswith("/bin/agent-computer-use-mcp"),
            command,
        )
        self.assertEqual(server.get("cwd"), "./")
        self.assertTrue((PLUGIN_ROOT / "bin" / "agent-computer-use-mcp").is_file())
        self.assertTrue(os.access(PLUGIN_ROOT / "bin" / "agent-computer-use-mcp", os.X_OK))
        launcher_src = (PLUGIN_ROOT / "bin" / "agent-computer-use-mcp").read_text()
        self.assertIn("AGENT_COMPUTER_USE_PYTHON", launcher_src)
        self.assertIn("command -v python3", launcher_src)
        self.assertNotIn("exec python3 -u", launcher_src)
        self.assertNotIn("CUA_DRIVER", json.dumps(server.get("env") or {}))
        harness = (PLUGIN_ROOT / "skills/macos-cua/scripts/install_harness.py").read_text()
        self.assertNotIn("rsync", harness)
        self.assertNotIn("_candidate_cursor_dests", harness)
        self.assertIn("_retire_stale_local", harness)
        self.assertIn("_remove_cursor_user_mcp", harness)
        self.assertNotIn('dest / "bin" / "cua-driver-mcp"', harness)

    def test_stale_local_plugin_moves_to_trash(self):
        path = PLUGIN_ROOT / "skills/macos-cua/scripts/install_harness.py"
        loaded = importlib.util.spec_from_file_location("install_harness", path)
        mod = importlib.util.module_from_spec(loaded)
        loaded.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stale = root / "local" / "agent-computer-use"
            (stale / "bin").mkdir(parents=True)
            trash = root / "Trash"
            (trash / "agent-computer-use").mkdir(parents=True)
            moved = mod._retire_stale_local(stale, trash)
            again = mod._retire_stale_local(stale, trash)
            self.assertTrue(moved["ok"])
            self.assertFalse(stale.exists())
            self.assertEqual(Path(moved["trashed"]), trash / "agent-computer-use 1")
            self.assertTrue((trash / "agent-computer-use 1" / "bin").is_dir())
            self.assertTrue(again["ok"])
            self.assertIsNone(again["trashed"])

    def test_default_sync_does_not_add_user_mcp(self):
        path = PLUGIN_ROOT / "skills/macos-cua/scripts/install_harness.py"
        loaded = importlib.util.spec_from_file_location("install_harness", path)
        mod = importlib.util.module_from_spec(loaded)
        loaded.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as directory:
            mcp = Path(directory) / "mcp.json"
            mcp.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "agent-computer-use": {"command": "/tmp/fake"},
                            "keep-me": {"command": "x"},
                        }
                    }
                )
            )
            result = mod._remove_cursor_user_mcp(mcp)
            data = json.loads(mcp.read_text())
        self.assertTrue(result["ok"])
        self.assertTrue(result["removed"])
        self.assertNotIn("agent-computer-use", data["mcpServers"])
        self.assertIn("keep-me", data["mcpServers"])
        source = path.read_text()
        self.assertIn("else _remove_cursor_user_mcp()", source)
        self.assertIn("--user-mcp", source)


    def test_raw_driver_launcher_is_retired(self):
        self.assertFalse((PLUGIN_ROOT / "bin" / "cua-driver-mcp").exists())


if __name__ == "__main__":
    unittest.main()
