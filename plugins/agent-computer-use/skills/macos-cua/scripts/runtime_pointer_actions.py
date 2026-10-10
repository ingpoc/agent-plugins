# Runtime dependencies are injected by the compatibility facade.
# ruff: noqa: F401, F821
"""Targeted pointer actions and driver fallback cleanup."""
from __future__ import annotations

import os
import subprocess
import time


def click_point(
    pid,
    window_id,
    x,
    y,
    *,
    button="left",
    click_count=1,
    delivery_mode="background",
    debug_image_out=None,
    logical_frame_recovery=True,
    preserve_pointer=False,
    app_name=None,
    verified_screen_point=None,
):
    """Window-local PNG pixels (or a verified global point) -> CUAService click.

    Same landing gate as index clicks; the hardware pointer is never moved.
    """
    if button != "left" or int(click_count) != 1:
        return {"ok": False, "error_code": "unsupported_click",
                "error": "CUAService point click is a single left click; use right-click/double-click by element"}
    if isinstance(verified_screen_point, dict):
        point = {"x": float(verified_screen_point["x"]), "y": float(verified_screen_point["y"])}
    else:
        point, error = _window_global_point(pid, x, y)
        if error:
            return {"ok": False, **error}
    result = _cuaservice_rpc(pid, "click", {"x": point["x"], "y": point["y"]})
    err = result.get("error")
    out = {**result, "ok": not err and result.get("ok") is not False, "path": "cuaservice-point",
           "screen_point": point}
    if debug_image_out:
        out["debug_image_omitted"] = True
    return out


def _native_double_click_point(pid, screen_point):
    if not isinstance(screen_point, dict):
        return {"ok": False, "error_code": "unsupported_click",
                "error": "double-click needs an element (verified screen point); raw pixel double-click is retired"}
    result = _native_input().post_mouse_click(pid, screen_point, button="left", count=2)
    error = result.get("error") if isinstance(result, dict) else None
    return {"ok": not error, "path": "verified-screen-point+native-pid-mouse", "result": result}


def double_click(
    pid,
    window_id,
    *,
    element_index=None,
    x=None,
    y=None,
    delivery_mode="background",
    snapshot_data=None,
    app_name=None,
):
    verified_screen_point = None
    if element_index is not None:
        fresh = snapshot_data or snapshot(
            pid,
            window_id,
            max_elements=max(120, int(element_index) + 1),
        )
        element = next(
            (
                item
                for item in fresh.get("elements", [])
                if item.get("element_index") == element_index
            ),
            None,
        )
        window = next(
            (
                element
                for element in fresh.get("elements", [])
                if element.get("role") == "AXWindow" and element.get("frame")
            ),
            None,
        )
        frame = _frame_rect((window or {}).get("frame"))
        element_frame = _frame_rect((element or {}).get("frame"))
        if (element or {}).get("role") == "AXButton":
            foreground_prepared = None
            if delivery_mode == "foreground":
                foreground_prepared = bring_resolved_window_to_front(pid, window_id)
                if foreground_prepared.get("error"):
                    return {
                        "ok": False,
                        "error": "foreground preparation failed before AX double press",
                        "detail": foreground_prepared,
                    }
            presses = []
            for press_index in range(2):
                # Glide once: the second press lands where the cursor already is.
                result = click(
                    pid,
                    window_id,
                    element_index,
                    app_name=app_name if press_index == 0 else None,
                )
                presses.append(result)
                if not _accepted(result):
                    return {
                        "ok": False,
                        "error": "native AX double press was not accepted",
                        "path": "native-ax-double-press",
                        "presses": presses,
                        "move": (presses[0] or {}).get("move"),
                    }
                time.sleep(0.05)
            return {
                "ok": True,
                "path": "native-ax-double-press",
                "method": (
                    "agent-cursor-glide+native-ax-double-press"
                    if (presses[0] or {}).get("move")
                    else "native-ax-double-press"
                ),
                "presses": presses,
                "move": (presses[0] or {}).get("move"),
                "foreground_prepared": foreground_prepared,
            }
        if element_frame is None or frame is None:
            return {
                "ok": False,
                "error": "double-click element has no window-local center",
                "element_index": element_index,
            }
        left = max(frame["x"], element_frame["x"])
        top = max(frame["y"], element_frame["y"])
        right = min(
            frame["x"] + frame["width"],
            element_frame["x"] + element_frame["width"],
        )
        bottom = min(
            frame["y"] + frame["height"],
            element_frame["y"] + element_frame["height"],
        )
        if right <= left or bottom <= top:
            return {
                "ok": False,
                "error": "double-click element is outside the visible window",
                "element_index": element_index,
            }
        x = (left + right) / 2 - frame["x"]
        y = (top + bottom) / 2 - frame["y"]
        verified_screen_point = {"x": frame["x"] + x, "y": frame["y"] + y}
    click_options = {
        "click_count": 2,
        "delivery_mode": delivery_mode,
    }
    if verified_screen_point is not None:
        click_options["verified_screen_point"] = verified_screen_point
    point_result = _native_double_click_point(pid, click_options.get("verified_screen_point"))
    if element_index is None or (element or {}).get("role") not in {
        "AXTextArea",
        "AXTextField",
    }:
        return point_result

    def selected_range():
        resolved, error = _resolve_native_ax_element(pid, fresh, element_index)
        if error:
            return None
        native_element, services = resolved
        value = _ax_value(native_element, "AXSelectedTextRange", services)
        if value is None:
            return None
        ok, raw = services.AXValueGetValue(
            value, services.kAXValueCFRangeType, None
        )
        if not ok:
            return None
        if isinstance(raw, tuple):
            return {"location": int(raw[0]), "length": int(raw[1])}
        return {"location": int(raw.location), "length": int(raw.length)}

    deadline = time.monotonic() + 0.4
    selection = selected_range()
    if selection is None:
        return point_result
    while (not selection or selection["length"] <= 0) and time.monotonic() < deadline:
        time.sleep(0.05)
        selection = selected_range()
    if selection and selection["length"] > 0:
        return {**point_result, "verified": True, "selection": selection}

    _cleanup_driver_cursors()
    fallback = call_driver(
        "click",
        {
            "pid": pid,
            "window_id": window_id,
            "x": float(x),
            "y": float(y),
            "button": "left",
            "count": 2,
            "delivery_mode": "background",
        },
    )
    _cleanup_driver_cursors()
    deadline = time.monotonic() + 0.6
    selection = selected_range()
    while (not selection or selection["length"] <= 0) and time.monotonic() < deadline:
        time.sleep(0.05)
        selection = selected_range()
    verified = bool(selection and selection["length"] > 0)
    return {
        "ok": verified,
        "accepted": _accepted(fallback),
        "verified": verified,
        "path": "native-target-pid+driver-window-local-fallback",
        "selection": selection,
        "native": point_result,
        "driver": fallback,
        "system_cursor_used": fallback.get("system_cursor_used"),
        "error": None if verified else "double-click did not select text",
    }


