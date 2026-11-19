#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

VOICE_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(VOICE_SCRIPTS))
import build_voice_helper as voice_builder  # noqa: E402


class BuildVoiceHelperTests(unittest.TestCase):
    def test_voice_builder_does_not_install_missing_pyinstaller(self):
        with patch("builtins.__import__", side_effect=ImportError), patch.object(voice_builder, "run") as run:
            with self.assertRaises(SystemExit) as raised:
                voice_builder.ensure_pyinstaller()
        self.assertEqual(raised.exception.code, 1)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
