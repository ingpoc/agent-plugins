#!/usr/bin/env python3
"""Unit coverage for the locked-runtime re-exec shim (no mcp import required)."""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import _locked_runtime as locked  # noqa: E402


class LockedRuntimeTests(unittest.TestCase):
    def test_mcp_available_treats_find_spec_raise_as_missing(self):
        with mock.patch.object(
            importlib.util, "find_spec", side_effect=ModuleNotFoundError("No module named 'mcp'")
        ):
            self.assertFalse(locked._mcp_server_available())

    def test_mcp_available_true_when_spec_present(self):
        with mock.patch.object(importlib.util, "find_spec", return_value=object()):
            self.assertTrue(locked._mcp_server_available())

    def test_mcp_available_false_when_spec_none(self):
        with mock.patch.object(importlib.util, "find_spec", return_value=None):
            self.assertFalse(locked._mcp_server_available())

    def test_reexec_returns_when_mcp_present(self):
        with mock.patch.object(locked, "_mcp_server_available", return_value=True):
            with mock.patch.object(locked, "_ensure_runtime") as ensure:
                locked.reexec_locked_runtime(Path(tempfile.gettempdir()))
        ensure.assert_not_called()


if __name__ == "__main__":
    unittest.main()
