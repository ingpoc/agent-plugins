import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SKILL = Path(__file__).resolve().parents[1]
PLUGIN = SKILL.parents[1]
WINDOW_RESOLVE = SKILL / "scripts" / "window_resolve.py"
sys.path.insert(0, str(SKILL / "scripts"))
import install_harness
import compact_backend
import runtime_contract
import window_resolve


class ReleaseScriptTests(unittest.TestCase):
    def run_signer(self, failure):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = root / "Test.app"
            contents = app / "Contents"
            (contents / "MacOS").mkdir(parents=True)
            (contents / "Info.plist").write_text("stub plist")
            (contents / "MacOS" / "TestApp").write_text("stub executable")
            entitlements = root / "entitlements"
            entitlements.mkdir()
            work = root / "work"
            stub_dir = root / "bin"
            stub_dir.mkdir()
            script = root / "sign-notarize.sh"
            source = (SKILL / "service/release/sign-notarize.sh").read_text()
            script.write_text(source.replace("/usr/libexec/PlistBuddy", "PlistBuddy"))

            stub = stub_dir / "stub"
            stub.write_text('''#!/bin/bash
set -u
tool=${0##*/}
case "$tool" in
  security) printf '  1) 0x0 "%s"\\n' "$TEST_IDENTITY" ;;
  PlistBuddy)
    case " $* " in
      *"Print :CFBundleExecutable"*)
        if [[ "$STUB_FAILURE" == metadata ]]; then exit 21; fi
        printf 'TestApp\\n' ;;
      *) exit 1 ;;
    esac ;;
  codesign)
    if [[ "${1:-}" == --verify ]]; then
      case " $* " in *--verbose=2*) exit 0 ;; *) exit 1 ;; esac
    fi
    if [[ "${1:-}" == -dvv ]]; then
      printf 'flags=0x10000(runtime)\\nTeamIdentifier=ABCDEFGHIJ\\nTimestamp=now\\n'
    elif [[ "${1:-}" == -d && "${2:-}" == --entitlements ]]; then
      if [[ "$STUB_FAILURE" == entitlements ]]; then exit 22; fi
      printf '<dict/>\\n'
    elif [[ "${1:-}" == -d && "${2:-}" == -r- ]]; then
      printf 'designated => identifier "test"\\n'
    fi ;;
  xcrun)
    if [[ "${1:-}" == notarytool && "${2:-}" == submit ]]; then
      printf '{"id":"stub-id","status":"Accepted"}\\n'
    fi ;;
  plutil)
    if [[ "$STUB_FAILURE" == notary && "${1:-}" == -extract && "${2:-}" == id ]]; then exit 23; fi ;;
  ditto)
    for target do :; done
    : > "$target" ;;
  file) printf 'Mach-O 64-bit executable arm64\\n' ;;
  *) ;;
esac
exit 0
''')
            stub.chmod(0o755)
            for name in ("security", "PlistBuddy", "codesign", "xcrun", "plutil", "ditto", "spctl", "xattr", "file"):
                (stub_dir / name).symlink_to(stub)

            env = os.environ.copy()
            env.update({
                "PATH": f"{stub_dir}:{env['PATH']}",
                "STUB_FAILURE": failure,
                "TEST_IDENTITY": "Developer ID Application: Unit Test (ABCDEFGHIJ)",
            })
            args = ["bash", str(script), "--identity", env["TEST_IDENTITY"], "--entitlements", str(entitlements), "--work", str(work)]
            if failure != "notary":
                args.append("--sign-only")
            args.append(str(app))
            return subprocess.run(args, env=env, capture_output=True, text=True)

    def test_sign_notarize_reports_metadata_command_failure(self):
        result = self.run_signer("metadata")
        self.assertEqual(result.returncode, 1, result.stderr)
        lines = result.stderr.strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r"^ERROR: can't read CFBundleExecutable in .*/Contents/Info\.plist; repair the bundle metadata$")

    def test_sign_notarize_reports_entitlements_command_failure(self):
        result = self.run_signer("entitlements")
        self.assertEqual(result.returncode, 1, result.stderr)
        lines = result.stderr.strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines, ["ERROR: can't inspect entitlements for Contents/Info.plist; repair the code signature"])

    def test_sign_notarize_reports_missing_notary_response_id(self):
        result = self.run_signer("notary")
        self.assertEqual(result.returncode, 1, result.stderr)
        lines = result.stderr.strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r"^ERROR: notary response has no submission id; inspect .*/notary-\d{8}-\d{6}\.json and retry$")

    def test_hard_reset_targets_only_the_exact_driver_process(self):
        with patch.object(window_resolve.subprocess, "run") as run, \
             patch.object(window_resolve.os, "unlink"), \
             patch.object(window_resolve.time, "sleep"), \
             patch.object(window_resolve, "restart_daemon"):
            window_resolve.hard_reset_daemon()
        self.assertEqual(run.call_args_list[0].args[0], ["pkill", "-x", "cua-driver"])

    def test_installed_package_root_comes_from_plugin_manifest(self):
        self.assertTrue((PLUGIN / "plugin.json").is_file())
        self.assertEqual(compact_backend.PLUGIN_ROOT, PLUGIN)
        self.assertEqual(install_harness.PLUGIN_ROOT, PLUGIN)
        self.assertEqual(runtime_contract.PLUGIN_ROOT, PLUGIN)

    def test_mediaremote_dev_wrapper_returns_the_adapter_exit_status(self):
        wrapper = SKILL / "service/Vendor/mediaremote-adapter/scripts/mediaremote-adapter-dev.sh"
        result = subprocess.run(["bash", str(wrapper), "not-a-command"], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stderr.strip())


if __name__ == "__main__":
    unittest.main()
