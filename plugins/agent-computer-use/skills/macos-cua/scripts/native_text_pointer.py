#!/usr/bin/env python3
"""AX coordinate semantics for accessible text controls."""
from __future__ import annotations


def _frame_contains(frame, point):
    try:
        left = float(frame.get("x"))
        top = float(frame.get("y"))
        width = float(frame.get("w", frame.get("width")))
        height = float(frame.get("h", frame.get("height")))
        x = float(point["x"])
        y = float(point["y"])
    except (KeyError, TypeError, ValueError):
        return False
    return left <= x <= left + width and top <= y <= top + height


def _range_tuple(services, value):
    if value is None:
        return None
    ok, raw = services.AXValueGetValue(
        value, services.kAXValueCFRangeType, None
    )
    if not ok:
        return None
    return (
        (int(raw[0]), int(raw[1]))
        if isinstance(raw, tuple)
        else (int(raw.location), int(raw.length))
    )


def _word_range(text, location):
    location = max(0, min(location, max(0, len(text) - 1)))
    while location < len(text) and not (
        text[location].isalnum() or text[location] == "_"
    ):
        location += 1
    start = location
    end = location
    while start > 0 and (text[start - 1].isalnum() or text[start - 1] == "_"):
        start -= 1
    while end < len(text) and (text[end].isalnum() or text[end] == "_"):
        end += 1
    return start, end - start


def accessible_text_click(
    *, resolve, ax_value, pid, observation, point, click_count=1
):
    """Apply coordinate text selection through AXRangeForPosition with readback."""
    candidates = [
        item
        for item in observation.get("elements", [])
        if item.get("role") in {"AXTextArea", "AXTextField"}
        and _frame_contains(item.get("frame") or {}, point)
    ]
    if not candidates:
        return None
    target = min(
        candidates,
        key=lambda item: float(item["frame"].get("w", item["frame"].get("width")))
        * float(item["frame"].get("h", item["frame"].get("height"))),
    )
    resolved, error = resolve(pid, observation, target["element_index"])
    if error:
        return {"ok": False, "error": error}
    element, services = resolved
    point_value = services.AXValueCreate(
        services.kAXValueCGPointType,
        (float(point["x"]), float(point["y"])),
    )
    error_code, range_value = services.AXUIElementCopyParameterizedAttributeValue(
        element,
        getattr(
            services,
            "kAXRangeForPositionParameterizedAttribute",
            "AXRangeForPosition",
        ),
        point_value,
        None,
    )
    raw_range = _range_tuple(services, range_value)
    if error_code != 0 or raw_range is None:
        return {
            "ok": False,
            "error": f"AXRangeForPosition failed with {error_code}",
        }
    location = raw_range[0]
    text = str(ax_value(element, services.kAXValueAttribute, services) or "")
    if int(click_count) == 1:
        selected = (max(0, min(location, len(text))), 0)
    elif int(click_count) == 2:
        selected = _word_range(text, location)
    else:
        return None
    selected_value = services.AXValueCreate(
        services.kAXValueCFRangeType, selected
    )
    set_error = services.AXUIElementSetAttributeValue(
        element, services.kAXSelectedTextRangeAttribute, selected_value
    )
    readback = ax_value(
        element, services.kAXSelectedTextRangeAttribute, services
    )
    verified = _range_tuple(services, readback)
    return {
        "ok": set_error == 0 and verified == selected,
        "accepted": set_error == 0,
        "verified": verified == selected,
        "effect": "verified" if verified == selected else "unverified",
        "path": "native_ax_range_for_position",
        "element": target["element_index"],
        "range": {"location": selected[0], "length": selected[1]},
        "error_code": set_error,
    }
