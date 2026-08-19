#!/usr/bin/env python3
"""Native input primitives kept separate from macos-cua orchestration."""
from __future__ import annotations

import json
import subprocess
import time

_LAST_MOUSE_CLICK_AT = 0.0


def accessible_slider_drag(
    *, resolve, ax_value, pid, observation, source, destination
):
    """Apply a drag semantically to the accessible slider under its source."""
    candidates = []
    for item in observation.get("elements", []):
        frame = item.get("frame") or {}
        try:
            x = float(frame.get("x"))
            y = float(frame.get("y"))
            width = float(frame.get("w", frame.get("width")))
            height = float(frame.get("h", frame.get("height")))
        except (TypeError, ValueError):
            continue
        if (
            item.get("role") == "AXSlider"
            and x <= source["x"] <= x + width
            and y <= source["y"] <= y + height
        ):
            candidates.append((item, x, width))
    if not candidates:
        return {
            "ok": False,
            "error": "drag target has no accessible slider; raw-HID fallback is disabled",
        }
    item, x, width = min(candidates, key=lambda row: row[2])
    resolved, error = resolve(pid, observation, item["element_index"])
    if error:
        return {"ok": False, "error": error}
    element, services = resolved
    minimum = ax_value(
        element, getattr(services, "kAXMinValueAttribute", "AXMinValue"), services
    )
    maximum = ax_value(
        element, getattr(services, "kAXMaxValueAttribute", "AXMaxValue"), services
    )
    try:
        minimum = float(0 if minimum is None else minimum)
        maximum = float(100 if maximum is None else maximum)
        ratio = min(1, max(0, (float(destination["x"]) - x) / width))
        requested = minimum + ratio * (maximum - minimum)
    except (TypeError, ValueError, ZeroDivisionError) as error:
        return {"ok": False, "error": f"slider range is invalid: {error}"}
    error_code = services.AXUIElementSetAttributeValue(
        element, services.kAXValueAttribute, requested
    )
    actual = ax_value(element, services.kAXValueAttribute, services)
    try:
        verified = error_code == 0 and abs(float(actual) - requested) < 0.01
    except (TypeError, ValueError):
        verified = False
    return {
        "ok": verified,
        "accepted": error_code == 0,
        "verified": verified,
        "effect": "verified" if verified else "unverified",
        "path": "native_ax_slider",
        "element": item["element_index"],
        "requested_value": requested,
        "actual_value": actual,
        "error_code": error_code,
        "system_cursor_moved": False,
    }


def drag_global_point(observation, x, y):
    screenshot = observation.get("screenshot") or {}
    frame = (observation.get("capture_geometry") or {}).get("expected") or {}
    try:
        width = float(screenshot.get("width") or 0)
        height = float(screenshot.get("height") or 0)
        frame_x = float(frame.get("x"))
        frame_y = float(frame.get("y"))
        frame_width = float(frame.get("width"))
        frame_height = float(frame.get("height"))
        point_x = float(x)
        point_y = float(y)
    except (TypeError, ValueError):
        return None
    if (
        width <= 0
        or height <= 0
        or frame_width <= 0
        or frame_height <= 0
        or not (0 <= point_x <= width and 0 <= point_y <= height)
    ):
        return None
    return {
        "x": frame_x + point_x * frame_width / width,
        "y": frame_y + point_y * frame_height / height,
    }


def _ax_value(element, attribute, services):
    error, value = services.AXUIElementCopyAttributeValue(element, attribute, None)
    return value if error == 0 else None


def verify_or_repair_native_select_all(pid):
    """Verify cmd+a on a focused native text control, repairing it through AX."""
    try:
        import ApplicationServices as services
    except ImportError:
        return None
    app = services.AXUIElementCreateApplication(pid)
    focused = _ax_value(
        app,
        getattr(services, "kAXFocusedUIElementAttribute", "AXFocusedUIElement"),
        services,
    )
    if focused is None:
        return None
    value = _ax_value(focused, services.kAXValueAttribute, services)
    role = str(_ax_value(focused, services.kAXRoleAttribute, services) or "")
    if not isinstance(value, str) or role not in {"AXTextArea", "AXTextField"}:
        return None
    expected = (0, len(value))

    def selected_range():
        current = _ax_value(
            focused, services.kAXSelectedTextRangeAttribute, services
        )
        if current is None:
            return None
        ok, raw = services.AXValueGetValue(
            current, services.kAXValueCFRangeType, None
        )
        if not ok:
            return None
        return (
            (int(raw[0]), int(raw[1]))
            if isinstance(raw, tuple)
            else (int(raw.location), int(raw.length))
        )

    actual = selected_range()
    repaired = actual != expected
    error_code = 0
    if repaired:
        range_value = services.AXValueCreate(services.kAXValueCFRangeType, expected)
        error_code = services.AXUIElementSetAttributeValue(
            focused, services.kAXSelectedTextRangeAttribute, range_value
        )
        actual = selected_range()
    return {
        "ok": error_code == 0 and actual == expected,
        "path": "hotkey+native_ax_readback",
        "verified": actual == expected,
        "repaired": repaired,
        "range": {"location": actual[0], "length": actual[1]}
        if actual is not None
        else None,
        "error_code": error_code,
    }


