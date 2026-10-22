#!/usr/bin/env python3
"""Real AppKit and /usr/bin/sips cursor raster cases (macOS only).

Moved verbatim from test_macos_cua.CursorProofTests, which now fakes AppKit at
the import boundary so its pure logic runs on every platform. This file is
not test_*.py, so unit discovery never collects it. scripts/check.py reports
"integration: skipped ..., needs <INTEGRATION_REQUIRES>" unless
AGENT_PLUGINS_INTEGRATION=1. When it runs, a non-macOS host or a missing
framework fails; it never skips. Drawing is offscreen (NSImage lockFocus) and
takes no window focus.
"""

import contextlib
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

INTEGRATION_REQUIRES = "macOS with AppKit (PyObjC) and /usr/bin/sips"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cursor_png_fixtures  # noqa: E402

SCRIPT = Path(__file__).parents[1] / "scripts" / "macos-cua.py"
SPEC = importlib.util.spec_from_file_location("macos_cua", SCRIPT)
macos_cua = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(macos_cua)


class CursorRasterIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sys.platform != "darwin":
            raise AssertionError(f"needs {INTEGRATION_REQUIRES}; host is {sys.platform}")
        import AppKit  # noqa: F401  -- ImportError is a failure, not a skip

        if not os.access("/usr/bin/sips", os.X_OK):
            raise AssertionError("/usr/bin/sips is not executable")

    def test_cursor_proof_uses_the_hermes_extension_asset(self):
        from AppKit import (
            NSBitmapImageFileTypePNG,
            NSBitmapImageRep,
            NSColor,
            NSImage,
            NSMakeRect,
            NSRectFill,
        )

        self.assertTrue(Path(macos_cua.CURSOR_ICON).is_file())
        self.assertEqual(Path(macos_cua.CURSOR_ICON).name, "pointer-shape-animated.svg")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.png"
            image = NSImage.alloc().initWithSize_((120, 120))
            image.lockFocus()
            NSColor.windowBackgroundColor().setFill()
            NSRectFill(NSMakeRect(0, 0, 120, 120))
            representation = NSBitmapImageRep.alloc().initWithFocusedViewRect_(
                NSMakeRect(0, 0, 120, 120)
            )
            image.unlockFocus()
            data = representation.representationUsingType_properties_(
                NSBitmapImageFileTypePNG, {}
            )
            data.writeToFile_atomically_(str(source), True)

            result = macos_cua.annotate_cursor_screenshot(source, 0.5, 0.5)

            self.assertTrue(result["ok"], result)
            self.assertEqual(result["cursor_asset"], macos_cua.CURSOR_ICON)
            self.assertGreater(
                Path(result["path"]).stat().st_size, source.stat().st_size
            )

    def test_native_cursor_raster_uses_the_same_hermes_asset(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "pointer.png"
            result = macos_cua.cursor_raster_path(
                macos_cua.CURSOR_ICON, output, size=96
            )

            self.assertEqual(Path(result), output)
            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 100)
            self.assertEqual(macos_cua._validate_cursor_raster(output, 96), (96, 96))

    def test_real_cursor_svg_uses_appkit_when_sips_exits_13(self):
        failure = SimpleNamespace(
            returncode=13,
            stderr="Cannot extract image from file / Error 13 unknown",
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(macos_cua.subprocess, "run", return_value=failure) as run,
            mock.patch.object(macos_cua.time, "sleep") as sleep,
        ):
            output = Path(directory) / "pointer.png"
            result = macos_cua.cursor_raster_path(
                macos_cua.CURSOR_ICON, output, size=96
            )

            self.assertEqual(Path(result), output)
            self.assertGreater(output.stat().st_size, 100)
            self.assertEqual(macos_cua._validate_cursor_raster(output, 96), (96, 96))
            self.assertEqual(run.call_count, 2)
            sleep.assert_called_once_with(0.05)

    def test_unit_fake_appkit_matches_real_appkit_on_fixtures(self):
        """The CursorProofTests AppKit fake reaches real AppKit's verdicts."""
        cases = {
            "visible": (cursor_png_fixtures.png_bytes(), None),
            "transparent": (
                cursor_png_fixtures.png_bytes(alpha=0),
                "cursor raster has no visible pixels",
            ),
            "low-alpha": (cursor_png_fixtures.png_bytes(alpha=1), None),
        }
        fake = cursor_png_fixtures.fake_appkit()
        for label, (payload, expected_error) in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "cursor.png"
                path.write_bytes(payload)
                verdicts = []
                for appkit in (None, fake):
                    patcher = (
                        mock.patch.dict(sys.modules, {"AppKit": appkit})
                        if appkit is not None
                        else contextlib.nullcontext()
                    )
                    with patcher:
                        try:
                            verdicts.append(("ok", macos_cua._validate_cursor_raster(path, 96)))
                        except ValueError as error:
                            verdicts.append(("error", str(error)))
                self.assertEqual(verdicts[0], verdicts[1], label)
                if expected_error is None:
                    self.assertEqual(verdicts[0], ("ok", (96, 96)))
                else:
                    self.assertEqual(verdicts[0], ("error", expected_error))

if __name__ == "__main__":
    unittest.main()
