# Runtime dependencies are injected by the compatibility facade.
# ruff: noqa: F401, F821
"""Keyboard, scrolling, value setting, and native AX primitives."""
from __future__ import annotations
from collections import deque
import time
def type_text(
    pid,
    window_id,
    element_index,
    text,
    *,
    x=None,
    y=None,
    delivery_mode="background",
    allow_newline=False,
):
    if not allow_newline and ("\n" in text or "\r" in text):
        return {
            "ok": False,
            "accepted": False,
            "error_code": "newline_may_submit",
            "error": (
                "typed newlines may submit a form or send a message; "
                "use set_value for multiline content or explicitly allow the newline"
            ),
        }
    if x is None and y is None:
        native = _native_type_selected_text(
            pid, text, window_id=window_id, element_index=element_index
        )
        if _accepted(native):
            return native
    params = {
        "pid": pid,
        "window_id": window_id,
        "text": text,
        "delivery_mode": delivery_mode,
    }
    if element_index is not None:
        params["element_index"] = element_index
    elif x is not None and y is not None:
        params.update(x=float(x), y=float(y))
    return call_driver("type_text", params)

def _native_type_selected_text(pid, text, *, window_id=None, element_index=None):
    try:
        import ApplicationServices as services
    except ImportError as error:
        return {"error": f"ApplicationServices unavailable: {error}"}
    focused = None
    if element_index is not None:
        state = _native_ax_snapshot(pid, max_elements=max(120, int(element_index) + 1), window_id=window_id)
        target = next(
            (item for item in state.get("elements", []) if item.get("element_index") == element_index),
            None,
        )
        focused = (target or {}).get("_native_element")
    else:
        app = services.AXUIElementCreateApplication(pid)
        focused = _ax_value(
            app,
            getattr(services, "kAXFocusedUIElementAttribute", "AXFocusedUIElement"),
            services,
        )
    if focused is None:
        return {"error": "native focused text element is unavailable"}
    error_code = services.AXUIElementSetAttributeValue(
        focused,
        getattr(services, "kAXSelectedTextAttribute", "AXSelectedText"),
        text,
    )
    if error_code != 0:
        return {"error": f"native selected-text replacement failed with {error_code}"}
    observed = _ax_value(focused, "AXValue", services)
    selected = _ax_value(focused, "AXSelectedText", services)
    if not typed_text_is_proven(text, observed, selected):
        return {
            "ok": False,
            "accepted": False,
            "path": "native_ax_selected_text",
            "error": "ax_incomplete_value",
            "error_code": "ax_incomplete_value",
        }
    return {
        "ok": True,
        "accepted": True,
        "path": "native_ax_selected_text",
        "error_code": 0,
    }


def _verify_or_repair_native_select_all(pid):
    return _native_input().verify_or_repair_native_select_all(pid)


def press_key(pid, window_id, keys, delivery_mode="background"):
    """Normalize a single key or combo before native/driver delivery."""
    aliases = {
        "super": "cmd",
        "command": "cmd",
        "control": "ctrl",
        "alt": "option",
    }
    if isinstance(keys, (list, tuple)):
        if not keys or not all(isinstance(part, str) and part.strip() for part in keys):
            return {"error": "keys must be a non-empty string or list of strings"}
        keys = "+".join(part.strip() for part in keys)
    elif not isinstance(keys, str) or not keys.strip():
        return {"error": "keys must be a non-empty string or list of strings"}
    else:
        keys = keys.strip()
    if delivery_mode == "system_events":
        return _system_events_press_key(pid, keys, aliases=aliases)
    if delivery_mode == "foreground":
        foreground = bring_resolved_window_to_front(pid, window_id)
        if foreground.get("error"):
            return foreground
    if "+" in keys:
        parts = [p.strip() for p in keys.split("+")]
        normalized = [aliases.get(p.lower(), p.lower()) for p in parts]
        if delivery_mode == "background" and normalized == ["cmd", "a"]:
            native = _verify_or_repair_native_select_all(pid)
            if _accepted(native):
                return native
        result = call_driver(
            "hotkey",
            {
                "pid": pid,
                "window_id": window_id,
                "keys": normalized,
                "delivery_mode": delivery_mode,
            },
        )
    else:
        result = call_driver(
            "press_key",
            {
                "pid": pid,
                "window_id": window_id,
                "key": keys.lower(),
                "delivery_mode": delivery_mode,
            },
        )
    if delivery_mode == "background" and (
        result.get("code") == "off_space_or_ax_unresolved"
        or (result.get("escalation") or {}).get("recommended") == "foreground"
    ):
        retried = press_key(pid, window_id, keys, "foreground")
        retried["escalated"] = "foreground"
        return retried
    return result


