# Runtime dependencies are injected by the compatibility facade.
# ruff: noqa: F401, F821
"""CUAService-backed observation and input: ONE element-index space.

cua-driver is retired (binary gone, socket dead). ``snap``/``state`` read the
CUAService ``get_app_state`` walk (``includeElements``) and every
index-addressed input (click, set-value, scroll, right-click, type --element)
goes to CUAService, which indexes the same walk. Nothing here falls back to a
different index space or to cua-driver; failures are returned, never hidden.
"""
from __future__ import annotations

import os
import shutil
import struct
import time
from pathlib import Path

CURSOR_NOT_LANDED_CODE = -32010


def _pin_window(params, window_id):
    """Attach caller-pinned windowId so CUAService does not re-resolve focus."""
    if window_id is not None:
        params["windowId"] = int(window_id)
    return params


_INPUT_METHODS = frozenset({
    "click", "type_text", "scroll", "set_value", "drag",
    "perform_secondary_action", "select_text", "press_key", "hold_key",
})


def _cuaservice_error(message, **extra):
    return {"ok": False, "accepted": False, "error": message, "engine": "CUAService", **extra}


def _cuaservice_target(pid):
    """(bundle id, None) for exactly one running instance with ``pid``, else (None, error)."""
    identity = _running_app_identity(f"pid:{int(pid)}")
    if not identity:
        return None, _cuaservice_error(f"no running app with pid {pid}")
    app = identity.get("bundle_id")
    if not app:
        return None, _cuaservice_error(
            f"pid {pid} has no bundle id; CUAService cannot target it exactly"
        )
    # CUAService resolves by bundle id (first match): never let it pick a
    # different instance than the pid the caller resolved.
    twins = _pids_for_app(app)
    if twins != [int(pid)]:
        return None, _cuaservice_error(
            f"{app} has instances {twins}; CUAService cannot target pid {pid} exactly"
        )
    return app, None


def _cuaservice_rpc(pid, method, params=None):
    """One CUAService call for the app owning ``pid``. Errors come back as dicts."""
    app, error = _cuaservice_target(pid)
    if error:
        return error
    payload = {"app": app, **(params or {})}
    if method in _INPUT_METHODS and os.environ.get("MACOS_CUA_ALLOW_UNLANDED") == "1":
        payload["allow_unlanded"] = True  # caller opted in to a blind press
    try:
        client = _cuaservice_client()
    except Exception as exc:  # CUAServiceUnavailable names the recovery command
        return _cuaservice_error(f"CUAService unavailable: {exc}")
    try:
        result = client.call(method, payload, retry=method == "get_app_state")
    except Exception as exc:
        code = getattr(exc, "code", None)
        extra = {"error_code": "CURSOR_NOT_LANDED"} if code == CURSOR_NOT_LANDED_CODE else {}
        return _cuaservice_error(f"CUAService {method} failed: {getattr(exc, 'message', exc)}", **extra)
    finally:
        client.close()
    if not isinstance(result, dict):
        return _cuaservice_error(f"CUAService {method} returned {result!r}")
    return {**result, "app": app, "pid": int(pid), "engine": "CUAService"}


def _png_size(path):
    try:
        with open(path, "rb") as handle:
            head = handle.read(24)
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", head[16:24])
    except OSError:
        pass
    return None, None


