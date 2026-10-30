"""Broker-side real input (native/cua_input.py) under the Chrome 155 debugger policy."""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
import struct
from unittest.mock import Mock, patch
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "native" / "cua_input.py"
SPEC = importlib.util.spec_from_file_location("cua_input_under_test", PATH)
ci = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci)

BOUNDS = {"left": 2560, "top": 317, "width": 1920, "height": 1050}


class FakeRpc:
    def __init__(self, titles=None, fail_keys=False):
        self.calls = []
        self.titles = titles or {}
        self.fail_keys = fail_keys

    def call(self, method, params, timeout=15.0):
        self.calls.append((method, params))
        if method == "get_app_state":
            wid = params["windowId"]
            out = {"windowId": wid, "windowTitle": self.titles.get(wid, ""), "windowWidth": 1920, "windowHeight": 1050}
            if params.get("includeScreenshot"):
                out["screenshot"] = {"url": f"file://{self.shot}"}
            if params.get("includeElements"):
                out["elements"] = [{"role": "AXButton", "element_index": 45, "actions": ["AXPress"],
                                    "frame": {"x": 2589, "y": 816, "w": 139, "h": 46}, "depth": 9}]
            return out
        if method == "execute_plan":
            return {"ok": True, "results": [{"ok": not self.fail_keys, "error": "boom" if self.fail_keys else None} for _ in params["steps"]]}
        if method in {"press_key", "click"}:
            return {"ok": True, "method": "cgevent-key-pid" if method == "press_key" else "cgevent-click-pid"}
        if method == "list_apps":
            return {"result": [1, 2]}
        raise AssertionError(method)


class FakeDesktop:
    def __init__(self, windows, front="com.anysphere.sand", keystrokes=True):
        self._windows = windows
        self.front = {"pid": 21405, "bundle_id": front}
        self.clipboard = [{"public.utf8-plain-text": b"user clip"}]
        self.put = []
        self.activated = []
        self.keystrokes = keystrokes

    def comet_pids(self):
        return [72359]

    def windows(self, pid):
        assert pid == 72359
        return self._windows

    def frontmost(self):
        return self.front

    def activate(self, pid):
        self.activated.append(pid)
        self.front = {"pid": pid, "bundle_id": "restored"}
        return True

    def clipboard_save(self):
        return list(self.clipboard)

    def clipboard_put_text(self, text):
        self.put.append(text)
        self.clipboard = [{"public.utf8-plain-text": text.encode()}]

    def clipboard_restore(self, items):
        self.clipboard = items

    def keystrokes_allowed(self):
        return self.keystrokes


class ImmediateTimer:
    def __init__(self, _delay, fn):
        self.fn = fn
        self.daemon = False

    def start(self):
        self.fn()

    def cancel(self):
        pass


def win(wid, x=2560, y=317, w=1920, h=1050):
    return {"id": wid, "x": x, "y": y, "w": w, "h": h, "onscreen": True}


class KeySpecTests(unittest.TestCase):
    def test_named_and_modifier_keys(self):
        self.assertEqual(ci.key_spec("Enter"), "return")
        self.assertEqual(ci.key_spec("Tab"), "tab")
        self.assertEqual(ci.key_spec("Backspace"), "delete")
        self.assertEqual(ci.key_spec("ArrowDown"), "down")
        self.assertEqual(ci.key_spec("a", ["Meta"]), "cmd+a")
        self.assertEqual(ci.key_spec("Meta+a"), "cmd+a")
        self.assertEqual(ci.key_spec("A"), "shift+a")
        self.assertEqual(ci.key_spec("?"), "shift+slash")
        self.assertEqual(ci.key_spec("Enter", ["Shift", "Control"]), "ctrl+shift+return")
        self.assertEqual(ci.key_spec("+"), "shift+equal")
        with self.assertRaises(ci.NativeInputError):
            ci.key_spec("Delete")
        with self.assertRaises(ci.NativeInputError):
            ci.key_spec("é")

    def test_text_mode_is_deterministic(self):
        self.assertEqual(ci.text_mode("hello world", "auto", True), "keystrokes")
        self.assertEqual(ci.text_mode("hello world", "auto", False), "paste")
        self.assertEqual(ci.text_mode("Héllo 😀", "auto", True), "paste")
        self.assertEqual(ci.text_mode("line1\nline2", "auto", True), "paste")
        self.assertEqual(ci.text_mode("x" * 41, "auto", True), "paste")
        self.assertEqual(ci.text_mode("abc", "paste", True), "paste")
        with self.assertRaises(ci.NativeInputError):
            ci.text_mode("😀", "keystrokes", True)

    def test_window_matching_and_title(self):
        wins = [win(1), win(2, x=0), win(3, w=1919)]
        self.assertEqual([w["id"] for w in ci.match_windows(wins, BOUNDS)], [1, 3])
        self.assertTrue(ci.title_matches("ACU input matrix - Comet", "ACU input matrix"))
        self.assertFalse(ci.title_matches("Orders / Kite by Zerodha - Comet", "ACU input matrix"))
        self.assertFalse(ci.title_matches("anything", ""))

    def test_loose_match_accepts_macos_clamped_size_only(self):
        lease = {"left": 0, "top": 30, "width": 1920, "height": 1050}
        wins = [win(1, x=0, y=30, w=1900, h=1030), win(2, x=234, y=30, w=1686, h=1050), win(3, x=0, y=30, w=1800, h=1050)]
        self.assertEqual(ci.match_windows(wins, lease), [])
        self.assertEqual([w["id"] for w in ci.match_windows_loose(wins, lease)], [1])


