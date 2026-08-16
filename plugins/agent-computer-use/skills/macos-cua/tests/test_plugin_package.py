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
        overlay = json.loads((PLUGIN_ROOT / ".cursor-plugin" / "plugin.json").read_text())
        self.assertEqual(overlay["logo"], "assets/logo.svg")
        self.assertTrue((PLUGIN_ROOT / "assets" / "logo.svg").is_file())
        self.assertNotIn("logo", data)

    def test_mcp_uses_packaged_relative_command(self):
        config = json.loads((PLUGIN_ROOT / "mcp.json").read_text())
        server = config["mcpServers"]["agent-computer-use"]
        self.assertEqual(
            config["$schema"],
            "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
        )
        self.assertEqual(server["type"], "stdio")
        self.assertEqual(server["command"], "./bin/agent-computer-use-mcp")
        self.assertEqual(server.get("cwd"), "./")
        self.assertTrue((PLUGIN_ROOT / "bin" / "agent-computer-use-mcp").is_file())
        self.assertTrue(os.access(PLUGIN_ROOT / "bin" / "agent-computer-use-mcp", os.X_OK))
        self.assertTrue((PLUGIN_ROOT / "bin" / "cua-driver-mcp").is_file())
        self.assertTrue(os.access(PLUGIN_ROOT / "bin" / "cua-driver-mcp", os.X_OK))
        self.assertEqual(server["env"]["CUA_DRIVER_RS_UPDATE_CHECK"], "0")
        harness = (PLUGIN_ROOT / "skills/macos-cua/scripts/install_harness.py").read_text()
        self.assertIn('dest / "bin" / "agent-computer-use-mcp"', harness)
        self.assertIn("_rewrite_cursor_plugin_mcp", harness)
        self.assertIn("_remove_cursor_user_mcp", harness)
        self.assertNotIn('dest / "bin" / "cua-driver-mcp"', harness)

    def test_cursor_dest_mcp_uses_absolute_command(self):
        path = PLUGIN_ROOT / "skills/macos-cua/scripts/install_harness.py"
        loaded = importlib.util.spec_from_file_location("install_harness", path)
        mod = importlib.util.module_from_spec(loaded)
        loaded.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory)
            (dest / "bin").mkdir()
            launcher = dest / "bin" / "agent-computer-use-mcp"
            launcher.write_text("#!/bin/sh\n")
            launcher.chmod(0o755)
            (dest / "mcp.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "agent-computer-use": {
                                "command": "./bin/agent-computer-use-mcp",
                                "cwd": ".",
                            }
                        }
                    }
                )
            )
            result = mod._rewrite_cursor_plugin_mcp(dest, launcher)
            rewritten = json.loads((dest / "mcp.json").read_text())
        self.assertTrue(result["ok"])
        self.assertEqual(
            rewritten["mcpServers"]["agent-computer-use"]["command"],
            str(launcher),
        )
        self.assertEqual(rewritten["mcpServers"]["agent-computer-use"]["cwd"], "./")
        self.assertFalse(
            rewritten["mcpServers"]["agent-computer-use"]["command"].startswith("./")
        )

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

    def test_raw_driver_launcher_still_executes_driver_mcp(self):
        launcher = PLUGIN_ROOT / "bin" / "cua-driver-mcp"
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "cua-driver"
            fake.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\"\n")
            fake.chmod(0o755)
            env = os.environ | {"CUA_DRIVER_BIN": str(fake)}
            result = subprocess.run(
                [str(launcher)],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )
        self.assertEqual(result.stdout.strip(), "mcp --client agent-computer-use")


if __name__ == "__main__":
    unittest.main()
