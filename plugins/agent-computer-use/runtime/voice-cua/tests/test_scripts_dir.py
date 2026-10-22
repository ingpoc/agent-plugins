"""Handoff A: frozen helper must find macos-cua scripts (no 'compact_mcp scripts not found')."""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Keep test events out of the real ~/.cache/macos-cua activity log.
os.environ.setdefault(
    "VOICE_CUA_ACTIVITY_LOG",
    str(Path(tempfile.mkdtemp(prefix="voice-cua-test-")) / "activity.jsonl"),
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from voice_cua import cua_bridge  # noqa: E402

ENV_KEY = "VOICE_CUA_MACOS_CUA_SCRIPTS"
ERROR_MESSAGE = (
    "compact_mcp scripts not found; set VOICE_CUA_MACOS_CUA_SCRIPTS to macos-cua/scripts"
)


def _scripts_tree(base: Path) -> Path:
    root = base / "scripts"
    root.mkdir(parents=True, exist_ok=True)
    (root / "compact_mcp.py").write_text("# stub\n")
    return root


def _meipass_tree(base: Path) -> Path:
    """PyInstaller shape: compact_mcp.py sits directly in _MEIPASS (add-data dest '.')."""
    base.mkdir(parents=True, exist_ok=True)
    (base / "compact_mcp.py").write_text("# stub\n")
    return base


class PluginScriptsDirTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.installed = _scripts_tree(self.base / "installed")
        self.packaged = _scripts_tree(self.base / "packaged")
        self.meipass = _meipass_tree(self.base / "meipass")

    def _patch(self, *, env: str = "", frozen: bool = False, meipass: Path | None = None) -> None:
        patches = [
            patch.dict(os.environ, {ENV_KEY: env}),
            patch.object(cua_bridge, "INSTALLED_PLUGIN_SCRIPTS", self.installed),
            patch.object(cua_bridge, "_PACKAGED_PLUGIN_SCRIPTS", self.packaged),
            patch.object(sys, "frozen", frozen, create=True),
        ]
        if meipass is not None:
            patches.append(patch.object(sys, "_MEIPASS", str(meipass), create=True))
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_env_wins(self) -> None:
        env_dir = _scripts_tree(self.base / "env")
        self._patch(env=str(env_dir), frozen=True, meipass=self.meipass)
        self.assertEqual(cua_bridge._plugin_scripts_dir(), env_dir)

    def test_frozen_prefers_meipass(self) -> None:
        self._patch(frozen=True, meipass=self.meipass)
        self.assertEqual(cua_bridge._plugin_scripts_dir(), self.meipass)

    def test_frozen_never_uses_parents4_and_falls_back_to_installed(self) -> None:
        # The old bug: frozen helper without bundled scripts. parents[4] is
        # meaningless inside the .app, so the installed plugin must be used.
        empty = self.base / "empty-meipass"
        empty.mkdir()
        self._patch(frozen=True, meipass=empty)
        self.assertEqual(cua_bridge._plugin_scripts_dir(), self.installed)
        self.assertNotIn(self.packaged, cua_bridge._scripts_candidates())

    def test_dev_prefers_repo_layout_over_installed_copy(self) -> None:
        self._patch()
        self.assertEqual(cua_bridge._plugin_scripts_dir(), self.packaged)

    def test_dev_falls_back_to_installed(self) -> None:
        (self.packaged / "compact_mcp.py").unlink()
        self._patch()
        self.assertEqual(cua_bridge._plugin_scripts_dir(), self.installed)

    def test_missing_everywhere_raises_known_error(self) -> None:
        (self.packaged / "compact_mcp.py").unlink()
        (self.installed / "compact_mcp.py").unlink()
        self._patch()
        with self.assertRaises(ModuleNotFoundError) as raised:
            cua_bridge._plugin_scripts_dir()
        self.assertEqual(str(raised.exception), ERROR_MESSAGE)

    def test_installed_default_path(self) -> None:
        self.assertEqual(
            cua_bridge.INSTALLED_PLUGIN_SCRIPTS,
            Path("~/.agents/plugins/agent-computer-use/skills/macos-cua/scripts"),
        )

    def test_repo_layout_really_has_compact_mcp(self) -> None:
        self.assertTrue((cua_bridge._PACKAGED_PLUGIN_SCRIPTS / "compact_mcp.py").is_file())


class BuildHelperDataTests(unittest.TestCase):
    def test_build_bundles_scripts_and_cua_client_as_data(self) -> None:
        path = ROOT / "scripts" / "build_voice_helper.py"
        spec = importlib.util.spec_from_file_location("build_voice_helper", path)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        args = mod.scripts_data_args()
        pairs = [args[i + 1] for i, a in enumerate(args) if a == "--add-data"]
        self.assertIn(f"{mod.MACOS_CUA_SCRIPTS}:.", pairs)
        self.assertIn(f"{mod.MACOS_CUA_SERVICE / 'cua_client.py'}:.", pairs)
        self.assertTrue((mod.MACOS_CUA_SCRIPTS / "compact_mcp.py").is_file())
        self.assertTrue((mod.MACOS_CUA_SERVICE / "cua_client.py").is_file())
        self.assertIn("*scripts_data_args()", path.read_text())


if __name__ == "__main__":
    unittest.main()