def set_value_with_readback(
    *, call_driver, snapshot, accepted, pid, window_id, element_index, value
):
    maximum = max(120, int(element_index) + 1)
    before = snapshot(
        pid, window_id, max_elements=maximum, retries=1, delay=0.1
    )
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
        "value": value,
    }
    if before.get("snapshot_id"):
        params["snapshot_id"] = before["snapshot_id"]
    elif (target or {}).get("element_token"):
        params["element_token"] = target["element_token"]
        params.pop("element_index", None)
    result = call_driver("set_value", params)
    if not accepted(result) or (target or {}).get("role") not in {
        "AXTextArea",
        "AXTextField",
    }:
        return result
    deadline = time.monotonic() + 2.0
    actual = None
    while time.monotonic() < deadline:
        fresh = snapshot(
            pid, window_id, max_elements=maximum, retries=0, delay=0
        )
        current = next(
            (
                item
                for item in fresh.get("elements", [])
                if item.get("element_index") == element_index
            ),
            None,
        )
        actual = (current or {}).get("value")
        if actual == value:
            return {
                "ok": True,
                "verified": True,
                "path": "driver+ax-value-readback",
                "element": element_index,
                "driver": result,
            }
        time.sleep(0.05)
    return {
        "ok": False,
        "accepted": False,
        "verified": False,
        "error": "set_value AX readback did not match the requested value",
        "element": element_index,
        "actual": actual,
        "driver": result,
    }


def system_events_press_key(pid, keys, *, aliases=None):
    aliases = aliases or {}
    parts = [part.strip() for part in str(keys).split("+") if part.strip()]
    if not parts:
        return {"error": "system-events key is empty"}
    modifier_names = {
        "cmd": "command down",
        "shift": "shift down",
        "option": "option down",
        "ctrl": "control down",
    }
    normalized = [aliases.get(part.lower(), part.lower()) for part in parts]
    key = normalized[-1]
    modifiers = []
    for modifier in normalized[:-1]:
        if modifier not in modifier_names:
            return {"error": f"unsupported System Events modifier: {modifier}"}
        modifiers.append(modifier_names[modifier])
    using = f" using {{{', '.join(modifiers)}}}" if modifiers else ""
    key_codes = {
        "return": 36, "enter": 36, "tab": 48, "space": 49,
        "delete": 51, "backspace": 51, "escape": 53,
        "left": 123, "right": 124, "down": 125, "up": 126,
    }
    if key in key_codes:
        key_line = f"key code {key_codes[key]}{using}"
    elif len(key) == 1:
        key_line = f"keystroke {json.dumps(key)}{using}"
    else:
        return {"error": f"unsupported System Events key: {key}"}
    script = (
        'tell application "System Events"\n'
        f"tell (first process whose unix id is {int(pid)})\n"
        "set frontmost to true\n"
        f"{key_line}\nend tell\nend tell"
    )
    try:
        completed = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return {"error": "System Events key delivery timed out after 5s"}
    if completed.returncode != 0:
        return {"error": (completed.stderr or completed.stdout).strip()}
    return {
        "accepted": True,
        "verified": False,
        "effect": "unverified",
        "path": "system_events_pid",
        "pid": int(pid),
        "keys": str(keys),
    }


def post_key_event(pid, key_code, is_down, delivery_mode="background"):
    from Quartz import (
        CGEventCreateKeyboardEvent,
        CGEventPost,
        CGEventPostToPid,
        kCGHIDEventTap,
    )

    event = CGEventCreateKeyboardEvent(None, key_code, is_down)
    if delivery_mode == "foreground":
        CGEventPost(kCGHIDEventTap, event)
    else:
        CGEventPostToPid(pid, event)


CG_KEY_CODES = {
    "a": 0, "s": 1, "d": 2, "w": 13, "space": 49,
    "return": 36, "enter": 36, "tab": 48,
    "delete": 51, "backspace": 51, "escape": 53, "esc": 53,
    "left": 123, "right": 124, "down": 125, "up": 126,
}


