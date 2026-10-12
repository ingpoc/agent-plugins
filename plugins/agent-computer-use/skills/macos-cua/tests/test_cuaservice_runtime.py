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

    def test_cli_snap_source_fails_loud_on_error(self):
        src = (Path(__file__).parents[1] / "scripts" / "runtime_cli.py").read_text()
        # Must check snapshot errors before pretty-printing Elements: 0.
        self.assertIn('snap = snapshot(pid, wid, args.max_elements, args.mode)', src)
        self.assertIn('snap.get("error")', src)
        self.assertIn('_emit_json(payload, require_ok=True)', src)
        # Pretty printer remains for success path only.
        self.assertIn('_print_snap(snap)', src)

    def test_snapshot_is_cuaservice_walk_in_legacy_shape(self):
        snap, client = self._run({"get_app_state": _state()}, macos_cua.snapshot, 4242, 77, 30)
        method, params = client.calls[0]
        self.assertEqual(method, "get_app_state")
        self.assertEqual((params["app"], params["maxElements"], params["includeElements"],
                          params["disableDiff"], params["raiseForInput"], params.get("windowId")),
                         (BUNDLE, 30, True, True, False, 77))
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
        clicked, client = self._run({"get_app_state": _state(), "click": {"ok": True, "method": "ax-press",
                                               "cursor_landing": {"landed": True}}}, macos_cua.click, 4242, 77, 2)
        self.assertEqual([c[0] for c in client.calls], ["get_app_state", "click"])
        self.assertEqual(client.calls[-1], ("click", {"app": BUNDLE, "element_index": 2, "windowId": 77}))
        self.assertTrue(macos_cua._accepted(clicked))
        self.assertEqual(clicked["cursor_landing"], {"landed": True})

    def test_refused_unlanded_press_is_not_accepted_and_names_the_code(self):
        err = Exception("RPC error -32010")
        err.code, err.message = -32010, "CURSOR_NOT_LANDED: agent cursor did not land"
        result, _ = self._run({"get_app_state": _state(), "click": err}, macos_cua.click, 4242, 77, 2)
        self.assertFalse(macos_cua._accepted(result))
        self.assertEqual(result["error_code"], "CURSOR_NOT_LANDED")

    def test_allow_unlanded_is_opt_in_via_env(self):
        _, client = self._run({"get_app_state": _state(), "click": {"ok": True}}, macos_cua.click, 4242, 77, 1,
                              env={"MACOS_CUA_ALLOW_UNLANDED": "1"})
        click_params = [p for m, p in client.calls if m == "click"][0]
        self.assertTrue(click_params["allow_unlanded"])
        _, client = self._run({"get_app_state": _state(), "click": {"ok": True}}, macos_cua.click, 4242, 77, 1)
        click_params = [p for m, p in client.calls if m == "click"][0]
        self.assertNotIn("allow_unlanded", click_params)

    def test_set_value_reads_back_from_the_same_walk(self):
        replies = {"set_value": {"ok": True, "method": "ax-set-value"}, "get_app_state": _state(value="new text")}
        ok, client = self._run(replies, macos_cua.set_value, 4242, 77, 1, "new text")
        self.assertEqual([c[0] for c in client.calls[:3]], ["get_app_state", "get_app_state", "set_value"])
        self.assertEqual(client.calls[2], ("set_value", {"app": BUNDLE, "element_index": 1, "value": "new text", "windowId": 77}))
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
        states = iter([_state(value="x"), err])
        replies = {"set_value": {"ok": True}, "get_app_state": lambda params: next(states)}
        bad, _ = self._run(replies, macos_cua.set_value, 4242, 77, 1, "x")
        self.assertFalse(macos_cua._accepted(bad))
        self.assertIn("readback failed", bad["error"])
        state = _state(value="x")
        state["elements"] = state["elements"][:1]
        states = iter([_state(value="x"), state])
        replies = {"set_value": {"ok": True}, "get_app_state": lambda params: next(states)}
        bad, _ = self._run(replies, macos_cua.set_value, 4242, 77, 1, "x")
        self.assertFalse(macos_cua._accepted(bad))
        self.assertIn("target missing", bad["error"])
        slider = _state(value="50.0")
        slider["elements"][1]["role"] = "AXSlider"
        replies = {"set_value": {"ok": True}, "get_app_state": slider}
        ok, _ = self._run(replies, macos_cua.set_value, 4242, 77, 1, "50")
        self.assertEqual(ok["readback"], "matched")
        text_state = _state(value="1")
        replies = {"set_value": {"ok": True}, "get_app_state": text_state}  # AXTextArea: "1" is not "001"
        bad, _ = self._run(replies, macos_cua.set_value, 4242, 77, 1, "001")
        bad, _ = self._run(replies, macos_cua.set_value, 4242, 77, 1, "001")
        self.assertEqual(bad["error"], "set_value readback mismatch")

    def test_snapshot_refuses_a_different_window_than_requested(self):
        snap, _ = self._run({"get_app_state": _state()}, macos_cua.snapshot, 4242, 99, 30)
        self.assertNotEqual(snap.get("ok"), True)
        self.assertIn("requested window 99", snap["error"])

    def test_type_text_focuses_element_then_types(self):
        replies = {"get_app_state": _state(), "click": {"ok": True, "method": "ax-press"},
                   "type_text": {"ok": True, "method": "ax-insert"}}
        res, client = self._run(replies, macos_cua.type_text, 4242, 77, 1, "abc")
        self.assertEqual([c[0] for c in client.calls], ["get_app_state", "click", "type_text"])
        # After the mutating focus click, omit stale element_index (r11c #2).
        self.assertEqual(client.calls[-1][1], {"app": BUNDLE, "text": "abc", "windowId": 77})
        self.assertTrue(macos_cua._accepted(res))

    def test_type_text_newline_guard_kept(self):
        res, client = self._run({}, macos_cua.type_text, 4242, 77, None, "a\nb")
        self.assertEqual(res["error_code"], "newline_may_submit")
        self.assertEqual(client.calls, [])
        res, client = self._run({"get_app_state": _state(), "type_text": {"ok": True}},
                                macos_cua.type_text, 4242, 77, None, "a\nb", allow_newline=True)
        self.assertEqual([c[0] for c in client.calls], ["get_app_state", "type_text"])
        self.assertEqual(client.calls[-1], ("type_text", {"app": BUNDLE, "text": "a\nb", "windowId": 77}))

    def test_scroll_element_and_point_contract(self):
        res, client = self._run({"get_app_state": _state(), "scroll": {"ok": True}}, macos_cua.scroll, 4242, 77, "down", 2,
                                by="page", element_index=1)
        self.assertEqual([c[0] for c in client.calls], ["get_app_state", "scroll"])
        self.assertEqual(client.calls[-1][1], {"app": BUNDLE, "direction": "down", "pages": 2, "element_index": 1, "windowId": 77})
        res, client = self._run({}, macos_cua.scroll, 4242, 77, "down", 3, x=10, y=10)
        self.assertIn("--element", res["error"])
        self.assertEqual(client.calls, [])

    def test_right_click_is_show_menu_on_the_cuaservice_element(self):
        _, client = self._run({"get_app_state": _state(), "perform_secondary_action": {"ok": True}},
                              macos_cua.right_click, 4242, 77, 2)
        self.assertEqual([c[0] for c in client.calls], ["get_app_state", "perform_secondary_action"])
        self.assertEqual(client.calls[-1][1], {"app": BUNDLE, "element_index": 2, "action": "AXShowMenu", "windowId": 77})

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

    def test_label_click_is_a_cuaservice_index_click(self):
        replies = {"get_app_state": _state(), "click": {"ok": True, "method": "ax-press",
                                                        "cursor_landing": {"landed": True}}}
        with mock.patch.object(macos_cua, "_native_input", side_effect=AssertionError("no native fallback")):
            res, client = self._run(replies, macos_cua.click_label_pointer, 4242, 77, "Close", app_name="TextEdit")
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["element"], res["method"], res["cursor_landing"]), (2, "cuaservice-click", {"landed": True}))
        self.assertEqual(client.calls[-1], ("click", {"app": BUNDLE, "element_index": 2, "windowId": 77}))

    def test_refused_label_click_does_not_fall_through(self):
        err = Exception("RPC error -32010")
        err.code, err.message = -32010, "CURSOR_NOT_LANDED: agent cursor did not land"
        with mock.patch.object(macos_cua, "_native_input", side_effect=AssertionError("no native fallback")):
            res, client = self._run({"get_app_state": _state(), "click": err},
                                    macos_cua.click_label_pointer, 4242, 77, "Close")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "CURSOR_NOT_LANDED")
        self.assertEqual([c[0] for c in client.calls].count("click"), 1)
        miss, _ = self._run({"get_app_state": _state()}, macos_cua.click_label_pointer, 4242, 77, "Nope")
        self.assertEqual((miss["ok"], miss["error_code"]), (False, "target_missing"))

    def test_point_click_maps_pixels_and_uses_cuaservice(self):
        replies = {"get_app_state": _state(), "click": {"ok": True, "method": "cgevent-pid"}}
        with mock.patch.object(macos_cua, "_backing_scale", return_value=2.0):
            res, client = self._run(replies, macos_cua.click_point, 4242, 77, 20, 40)
        self.assertTrue(res["ok"], res)
        self.assertEqual(client.calls[-1], ("click", {"app": BUNDLE, "x": 110.0, "y": 70.0, "windowId": 77}))
        dbl, client = self._run(replies, macos_cua.click_point, 4242, 77, 20, 40, click_count=2)
        self.assertEqual((dbl["ok"], dbl["error_code"]), (False, "unsupported_click"))
        self.assertEqual(client.calls, [])

    def test_index_click_enforces_requested_window_id(self):
        bad = _state(); bad["windowId"] = 88
        res, client = self._run({"get_app_state": bad}, macos_cua.click, 4242, 77, 2)
        self.assertFalse(res["ok"])
        self.assertIn("requested window 77", res["error"])
        self.assertEqual([c[0] for c in client.calls], ["get_app_state"])
        # Matching window proceeds to click.
        res, client = self._run({"get_app_state": _state(), "click": {"ok": True, "method": "ax-press"}},
                                macos_cua.click, 4242, 77, 2)
        self.assertTrue(res["ok"], res)
        self.assertEqual([c[0] for c in client.calls], ["get_app_state", "click"])

    def test_point_click_enforces_requested_window_id(self):
        # Focused/observed window 88 != requested 77 -> refuse before click.
        bad = _state()
        bad["windowId"] = 88
        res, client = self._run({"get_app_state": bad}, macos_cua.click_point, 4242, 77, 20, 40)
        self.assertFalse(res["ok"])
        self.assertIn("requested window 77", res["error"])
        self.assertEqual([c[0] for c in client.calls], ["get_app_state"])

    def test_label_click_discards_foreign_native_index(self):
        # Native one-based index 1 would hit AXTextArea in CUAService space; label
        # resolution in the refreshed CUAService walk must click Close (index 2).
        native = {"source": "native_ax", "elements": [
            {"element_index": 1, "role": "AXButton", "label": "Close", "value": None,
             "frame": {}, "actions": ["AXPress"], "parent_index": None},
        ]}
        replies = {"get_app_state": _state(), "click": {"ok": True, "method": "ax-press",
                                                        "cursor_landing": {"landed": True}}}
        res, client = self._run(
            replies, macos_cua.click_label_pointer, 4242, 77, "Close",
            snapshot_data=native, element_index=1,
        )
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["element"], 2)
        self.assertEqual(client.calls[-1], ("click", {"app": BUNDLE, "element_index": 2, "windowId": 77}))

    def test_desktop_click_is_retired(self):
        res, client = self._run({}, macos_cua.click_at_desktop, 10, 10)
        self.assertEqual((res["ok"], res["error_code"]), (False, "desktop_click_retired"))
        self.assertEqual(client.calls, [])

    def test_set_value_null_readback_is_not_a_match(self):
        state = _state(value=None)
        res, _ = self._run({"set_value": {"ok": True}, "get_app_state": state}, macos_cua.set_value, 4242, 77, 1, "")
        self.assertFalse(macos_cua._accepted(res))
        self.assertIn("readback unavailable", res["error"])

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

    def test_action_rpcs_pin_requested_window_id(self):
        """Action RPCs must carry windowId so CUAService does not re-resolve focus (r8 #1)."""
        replies = {"get_app_state": _state(), "click": {"ok": True, "method": "ax-press"},
                   "type_text": {"ok": True}, "scroll": {"ok": True},
                   "perform_secondary_action": {"ok": True}, "set_value": {"ok": True},
                   "drag": {"ok": True}}
        _, client = self._run(replies, macos_cua.type_text, 4242, 77, 1, "hi")
        typed = [c for c in client.calls if c[0] == "type_text"][-1][1]
        clicked = [c for c in client.calls if c[0] == "click"][-1][1]
        self.assertEqual(typed.get("windowId"), 77)
        self.assertEqual(clicked.get("windowId"), 77)
        _, client = self._run(replies, macos_cua.scroll, 4242, 77, "down", 2, by="page", element_index=1)
        self.assertEqual(client.calls[-1][1].get("windowId"), 77)
        _, client = self._run(replies, macos_cua.right_click, 4242, 77, 2)
        self.assertEqual(client.calls[-1][1].get("windowId"), 77)
        _, client = self._run(replies, macos_cua.set_value, 4242, 77, 1, "hello")
        setp = [c for c in client.calls if c[0] == "set_value"][-1][1]
        self.assertEqual(setp.get("windowId"), 77)
        with mock.patch.object(macos_cua, "_backing_scale", return_value=2.0):
            _, client = self._run(replies, macos_cua.drag, 4242, 77, 20, 40, 220, 40)
        self.assertEqual(client.calls[-1][1].get("windowId"), 77)

    def test_plan_state_swift_includes_elements(self):
        """Native plan snapshots must request includeElements (r8 #2)."""
        router = (Path(__file__).parents[1] / "service" / "Sources" / "CUAService" / "MethodRouter.swift").read_text()
        # planState block must include both flags.
        start = router.index("private func planState")
        block = router[start:start + 700]
        self.assertIn('"includeScreenshot": AnyCodable(false)', block)
        self.assertIn('"includeElements": AnyCodable(true)', block)




if __name__ == "__main__":
    unittest.main()
