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
):
    """Click raw PNG pixels; optionally restore the single system pointer."""
    pointer_move = None
    if app_name:
        pointer_move = _move_operator_cursor_to_point(
            app_name, pid, window_id, float(x), float(y)
        )
        if not pointer_move.get("ok"):
            return {
                "ok": False,
                "error": "visible operator cursor did not reach the point target",
                "move": pointer_move,
            }
    recovery = (
        _logical_pixel_target(pid, window_id, x, y)
        if logical_frame_recovery
        else None
    )
    foreground_prepared = None
    if delivery_mode == "foreground":
        foreground_prepared = bring_resolved_window_to_front(pid, window_id)
        if foreground_prepared.get("error"):
            return {
                "ok": False,
                "error": "foreground preparation failed before point click",
                "detail": foreground_prepared,
            }
        if logical_frame_recovery:
            recovery = _logical_pixel_target(pid, window_id, x, y)
    if recovery and recovery.get("error"):
        return {"ok": False, **recovery}
    if recovery:
        params = {
            "pid": pid,
            "x": recovery["x"],
            "y": recovery["y"],
            "button": button,
            "count": click_count,
            "delivery_mode": "background",
        }
        # No session → cursor-less per driver docs; still wipe if one appears.
        _cleanup_driver_cursors()
        result = _dispatch_coordinate_click(
            params, preserve_pointer=preserve_pointer
        )
        _cleanup_driver_cursors()
        error = result.get("error") if isinstance(result, dict) else None
        return {
            "ok": not error,
            "path": "logical-ax-screen-coordinate",
            "result": result,
            "mapping": recovery,
            "move": pointer_move,
            "foreground_prepared": foreground_prepared,
            "debug_image_omitted": bool(debug_image_out),
        }
    params = {
        "pid": pid,
        "window_id": window_id,
        "x": float(x),
        "y": float(y),
        "button": button,
        "count": click_count,
        "delivery_mode": delivery_mode,
    }
    if debug_image_out:
        params["debug_image_out"] = _absolute_output_path(debug_image_out)
    _cleanup_driver_cursors()
    result = _dispatch_coordinate_click(
        params, preserve_pointer=preserve_pointer
    )
    _cleanup_driver_cursors()
    if isinstance(result, dict) and pointer_move is not None:
        result = {**result, "move": pointer_move}
    return result


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
    point_result = click_point(
        pid,
        window_id,
        x,
        y,
        click_count=2,
        delivery_mode=delivery_mode,
    )
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
    prepared = None
    if delivery_mode == "foreground":
        prepared = bring_resolved_window_to_front(pid, window_id)
        if prepared.get("error"):
            return {
                "ok": False,
                "error": "foreground preparation failed before drag",
                "detail": prepared,
            }
    identity = _running_app_identity(f"pid:{int(pid)}") or {}
    pointer_app = app_name or identity.get("name") or f"pid:{int(pid)}"
    observation = app_state(
        pointer_app,
        pid,
        window_id,
        max_elements=40,
        include_screenshot=True,
        prepare_foreground=True,
        foreground_prepared=prepared is not None,
    )
    if not observation.get("ok"):
        return {
            "ok": False,
            "error": "fresh drag observation failed",
            "detail": observation.get("error"),
        }
    source_global = _native_input().drag_global_point(observation, from_x, from_y)
    destination_global = _native_input().drag_global_point(
        observation, to_x, to_y
    )
    if source_global is None or destination_global is None:
        return {
            "ok": False,
            "error": "drag coordinates cannot be mapped through verified capture geometry",
        }
    source_move = _move_operator_cursor_to_point(
        pointer_app, pid, window_id, float(from_x), float(from_y)
    )
    if not source_move.get("ok"):
        return {
            "ok": False,
            "error": "visible operator cursor did not reach the drag source",
            "move": {"source": source_move},
        }
    destination_move = _move_operator_cursor_to_point(
        pointer_app,
        pid,
        window_id,
        float(to_x),
        float(to_y),
        duration_ms=duration_ms,
        wait_for_sync=False,
    )
    if not destination_move.get("ok"):
        return {
            "ok": False,
            "error": "visible operator cursor did not start the drag trajectory",
            "move": {"source": source_move, "destination": destination_move},
        }
    result = _native_input().accessible_slider_drag(
        resolve=_resolve_native_ax_element,
        ax_value=_ax_value,
        pid=pid,
        observation=observation,
        source=source_global,
        destination=destination_global,
    )
    system_cursor_used = False
    if not _accepted(result):
        result = call_driver(
            "drag",
            {
                "pid": pid,
                "window_id": window_id,
                "from_x": float(from_x),
                "from_y": float(from_y),
                "to_x": float(to_x),
                "to_y": float(to_y),
                "delivery_mode": "foreground",
                "duration_ms": int(duration_ms),
                "steps": int(steps),
            },
        )
        system_cursor_used = True
    normalized = destination_move["cursor_normalized"]
    destination_sync = _wait_for_operator_cursor(
        normalized["x"],
        normalized["y"],
        update_id=(destination_move.get("publish", {}).get("state") or {}).get(
            "cursor_update_id"
        ),
        app_name=pointer_app,
        pid=pid,
        window_id=window_id,
        timeout=max(1.5, float(duration_ms) / 1000 + 0.5),
    )
    if isinstance(result, dict) and not result.get("error"):
        result = dict(result)
        result.setdefault("ok", True)
        result.setdefault("effect", "unverified")
        result["system_cursor_used"] = system_cursor_used
        result["move"] = {
            "source": source_move,
            "destination": {**destination_move, "sync": destination_sync},
        }
        if not destination_sync.get("ok"):
            result["ok"] = False
            result["error"] = "visible operator cursor did not finish the drag trajectory"
        if prepared is not None:
            result["foreground_prepared"] = prepared
    return result