def _system_events_press_key(pid, keys, *, aliases=None):
    return _native_input().system_events_press_key(pid, keys, aliases=aliases)


KEY_CODES = {
    "a": 0, "s": 1, "d": 2, "w": 13, "space": 49,
    "left": 123, "right": 124, "down": 125, "up": 126,
}


def _post_key_event(pid, key_code, is_down, delivery_mode="background"):
    return _native_input().post_key_event(pid, key_code, is_down, delivery_mode)


def hold_key(pid, key, duration, *, window_id=None, foreground=False):
    return _native_input().hold_key(
        post=_post_key_event,
        prepare_foreground=bring_resolved_window_to_front,
        key_codes=KEY_CODES,
        pid=pid,
        key=key,
        duration=duration,
        window_id=window_id,
        foreground=foreground,
    )


def scroll(
    pid,
    window_id,
    direction="down",
    amount=3,
    *,
    by="line",
    element_index=None,
    x=None,
    y=None,
    delivery_mode="background",
):
    if element_index is not None and by == "page":
        native = _native_ax_snapshot(
            pid, max_elements=max(120, int(element_index) + 1), window_id=window_id
        )
        native_result = _native_input().scroll_page(native, element_index, direction)
        if _accepted(native_result):
            return native_result
    params = {
        "pid": pid,
        "window_id": window_id,
        "direction": direction,
        "amount": amount,
        "by": by,
        "delivery_mode": delivery_mode,
    }
    if element_index is not None:
        params["element_index"] = element_index
    elif x is not None and y is not None:
        params.update(x=float(x), y=float(y))
    result = call_driver("scroll", params)
    if result.get("code") == "px_capture_unavailable" and by == "page":
        return _native_input().page_key_scroll(
            _post_key_event, pid, direction, amount
        )
    return result


def set_value(pid, window_id, element_index, value):
    native = _native_ax_snapshot(
        pid, max_elements=max(120, int(element_index) + 1), window_id=window_id
    )
    if not snapshot_content_error(native):
        result = _native_ax_set_value(native, element_index, value)
        if _accepted(result):
            return result
    return _native_input().set_value_with_readback(
        call_driver=call_driver,
        snapshot=snapshot,
        accepted=_accepted,
        pid=pid,
        window_id=window_id,
        element_index=element_index,
        value=value,
    )


def _selection_range(value, text, *, prefix=None, suffix=None, selection_type="text"):
    return _native_input().selection_range(
        value,
        text,
        prefix=prefix,
        suffix=suffix,
        selection_type=selection_type,
    )


def _ax_set_timeout(element, services, seconds=1.5):
    setter = getattr(services, "AXUIElementSetMessagingTimeout", None)
    if setter is None or element is None:
        return
    try:
        setter(element, float(seconds))
    except (AttributeError, TypeError):
        pass


def _ax_value(element, attribute, services):
    error, value = services.AXUIElementCopyAttributeValue(element, attribute, None)
    return value if error == 0 else None


def _ax_frame(element, services):
    position = _ax_value(element, services.kAXPositionAttribute, services)
    size = _ax_value(element, services.kAXSizeAttribute, services)
    if position is None or size is None:
        return None
    pos_ok, point = services.AXValueGetValue(
        position, services.kAXValueCGPointType, None
    )
    size_ok, dimensions = services.AXValueGetValue(
        size, services.kAXValueCGSizeType, None
    )
    if not pos_ok or not size_ok:
        return None
    return {
        "x": float(point.x),
        "y": float(point.y),
        "w": float(dimensions.width),
        "h": float(dimensions.height),
    }


