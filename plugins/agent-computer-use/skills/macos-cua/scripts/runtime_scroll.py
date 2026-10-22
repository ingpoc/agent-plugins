# Runtime dependencies are injected by the compatibility facade.
# ruff: noqa: F401, F821
"""Page/line scroll through CUAService with native AX and page-key fallbacks."""
from __future__ import annotations

import json
import sys
import time


def _page_scroll_ax(pid, window_id, element_index, direction, amount, observation=None):
    """Page-scroll the native AX ancestor that advertises the page action."""
    action = {
        "down": "AXScrollDownByPage",
        "up": "AXScrollUpByPage",
        "left": "AXScrollLeftByPage",
        "right": "AXScrollRightByPage",
    }.get(str(direction).lower())
    if action is None:
        return {"ok": False, "error": f"unsupported page direction: {direction}"}
    try:
        count = max(1, int(amount))
    except (TypeError, ValueError):
        return {"ok": False, "error": "scroll amount must be an integer"}
    snap = observation if observation is not None else snapshot(
        pid, window_id, max_elements=max(80, int(element_index) + 1)
    )
    if not isinstance(snap, dict) or snap.get("error"):
        return snap if isinstance(snap, dict) else {"ok": False, "error": "no page-scroll observation"}
    snap["window_id"] = snap.get("window_id") or window_id
    resolved, error = _resolve_native_ax_element(pid, snap, element_index)
    if error:
        return {"ok": False, "error": error}
    element, services = resolved
    for _ in range(count):
        current = element
        names = []
        for _ in range(8):
            copy_error, names = services.AXUIElementCopyActionNames(current, None)
            names = list(names or ()) if copy_error == 0 else []
            if action in names:
                break
            parent = _ax_value(current, "AXParent", services)
            if parent is None:
                current = None
                break
            current = parent
        else:
            current = None
        if current is None or action not in names:
            return {"ok": False, "error": "native AX page scroll is unavailable"}
        result = services.AXUIElementPerformAction(current, action)
        if result != 0:
            return {"ok": False, "error": f"{action} failed with {result}"}
    return {"ok": True, "accepted": True, "path": "native_ax_scroll", "action": action}


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
    if x is not None or y is not None:
        return {"ok": False, "error": "CUAService scroll targets an element or the window; use --element N",
                "engine": "CUAService"}
    bound = max(80, int(element_index) + 1) if element_index is not None else 80
    snap = None
    if window_id is not None:
        snap = cua_snapshot(pid, window_id, bound, retries=0)
        if snap.get("error"):
            return {"ok": False, "accepted": False, "error": snap["error"],
                    "error_code": snap.get("error_code") or "window_mismatch",
                    "window_id": window_id, "engine": "CUAService"}
    page = by == "page" and element_index is not None and snap is not None
    before = _visible_marker(pid, window_id, element_index, snap) if page else None
    gestured = cua_scroll(
        pid, direction, amount, by=by, element_index=element_index, x=x, y=y,
        window_id=window_id,
    )
    if not page:
        return gestured
    if gestured.get("ok") is False or gestured.get("error"):
        return gestured
    landing = gestured.get("cursor_landing")
    measurable = before is not None
    attempts = []

    def moved():
        if not measurable:
            return None
        deadline = time.monotonic() + 0.6
        while True:
            now = _visible_marker(pid, window_id, element_index, snap)
            if now is not None and now != before:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def done(result, path):
        out = {**result, "ok": True, "path": path, "attempts": attempts,
               "moved": attempts[-1]["moved"], "cursor_landing": landing}
        _log_scroll_path(out)
        return out

    # The wheel RPC is accepted even when a text view ignores it, so each path
    # is judged by the visible range actually moving.
    attempts.append({"path": "service_scroll", "ok": True, "moved": moved()})
    if attempts[-1]["moved"]:
        return done(gestured, "service_scroll")
    ax = _page_scroll_ax(pid, window_id, element_index, direction, amount, observation=snap)
    attempts.append({"path": "native_ax_scroll", "ok": bool(ax.get("ok")),
                     "error": ax.get("error"), "moved": moved() if ax.get("ok") else False})
    if ax.get("ok") and (attempts[-1]["moved"] or not measurable):
        return done(ax, "native_ax_scroll")
    key = {"down": "pagedown", "up": "pageup"}.get(str(direction).lower())
    if key:
        pressed = press_key(pid, window_id, key, "background")
        key_ok = pressed.get("ok") is not False and not pressed.get("error")
        attempts.append({"path": "page-key", "ok": key_ok, "error": pressed.get("error"),
                         "moved": moved() if key_ok else False})
        if key_ok and (attempts[-1]["moved"] or not measurable):
            return done(pressed, "page-key")
    if not measurable:
        return done(gestured, "service_scroll")
    out = {"ok": False, "accepted": True, "error": "page scroll did not move the visible range",
           "error_code": "scroll_no_effect", "path": None, "attempts": attempts,
           "moved": False, "cursor_landing": landing, "engine": "CUAService"}
    _log_scroll_path(out)
    return out


def _visible_marker(pid, window_id, element_index, snap):
    """Visible character range (or vertical scroll bar value) of the target, or None."""
    try:
        snap = {**snap, "window_id": snap.get("window_id") or window_id}
        resolved, error = _resolve_native_ax_element(pid, snap, element_index)
        if error:
            return None
        element, services = resolved
        raw = _ax_value(element, getattr(services, "kAXVisibleCharacterRangeAttribute",
                                         "AXVisibleCharacterRange"), services)
        if raw is not None:
            ok, value = services.AXValueGetValue(raw, services.kAXValueCFRangeType, None)
            if ok:
                return ("range", int(value[0] if isinstance(value, tuple) else value.location))
        current = element
        for _ in range(8):
            bar = _ax_value(current, "AXVerticalScrollBar", services)
            if bar is not None:
                value = _ax_value(bar, "AXValue", services)
                return None if value is None else ("bar", round(float(value), 4))
            current = _ax_value(current, "AXParent", services)
            if current is None:
                break
    except Exception:
        return None
    return None


def _log_scroll_path(result):
    print(json.dumps({"scroll_path": result.get("path"), "moved": result.get("moved"),
                      "attempts": result.get("attempts")}, default=str), file=sys.stderr)
