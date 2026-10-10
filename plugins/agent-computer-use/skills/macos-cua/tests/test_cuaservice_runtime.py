#!/usr/bin/env python3
"""snap/state and index-addressed input share ONE CUAService index space;
nothing reaches the retired cua-driver (batch-3 #4)."""
from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).parents[1] / "scripts" / "macos-cua.py"
SPEC = importlib.util.spec_from_file_location("macos_cua_cuaservice_rt", SCRIPT)
macos_cua = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(macos_cua)

BUNDLE = "com.apple.TextEdit"
ELEMENTS = [
    {"element_index": 0, "role": "AXWindow", "label": "Untitled", "value": None,
     "frame": {"x": 100.0, "y": 50.0, "w": 600.0, "h": 400.0}, "actions": [], "parent_index": None, "depth": 0},
    {"element_index": 1, "role": "AXTextArea", "label": None, "value": "hello",
     "frame": {"x": 110.0, "y": 90.0, "w": 580.0, "h": 350.0}, "actions": ["AXPress"], "parent_index": 0, "depth": 1},
    {"element_index": 2, "role": "AXButton", "label": "Close", "value": None,
     "frame": {"x": 108.0, "y": 56.0, "w": 14.0, "h": 14.0}, "actions": ["AXPress"], "parent_index": 0, "depth": 1},
]


class FakeClient:
    def __init__(self, replies):
        self.replies = replies
        self.calls = []
        self.closed = 0

    def call(self, method, params=None, *, retry=True):
        self.calls.append((method, dict(params or {})))
        reply = self.replies.get(method)
        if isinstance(reply, Exception):
            raise reply
        return reply(params) if callable(reply) else reply

    def list_apps(self):
        return self.call("list_apps")

    def close(self):
        self.closed += 1


def _state(shot=None, value="hello"):
    els = [dict(e) for e in ELEMENTS]
    els[1]["value"] = value
    out = {"app": "TextEdit", "pid": 4242, "windowId": 77, "text": "[0] AXWindow \"Untitled\"\n  [1] AXTextArea",
           "elementCount": len(els), "elements": els, "axTrusted": True,
           "windowFrame": {"x": 100.0, "y": 50.0, "w": 600.0, "h": 400.0}}
    if shot:
        out["screenshot"] = {"url": f"file://{shot}"}
    return out


