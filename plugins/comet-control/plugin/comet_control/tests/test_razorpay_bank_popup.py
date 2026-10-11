#!/usr/bin/env python3
"""razorpay_bank_popup safety: never AXPress when multiple same-title windows exist."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[3] / "skills" / "comet-control" / "scripts"
spec = importlib.util.spec_from_file_location("razorpay_bank_popup", SCRIPTS / "razorpay_bank_popup.py")
rzp = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = rzp
spec.loader.exec_module(rzp)


class FakeAX:
    def __init__(self, role, title=None, desc=None, children=None):
        self.role, self.title, self.desc, self.children = role, title, desc, children or []


def _copy(node, attr, _):
    if attr == "AXRole":
        return 0, node.role
    if attr == "AXTitle":
        return 0, node.title
    if attr == "AXDescription":
        return 0, node.desc
    if attr == "AXWindows":
        return 0, node.children if node.role == "APP" else None
    if attr == "AXChildren":
        return 0, node.children
    return 0, None


class RazorpayAxPressTests(unittest.TestCase):
    def test_ambiguous_same_title_windows_are_refused(self):
        app = FakeAX("APP", children=[
            FakeAX("AXWindow", "Razorpay Bank", children=[FakeAX("AXButton", "Success")]),
            FakeAX("AXWindow", "Razorpay Bank", children=[FakeAX("AXButton", "Success")]),
        ])
        with mock.patch.dict("sys.modules", {"ApplicationServices": mock.Mock(
            AXUIElementCreateApplication=lambda pid: app,
            AXUIElementCopyAttributeValue=_copy,
            AXUIElementPerformAction=lambda *a, **k: 0,
        )}):
            # Force import path used inside ax_press
            import ApplicationServices  # noqa: F401
            res = rzp.ax_press(1, "Razorpay Bank", "Success")
        self.assertFalse(res["pressed"])
        self.assertEqual(res["error"], "ax_ambiguous_windows")
        self.assertEqual(res["matches"], 2)

    def test_unique_title_presses_once(self):
        btn = FakeAX("AXButton", "Success")
        win = FakeAX("AXWindow", "Razorpay Bank", children=[btn])
        app = FakeAX("APP", children=[win])
        presses = []

        def perform(node, action):
            presses.append((node, action))
            return 0

        with mock.patch.dict("sys.modules", {"ApplicationServices": mock.Mock(
            AXUIElementCreateApplication=lambda pid: app,
            AXUIElementCopyAttributeValue=_copy,
            AXUIElementPerformAction=perform,
        )}):
            import ApplicationServices  # noqa: F401
            res = rzp.ax_press(1, "Razorpay Bank", "Success")
        self.assertTrue(res["pressed"], res)
        self.assertEqual([a for _, a in presses], ["AXRaise", "AXPress"])


if __name__ == "__main__":
    unittest.main()
