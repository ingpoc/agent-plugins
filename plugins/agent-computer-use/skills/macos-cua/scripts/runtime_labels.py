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
import threading
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
    """Resolve the label in the CUAService walk, then CUAService click by index.

    One index space and one landing contract: the press is refused
    (CURSOR_NOT_LANDED) unless the overlay tip was drawn at the target.
    """
    snap = snapshot_data
    foreign_index_space = isinstance(snap, dict) and snap.get("engine") != "CUAService"
    if not (isinstance(snap, dict) and snap.get("engine") == "CUAService"):
        snap = cua_snapshot(pid, window_id, max(int(max_elements), 50))
    if snap.get("error"):
        return {"ok": False, "error": snap["error"], "label": label,
                "error_code": snap.get("error_code") or "observation_failed"}
    # Native/one-based walks are a different index space than CUAService; never
    # reuse those indices after refreshing into the CUAService walk.
    idx = None if foreign_index_space else element_index
    if idx is None:
        try:
            idx = find_clickable_index(snap, label)
        except Exception as exc:
            if exc.__class__.__name__ == "AmbiguousLabelError":
                return {"ok": False, "error": str(exc), "error_code": "ambiguous_label",
                        "matches": getattr(exc, "matches", []), "label": label}
            raise
    if idx is None:
        return {"ok": False, "error": "not found", "error_code": "target_missing", "label": label,
                "element_count": snap.get("element_count")}
    res = cua_click(pid, idx, window_id=window_id)
    err = res.get("error") if isinstance(res, dict) else "CUAService click returned no result"
    ok = not err and res.get("ok") is not False
    payload = {
        "ok": ok,
        "label": label,
        "element": idx,
        "coords": None,
        "result": res,
        "method": "cuaservice-click",
    }
    if isinstance(res, dict) and "cursor_landing" in res:
        payload["cursor_landing"] = res["cursor_landing"]
    if not ok:
        payload["error"] = err or "CUAService click not accepted"
        if isinstance(res, dict) and res.get("error_code"):
            payload["error_code"] = res["error_code"]
    return payload

def click_with_retry(pid, window_id, element_index, max_elements=120, *, app_name=None):
    res = click(pid, window_id, element_index, app_name=app_name)
    if isinstance(res, dict) and any(
        marker in str(res) for marker in ("not found in cache", "AX action failed")
    ):
        snapshot(pid, window_id, max_elements=max_elements)
        res = click(pid, window_id, element_index, app_name=app_name)
    return res

CONTAINER_PRESS_ROLES = {"AXRow", "AXCell", "AXOutline", "AXList", "AXGroup"}


def _ax_press_unsupported(result):
    err = str((result or {}).get("error") or "") if isinstance(result, dict) else ""
    return "-25204" in err or "-25206" in err


def pressable_descendant_index(snapshot_data, element_index):
    elements = snapshot_data.get("elements") or []
    children: dict = {}
    for item in elements:
        children.setdefault(item.get("parent_index"), []).append(item)
    queue = list(children.get(element_index, []))
    seen: set = set()
    pressable_roles = PRIMARY_CLICK_ROLES | {"AXStaticText", "AXDisclosureTriangle"}
    while queue:
        child = queue.pop(0)
        idx = child.get("element_index")
        if idx in seen:
            continue
        seen.add(idx)
        role = child.get("role")
        actions = {
            re.sub(r"[^a-z]", "", str(item).lower()).removeprefix("ax")
            for item in (child.get("actions") or [])
        }
        if role not in CONTAINER_PRESS_ROLES and (
            "press" in actions or role in pressable_roles
        ):
            return idx
        queue.extend(children.get(idx, []))
    return None


def _native_ax_press_once(snapshot_data, element_index):
    target = next(
        (
            element
            for element in snapshot_data.get("elements", [])
            if element.get("element_index") == element_index
        ),
        None,
    )
    if target is None or target.get("_native_element") is None:
        return {"error": f"native AX element {element_index} is unavailable"}
    services = target.get("_native_services")
    element = target["_native_element"]
    _ax_set_timeout(element, services)
    result = services.AXUIElementPerformAction(element, "AXPress")
    if result != 0:
        return {"error": f"AXUIElementPerformAction(AXPress) returned {result}"}
    return {"ok": True, "path": "native_ax", "action": "press", "error_code": 0}


def _native_ax_press(snapshot_data, element_index):
    result = _native_ax_press_once(snapshot_data, element_index)
    if not _ax_press_unsupported(result):
        return result
    descendant = pressable_descendant_index(snapshot_data, element_index)
    if descendant is None or descendant == element_index:
        return result
    retried = _native_ax_press_once(snapshot_data, descendant)
    if retried.get("ok"):
        return {
            **retried,
            "element": descendant,
            "pressed_descendant": True,
            "requested_element": element_index,
        }
    return result


def _native_ax_press_label_with_retry(
    pid, window_id, label, snap, idx, max_elements=120
):
    res = _native_ax_press(snap, idx)
    if _ax_press_unsupported(res):
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
    # Click, select, and delete can rerender. Re-resolve the label before
    # each later fill, and fail if a clearing key was refused (Codex r11).
    time.sleep(0.15)
    snap = snapshot(pid, window_id, max_elements=max_elements)
    idx = find_field_index(snap, field_label)
    if idx is None:
        return {"ok": False, "error": "field not found after focus", "label": field_label, "focus": focus}
    selected = press_key(pid, window_id, "cmd+a")
    if not _accepted(selected):
        return {
            "ok": False,
            "error": "clear selection refused",
            "label": field_label,
            "focus": focus,
            "clear": selected,
        }
    time.sleep(0.08)
    deleted = press_key(pid, window_id, "delete")
    if not _accepted(deleted):
        return {
            "ok": False,
            "error": "clear deletion refused",
            "label": field_label,
            "focus": focus,
            "clear": deleted,
        }
    time.sleep(0.08)
    snap = snapshot(pid, window_id, max_elements=max_elements)
    idx = find_field_index(snap, field_label)
    if idx is None:
        return {"ok": False, "error": "field not found after clear", "label": field_label, "focus": focus}
    res = type_text(
        pid,
        window_id,
        idx,
        text,
        allow_newline=allow_newline,
    )
    if isinstance(res, dict) and "not found in cache" in str(res.get("error", "")):
        snap = snapshot(pid, window_id, max_elements=max_elements)
        idx = find_field_index(snap, field_label)
        if idx is None:
            return {"ok": False, "error": "field not found after clear", "label": field_label, "focus": focus}
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
    """CUAService click by index (tip lands before the press, else refused).

    When window_id is set, refuse if CUAService is observing a different window
    so the same index cannot press a control in the wrong focused window.
    """
    refused = require_cuaservice_window(pid, window_id, max(80, int(element_index) + 1))
    if refused:
        return {**refused, "element": int(element_index)}
    return cua_click(pid, element_index, window_id=window_id)




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
    ax_name = {
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
    refused = require_cuaservice_window(pid, window_id, max(80, int(element_index) + 1))
    if refused:
        return {**refused, "element": int(element_index), "action": normalized}
    return _cuaservice_rpc(
        pid,
        "perform_secondary_action",
        _pin_window({"element_index": int(element_index), "action": ax_name}, window_id),
    )
