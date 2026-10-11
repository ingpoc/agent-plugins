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


class RazorpayBindTests(unittest.TestCase):
    def test_test_mode_requires_key_id_value(self):
        self.assertTrue(rzp.is_test_key("https://api.razorpay.com/v1/gateway/?key_id=rzp_test_abc"))
        self.assertFalse(rzp.is_test_key(
            "https://api.razorpay.com/v1/gateway/?key_id=rzp_live_abc&note=key_id%3Drzp_test_"
        ))
        self.assertFalse(rzp.is_test_key("https://api.razorpay.com/v1/gateway/?note=key_id=rzp_test_"))

    def test_explicit_ax_does_not_probe_and_uses_bound_tab(self):
        window = {
            "id": "9", "name": "Razorpay Bank",
            "url": "https://api.razorpay.com/v1/gateway/?key_id=rzp_test_abc",
            "active_tab_id": "3", "tabs": 1,
            "tab_list": [{
                "id": "3",
                "url": "https://api.razorpay.com/v1/gateway/?key_id=rzp_test_abc",
                "active": True,
            }],
        }
        with (
            mock.patch.object(rzp, "comet_windows", return_value=[window]),
            mock.patch.object(rzp, "_probe_all", side_effect=AssertionError("js probe")),
            mock.patch.object(rzp, "exec_js", side_effect=AssertionError("exec js")),
            mock.patch.object(rzp, "preflight", return_value={"comet_pid": 1, "recommended_method": "ax"}),
            mock.patch.object(rzp, "ax_press", return_value={"pressed": True}) as press,
        ):
            code = rzp.main(["press", "--method", "ax", "--verify-seconds", "0"])
        press.assert_called_once_with(1, "Razorpay Bank", "Success")
        self.assertEqual(code, 6)

    def test_ax_refuses_when_probed_tab_is_not_active(self):
        window = {
            "id": "9", "name": "Razorpay Bank",
            "url": "https://example.test/other",
            "active_tab_id": "8", "tabs": 2,
            "tab_list": [
                {"id": "3", "url": "https://api.razorpay.com/v1/gateway/?key_id=rzp_test_abc", "active": False},
                {"id": "8", "url": "https://example.test/other", "active": True},
            ],
        }
        with (
            mock.patch.object(rzp, "comet_windows", return_value=[window]),
            mock.patch.object(rzp, "_probe_all", side_effect=AssertionError("js probe")),
            mock.patch.object(rzp, "preflight", return_value={"comet_pid": 1}),
            mock.patch.object(rzp, "ax_press") as press,
        ):
            code = rzp.main(["press", "--method", "ax", "--verify-seconds", "0"])
        press.assert_not_called()
        self.assertEqual(code, 5)

    def test_exec_script_targets_tab_by_id(self):
        self.assertIn("w.tabs.byId", rzp._JXA_EXEC)


if __name__ == "__main__":
    unittest.main()