def _ax_sequence(value):
    """Normalize Python and Cocoa collection proxies without treating text as children."""
    if value is None or isinstance(value, (str, bytes, bytearray)):
        return []
    try:
        return list(value)
    except TypeError:
        return []


def _ax_element_marker(element):
    """Use the underlying Objective-C identity when PyObjC returns new proxies."""
    try:
        import objc

        return int(objc.pyobjc_id(element))
    except (AttributeError, ImportError, TypeError, ValueError):
        return id(element)


def _first_ax_window(app, services, attribute):
    value = _ax_value(app, attribute, services)
    items = _ax_sequence(value)
    if items:
        return items[0]
    if value is not None and not isinstance(value, (str, bytes, bytearray)):
        return value
    return None


def _unique_ax_windows(app, services, pid, window_id=None):
    """One window. Sibling AXWindows scans were the Calculator 16s regression."""
    if window_id is not None:
        target = next(
            (
                item.get("_ax_element")
                for item in _ax_window_candidates(pid, include_element=True)
                if int(item.get("window_id") or 0) == int(window_id)
            ),
            None,
        )
        return None if target is None else [target]
    for attribute in ("AXFocusedWindow", "AXMainWindow", "AXWindows"):
        window = _first_ax_window(app, services, attribute)
        if window is not None:
            return [window]
    return []


def _shallow_active_surfaces(root, services, depth=1):
    found, stack, seen = [], [(root, 0)], set()
    while stack:
        element, level = stack.pop()
        marker = _ax_element_marker(element)
        if marker in seen:
            continue
        seen.add(marker)
        if str(_ax_value(element, "AXRole", services) or "") in ACTIVE_SURFACE_ROLES:
            found.append(element)
            continue
        if level < depth:
            stack.extend(
                (child, level + 1)
                for child in _ax_sequence(_ax_value(element, "AXChildren", services))
            )
    return found


def _native_ax_snapshot(pid, max_elements=120, window_id=None):
    """Read bounded native AX state without letting another app steal focus."""
    telemetry_record_ax()
    try:
        import ApplicationServices as services
    except ImportError as error:
        return {"error": f"ApplicationServices unavailable: {error}", "elements": []}

    app = services.AXUIElementCreateApplication(pid)
    _ax_set_timeout(app, services)
    windows = _unique_ax_windows(app, services, pid, window_id)
    if windows is None:
        return {"error": "resolved AX window is unavailable", "elements": []}
    surfaces = [
        surface
        for window in windows
        for surface in _shallow_active_surfaces(window, services)
    ]
    roots = choose_walk_roots(
        windows,
        surfaces,
        menus=_open_ax_menus(app, services),
    ) or [app]

    elements = []
    stack = deque((root, None) for root in roots)
    seen = set()
    visit_limit = max(400, int(max_elements) * 8)
    while stack and len(seen) < visit_limit and len(elements) < max_elements:
        element, parent_index = stack.popleft()
        marker = _ax_element_marker(element)
        if marker in seen:
            continue
        seen.add(marker)
        role = str(_ax_value(element, "AXRole", services) or "")
        title = _ax_value(element, "AXTitle", services)
        label = str(title or "").strip()
        if not label:
            label = str(_ax_value(element, "AXDescription", services) or "").strip()
        if not label:
            label = str(_ax_value(element, "AXHelp", services) or "").strip()
        value = _ax_value(element, "AXValue", services)
        if not label and role in CLICK_ROLES and isinstance(value, str):
            label = value.strip()
        item = {
            "element_index": len(elements) + 1,
            "role": role,
            "label": label,
            "value": value if isinstance(value, (str, int, float, bool)) else "",
            "parent_index": parent_index,
            "frame": _ax_frame(element, services),
            "actions": [],
            "_native_element": element,
            "_native_services": services,
        }
        elements.append(item)
        children = _ax_sequence(_ax_value(element, "AXChildren", services))
        stack.extend((child, item["element_index"]) for child in children)

    _attach_static_child_text(elements)
    tree = "\n".join(
        f"[{item['element_index']}] {item['role']} {item['label'] or item['value']}".rstrip()
        for item in elements
    )
    return {
        "elements": elements,
        "element_count": len(elements),
        "tree_markdown": tree,
        "source": "native_ax",
    }