def press_key_event(pid, keys, delivery_mode="background", *, aliases=None):
    """PID/HID key when cua-driver has no live session. Single keys only."""
    aliases = aliases or {}
    key = str(keys or "").strip().lower()
    key = aliases.get(key, key)
    if not key or "+" in key or key not in CG_KEY_CODES:
        return {"error": f"native key unavailable: {keys}"}
    post_key_event(pid, CG_KEY_CODES[key], True, delivery_mode)
    post_key_event(pid, CG_KEY_CODES[key], False, delivery_mode)
    return {
        "ok": True,
        "accepted": True,
        "verified": False,
        "path": "native_cg_key",
        "pid": int(pid),
        "keys": str(keys),
    }


def press_key_after_dropped_session(
    pid, keys, delivery_mode="background", *, aliases=None
):
    """Driver session_ended is not an outcome; deliver the key without cua-driver."""
    if "+" in str(keys or ""):
        result = system_events_press_key(pid, keys, aliases=aliases)
    else:
        result = press_key_event(pid, keys, delivery_mode, aliases=aliases)
    if isinstance(result, dict) and (
        result.get("ok") is True or result.get("accepted") is True
    ):
        return {**result, "session_recovered": True}
    return result


def post_mouse_click(pid, point, *, button="left", count=1, delivery_mode="background"):
    """Post a screen-mapped mouse click to one PID, or HID after an AX-frame glide."""
    global _LAST_MOUSE_CLICK_AT
    from Quartz import (
        CGEventCreateMouseEvent,
        CGEventPost,
        CGEventPostToPid,
        CGEventSetIntegerValueField,
        CGPointMake,
        kCGEventLeftMouseDown,
        kCGEventLeftMouseUp,
        kCGEventOtherMouseDown,
        kCGEventOtherMouseUp,
        kCGEventRightMouseDown,
        kCGEventRightMouseUp,
        kCGHIDEventTap,
        kCGMouseButtonCenter,
        kCGMouseButtonLeft,
        kCGMouseButtonRight,
        kCGMouseEventClickState,
    )

    events = {
        "left": (kCGEventLeftMouseDown, kCGEventLeftMouseUp, kCGMouseButtonLeft),
        "right": (kCGEventRightMouseDown, kCGEventRightMouseUp, kCGMouseButtonRight),
        "middle": (
            kCGEventOtherMouseDown,
            kCGEventOtherMouseUp,
            kCGMouseButtonCenter,
        ),
    }
    if button not in events:
        return {"error": f"unsupported mouse button: {button}"}
    try:
        repetitions = int(count)
        location = CGPointMake(float(point["x"]), float(point["y"]))
    except (KeyError, TypeError, ValueError):
        return {"error": "PID mouse click requires a valid screen point and count"}
    if repetitions < 1 or repetitions > 3:
        return {"error": "PID mouse click count must be between 1 and 3"}

    double_click_interval = 0.5
    try:
        from AppKit import NSEvent

        double_click_interval = float(NSEvent.doubleClickInterval())
    except (AttributeError, ImportError, TypeError, ValueError):
        pass
    remaining = (
        double_click_interval
        + 0.05
        - (time.monotonic() - _LAST_MOUSE_CLICK_AT)
    )
    if remaining > 0:
        time.sleep(remaining)

    down_type, up_type, quartz_button = events[button]
    for click_index in range(1, repetitions + 1):
        for event_index, event_type in enumerate((down_type, up_type)):
            event = CGEventCreateMouseEvent(
                None, event_type, location, quartz_button
            )
            CGEventSetIntegerValueField(
                event, kCGMouseEventClickState, click_index
            )
            if delivery_mode == "foreground":
                CGEventPost(kCGHIDEventTap, event)
            else:
                CGEventPostToPid(int(pid), event)
            if event_index == 0:
                time.sleep(0.01)
        if click_index < repetitions:
            time.sleep(0.05)
    _LAST_MOUSE_CLICK_AT = time.monotonic()
    return {
        "ok": True,
        "accepted": True,
        "verified": False,
        "effect": "unverifiable",
        "path": (
            "native_hid_mouse" if delivery_mode == "foreground" else "native_pid_mouse"
        ),
        "pid": int(pid),
        "button": button,
        "count": repetitions,
    }