def _system_pointer_position():
    from Quartz import CGEventCreate, CGEventGetLocation

    point = CGEventGetLocation(CGEventCreate(None))
    return float(point.x), float(point.y)


def _restore_system_pointer(position):
    from Quartz import CGPointMake, CGWarpMouseCursorPosition

    return CGWarpMouseCursorPosition(CGPointMake(*position)) == 0


def _dispatch_coordinate_click(params, *, preserve_pointer=False):
    """Dispatch an interruptive CGEvent click, optionally restoring the pointer."""
    before = _system_pointer_position() if preserve_pointer else None
    restored = False
    try:
        result = call_driver("click", params)
    finally:
        if before is not None:
            restored = _restore_system_pointer(before)
    if not isinstance(result, dict):
        result = {"result": result}
    return {
        **result,
        "user_interruptive": True,
        "isolated_pointer": False,
        "pointer_preserve_requested": bool(preserve_pointer),
        "pointer_restored": restored if preserve_pointer else None,
    }


def click_at_desktop(
    x: float,
    y: float,
    *,
    button: str = "left",
    click_count: int = 1,
    preserve_pointer=False,
):
    """Screen-absolute pixel click, sessionless by design.

    NEVER pass the glide session here: the driver would snap that session's
    cursor to these GLOBAL coords while move_cursor uses OVERLAY-LOCAL coords,
    making the pointer sweep across the screen. Sessionless clicks can mint a
    stray cyan `auto-*` driver cursor beside the signed Hermes overlay — wipe
    driver cursors before and after so only the signed operator stays visible.
    """
    _cleanup_driver_cursors()
    res = _dispatch_coordinate_click(
        {
            "x": float(x),
            "y": float(y),
            "scope": "desktop",
            "button": button,
            "count": int(click_count),
        },
        preserve_pointer=preserve_pointer,
    )
    _cleanup_driver_cursors()
    return res


def _cleanup_driver_cursors(*, include_named: bool = False) -> dict:
    """Hide/end cua-driver agent cursors so only the signed Hermes overlay shows.

    Pixel/desktop clicks and interrupted proves leave cyan `auto-*` sessions
    (default arrow, no custom icon). Ending without disable first can leave the
    overlay painted for a frame; always disable, then end.
    """
    state = call_driver("get_agent_cursor_state", {})
    ended: list[str] = []
    for c in state.get("cursors", []) if isinstance(state, dict) else []:
        cid = (c.get("config") or {}).get("cursor_id") or ""
        if not cid:
            continue
        if not include_named and not cid.startswith("auto-"):
            continue
        call_driver(
            "set_agent_cursor_enabled",
            {"enabled": False, "session": cid},
        )
        call_driver("end_session", {"session": cid})
        ended.append(cid)
    # Avoid a redundant driver round-trip when state already proves there is
    # no enabled cursor. A dirty/unknown state still fails safe by disabling.
    global_enabled = state.get("enabled") if isinstance(state, dict) else None
    if ended or global_enabled is not False or include_named:
        call_driver("set_agent_cursor_enabled", {"enabled": False})
    if include_named:
        if CUA_SESSION not in ended:
            call_driver(
                "set_agent_cursor_enabled",
                {"enabled": False, "session": CUA_SESSION},
            )
            call_driver("end_session", {"session": CUA_SESSION})
    return {"ended": ended, "enabled": False}


# Back-compat alias for older callers/tests.
def _cleanup_auto_cursors():
    return _cleanup_driver_cursors()
