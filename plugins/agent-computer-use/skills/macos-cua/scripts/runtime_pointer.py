# Runtime dependencies are injected by the compatibility facade.
# ruff: noqa: F401, F821
"""Visible pointer actions, coordinate proof, drag, and cleanup.

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

def _frame_rect(frame):
    """Normalize AX/Quartz frame key variants to one logical-point rectangle."""
    if not isinstance(frame, dict):
        return None
    try:
        rect = {
            "x": float(frame.get("x", 0)),
            "y": float(frame.get("y", 0)),
            "width": float(frame.get("w", frame.get("width", 0))),
            "height": float(frame.get("h", frame.get("height", 0))),
        }
    except (TypeError, ValueError):
        return None
    return rect if rect["width"] > 0 and rect["height"] > 0 else None


def _glide_container_frame(snap, point=None):
    """Tightest preferred surface containing the target, else largest frame."""
    preferred = {"AXWindow", "AXSheet", "AXPopover", "AXDialog", "AXMenu"}
    containing = []
    first_preferred = None
    best = None
    for element in snap.get("elements") or []:
        rect = _frame_rect(element.get("frame"))
        if rect is None:
            continue
        packed = {
            "x": rect["x"],
            "y": rect["y"],
            "w": rect["width"],
            "h": rect["height"],
        }
        area = rect["width"] * rect["height"]
        if best is None or area > best[0]:
            best = (area, packed)
        if element.get("role") not in preferred:
            continue
        if first_preferred is None:
            first_preferred = packed
        if point and (
            rect["x"] <= point[0] <= rect["x"] + rect["width"]
            and rect["y"] <= point[1] <= rect["y"] + rect["height"]
        ):
            containing.append((area, packed))
    if containing:
        return min(containing, key=lambda item: item[0])[1]
    return first_preferred if first_preferred is not None else (None if best is None else best[1])


def _snap_window_frame(snap):
    """Largest AXWindow rectangle in a snapshot, or None."""
    best = None
    for element in snap.get("elements") or []:
        if element.get("role") != "AXWindow":
            continue
        rect = _frame_rect(element.get("frame"))
        if rect is None:
            continue
        if best is None or rect["width"] * rect["height"] > best["width"] * best["height"]:
            best = rect
    return best


def _live_window_frame(pid, window_id):
    """Cheap live window rectangle. Quartz first; AX only if Quartz misses."""
    quartz, _error = _quartz_window_bounds(pid, window_id)
    return _frame_rect(quartz) or _logical_ax_window_frame(pid, window_id)


def _logical_ax_window_frame(pid, window_id):
    """Return the largest logical AX window frame without foregrounding the app."""
    native = _native_ax_snapshot(pid, max_elements=80, window_id=window_id)
    frames = [
        _frame_rect(element.get("frame"))
        for element in native.get("elements", [])
        if element.get("role") == "AXWindow"
    ]
    frames = [frame for frame in frames if frame]
    return max(frames, key=lambda frame: frame["width"] * frame["height"]) if frames else None


def _frames_materially_differ(first, second, tolerance=2.0):
    if not first or not second:
        return False
    return any(
        abs(float(first[key]) - float(second[key])) > tolerance
        for key in ("x", "y", "width", "height")
    )


def _logical_pixel_target(pid, window_id, x, y):
    """Map screenshot pixels through AX when Stage Manager stale Quartz bounds differ.

    Stage Manager may retain an off-screen WindowServer thumbnail while native AX
    exposes the correctly placed logical window. In that proven mismatch, derive
    the screen point from a fresh screenshot and the logical AX frame, then avoid
    anchoring the click to the stale window bounds.
    """
    quartz, quartz_error = _quartz_window_bounds(pid, window_id)
    logical = _logical_ax_window_frame(pid, window_id)
    quartz = _frame_rect(quartz)
    if not _frames_materially_differ(quartz, logical):
        return None

    screenshot_path = _default_screenshot_path(f"pixel-map-{pid}-{window_id}")
    fresh = snapshot(
        pid,
        window_id,
        max_elements=20,
        include_screenshot=True,
        screenshot_out_file=screenshot_path,
    )
    try:
        width = float(fresh.get("screenshot_width", 0))
        height = float(fresh.get("screenshot_height", 0))
    except (AttributeError, TypeError, ValueError):
        width = height = 0
    if width <= 0 or height <= 0:
        return {
            "error": "cannot map screenshot point through the logical AX frame",
            "reason": "fresh screenshot dimensions are unavailable",
            "quartz_frame": quartz,
            "logical_ax_frame": logical,
            "quartz_error": quartz_error,
        }
    if not (0 <= float(x) <= width and 0 <= float(y) <= height):
        return {
            "error": "click point is outside the fresh window screenshot",
            "point": {"x": float(x), "y": float(y)},
            "screenshot": {"width": width, "height": height},
        }
    return {
        "x": logical["x"] + float(x) * logical["width"] / width,
        "y": logical["y"] + float(y) * logical["height"] / height,
        "screenshot": {"width": width, "height": height},
        "quartz_frame": quartz,
        "logical_ax_frame": logical,
        "quartz_error": quartz_error,
    }


def _move_operator_cursor_to_point(
    app_name, pid, window_id, x, y, *, duration_ms=None, wait_for_sync=True
):
    """Render a coordinate target using the exact fresh operator screenshot."""
    state_path = Path(CACHE_DIR) / "operator-state.json"
    try:
        state = json.loads(state_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError) as error:
        return {"ok": False, "error": f"operator state is unavailable: {error}"}
    if (
        state.get("app") != app_name
        or state.get("pid") != pid
        or state.get("window_id") != window_id
    ):
        return {
            "ok": False,
            "error": "operator proof belongs to a different app, process, or window",
        }
    screenshot_path = state.get("raw_screenshot_path") or state.get("screenshot_path")
    if not screenshot_path or not Path(screenshot_path).is_file():
        return {"ok": False, "error": "fresh operator screenshot is unavailable"}
    max_age = float(os.environ.get("MACOS_CUA_POINT_MAX_AGE", "30"))
    try:
        screenshot_age = max(0.0, time.time() - Path(screenshot_path).stat().st_mtime)
    except OSError as error:
        return {"ok": False, "error": f"cannot stat operator screenshot: {error}"}
    if screenshot_age > max_age:
        return {
            "ok": False,
            "error": "operator screenshot is stale; observe again before point input",
            "screenshot_age_seconds": round(screenshot_age, 3),
            "max_age_seconds": max_age,
        }
    if not state.get("snapshot_id"):
        return {"ok": False, "error": "operator screenshot has no snapshot identity"}
    try:
        from AppKit import NSBitmapImageRep

        image = NSBitmapImageRep.imageRepWithContentsOfFile_(str(screenshot_path))
        width = float(image.pixelsWide()) if image is not None else 0.0
        height = float(image.pixelsHigh()) if image is not None else 0.0
    except (ImportError, AttributeError, TypeError, ValueError) as error:
        return {"ok": False, "error": f"cannot read proof image geometry: {error}"}
    if width <= 0 or height <= 0:
        return {"ok": False, "error": "proof image geometry is invalid"}
    recorded_width = float(state.get("screenshot_width") or 0)
    recorded_height = float(state.get("screenshot_height") or 0)
    if abs(width - recorded_width) > 1 or abs(height - recorded_height) > 1:
        return {
            "ok": False,
            "error": "operator screenshot dimensions do not match its state contract",
        }
    recorded_window_frame = state.get("window_frame") or {}
    recorded_frame = _frame_rect(recorded_window_frame)
    if "ax" in str(recorded_window_frame.get("source", "")):
        current_frame, frame_error = _exact_ax_window_frame(pid, window_id)
    else:
        current_frame, frame_error = _quartz_window_bounds(pid, window_id)
    current_frame = _frame_rect(current_frame)
    if not recorded_frame or not current_frame:
        return {
            "ok": False,
            "error": "current window geometry cannot be matched to point proof",
            "geometry_error": frame_error,
        }
    if _frames_materially_differ(recorded_frame, current_frame):
        return {
            "ok": False,
            "error": "window geometry changed after observation; observe again",
            "observed_frame": recorded_frame,
            "current_frame": current_frame,
        }
    if not (0 <= x <= width and 0 <= y <= height):
        return {
            "ok": False,
            "error": "point is outside the fresh operator screenshot",
            "point": {"x": x, "y": y},
            "screenshot": {"width": width, "height": height},
        }
    normalized = {"x": x / width, "y": y / height}
    screen_x = recorded_frame["x"] + x * recorded_frame["width"] / width
    screen_y = recorded_frame["y"] + y * recorded_frame["height"] / height
    published = operator_update(
        app_name,
        pid,
        window_id,
        status="acting",
        active=True,
        cursor_x=normalized["x"],
        cursor_y=normalized["y"],
        cursor_screen_x=screen_x,
        cursor_screen_y=screen_y,
        cursor_duration_ms=duration_ms or 120,
        cursor_visible=True,
        message="Moving to coordinate target",
    )
    update_id = (published.get("state") or {}).get("cursor_update_id")
    synchronized = None
    if wait_for_sync:
        synchronized = _wait_for_operator_cursor(
            normalized["x"],
            normalized["y"],
            update_id=update_id,
            app_name=app_name,
            pid=pid,
            window_id=window_id,
            timeout=max(1.5, float(duration_ms or 0) / 1000 + 0.5),
        )
    return {
        "ok": bool(published.get("ok"))
        and (synchronized is None or bool(synchronized.get("ok"))),
        "cursor_normalized": normalized,
        "publish": published,
        "sync": synchronized,
    }


def _wait_for_operator_cursor(
    x: float,
    y: float,
    *,
    update_id: str | None,
    app_name: str,
    pid: int,
    window_id: int,
    timeout: float | None = None,
) -> dict:
    """Wait for the signed overlay to acknowledge its final rendered target."""
    if os.environ.get("MACOS_CUA_OPERATOR_UI", "1") == "0":
        return {"ok": True, "disabled": True, "duration_ms": 0}
    started = time.monotonic()
    if not update_id:
        return {
            "ok": False,
            "error": "operator cursor update id is missing",
            "duration_ms": 0,
        }
    timeout = timeout or float(os.environ.get("MACOS_CUA_CURSOR_SYNC_TIMEOUT", "1.5"))
    state_path = Path(CACHE_DIR) / "operator-state.json"
    while time.monotonic() - started < timeout:
        try:
            state = json.loads(state_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            state = {}
        rendered_x = state.get("cursor_rendered_x")
        rendered_y = state.get("cursor_rendered_y")
        if (
            state.get("cursor_update_id") == update_id
            and state.get("cursor_rendered_update_id") == update_id
            and state.get("app") == app_name
            and state.get("pid") == pid
            and state.get("window_id") == window_id
            and isinstance(rendered_x, (int, float))
            and isinstance(rendered_y, (int, float))
            and abs(float(rendered_x) - x) < 0.0001
            and abs(float(rendered_y) - y) < 0.0001
        ):
            return {
                "ok": True,
                "x": float(rendered_x),
                "y": float(rendered_y),
                "update_id": update_id,
                "duration_ms": round((time.monotonic() - started) * 1000),
            }
        time.sleep(0.02)
    return {
        "ok": False,
        "error": "signed operator cursor acknowledgement timed out",
        "target": {"x": x, "y": y},
        "update_id": update_id,
        "duration_ms": round((time.monotonic() - started) * 1000),
    }


def glide_operator_to_element(
    app_name, pid, window_id, snap, idx, *, message="Moving"
):
    """Publish the signed operator cursor to an AX element and wait for render ack.

    Normalize against the snapshot window. Omit cursor_screen_x/y so the
    operator maps onto live bounds. Do not Quartz-read here — that path
    made official Calculator 17.21s after a 5.23s reuse run.
    """
    center = element_center(snap, idx)
    if not center:
        return {"ok": False, "error": "element has no frame for cursor glide"}
    snap_window = _snap_window_frame(snap)
    container = _glide_container_frame(snap, center) or {}
    basis = snap_window or _frame_rect(container)
    width = (basis or {}).get("width") or container.get("w")
    height = (basis or {}).get("height") or container.get("h")
    origin_x = (basis or {}).get("x", container.get("x", 0))
    origin_y = (basis or {}).get("y", container.get("y", 0))
    if not width or not height:
        return {"ok": False, "error": "window frame missing for cursor glide"}
    if not app_name:
        return {"ok": False, "error": "app_name required for visible cursor glide"}
    normalized = {
        "x": min(1.0, max(0.0, (center[0] - origin_x) / width)),
        "y": min(1.0, max(0.0, (center[1] - origin_y) / height)),
    }
    screen_x = screen_y = None
    if snap_window is None:
        screen_x, screen_y = center
    published = operator_update(
        app_name,
        pid,
        window_id,
        status="acting",
        active=True,
        cursor_x=normalized["x"],
        cursor_y=normalized["y"],
        cursor_screen_x=screen_x,
        cursor_screen_y=screen_y,
        cursor_visible=True,
        message=message,
    )
    published_id = (published.get("state") or {}).get("cursor_update_id")
    synchronized = _wait_for_operator_cursor(
        normalized["x"],
        normalized["y"],
        update_id=published_id,
        app_name=app_name,
        pid=pid,
        window_id=window_id,
    )
    recovery = None
    if published.get("ok") and not synchronized.get("ok"):
        recovery = operator_update(
            app_name,
            pid,
            window_id,
            status="acting",
            active=True,
            cursor_x=normalized["x"],
            cursor_y=normalized["y"],
            cursor_screen_x=screen_x,
            cursor_screen_y=screen_y,
            cursor_visible=True,
            message=f"Resynchronizing cursor to {message}",
        )
        if recovery.get("ok"):
            recovery_id = (recovery.get("state") or {}).get("cursor_update_id")
            synchronized = _wait_for_operator_cursor(
                normalized["x"],
                normalized["y"],
                update_id=recovery_id,
                app_name=app_name,
                pid=pid,
                window_id=window_id,
            )
    move = {
        "ok": bool(published.get("ok")) and bool(synchronized.get("ok")),
        "publish": published,
        "sync": synchronized,
        "recovery": recovery,
    }
    return {
        "ok": move["ok"],
        "move": move,
        "cursor_normalized": normalized,
        "coords": {"x": center[0], "y": center[1]},
        "error": None if move["ok"] else "visible operator cursor did not reach the target",
    }


def pointer_preflight(pointer, app_name, pid, window_id, snap, element, message):
    """Glide when pointer is on. None skips; error dict fails; ok dict attaches."""
    if not pointer or not app_name or element is None or snap is None:
        return None
    glide = glide_operator_to_element(
        app_name, pid, window_id, snap, element, message=message
    )
    if not glide.get("ok"):
        return glide
    return {
        "ok": True,
        "move": glide.get("move"),
        "method": "agent-cursor-glide+native-axpress-fallback",
        "cursor_normalized": glide.get("cursor_normalized"),
    }


def merge_pointer_proof(result, preflight):
    if not preflight or not isinstance(result, dict):
        return result
    return {**result, "move": preflight.get("move"), "method": preflight.get("method")}


def right_click(pid, window_id, element_index, *, app_name=None, snapshot_data=None):
    native = snapshot_data or _native_ax_snapshot(
        pid, max_elements=max(120, int(element_index) + 1), window_id=window_id
    )
    pre = pointer_preflight(
        True, app_name, pid, window_id, native, element_index, "Right-click"
    )
    if pre and not pre.get("ok"):
        return pre
    if not snapshot_content_error(native):
        result = perform_action(
            pid, window_id, element_index, "showmenu", snapshot_data=native
        )
        if _accepted(result):
            return merge_pointer_proof(result, pre)
        target = next(
            (
                item
                for item in native.get("elements", [])
                if item.get("element_index") == element_index
            ),
            None,
        )
        frame = (target or {}).get("frame") or {}
        window, _ = _exact_ax_window_frame(pid, window_id)
        if frame and window:
            return merge_pointer_proof(
                call_driver(
                    "right_click",
                    {
                        "pid": pid,
                        "window_id": window_id,
                        "x": float(frame["x"] + frame["w"] / 2 - window["x"]),
                        "y": float(frame["y"] + frame["h"] / 2 - window["y"]),
                        "delivery_mode": "background",
                    },
                ),
                pre,
            )
    return merge_pointer_proof(
        call_driver(
            "right_click",
            {"pid": pid, "window_id": window_id, "element_index": element_index},
        ),
        pre,
    )