def cua_snapshot(pid, window_id=None, max_elements=80, *, include_screenshot=False,
                 screenshot_out_file=None, raise_for_input=False, retries=1, delay=0.2):
    """CUAService AX walk in the legacy snapshot shape (elements + tree_markdown)."""
    last = None
    for attempt in range(max(0, int(retries)) + 1):
        snap_params = {
            "maxElements": int(max_elements),
            "includeElements": True,
            "includeScreenshot": bool(include_screenshot),
            "disableDiff": True,
            "raiseForInput": bool(raise_for_input),
        }
        last = _cuaservice_rpc(pid, "get_app_state", _pin_window(snap_params, window_id))
        if last.get("error") or last.get("elementCount"):
            break
        if attempt < retries:
            time.sleep(delay)
    if last.get("error"):
        return last
    observed_window = last.get("windowId")
    if window_id is not None and observed_window is not None and int(observed_window) != int(window_id):
        # CUAService walks the app's AX-focused window; indices from another
        # window would address the wrong controls.
        return _cuaservice_error(
            f"requested window {window_id} but CUAService observed window {observed_window} "
            "(it walks the AX-focused window); focus the requested window or omit the window id",
            requested_window_id=window_id, window_id=observed_window)
    if window_id is not None and observed_window is None:
        return _cuaservice_error(
            f"requested window {window_id} but CUAService reported no window id; cannot prove the target window",
            requested_window_id=window_id)
    shot = None
    url = ((last.get("screenshot") or {}).get("url") or "")
    if url.startswith("file://"):
        shot = url[len("file://"):]
    if include_screenshot:
        if not shot or not Path(shot).is_file() or Path(shot).stat().st_size == 0:
            return _cuaservice_error("screenshot artifact did not materialize",
                                     capture_path=shot or screenshot_out_file)
        if screenshot_out_file:
            out = os.path.abspath(os.path.expanduser(screenshot_out_file))
            os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
            shutil.copyfile(shot, out)
            shot = out
    width, height = _png_size(shot) if shot else (None, None)
    elements = list(last.get("elements") or [])
    tree = last.get("text") or ""
    if not elements and not tree.strip() and not shot:
        # Empty output is a failure, never an empty-but-ok observation.
        return _cuaservice_error(
            "CUAService returned an empty observation (no AX elements, no tree, no screenshot) "
            f"after {max(0, int(retries)) + 1} attempt(s); retry with screenshot or check Accessibility",
            pid=pid, window_id=last.get("windowId"))
    snap = {
        "ok": True,
        "engine": "CUAService",
        "source": "cuaservice_ax",
        "app": last.get("app"),
        "pid": last.get("pid"),
        "window_id": last.get("windowId"),
        "requested_window_id": window_id,
        "window_title": last.get("windowTitle"),
        "window_frame": last.get("windowFrame"),
        "tree_markdown": tree,
        "elements": elements,
        "element_count": int(last.get("elementCount") or 0),
        "ax_trusted": last.get("axTrusted"),
        "screenshot_file_path": shot,
        "screenshot_width": width,
        "screenshot_height": height,
        "timings_ms": last.get("timings_ms"),
    }
    if "cursor_landing" in last:
        snap["cursor_landing"] = last["cursor_landing"]
    return _enrich_elements(snap)


def cua_app_state(app_name, pid, window_id, *, max_elements=120, query=None,
                  include_screenshot=True, screenshot_out_file=None,
                  prepare_foreground=False):
    """`state`: CUAService AX text + elements (+ window screenshot) as one object."""
    shot_path = None
    if include_screenshot:
        shot_path = (_absolute_output_path(screenshot_out_file) if screenshot_out_file
                     else _default_screenshot_path(app_name))
    raw = cua_snapshot(pid, window_id, max_elements, include_screenshot=include_screenshot,
                       screenshot_out_file=shot_path, raise_for_input=prepare_foreground)
    if raw.get("error"):
        return {"ok": False, "app": app_name, "pid": pid, "window_id": window_id,
                "error": raw["error"], "engine": "CUAService"}
    raw_elements = raw.get("elements") or []
    elements = _state_elements(raw_elements)
    non_menu = {e.get("role") for e in elements
                if e.get("role") and not str(e.get("role")).startswith("AXMenu")}
    captured = raw.get("screenshot_file_path")
    return {
        "ok": bool(raw.get("tree_markdown")) and (not include_screenshot or bool(captured)),
        "engine": "CUAService",
        "app": app_name,
        "pid": raw.get("pid") or pid,
        "window_id": raw.get("window_id"),
        "snapshot_id": None,
        "text": _state_text(app_name, elements, query=query),
        "elements": elements,
        "element_count": len(elements),
        "hidden_element_count": max(0, len(raw_elements) - len(elements)),
        "window_frame": raw.get("window_frame"),
        "screenshot": {
            "path": captured,
            "raw_path": captured,
            "width": raw.get("screenshot_width"),
            "height": raw.get("screenshot_height"),
            "mime_type": "image/png",
        } if captured else None,
        "signals": {
            "ax_available": bool(elements),
            "app_content_available": bool(non_menu),
            "screenshot_available": bool(captured),
        },
        **({"cursor_landing": raw["cursor_landing"]} if "cursor_landing" in raw else {}),
    }