class CUAServiceRuntimeTests(unittest.TestCase):
    def _run(self, replies, fn, *args, env=None, **kwargs):
        client = FakeClient(replies)
        with mock.patch.object(macos_cua, "_running_app_identity",
                               return_value={"pid": 4242, "bundle_id": BUNDLE, "name": "TextEdit"}), \
             mock.patch.object(macos_cua, "_pids_for_app", return_value=[4242]), \
             mock.patch.object(macos_cua, "_cuaservice_client", return_value=client), \
             mock.patch.object(macos_cua, "call_driver", side_effect=AssertionError("cua-driver is retired")), \
             mock.patch.dict(os.environ, env or {}, clear=False):
            result = fn(*args, **kwargs)
        return result, client

    def test_snapshot_is_cuaservice_walk_in_legacy_shape(self):
        snap, client = self._run({"get_app_state": _state()}, macos_cua.snapshot, 4242, 77, 30)
        method, params = client.calls[0]
        self.assertEqual(method, "get_app_state")
        self.assertEqual((params["app"], params["maxElements"], params["includeElements"],
                          params["disableDiff"], params["raiseForInput"]), (BUNDLE, 30, True, True, False))
        self.assertEqual(snap["engine"], "CUAService")
        self.assertEqual([e["element_index"] for e in snap["elements"]], [0, 1, 2])
        self.assertEqual(snap["window_id"], 77)
        self.assertEqual(snap["element_count"], 3)
        self.assertTrue(snap["tree_markdown"])

    def test_vision_snapshot_fails_loud(self):
        snap, client = self._run({}, macos_cua.snapshot, 4242, 77, 30, mode="vision")
        self.assertIn("retired cua-driver", snap["error"])
        self.assertEqual(client.calls, [])

    def test_required_screenshot_missing_is_an_error(self):
        snap, _ = self._run({"get_app_state": _state()}, macos_cua.snapshot, 4242, 77, 30, include_screenshot=True)
        self.assertEqual(snap["error"], "screenshot artifact did not materialize")

    def test_state_indices_match_click_index_and_copy_screenshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp, "cua.png")
            src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (1200).to_bytes(4, "big") + (800).to_bytes(4, "big"))
            out = Path(tmp, "out.png")
            state, _ = self._run({"get_app_state": _state(shot=src)}, macos_cua.app_state, "TextEdit", 4242, 77,
                                 screenshot_out_file=str(out), prepare_foreground=False)
            self.assertTrue(state["ok"], state)
            self.assertTrue(out.is_file())
            self.assertEqual((state["screenshot"]["path"], state["screenshot"]["width"]), (str(out), 1200))
        self.assertIn("[2] AXButton \"Close\"", state["text"])
        clicked, client = self._run({"click": {"ok": True, "method": "ax-press",
                                               "cursor_landing": {"landed": True}}}, macos_cua.click, 4242, 77, 2)
        self.assertEqual(client.calls, [("click", {"app": BUNDLE, "element_index": 2})])
        self.assertTrue(macos_cua._accepted(clicked))
        self.assertEqual(clicked["cursor_landing"], {"landed": True})

    def test_refused_unlanded_press_is_not_accepted_and_names_the_code(self):
        err = Exception("RPC error -32010")
        err.code, err.message = -32010, "CURSOR_NOT_LANDED: agent cursor did not land"
        result, _ = self._run({"click": err}, macos_cua.click, 4242, 77, 2)
        self.assertFalse(macos_cua._accepted(result))
        self.assertEqual(result["error_code"], "CURSOR_NOT_LANDED")

    def test_allow_unlanded_is_opt_in_via_env(self):
        _, client = self._run({"click": {"ok": True}}, macos_cua.click, 4242, 77, 1,
                              env={"MACOS_CUA_ALLOW_UNLANDED": "1"})
        self.assertTrue(client.calls[0][1]["allow_unlanded"])
        _, client = self._run({"click": {"ok": True}}, macos_cua.click, 4242, 77, 1)
        self.assertNotIn("allow_unlanded", client.calls[0][1])

    def test_set_value_reads_back_from_the_same_walk(self):
        replies = {"set_value": {"ok": True, "method": "ax-set-value"}, "get_app_state": _state(value="new text")}
        ok, client = self._run(replies, macos_cua.set_value, 4242, 77, 1, "new text")
        self.assertEqual(client.calls[0], ("set_value", {"app": BUNDLE, "element_index": 1, "value": "new text"}))
        self.assertEqual(ok["readback"], "matched")
        replies["get_app_state"] = _state(value="stale")
        bad, _ = self._run(replies, macos_cua.set_value, 4242, 77, 1, "new text")
        self.assertFalse(macos_cua._accepted(bad))
        self.assertEqual(bad["error"], "set_value readback mismatch")

    def test_empty_observation_is_an_error_not_ok(self):
        empty = {"app": "TextEdit", "pid": 4242, "windowId": 77, "text": "", "elementCount": 0, "elements": []}
        snap, client = self._run({"get_app_state": empty}, macos_cua.snapshot, 4242, 77, 30)
        self.assertNotEqual(snap.get("ok"), True)
        self.assertIn("empty observation", snap["error"])
        self.assertGreater(len(client.calls), 1)  # retried before failing

    def test_set_value_readback_error_or_missing_target_is_not_accepted(self):
        err = Exception("RPC error -32000")
        err.code, err.message = -32000, "walk failed"
        replies = {"set_value": {"ok": True}, "get_app_state": err}
        bad, _ = self._run(replies, macos_cua.set_value, 4242, 77, 1, "x")
        self.assertFalse(macos_cua._accepted(bad))
        self.assertIn("readback failed", bad["error"])
        state = _state(value="x")
        state["elements"] = state["elements"][:1]
        replies["get_app_state"] = state
        bad, _ = self._run(replies, macos_cua.set_value, 4242, 77, 1, "x")
        self.assertFalse(macos_cua._accepted(bad))
        self.assertIn("target missing", bad["error"])
        slider = _state(value="50.0")
        slider["elements"][1]["role"] = "AXSlider"
        replies["get_app_state"] = slider
        ok, _ = self._run(replies, macos_cua.set_value, 4242, 77, 1, "50")
        self.assertEqual(ok["readback"], "matched")

    def test_type_text_focuses_element_then_types(self):
        replies = {"click": {"ok": True, "method": "ax-press"}, "type_text": {"ok": True, "method": "ax-insert"}}
        res, client = self._run(replies, macos_cua.type_text, 4242, 77, 1, "abc")
        self.assertEqual([c[0] for c in client.calls], ["click", "type_text"])
        self.assertEqual(client.calls[1][1], {"app": BUNDLE, "text": "abc"})
        self.assertTrue(macos_cua._accepted(res))

    def test_type_text_newline_guard_kept(self):
        res, client = self._run({}, macos_cua.type_text, 4242, 77, None, "a\nb")
        self.assertEqual(res["error_code"], "newline_may_submit")
        self.assertEqual(client.calls, [])
        res, client = self._run({"type_text": {"ok": True}}, macos_cua.type_text, 4242, 77, None, "a\nb",
                                allow_newline=True)
        self.assertEqual(client.calls, [("type_text", {"app": BUNDLE, "text": "a\nb"})])

    def test_scroll_element_and_point_contract(self):
        res, client = self._run({"scroll": {"ok": True}}, macos_cua.scroll, 4242, 77, "down", 2,
                                by="page", element_index=1)
        self.assertEqual(client.calls[0][1], {"app": BUNDLE, "direction": "down", "pages": 2, "element_index": 1})
        res, client = self._run({}, macos_cua.scroll, 4242, 77, "down", 3, x=10, y=10)
        self.assertIn("--element", res["error"])
        self.assertEqual(client.calls, [])

    def test_right_click_is_show_menu_on_the_cuaservice_element(self):
        _, client = self._run({"perform_secondary_action": {"ok": True}}, macos_cua.right_click, 4242, 77, 2)
        self.assertEqual(client.calls[0][1], {"app": BUNDLE, "element_index": 2, "action": "AXShowMenu"})

    def test_drag_maps_window_local_pixels_to_global_points(self):
        replies = {"get_app_state": _state(), "drag": {"ok": True, "method": "cgevent-drag-pid"}}
        with mock.patch.object(macos_cua, "_backing_scale", return_value=2.0):
            res, client = self._run(replies, macos_cua.drag, 4242, 77, 20, 40, 220, 40)
        method, params = client.calls[-1]
        self.assertEqual(method, "drag")
        self.assertEqual((params["from_x"], params["from_y"], params["to_x"], params["to_y"]),
                         (110.0, 70.0, 210.0, 70.0))
        with mock.patch.object(macos_cua, "_backing_scale", return_value=1.0):
            out, client = self._run(replies, macos_cua.drag, 4242, 77, 20, 40, 5000, 40)
        self.assertIn("outside the window", out["error"])
        self.assertNotIn("drag", [c[0] for c in client.calls])

    def test_list_apps_shape(self):
        apps, _ = self._run({"list_apps": [{"bundleId": BUNDLE, "name": "TextEdit", "pid": 4242, "isActive": False}]},
                            macos_cua.list_apps)
        self.assertEqual(apps["apps"][0], {"name": "TextEdit", "bundle_id": BUNDLE, "pid": 4242,
                                           "running": True, "active": False})

    def test_twin_instances_are_refused_before_dispatch(self):
        client = FakeClient({})
        with mock.patch.object(macos_cua, "_running_app_identity",
                               return_value={"pid": 4242, "bundle_id": BUNDLE}), \
             mock.patch.object(macos_cua, "_pids_for_app", return_value=[4242, 5151]), \
             mock.patch.object(macos_cua, "_cuaservice_client", return_value=client):
            res = macos_cua.click(4242, 77, 1)
        self.assertIn("instances", res["error"])
        self.assertEqual(client.calls, [])

    def test_no_index_input_reaches_cua_driver(self):
        import inspect
        for name in ("snapshot", "app_state", "click", "right_click", "set_value",
                     "type_text", "scroll", "drag", "list_apps"):
            src = inspect.getsource(inspect.unwrap(getattr(macos_cua, name)))
            self.assertNotIn("call_driver", src, name)


if __name__ == "__main__":
    unittest.main()