def hold_key(
    *, post, prepare_foreground, key_codes, pid, key, duration,
    window_id=None, foreground=False
):
    normalized = key.lower()
    if normalized not in key_codes:
        raise ValueError(
            f"Unsupported hold key {key!r}; choose {', '.join(sorted(key_codes))}"
        )
    if duration < 0.05 or duration > 10.0:
        raise ValueError("duration must be between 0.05 and 10.0 seconds")
    delivery_mode = "foreground_pid" if foreground else "background"
    if foreground:
        if window_id is None:
            raise ValueError("foreground hold-key requires a resolved window")
        prepared = prepare_foreground(pid, window_id)
        if prepared.get("error"):
            return prepared
    key_code = key_codes[normalized]
    post(pid, key_code, True, delivery_mode)
    try:
        time.sleep(duration)
    finally:
        post(pid, key_code, False, delivery_mode)
    return {
        "ok": True,
        "effect": "unverifiable",
        "delivery_mode": delivery_mode,
        "key": normalized,
        "duration_seconds": duration,
        "pid": pid,
    }


def scroll_page(snapshot_data, element_index, direction):
    target = next(
        (
            item
            for item in snapshot_data.get("elements", [])
            if item.get("element_index") == element_index
        ),
        None,
    )
    action = {
        "down": "AXScrollDownByPage",
        "up": "AXScrollUpByPage",
        "left": "AXScrollLeftByPage",
        "right": "AXScrollRightByPage",
    }.get(str(direction).lower())
    if not target:
        return {"error": "native AX page scroll is unavailable"}
    services = target.get("_native_services")
    element = target.get("_native_element")
    if services is None or element is None:
        return {"error": "native AX page scroll is unavailable"}
    actions = target.get("actions", [])
    for _ in range(8):
        if action in actions:
            break
        element = _ax_value(
            element, getattr(services, "kAXParentAttribute", "AXParent"), services
        )
        if element is None:
            break
        error, actions = services.AXUIElementCopyActionNames(element, None)
        actions = list(actions or ()) if error == 0 else []
    else:
        element = None
    if element is None or action not in actions:
        return {"error": "native AX page scroll is unavailable"}
    result = services.AXUIElementPerformAction(element, action)
    if result != 0:
        return {"error": f"{action} failed with {result}"}
    return {"ok": True, "path": "native_ax_scroll", "action": action}


def page_key_scroll(post, pid, direction, amount):
    key_code = {"up": 116, "down": 121}.get(str(direction).lower())
    if key_code is None:
        return {"error": "native page-key scroll supports only up and down"}
    try:
        count = max(1, int(amount))
    except (TypeError, ValueError):
        return {"error": "scroll amount must be an integer"}
    for _ in range(count):
        post(pid, key_code, True, "background")
        post(pid, key_code, False, "background")
    return {"ok": True, "accepted": True, "path": "native_pid_page_key"}


def selection_range(value, text, *, prefix=None, suffix=None, selection_type="text"):
    prefix = prefix or ""
    suffix = suffix or ""
    context_start = value.find(f"{prefix}{text}{suffix}")
    if context_start < 0:
        return None
    start = context_start + len(prefix)
    if selection_type == "cursor_before":
        return start, 0
    if selection_type == "cursor_after":
        return start + len(text), 0
    return start, len(text)


def select_text_action(
    *, resolve, ax_value, pid, snapshot_data, element_index, text,
    prefix=None, suffix=None, selection_type="text"
):
    resolved, error = resolve(pid, snapshot_data, element_index)
    if error:
        return {"ok": False, "error": error, "element": element_index}
    element, services = resolved
    value = str(ax_value(element, services.kAXValueAttribute, services) or "")
    selected = selection_range(
        value, text, prefix=prefix, suffix=suffix, selection_type=selection_type
    )
    if selected is None:
        return {"ok": False, "error": "text/context not found", "element": element_index}
    range_value = services.AXValueCreate(services.kAXValueCFRangeType, selected)
    focus_error = services.AXUIElementSetAttributeValue(
        element, getattr(services, "kAXFocusedAttribute", "AXFocused"), True
    )
    set_error = services.AXUIElementSetAttributeValue(
        element, services.kAXSelectedTextRangeAttribute, range_value
    )
    readback = ax_value(element, services.kAXSelectedTextRangeAttribute, services)
    verified = None
    if readback is not None:
        ok, actual = services.AXValueGetValue(
            readback, services.kAXValueCFRangeType, None
        )
        if ok:
            verified = (
                (int(actual[0]), int(actual[1]))
                if isinstance(actual, tuple)
                else (int(actual.location), int(actual.length))
            )
    return {
        "ok": set_error == 0 and verified == selected,
        "element": element_index,
        "selection_type": selection_type,
        "range": {"location": selected[0], "length": selected[1]},
        "verified_range": {"location": verified[0], "length": verified[1]}
        if verified else None,
        "error_code": set_error,
        "focus_error_code": focus_error,
    }