class AxPressTargetTests(unittest.TestCase):
    def test_deepest_pressable_element_under_point(self):
        els = [
            {"role": "AXWindow", "element_index": 0, "actions": ["AXRaise"], "frame": {"x": 0, "y": 0, "w": 900, "h": 900}, "depth": 0},
            {"role": "AXGroup", "element_index": 3, "actions": ["AXPress"], "frame": {"x": 10, "y": 10, "w": 500, "h": 500}, "depth": 5},
            {"role": "AXButton", "element_index": 45, "actions": ["AXPress"], "frame": {"x": 29, "y": 81, "w": 139, "h": 46}, "depth": 9},
            {"role": "AXStaticText", "element_index": 46, "actions": [], "frame": {"x": 40, "y": 90, "w": 50, "h": 20}, "depth": 10},
        ]
        self.assertEqual(ci.ax_press_target(els, 98, 104), 45)
        self.assertEqual(ci.ax_press_target(els, 400, 400), 3)
        self.assertIsNone(ci.ax_press_target(els, 800, 800))


class NativeInputTests(unittest.TestCase):
    def make(self, windows, titles, **kw):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rpc = FakeRpc(titles=titles, fail_keys=kw.pop("fail_keys", False))
        shot = Path(self.tmp.name) / "cua.png"
        shot.write_bytes(b"\x89PNG fake")
        self.rpc.shot = shot
        self.desk = FakeDesktop(windows, **kw)
        self.log = Path(self.tmp.name) / "fr" / "input-paths.jsonl"
        return ci.NativeInput(
            browser_pid=lambda: 72359, rpc=self.rpc, desktop=self.desk,
            screenshot_dir=lambda: Path(self.tmp.name) / "shots", log_path=lambda: self.log,
            sleep=lambda _s: None, timer=ImmediateTimer,
        )

    def raise_(self, n, title="ACU input matrix"):
        return n.handle({"op": "raise", "sessionId": "lease-1", "params": {"window": BOUNDS, "title": title}})

    def test_same_bounds_as_humans_window_resolves_by_title_only(self):
        # A full-screen app window shares the full-display bounds with a single-lease window.
        n = self.make([win(16790), win(18599)], {16790: "Orders / Kite by Zerodha - Comet", 18599: "ACU input matrix - Comet"})
        out = self.raise_(n)
        self.assertTrue(out["success"], out)
        self.assertEqual(out["window_id"], 18599)
        raised = [p for m, p in self.rpc.calls if m == "get_app_state" and p.get("raiseForInput")]
        self.assertEqual([p["windowId"] for p in raised], [18599])
        self.assertEqual(out["saved_front"], "com.anysphere.sand")

    def test_raise_waits_for_stage_manager_thumbnail_to_settle(self):
        n = self.make([win(18599, x=-288, y=739, w=217, h=177)], {18599: "ACU input matrix - Comet"})
        calls = {"n": 0}
        real = self.desk.windows
        def settling(pid):
            calls["n"] += 1
            return real(pid) if calls["n"] < 4 else [win(18599)]
        self.desk.windows = settling
        out = self.raise_(n)
        self.assertTrue(out["success"], out)
        self.assertEqual(calls["n"], 5)  # 4 settle polls + 1 post-raise frame read
        settled = win(18599)
        self.assertEqual(out["frame"], {"left": settled["x"], "top": settled["y"], "width": settled["w"], "height": settled["h"]})

    def test_ambiguous_or_mismatched_title_refuses(self):
        n = self.make([win(1), win(2)], {1: "Same - Comet", 2: "Same - Comet"})
        out = self.raise_(n, "Same")
        self.assertEqual(out["error_code"], "WINDOW_AMBIGUOUS")
        n = self.make([win(1)], {1: "Other - Comet"})
        self.assertEqual(self.raise_(n, "Mine")["error_code"], "WINDOW_TITLE_MISMATCH")
        n = self.make([win(1, x=0)], {1: "Mine - Comet"})
        self.assertEqual(self.raise_(n, "Mine")["error_code"], "WINDOW_NOT_FOUND")
        self.assertFalse(any(p.get("raiseForInput") for _m, p in self.rpc.calls))

    def test_real_input_steps_are_refused_in_page_only(self):
        n = self.make([win(18599)], {18599: "ACU input matrix - Comet"})
        self.raise_(n)
        for step in ({"kind": "key", "key": "Tab"}, {"kind": "text", "text": "hi"}, {"kind": "click", "x": 2658, "y": 844}):
            out = n.handle({"op": "steps", "sessionId": "lease-1", "params": {"window_id": 18599, "steps": [step]}})
            self.assertEqual(out["error_code"], "IN_PAGE_ONLY")
        self.assertFalse([m for m, _ in self.rpc.calls if m in {"execute_plan", "press_key", "click", "type_text"}])
        self.assertEqual(n.handle({"op": "raise", "params": {}})["error_code"], "SESSION_REQUIRED")

    def test_release_restores_front_only_while_comet_still_front(self):
        n = self.make([win(18599)], {18599: "ACU input matrix - Comet"})
        self.raise_(n)
        self.desk.front = {"pid": 72359, "bundle_id": ci.COMET_BUNDLE_ID}
        n.handle({"op": "release", "sessionId": "lease-1", "params": {}})
        self.assertEqual(self.desk.activated, [21405])
        n = self.make([win(18599)], {18599: "ACU input matrix - Comet"})
        self.raise_(n)
        self.desk.front = {"pid": 555, "bundle_id": "com.human.app"}
        n.handle({"op": "release", "sessionId": "lease-1", "params": {}})
        self.assertEqual(self.desk.activated, [])

    def test_capture_copies_into_screenshot_dir(self):
        n = self.make([win(18599)], {18599: "ACU input matrix - Comet"})
        out = n.handle({"op": "capture", "sessionId": "lease-1", "params": {"window": BOUNDS, "title": "ACU input matrix", "inline": True}})
        self.assertTrue(out["success"], out)
        self.assertTrue(Path(out["screenshot_path"]).is_file())
        self.assertEqual(out["format"], "png")
        self.assertTrue(out["base64"])
        self.assertFalse(any(p.get("raiseForInput") for _m, p in self.rpc.calls))
        entry = json.loads(self.log.read_text().splitlines()[-1])
        self.assertEqual(entry["op"], "capture")



