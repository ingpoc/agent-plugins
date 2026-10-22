"""Regression tests for the fix-pending-issues review majors."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

SCRIPTS = Path(__file__).parents[1] / "scripts"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


macos_cua = _load("macos_cua_review_majors", "macos-cua.py")
native_input = _load("macos_cua_review_native_input", "native_input.py")
validator_live = _load("macos_cua_review_validator_live", "validator_live.py")

SLIDER = {"elements": [{"element_index": 4, "role": "AXSlider",
                        "frame": {"x": 100, "y": 200, "w": 300, "h": 30}}]}


def _outline(source):
    return {"source": source, "engine": "CUAService", "elements": [
        {"element_index": 1, "role": "AXOutline"},
        {"element_index": 2, "role": "AXStaticText", "label": "Downloads", "parent_index": 1},
    ]}


class OutlineLabelTests(unittest.TestCase):
    def _native(self, names=(0, ["AXOpen"])):
        row, cell, window = object(), object(), {"role": "AXWindow", "label": "Downloads"}
        services = SimpleNamespace(
            AXUIElementCopyActionNames=mock.Mock(return_value=names),
            AXUIElementPerformAction=mock.Mock(return_value=-25205),
        )
        native = {"source": "native_ax", "elements": [
            {"element_index": 5, "role": "AXRow", "_native_element": row, "_native_services": services},
            {"element_index": 6, "role": "AXCell", "parent_index": 5, "_native_element": cell},
        ]}
        return native, services, {"elements": [window]}

    def test_plan_click_with_cuaservice_walk_fetches_native_walk_and_opens(self):
        native, services, fresh = self._native()
        with (
            mock.patch.object(macos_cua, "_native_ax_snapshot", side_effect=[native, fresh]) as walk,
            mock.patch.object(macos_cua, "find_clickable_index", return_value=5),
            mock.patch.object(macos_cua, "_ax_value", return_value=False),
        ):
            result = macos_cua._open_outline_label(
                _outline("cuaservice_ax"), _outline("cuaservice_ax"), 2, "Downloads", 10, 20)
        self.assertTrue(result["ok"], result)
        self.assertEqual(walk.call_count, 2)
        services.AXUIElementPerformAction.assert_called_once()

    def test_action_name_copy_error_is_reported(self):
        native, services, fresh = self._native(names=(-25202, None))
        with mock.patch.object(macos_cua, "find_clickable_index", return_value=5):
            result = macos_cua._open_outline_label(native, _outline("x"), 2, "Downloads", 10, 20)
        self.assertFalse(result["ok"])
        self.assertIn("-25202", result["error"])
        services.AXUIElementPerformAction.assert_not_called()

    def test_title_match_is_exact(self):
        native, _, _ = self._native()
        fresh = {"elements": [{"role": "AXWindow", "label": "Old Downloads"}]}
        with (
            mock.patch.object(macos_cua, "_native_ax_snapshot", return_value=fresh),
            mock.patch.object(macos_cua, "find_clickable_index", return_value=5),
            mock.patch.object(macos_cua, "_ax_value", return_value=False),
        ):
            result = macos_cua._open_outline_label(native, _outline("x"), 2, "Downloads", 10, 20)
        self.assertFalse(result["ok"], result)


class PageScrollTests(unittest.TestCase):
    def _scroll(self, markers, ax, key=None):
        patches = [
            mock.patch.object(macos_cua, "cua_snapshot", return_value={"elements": []}),
            mock.patch.object(macos_cua, "cua_scroll", return_value={"ok": True, "cursor_landing": {"landed": True}}),
            mock.patch.object(macos_cua, "_visible_marker", side_effect=markers),
            mock.patch.object(macos_cua, "_page_scroll_ax", return_value=ax),
            mock.patch.object(macos_cua, "press_key", return_value=key or {"ok": True}),
            mock.patch.object(macos_cua.time, "sleep"),
            mock.patch.object(macos_cua.time, "monotonic", side_effect=[0, 1] * 10),
        ]
        for p in patches:
            p.start()
        self.addCleanup(mock.patch.stopall)
        return macos_cua.scroll(10, 20, "down", 1, by="page", element_index=3)

    def test_service_scroll_that_moves_stops_there(self):
        result = self._scroll([("range", 0), ("range", 40)], {"ok": True})
        self.assertEqual((result["ok"], result["path"]), (True, "service_scroll"))
        macos_cua._page_scroll_ax.assert_not_called()

    def test_unavailable_ax_falls_back_to_page_key(self):
        result = self._scroll([("range", 0), ("range", 0), ("range", 50)],
                              {"ok": False, "error": "native AX page scroll is unavailable"})
        self.assertEqual((result["ok"], result["path"]), (True, "page-key"))
        self.assertEqual([a["path"] for a in result["attempts"]],
                         ["service_scroll", "native_ax_scroll", "page-key"])

    def test_accepted_ax_that_does_not_move_falls_back_to_page_key(self):
        result = self._scroll([("range", 0), ("range", 0), ("range", 0), ("range", 9)], {"ok": True})
        self.assertEqual(result["path"], "page-key")

    def test_nothing_moved_is_not_ok(self):
        result = self._scroll([("range", 0)] * 4, {"ok": True})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "scroll_no_effect")


class SliderDragTests(unittest.TestCase):
    def _drag(self, destination, slider=False):
        return native_input.accessible_slider_drag(
            resolve=mock.Mock(return_value=(None, "resolved")), ax_value=mock.Mock(), pid=1,
            observation=SLIDER, source={"x": 120, "y": 210}, destination=destination, slider=slider)

    def test_target_off_the_slider_is_not_a_slider_drag(self):
        result = self._drag({"x": 600, "y": 500})
        self.assertFalse(result["ok"])
        self.assertIn("no accessible slider", result["error"])

    def test_target_on_the_same_slider_uses_the_slider(self):
        self.assertEqual(self._drag({"x": 350, "y": 215})["error"], "resolved")

    def test_explicit_slider_request_overrides_the_target_check(self):
        self.assertEqual(self._drag({"x": 600, "y": 500}, slider=True)["error"], "resolved")


class DoctorTests(unittest.TestCase):
    def _run(self, *, health=True, probe=None, signing="identity", app_error=None):
        operator = SimpleNamespace(status=lambda: {
            "ok": True, "pid": 7, "signing": {"mode": signing},
            "service": {"installed": True, "running": True}})
        calls = []
        def resolve(name, **kwargs):
            calls.append(kwargs)
            return (1, 2, name, app_error)
        with mock.patch.object(macos_cua, "cuaservice_health", return_value={"ok": health}), \
             mock.patch.object(macos_cua, "resolve_app", side_effect=resolve), \
             mock.patch.object(macos_cua, "cua_snapshot", return_value=probe or {}), \
             mock.patch.object(macos_cua, "_operator_ui", return_value=operator):
            return macos_cua.doctor(), calls

    def test_all_green(self):
        report, calls = self._run(probe={"ax_trusted": True, "screen_capture": True})
        self.assertTrue(report["ok"], report)
        self.assertEqual(calls, [{"launch_if_missing": False}])  # read-only: never launches

    def test_missing_or_none_permissions_fail(self):
        for probe in ({}, {"ax_trusted": True, "screen_capture": None}, {"ax_trusted": None, "screen_capture": True}):
            report, _ = self._run(probe=probe)
            self.assertEqual(report["failed"], ["permissions"])

    def test_unhealthy_service_skips_probe(self):
        report, calls = self._run(health=False)
        self.assertEqual(report["failed"], ["cuaservice", "permissions"])
        self.assertEqual(calls, [])

    def test_optional_operator_does_not_mask_healthy_service(self):
        report, _ = self._run(probe={"ax_trusted": True, "screen_capture": True}, signing="adhoc")
        self.assertTrue(report["ok"], report)
        self.assertFalse(report["checks"]["operator"]["ok"])
        self.assertFalse(report["checks"]["operator"]["required"])

    def test_validator_gates_on_doctor(self):
        cua = SimpleNamespace(doctor=lambda: {"ok": False, "failed": ["operator"]})
        with self.assertRaises(AssertionError):
            validator_live.doctor_ready(cua)
        self.assertEqual(validator_live.doctor_ready(SimpleNamespace(doctor=lambda: {"ok": True})), {"doctor": "ok"})


if __name__ == "__main__":
    unittest.main()