def require_cuaservice_window(pid, window_id, max_elements=80):
    """Refuse when window_id is set but CUAService observes a different window."""
    if window_id is None:
        return None
    snap = cua_snapshot(pid, window_id, int(max_elements), retries=0)
    if snap.get("error"):
        return {"ok": False, "accepted": False, "error": snap["error"],
                "error_code": snap.get("error_code") or "window_mismatch",
                "window_id": window_id}
    return None


def _window_global_point(pid, x, y, window_id=None):
    """Window-local screenshot pixels -> global Quartz points (CUAService frame + scale).

    When window_id is set, refuse if CUAService observed a different window
    (same contract as cua_snapshot) instead of converting against the wrong frame.
    """
    snap = cua_snapshot(pid, window_id, 1, retries=0)
    if snap.get("error"):
        return None, snap
    frame = snap.get("window_frame") or {}
    try:
        fx, fy, fw, fh = (float(frame[k]) for k in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError):
        return None, _cuaservice_error("CUAService reported no window frame for coordinate input")
    scale = _backing_scale(fx + fw / 2, fy + fh / 2)
    gx, gy = fx + float(x) / scale, fy + float(y) / scale
    if not (fx <= gx <= fx + fw and fy <= gy <= fy + fh):
        return None, _cuaservice_error(
            f"point ({x}, {y}) is outside the window ({fw * scale:.0f}x{fh * scale:.0f} px)"
        )
    return {"x": gx, "y": gy}, None


def _backing_scale(qx, qy):
    try:
        from AppKit import NSScreen
        import Quartz
    except ImportError:
        return 1.0
    main_h = float(Quartz.CGDisplayBounds(Quartz.CGMainDisplayID()).size.height)
    for screen in NSScreen.screens():
        f = screen.frame()
        top = main_h - (f.origin.y + f.size.height)
        if f.origin.x <= qx <= f.origin.x + f.size.width and top <= qy <= top + f.size.height:
            return float(screen.backingScaleFactor())
    return 1.0


def cua_click(pid, element_index, window_id=None):
    params = _pin_window({"element_index": int(element_index)}, window_id)
    result = _cuaservice_rpc(pid, "click", params)
    return {**result, "element": int(element_index)}


def cua_right_click(pid, element_index, window_id=None):
    params = _pin_window(
        {"element_index": int(element_index), "action": "AXShowMenu"}, window_id
    )
    result = _cuaservice_rpc(pid, "perform_secondary_action", params)
    return {**result, "element": int(element_index)}


def _same_set_value_target(before, after):
    """Index identity: role, label, and a nearby frame. A rerender that parks
    another field on this index must not read back as a match."""
    if str(before.get("role") or "") != str(after.get("role") or ""):
        return False
    if str(before.get("label") or "") != str(after.get("label") or ""):
        return False
    bf, af = before.get("frame") or {}, after.get("frame") or {}
    try:
        dx = abs(float(bf.get("x") or 0) - float(af.get("x") or 0))
        dy = abs(float(bf.get("y") or 0) - float(af.get("y") or 0))
    except (TypeError, ValueError):
        return False
    return dx <= 12 and dy <= 12


def cua_set_value(pid, element_index, value, window_id=None):
    """AX set through CUAService, then a readback of the same element identity."""
    index = int(element_index)
    before = cua_snapshot(pid, window_id, max(80, index + 1), retries=0)
    if before.get("error") or before.get("ok") is not True:
        return {"ok": False, "accepted": False, "element": index,
                "error": f"set_value readback failed: {before.get('error') or 'snapshot not ok'}"}
    before_target = next((e for e in before.get("elements") or [] if e.get("element_index") == index), None)
    if before_target is None:
        return {"ok": False, "accepted": False, "element": index,
                "error": f"set_value readback target missing: element {index} not in the fresh walk"}
    params = _pin_window({"element_index": index, "value": str(value)}, window_id)
    result = _cuaservice_rpc(pid, "set_value", params)
    if result.get("error") or result.get("ok") is not True:
        return {**result, "element": index}
    after = cua_snapshot(pid, window_id, max(80, index + 1), retries=0)
    if after.get("error") or after.get("ok") is not True:
        return {**result, "ok": False, "accepted": False, "element": index,
                "error": f"set_value readback failed: {after.get('error') or 'snapshot not ok'}"}
    target = next((e for e in after.get("elements") or [] if e.get("element_index") == index), None)
    if target is None:
        return {**result, "ok": False, "accepted": False, "element": index,
                "error": f"set_value readback target missing: element {index} not in the fresh walk"}
    if not _same_set_value_target(before_target, target):
        return {**result, "ok": False, "accepted": False, "element": index,
                "error": "set_value readback identity changed",
                "role": target.get("role"), "label": target.get("label")}
    if target.get("value") is None:
        return {**result, "ok": False, "accepted": False, "element": index, "role": target.get("role"),
                "error": f"set_value readback unavailable: element {index} reports no AX value"}
    observed = str(target.get("value"))
    if not _readback_matches(observed, str(value), target.get("role")):
        return {**result, "ok": False, "accepted": False, "element": index,
                "error": "set_value readback mismatch", "role": target.get("role"),
                "observed": observed[:200]}
    return {**result, "readback": "matched", "element": index}