def _native_ax_snapshot_after_activation(pid, max_elements=120):
    """Reactivate/reopen the exact PID, then read AX without driver focus theft."""
    activation = _activate_running_identity({"pid": int(pid)})
    latest = _native_ax_snapshot(pid, max_elements=max_elements)
    if not snapshot_content_error(latest):
        if activation.get("error"):
            latest["activation_warning"] = activation["error"]
        return latest
    reopen = _reopen_running_identity(pid)
    for attempt in range(4):
        time.sleep(0.2)
        latest = _native_ax_snapshot(pid, max_elements=max_elements)
        if not snapshot_content_error(latest):
            if activation.get("error"):
                latest["activation_warning"] = activation["error"]
            if reopen.get("error"):
                latest["reopen_warning"] = reopen["error"]
            return latest
    result = dict(snapshot_content_error(latest) or {"error": "native AX unavailable"})
    result["elements"] = (latest or {}).get("elements", [])
    result["tree_markdown"] = (latest or {}).get("tree_markdown", "")
    result["source"] = "native_ax"
    if activation.get("error"):
        result["activation_warning"] = activation["error"]
    if reopen.get("error"):
        result["reopen_warning"] = reopen["error"]
    return result


def _new_ax_window_id(pid, current_window_id):
    candidates = _ax_window_candidates(pid)
    for flag in ("ax_focused", "ax_main"):
        target = next(
            (
                item
                for item in candidates
                if item.get(flag)
                and int(item.get("window_id") or 0) != int(current_window_id)
            ),
            None,
        )
        if target:
            return int(target["window_id"])
    return None


def _native_ax_press(snapshot_data, element_index):
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


def _native_ax_set_value(snapshot_data, element_index, value):
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
    result = services.AXUIElementSetAttributeValue(
        element, services.kAXValueAttribute, value
    )
    actual = _ax_value(element, services.kAXValueAttribute, services)
    if result != 0 or actual != value:
        return {
            "error": f"native AX set_value failed with {result}",
            "actual": actual,
        }
    return {
        "ok": True,
        "path": "native_ax_value",
        "verified": True,
        "element": element_index,
        "error_code": 0,
    }


def _resolve_native_ax_element(pid, snapshot_data, element_index):
    """Map a driver element to the same native AX element by role/value/frame."""
    try:
        import ApplicationServices as services
    except ImportError as error:
        return None, f"ApplicationServices unavailable: {error}"
    target = next(
        (
            element
            for element in snapshot_data.get("elements", [])
            if element.get("element_index") == element_index
        ),
        None,
    )
    if target is None:
        return None, "element is absent from fresh state"
    target_role = target.get("role")
    target_value = str(target.get("value") or "")
    target_frame = target.get("frame") or {}
    root = services.AXUIElementCreateApplication(pid)
    _ax_set_timeout(root, services)
    stack = [root]
    best = None
    best_score = -1
    visited = 0
    while stack and visited < 5000:
        element = stack.pop()
        visited += 1
        role = _ax_value(element, services.kAXRoleAttribute, services)
        if role == target_role:
            value = str(_ax_value(element, services.kAXValueAttribute, services) or "")
            score = 1
            if target_value and value == target_value:
                score += 5
            frame = _ax_frame(element, services)
            if frame and target_frame:
                delta = sum(
                    abs(frame.get(key, 0) - float(target_frame.get(key, 0)))
                    for key in ("x", "y", "w", "h")
                )
                if delta < 4:
                    score += 5
                elif delta < 20:
                    score += 2
            if score > best_score:
                best, best_score = element, score
        children = _ax_value(element, services.kAXChildrenAttribute, services) or ()
        stack.extend(reversed(list(children)))
    if best is None or best_score < 3:
        return None, f"native AX match not found for element {element_index}"
    return (best, services), None


def select_text_action(
    pid,
    snapshot_data,
    element_index,
    text,
    *,
    prefix=None,
    suffix=None,
    selection_type="text",
):
    return _native_input().select_text_action(
        resolve=_resolve_native_ax_element,
        ax_value=_ax_value,
        pid=pid,
        snapshot_data=snapshot_data,
        element_index=element_index,
        text=text,
        prefix=prefix,
        suffix=suffix,
        selection_type=selection_type,
    )