def drag(
    pid,
    window_id,
    from_x,
    from_y,
    to_x,
    to_y,
    *,
    delivery_mode="background",
    duration_ms=500,
    steps=20,
    app_name=None,
):
    """CUAService drag between window-local screenshot coordinates."""
    return cua_drag(pid, from_x, from_y, to_x, to_y)


def _system_pointer_position():
    from Quartz import CGEventCreate, CGEventGetLocation

    point = CGEventGetLocation(CGEventCreate(None))
    return float(point.x), float(point.y)


def _restore_system_pointer(position):
    from Quartz import CGPointMake, CGWarpMouseCursorPosition

    return CGWarpMouseCursorPosition(CGPointMake(*position)) == 0


def click_at_desktop(
    x: float,
    y: float,
    *,
    button: str = "left",
    click_count: int = 1,
    preserve_pointer=False,
):
    """Desktop-global clicks are retired: no app target, no landing gate."""
    return {
        "ok": False,
        "error_code": "desktop_click_retired",
        "error": "desktop-global click is retired (no target app for the CUAService landing gate); "
                 "use click-point <app> <x> <y> or click by label/element",
        "coords": {"x": float(x), "y": float(y)},
    }


def _cleanup_driver_cursors(*, include_named: bool = False) -> dict:
    """cua-driver is retired, so there are no driver cursors/sessions to end.

    Per-action cleanup is a no-op (no socket round-trip). Closeout
    (include_named) hides the CUAService overlay cursor over its socket.
    """
    if not include_named:
        return {"ended": [], "enabled": False, "engine": "CUAService"}
    client = _cuaservice_client()
    try:
        hidden = client.hide_agent_cursor() or {"ok": True}
    finally:
        client.close()
    return {"ended": [], "enabled": False, "engine": "CUAService", "hidden": hidden}


def _hide_task_cursor() -> dict:
    """Task end: hide the CUAService agent cursor. Never masks the plan result."""
    try:
        hidden = _cleanup_driver_cursors(include_named=True).get("hidden")
        return hidden if isinstance(hidden, dict) else {"ok": True}
    except Exception as exc:  # report, do not raise over the plan outcome
        return {"ok": False, "error": f"cursor hide failed: {exc}"}


# Back-compat alias for older callers/tests.
def _cleanup_auto_cursors():
    return _cleanup_driver_cursors()