NUMERIC_ROLES = {"AXSlider", "AXIncrementor", "AXStepper", "AXValueIndicator", "AXLevelIndicator", "AXProgressIndicator"}


def _readback_matches(observed, wanted, role=None):
    if observed == wanted:
        return True
    if role in NUMERIC_ROLES:  # numeric controls report "50.0" for "50"; text stays exact
        try:
            return float(observed) == float(wanted)
        except ValueError:
            return False
    return False


def cua_type_text(pid, text, *, element_index=None, x=None, y=None, window_id=None):
    """Focus the target (element or window-local point) via CUAService click, then type."""
    focus = None
    if element_index is not None:
        focus = cua_click(pid, element_index, window_id=window_id)
    elif x is not None and y is not None:
        point, error = _window_global_point(pid, x, y, window_id=window_id)
        if error:
            return error
        focus = _cuaservice_rpc(pid, "click", _pin_window({"x": point["x"], "y": point["y"]}, window_id))
    if focus is not None and (focus.get("error") or focus.get("ok") is not True):
        return {**focus, "ok": False, "accepted": False,
                "error": focus.get("error") or "focus click before typing failed"}
    params = {"text": text}
    if element_index is not None:
        params["element_index"] = int(element_index)
    result = _cuaservice_rpc(pid, "type_text", _pin_window(params, window_id))
    if focus is not None:
        result = {**result, "focus": {k: focus.get(k) for k in ("method", "cursor_landing") if k in focus}}
    return result


def cua_scroll(pid, direction, amount=3, *, by="line", element_index=None, x=None, y=None, window_id=None):
    if x is not None or y is not None:
        return _cuaservice_error("CUAService scroll targets an element or the window; use --element N")
    pages = max(1, int(amount)) if by == "page" else max(1, round(int(amount) / 5))
    params = {"direction": direction, "pages": pages}
    if element_index is not None:
        params["element_index"] = int(element_index)
    return {**_cuaservice_rpc(pid, "scroll", _pin_window(params, window_id)), "pages": pages}


def cua_drag(pid, from_x, from_y, to_x, to_y, window_id=None):
    source, error = _window_global_point(pid, from_x, from_y, window_id=window_id)
    if error:
        return error
    dest, error = _window_global_point(pid, to_x, to_y, window_id=window_id)
    if error:
        return error
    params = _pin_window(
        {"from_x": source["x"], "from_y": source["y"], "to_x": dest["x"], "to_y": dest["y"]},
        window_id,
    )
    return _cuaservice_rpc(pid, "drag", params)


def cua_list_apps():
    try:
        client = _cuaservice_client()
    except Exception as exc:
        return _cuaservice_error(f"CUAService unavailable: {exc}")
    try:
        apps = client.list_apps()
    except Exception as exc:
        return _cuaservice_error(f"CUAService list_apps failed: {exc}")
    finally:
        client.close()
    if not isinstance(apps, list):
        return _cuaservice_error(f"CUAService list_apps returned {apps!r}")
    return {
        "ok": True,
        "engine": "CUAService",
        "scope": "running regular apps",
        "apps": [
            {"name": a.get("name"), "bundle_id": a.get("bundleId"), "pid": a.get("pid"),
             "running": True, "active": bool(a.get("isActive"))}
            for a in apps
        ],
    }
