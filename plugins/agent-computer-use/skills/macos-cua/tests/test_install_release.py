#!/usr/bin/env python3
"""Install target, compat symlink, signing identity choice, release entitlements (offline; nothing installed)."""
from __future__ import annotations

import plistlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))
import install_service as inst  # noqa: E402


class InstallReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cua-inst-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_install_target_is_applications(self):
        self.assertEqual(inst.INSTALL_DIR, Path("/Applications/Agent Computer Use.app"))
        self.assertEqual(inst.LEGACY_DIR, Path("~/.cache/macos-cua/CUAService.app").expanduser())

    def test_compat_link_created_kept_and_legacy_bundle_trashed(self):
        dest = self.tmp / "Applications" / "Agent Computer Use.app"
        dest.mkdir(parents=True)
        legacy = self.tmp / "cache" / "CUAService.app"
        trash = self.tmp / "Trash"
        trash.mkdir()
        self.assertEqual(inst.ensure_compat_link(dest, legacy, trash), "linked")
        self.assertTrue(legacy.is_symlink())
        self.assertEqual(legacy.resolve(), dest.resolve())
        self.assertEqual(inst.ensure_compat_link(dest, legacy, trash), "kept")
        legacy.unlink()
        (legacy / "Contents").mkdir(parents=True)   # an old real install
        self.assertEqual(inst.ensure_compat_link(dest, legacy, trash), "linked")
        self.assertTrue(legacy.is_symlink())
        self.assertEqual(len(list(trash.iterdir())), 1, "old bundle moved to Trash, not deleted")

    def test_developer_id_preferred(self):
        dev = ("A" * 40, "subject=CN=Apple Development: Gurusharan Gupta (9X7F7TPT2K),OU=9UPQL479Z5")
        did = ("B" * 40, "subject=UID=9UPQL479Z5,CN=Developer ID Application: Gurusharan Gupta (9UPQL479Z5),OU=9UPQL479Z5")
        self.assertEqual(inst.pick_identity([dev, did]), "B" * 40)
        self.assertEqual(inst.pick_identity([dev]), "A" * 40)
        self.assertIsNone(inst.pick_identity([]))

    def test_entitlements_per_component(self):
        def keys(name):
            return set(plistlib.loads((SERVICE / "Resources" / name).read_bytes()))
        ae, mic, cal = ("com.apple.security.automation.apple-events", "com.apple.security.device.audio-input",
                        "com.apple.security.personal-information.calendars")
        self.assertEqual(keys("CUAService.entitlements"), {ae, mic, cal})
        self.assertEqual(keys("voice-cua.entitlements"), {ae, mic})
        self.assertEqual(keys("mic-preflight.entitlements"), {mic})
        self.assertEqual(inst.VOICE_ENTITLEMENTS.name, "voice-cua.entitlements")
        self.assertEqual(inst.MIC_ENTITLEMENTS.name, "mic-preflight.entitlements")

    def test_usage_strings_match_entitlements(self):
        app = plistlib.loads((SERVICE / "Resources" / "Info.plist").read_bytes())
        helper = plistlib.loads((SERVICE / "Resources" / "voice-cua-Info.plist").read_bytes())
        for k in ("NSAppleEventsUsageDescription", "NSMicrophoneUsageDescription",
                  "NSCalendarsFullAccessUsageDescription", "NSCalendarsUsageDescription"):
            self.assertTrue(app.get(k), k)
        for k in ("NSAppleEventsUsageDescription", "NSMicrophoneUsageDescription"):
            self.assertTrue(helper.get(k), k)
        self.assertEqual(app["CFBundleIdentifier"], "com.ingpoc.cua-service", "bundle id unchanged: TCC rows stay keyed")
        self.assertEqual(app["CFBundleExecutable"], "CUAService", "pgrep -x CUAService keeps working")
        # TCC / System Settings label = LaunchServices "name" = CFBundleName (not the display name).
        self.assertEqual(app["CFBundleName"], "Agent Computer Use")
        self.assertEqual(app["CFBundleDisplayName"], "Agent Computer Use")

    @unittest.skipUnless(shutil.which("iconutil"), "macOS iconutil")
    def test_icns_built_by_iconutil(self):
        out = self.tmp / "AppIcon.icns"
        self.assertTrue(inst.build_icns(out))
        self.assertGreater(out.stat().st_size, 100_000)
        src = (SERVICE / "install_service.py").read_text()
        self.assertNotIn('"icnsutil"', src)

    def test_version_single_source(self):
        version = inst.plugin_version()
        app = plistlib.loads((SERVICE / "Resources" / "Info.plist").read_bytes())
        self.assertEqual(app["CFBundleShortVersionString"], version, "Info.plist drifted from plugin.json")
        stamped = self.tmp / "Info.plist"
        stamped.write_bytes(plistlib.dumps({**app, "CFBundleShortVersionString": "0.0.1"}))
        inst.stamp_version(stamped)
        out = plistlib.loads(stamped.read_bytes())
        self.assertEqual(out["CFBundleShortVersionString"], version, "installer stamps plugin.json's version")
        self.assertTrue(out["CFBundleVersion"])

    def test_release_script_defaults_and_guards(self):
        sh = inst.SIGN_NOTARIZE.read_text()
        self.assertIn('Developer ID Application: Gurusharan Gupta (9UPQL479Z5)', sh)
        self.assertIn("acu-notary", sh)
        self.assertIn('ENT_DIR="$SCRIPT_DIR/../Resources"', sh)
        self.assertIn('"/Applications/Agent Computer Use.app") die "refusing to modify the live install', sh)
        self.assertIn("ent_for_bundle", sh)
        self.assertNotIn("--deep --force", sh)


if __name__ == "__main__":
    unittest.main()
