# Runtime dependencies are injected by the compatibility facade.
# ruff: noqa: F401, F821
"""Label-directed interactions and accessibility actions.

Loaded behind the stable macos-cua compatibility facade.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

def click_label_pointer(
    pid,
    window_id,
    label,
    max_elements=50,
    *,
    snapshot_data=None,
    app_name=None,
    prepare_cursor=True,
    element_index=None,
):
    """Glide the signed operator cursor, then AX-click without user-pointer motion."""
    if prepare_cursor:
        # Direct calls own preparation; batched plans do this once up front.
        _cleanup_driver_cursors()
    snap = snapshot_data
    native_fallback = bool(
        isinstance(snap, dict) and snap.get("source") == "native_ax"
    )
    visual_fallback = False
    force_pixel = os.environ.get("MACOS_CUA_PIXEL_CLICK") == "1"
    if snap is None:
        native = _native_ax_snapshot(pid, max_elements=max_elements, window_id=window_id)
        if find_clickable_index(native, label) is not None:
            snap = native
            native_fallback = True
        else:
            snap = snapshot(pid, window_id, max_elements=max_elements, mode="ax")
    idx = element_index
    if idx is None:
        try:
            idx = find_clickable_index(snap, label)
        except Exception as exc:
            if exc.__class__.__name__ == "AmbiguousLabelError":
                return {
                    "ok": False,
                    "error": str(exc),
                    "error_code": "ambiguous_label",
                    "matches": getattr(exc, "matches", []),
                    "label": label,
                }
            raise
    if snapshot_content_error(snap) or idx is None:
        native = _native_ax_snapshot(pid, max_elements=max_elements, window_id=window_id)
        try:
            native_idx = find_clickable_index(native, label)
        except Exception as exc:
            if exc.__class__.__name__ == "AmbiguousLabelError":
                return {
                    "ok": False,
                    "error": str(exc),
                    "error_code": "ambiguous_label",
                    "matches": getattr(exc, "matches", []),
                    "label": label,
                }
            raise
        if native_idx is not None:
            snap = native
            idx = native_idx
            native_fallback = True
    if idx is None and force_pixel:
        visual = _vision_snapshot_after_activation(
            pid,
            window_id,
            max_elements=max_elements,
        )
        try:
            visual_idx = find_visual_index(visual, label)
        except Exception as exc:
            if exc.__class__.__name__ == "AmbiguousLabelError":
                return {
                    "ok": False,
                    "error": str(exc),
                    "error_code": "ambiguous_label",
                    "matches": getattr(exc, "matches", []),
                    "label": label,
                }
            raise
        if visual_idx is not None:
            snap = visual
            idx = visual_idx
            visual_fallback = True
    if idx is None:
        return {
            "ok": False,
            "error": "not found",
            "label": label,
            "snapshot_error": snapshot_content_error(snap),
        }
    center = element_center(snap, idx)
    move = None
    normalized = None
    if app_name:
        glide = glide_operator_to_element(
            app_name,
            pid,
            window_id,
            snap,
            idx,
            message=f"Moving to {label}",
        )
        if not glide.get("ok"):
            return {
                "ok": False,
                "error": glide.get("error")
                or "visible operator cursor did not reach the target",
                "label": label,
                "cursor_normalized": glide.get("cursor_normalized"),
                "move": glide.get("move"),
            }
        move = glide.get("move")
        normalized = glide.get("cursor_normalized")
        coords = glide.get("coords") or {}
        if coords.get("x") is not None and coords.get("y") is not None:
            center = (coords["x"], coords["y"])
    if visual_fallback and center and force_pixel:
        res = click_at_desktop(center[0], center[1])
        method = "agent-cursor-glide+vision-desktop-click-fallback"
    elif visual_fallback:
        return {
            "ok": False,
            "error": (
                "label is vision-only; coordinate clicking is user-interruptive "
                "and requires MACOS_CUA_PIXEL_CLICK=1"
            ),
            "error_code": "pointer_interruption_required",
            "label": label,
            "coords": {"x": center[0], "y": center[1]} if center else None,
        }
    elif native_fallback:
        res, snap, idx = _native_ax_press_label_with_retry(
            pid, window_id, label, snap, idx, max_elements
        )
        method = "agent-cursor-glide+native-axpress-fallback"
    elif force_pixel and center:
        res = click_at_desktop(center[0], center[1])
        method = "agent-cursor-glide+desktop-click"
    else:
        res = click_with_retry(pid, window_id, idx, max_elements)
        method = "agent-cursor-glide+ax-click"
    err = res.get("error") if isinstance(res, dict) else None
    ok = not err
    if not ok and center and force_pixel:
        # Explicit custom-surface escalation only; never silently steal the pointer.
        res = click_at_desktop(center[0], center[1])
        err = res.get("error") if isinstance(res, dict) else None
        ok = not err
        method = "agent-cursor-glide+desktop-click-fallback"
    payload = {
        "ok": ok,
        "label": label,
        "element": idx,
        "coords": {"x": center[0], "y": center[1]} if center else None,
        "cursor_normalized": normalized,
        "move": move,
        "result": res,
        "method": method,
    }
    if err:
        payload["error"] = err
    return payload


def click_with_retry(pid, window_id, element_index, max_elements=120, *, app_name=None):
    res = click(pid, window_id, element_index, app_name=app_name)
    if isinstance(res, dict) and any(
        marker in str(res) for marker in ("not found in cache", "AX action failed")
    ):
        snapshot(pid, window_id, max_elements=max_elements)
        res = click(pid, window_id, element_index, app_name=app_name)
    return res


def _native_ax_press_label_with_retry(
    pid, window_id, label, snap, idx, max_elements=120
):
    res = _native_ax_press(snap, idx)
    if isinstance(res, dict) and "returned -25204" in str(res.get("error", "")):
        previous = next(
            (item for item in snap.get("elements", []) if item.get("element_index") == idx),
            None,
        )
        fresh = _native_ax_snapshot(pid, max_elements=max_elements, window_id=window_id)
        fresh_idx = find_clickable_index(fresh, label)
        if fresh_idx is None and previous:
            old_frame = previous.get("frame") or {}
            match = next(
                (
                    item
                    for item in fresh.get("elements", [])
                    if item.get("role") == previous.get("role")
                    and all(
                        abs(float((item.get("frame") or {}).get(key, -9999)) - float(old_frame.get(key, 9999))) <= 2
                        for key in ("x", "y", "w", "h")
                    )
                ),
                None,
            )
            fresh_idx = (match or {}).get("element_index")
        if fresh_idx is not None:
            snap, idx = fresh, fresh_idx
            res = _native_ax_press(snap, idx)
    return res, snap, idx


def type_label_action(
    pid,
    window_id,
    field_label,
    text,
    max_elements=50,
    *,
    app_name=None,
    allow_newline=False,
):
    snap = snapshot(pid, window_id, max_elements=max_elements)
    idx = find_field_index(snap, field_label)
    if idx is None:
        return {"ok": False, "error": "field not found", "label": field_label}
    focus = click_label_pointer(
        pid,
        window_id,
        field_label,
        max_elements,
        snapshot_data=snap,
        app_name=app_name,
        element_index=idx,
    )
    if not focus.get("ok"):
        return {
            "ok": False,
            "error": "visible agent cursor could not focus the field",
            "label": field_label,
            "focus": focus,
        }
    time.sleep(0.15)
    press_key(pid, window_id, "cmd+a")
    time.sleep(0.08)
    press_key(pid, window_id, "delete")
    time.sleep(0.08)
    res = type_text(
        pid,
        window_id,
        idx,
        text,
        allow_newline=allow_newline,
    )
    if isinstance(res, dict) and "not found in cache" in str(res.get("error", "")):
        snapshot(pid, window_id, max_elements=max_elements)
        res = type_text(
            pid,
            window_id,
            idx,
            text,
            allow_newline=allow_newline,
        )
    fast = os.environ.get("MACOS_CUA_FAST", "1") == "1"
    if fast:
        err = res.get("error") if isinstance(res, dict) else None
        return {
            "ok": not err,
            "label": field_label,
            "element": idx,
            "result": res,
            "focus": focus,
        }
    time.sleep(0.15)
    verify = snapshot(pid, window_id, max_elements=max_elements)
    value = ""
    for e in verify.get("elements", []):
        if e.get("element_index") == idx:
            value = e.get("value") or ""
            break
    ok = text in value
    return {
        "ok": ok,
        "label": field_label,
        "element": idx,
        "value": value,
        "result": res,
        "focus": focus,
    }


def click(pid, window_id, element_index, *, app_name=None):
    maximum = max(120, int(element_index) + 1)
    native_snap = _native_ax_snapshot(pid, max_elements=maximum, window_id=window_id)
    move = None
    normalized = None
    if app_name and not snapshot_content_error(native_snap):
        glide = glide_operator_to_element(
            app_name,
            pid,
            window_id,
            native_snap,
            element_index,
            message=f"Moving to element {element_index}",
        )
        if not glide.get("ok"):
            return {
                "ok": False,
                "accepted": False,
                "error": glide.get("error")
                or "visible operator cursor did not reach the target",
                "element": element_index,
                "cursor_normalized": glide.get("cursor_normalized"),
                "move": glide.get("move"),
            }
        move = glide.get("move")
        normalized = glide.get("cursor_normalized")
    native = _native_ax_press(native_snap, element_index)
    if isinstance(native, dict) and native.get("ok"):
        return {
            **native,
            "accepted": True,
            "element": element_index,
            "cursor_normalized": normalized,
            "move": move,
            "method": (
                "agent-cursor-glide+native-axpress" if move else "native-axpress"
            ),
        }
    before = snapshot(pid, window_id, max_elements=maximum, retries=1, delay=0.1)
    if not isinstance(before, dict) or before.get("error"):
        return {
            "ok": False,
            "accepted": False,
            "error": (before or {}).get("error") or "snapshot failed before click",
            "element": element_index,
        }
    if app_name and move is None:
        glide = glide_operator_to_element(
            app_name,
            pid,
            window_id,
            before,
            element_index,
            message=f"Moving to element {element_index}",
        )
        if not glide.get("ok"):
            return {
                "ok": False,
                "accepted": False,
                "error": glide.get("error")
                or "visible operator cursor did not reach the target",
                "element": element_index,
                "cursor_normalized": glide.get("cursor_normalized"),
                "move": glide.get("move"),
            }
        move = glide.get("move")
        normalized = glide.get("cursor_normalized")
    target = next(
        (
            item
            for item in before.get("elements", [])
            if item.get("element_index") == element_index
        ),
        None,
    )
    params = {
        "pid": pid,
        "window_id": window_id,
        "element_index": element_index,
    }
    if before.get("snapshot_id"):
        params["snapshot_id"] = before["snapshot_id"]
    elif (target or {}).get("element_token"):
        params["element_token"] = target["element_token"]
        params.pop("element_index", None)
    else:
        return {
            "ok": False,
            "accepted": False,
            "error": "click requires snapshot_id or element_token",
            "element": element_index,
        }
    result = call_driver("click", params)
    if not isinstance(result, dict) or move is None:
        return result
    return {
        **result,
        "cursor_normalized": normalized,
        "move": move,
        "method": "agent-cursor-glide+ax-click",
    }


def perform_action(pid, window_id, element_index, action, snapshot_data=None):
    """Perform a secondary AX action advertised by the fresh element state."""
    key = re.sub(r"[^a-z]", "", str(action).lower())
    normalized = {
        "showmenu": "show_menu",
        "press": "press",
        "pick": "pick",
        "confirm": "confirm",
        "cancel": "cancel",
        "open": "open",
        "increment": "increment",
        "decrement": "decrement",
        "raise": "raise",
        "zoomwindow": "zoom_window",
    }.get(key)
    if normalized is None:
        return {"error": f"unsupported AX action: {action}"}
    if snapshot_data is not None:
        target = next(
            (
                element
                for element in snapshot_data.get("elements", [])
                if element.get("element_index") == element_index
            ),
            None,
        )
        advertised = {
            re.sub(r"[^a-z]", "", str(item).lower()).removeprefix("ax")
            for item in (target or {}).get("actions", [])
        }
        if advertised and key not in advertised:
            return {
                "error": f"AX action '{action}' is not advertised by element {element_index}",
                "advertised_actions": sorted((target or {}).get("actions", [])),
            }
        native, error = _resolve_native_ax_element(pid, snapshot_data, element_index)
        if native is not None:
            element, services = native
            action_name = {
                "show_menu": "AXShowMenu",
                "press": "AXPress",
                "pick": "AXPick",
                "confirm": "AXConfirm",
                "cancel": "AXCancel",
                "open": "AXOpen",
                "increment": "AXIncrement",
                "decrement": "AXDecrement",
                "raise": "AXRaise",
                "zoom_window": "AXZoomWindow",
            }[normalized]
            result = services.AXUIElementPerformAction(element, action_name)
            if result == 0:
                return {
                    "ok": True,
                    "element": element_index,
                    "action": normalized,
                    "path": "native_ax",
                    "error_code": 0,
                }
            error = f"AXUIElementPerformAction({action_name}) returned {result}"
        if normalized not in {
            "show_menu",
            "press",
            "pick",
            "confirm",
            "cancel",
            "open",
        }:
            return {"error": error or f"native AX action failed: {normalized}"}
    return call_driver(
        "click",
        {
            "pid": pid,
            "window_id": window_id,
            "element_index": element_index,
            "action": normalized,
        },
    )
