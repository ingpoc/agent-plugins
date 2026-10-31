from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import Mock

SCRIPT = Path(__file__).with_name("release_assets.py")
SPEC = importlib.util.spec_from_file_location("release_assets", SCRIPT)
release = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(release)


class ReleaseAssetsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dmg = self.root / "AgentComputerUse-1.2.3.dmg"
        self.dmg.write_bytes(b"fixture dmg content")
        self.out = self.root / "release"
        scripts = self.root / "scripts"
        scripts.mkdir()
        (scripts / "install-mac.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.mounted = self.root / "mounted"
        app = self.mounted / "Agent Computer Use.app" / "Contents"
        app.mkdir(parents=True)
        (app / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": "org.agentplugins.AgentComputerUse",
            "CFBundleShortVersionString": "1.2.3",
            "CFBundleVersion": "45",
            "LSMinimumSystemVersion": "13.0",
        }))
        self.mount = Mock(return_value=self.mounted)
        self.detach = Mock()

    def build(self):
        return release.build_manifest([self.dmg], self.out, root=self.root,
                                      mount_image=self.mount, detach_image=self.detach,
                                      generated_at="2026-10-31T00:00:00Z")

    def test_build_manifest_copies_all_assets_and_records_app_identity_and_hashes(self):
        path = self.build()
        manifest = json.loads(path.read_text())
        self.assertEqual(manifest["schema"], 1)
        self.assertEqual(manifest["repo"], "ingpoc/agent-plugins")
        self.assertEqual(manifest["generated_at"], "2026-10-31T00:00:00Z")
        item = manifest["plugins"]["agent-computer-use"]
        self.assertEqual({k: item[k] for k in ("version", "build", "minMacOS", "bundleId")}, {
            "version": "1.2.3", "build": "45", "minMacOS": "13.0",
            "bundleId": "org.agentplugins.AgentComputerUse",
        })
        self.assertEqual(set(item), {"version", "build", "url", "sha256", "minMacOS", "bundleId"})
        self.assertEqual(item["url"], "https://github.com/ingpoc/agent-plugins/releases/download/agent-computer-use-v1.2.3/AgentComputerUse-1.2.3.dmg")
        self.assertEqual(item["sha256"], hashlib.sha256(self.dmg.read_bytes()).hexdigest())
        self.assertTrue((self.out / self.dmg.name).is_file())
        self.assertTrue((self.out / "install-mac.sh").is_file())
        sums = dict(line.split("  ", 1)[::-1] for line in (self.out / "SHA256SUMS").read_text().splitlines())
        self.assertEqual(set(sums), {self.dmg.name, "manifest.json", "install-mac.sh"})
        self.assertEqual(sums["manifest.json"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.mount.assert_called_once()
        self.detach.assert_called_once_with(self.mounted)

    def test_multiple_matching_dmgs_choose_first_primary_and_sum_every_asset(self):
        second = self.root / "AgentComputerUse-1.2.3-alt.dmg"
        second.write_bytes(b"alternate fixture dmg")
        release.build_manifest([self.dmg, second], self.out, root=self.root,
                               mount_image=self.mount, detach_image=self.detach,
                               generated_at="2026-10-31T00:00:00Z")
        item = json.loads((self.out / "manifest.json").read_text())["plugins"]["agent-computer-use"]
        self.assertTrue(item["url"].endswith("/" + self.dmg.name))
        names = {line.split("  ", 1)[1] for line in (self.out / "SHA256SUMS").read_text().splitlines()}
        self.assertEqual(names, {self.dmg.name, second.name, "manifest.json", "install-mac.sh"})

    def test_detaches_even_when_plist_is_invalid(self):
        plist = self.mounted / "Agent Computer Use.app" / "Contents" / "Info.plist"
        plist.write_bytes(b"not a plist")
        with self.assertRaises(Exception):
            release.read_app(self.dmg, self.mount, self.detach)
        self.detach.assert_called_once_with(self.mounted)

    def test_rejects_conflicting_app_identity_after_detaching_each_image(self):
        other_dmg = self.root / "other.dmg"
        other_dmg.write_bytes(b"other")
        other_mount = self.root / "other-mounted"
        contents = other_mount / "Another.app" / "Contents"
        contents.mkdir(parents=True)
        (contents / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": "org.agentplugins.AgentComputerUse",
            "CFBundleShortVersionString": "2.0.0", "CFBundleVersion": "46",
            "LSMinimumSystemVersion": "13.0",
        }))
        def mount(dmg, _mountpoint):
            return self.mounted if dmg.name == self.dmg.name else other_mount
        with self.assertRaisesRegex(RuntimeError, "ambiguous DMGs"):
            release.build_manifest([self.dmg, other_dmg], self.out, root=self.root,
                                   mount_image=mount, detach_image=self.detach)
        self.assertEqual(self.detach.call_count, 2)

    def test_upload_dry_run_validates_assets_without_executing_gh(self):
        self.build()
        runner = Mock()
        result = release.upload("agent-computer-use-v1.2.3", self.out, run=runner)
        self.assertEqual(result, 0)
        runner.assert_not_called()

    def test_upload_rejects_tampering_and_never_runs_on_default_path(self):
        self.build()
        (self.out / "install-mac.sh").write_text("tampered", encoding="utf-8")
        runner = Mock()
        with self.assertRaisesRegex(RuntimeError, "checksum-mismatched"):
            release.upload("agent-computer-use-v1.2.3", self.out, run=runner)
        runner.assert_not_called()

    def test_upload_refuses_a_different_release_tag(self):
        self.build()
        runner = Mock()
        with self.assertRaisesRegex(RuntimeError, "tag disagrees"):
            release.upload("agent-computer-use-v9.9.9", self.out, run=runner)
        runner.assert_not_called()

    def test_explicit_yes_is_the_only_upload_execution_path(self):
        self.build()
        runner = Mock(return_value=Mock(returncode=0))
        self.assertEqual(release.upload("agent-computer-use-v1.2.3", self.out, yes=True, run=runner), 0)
        command = runner.call_args.args[0]
        self.assertEqual(command[:4], ["gh", "release", "upload", "agent-computer-use-v1.2.3"])
        self.assertIn(str(self.out / "SHA256SUMS"), command)
        self.assertIn("--clobber", command)
        self.assertIn("-R", command)


if __name__ == "__main__":
    unittest.main()