class RuntimeContractTests(unittest.TestCase):
    def test_request_uses_installed_package_identity_and_caches_it(self):
        identity = {"version": "test", "sha256": "abc"}
        rpc = ci.CuaRpc()
        sock = Mock()
        params = {"app": "Comet", "runtimeContract": {"sha256": "untrusted"}}
        with patch.object(ci.runpy, "run_path", return_value={"package_identity": Mock(return_value=identity)}) as owner, patch.object(ci.socket, "socket", return_value=sock):
            for req_id in (1, 2):
                response = json.dumps({"id": req_id, "result": {"ok": True}}).encode()
                sock.recv.side_effect = [struct.pack("<I", len(response)), response]
                self.assertTrue(rpc.call("get_app_state", params)["ok"])
                wire = sock.sendall.call_args.args[0]
                sent = json.loads(wire[4:])
                self.assertEqual(sent["params"]["runtimeContract"], identity)
            owner.assert_called_once()
        self.assertEqual(params["runtimeContract"], {"sha256": "untrusted"})

    def test_missing_identity_fails_before_connecting(self):
        with patch.object(ci.runpy, "run_path", side_effect=FileNotFoundError), patch.object(ci.socket, "socket") as sock:
            with self.assertRaises(ci.NativeInputError) as caught:
                ci.CuaRpc().call("get_app_state", {})
            self.assertEqual(caught.exception.code, "CUA_CONTRACT")
            sock.assert_not_called()

    def test_service_rejection_is_not_bypassed(self):
        rpc = ci.CuaRpc()
        rpc._contract = {"version": "stale", "sha256": "stale"}
        response = json.dumps({"id": 1, "error": {"code": -32010, "message": "RUNTIME_CONTRACT_FAILED"}}).encode()
        sock = Mock()
        sock.recv.side_effect = [struct.pack("<I", len(response)), response]
        with patch.object(ci.socket, "socket", return_value=sock):
            with self.assertRaises(ci.NativeInputError) as caught:
                rpc.call("get_app_state", {})
            self.assertIn("RUNTIME_CONTRACT_FAILED", str(caught.exception))
            self.assertEqual(sock.sendall.call_count, 1)

if __name__ == "__main__":
    unittest.main()
