#!/usr/bin/env python3
"""macOS display detection and window placement. `MACOS_CUA_DISPLAY` pins."""

from __future__ import annotations

import os
import re
import subprocess
import time


def resolve_display_token() -> str:
    """MACOS_CUA_DISPLAY override, else the first secondary/main display."""
    env = (os.environ.get("MACOS_CUA_DISPLAY") or "").strip()
    if env:
        return env
    displays = list_displays()
    target = next((display for display in displays if not display.get("main")), None)
    target = target or (displays[0] if displays else None)
    return str((target or {}).get("name") or "")


def list_displays() -> list[dict]:
    """Active NSScreens with Cocoa global frame (bottom-left origin)."""
    from AppKit import NSScreen

    main = NSScreen.mainScreen()
    out = []
    for screen in NSScreen.screens():
        frame = screen.frame()
        desc = screen.deviceDescription() or {}
        out.append({
            "id": int(desc.get("NSScreenNumber") or 0),
            "name": str(screen.localizedName()),
            "x": int(frame.origin.x),
            "y": int(frame.origin.y),
            "width": int(frame.size.width),
            "height": int(frame.size.height),
            "scale": float(screen.backingScaleFactor()),
            "main": bool(screen == main),
        })
    return out


def _cg_display_count(*, online: bool) -> int:
    from Quartz import CGGetActiveDisplayList, CGGetOnlineDisplayList

    err, _ids, count = (CGGetOnlineDisplayList if online else CGGetActiveDisplayList)(
        16, None, None
    )
    return int(count or 0) if err == 0 else 0


def display_packet(window_bounds=None) -> dict:
    """Active=awake, configured=CG online. Asleep screens are not move targets."""
    active = list_displays()
    packet = {
        "display_count_active": len(active),
        "display_count_configured": max(_cg_display_count(online=True), len(active)),
        "displays": [
            {
                "id": item.get("id"), "name": item["name"], "main": bool(item.get("main")),
                "x": item["x"], "y": item["y"], "w": item["width"], "h": item["height"],
                "scale": item.get("scale") or 1,
            }
            for item in active
        ],
    }
    if window_bounds:
        hit = display_for_point(
            float(window_bounds.get("x", 0)) + float(window_bounds.get("width", 0)) / 2,
            float(window_bounds.get("y", 0)) + float(window_bounds.get("height", 0)) / 2,
        )
        if hit:
            packet["target_window_display"] = {
                "id": hit.get("id"), "name": hit["name"], "main": bool(hit.get("main")),
            }
    pin = (os.environ.get("MACOS_CUA_DISPLAY") or "").strip().lower()
    if pin and not any(pin in str(d.get("name") or "").lower() for d in packet["displays"]):
        packet["pin_error"] = "pinned display is not active"
    elif pin:
        target = str((packet.get("target_window_display") or {}).get("name") or "")
        if target and pin not in target.lower():
            packet["pin_error"] = "window is not on the pinned display"
    return packet


def find_display(name_substring: str) -> dict | None:
    needle = name_substring.lower()
    for d in list_displays():
        if needle in d["name"].lower():
            return d
    return None


def display_for_point(x: float, y: float) -> dict | None:
    """Quartz top-left x; Cocoa y-band, else x-band only."""
    max_top = max(d["y"] + d["height"] for d in list_displays())
    for d in list_displays():
        if d["x"] <= x < d["x"] + d["width"] and d["y"] <= (max_top - y) < d["y"] + d["height"]:
            return d
    for d in list_displays():
        if d["x"] <= x < d["x"] + d["width"]:
            return d
    return None


def window_bounds_for_process(process_name: str) -> dict | None:
    """Largest on-screen window for process via Quartz."""
    try:
        from Quartz import (
            CGWindowListCopyWindowInfo,
            kCGNullWindowID,
            kCGWindowListOptionOnScreenOnly,
        )
    except ImportError:
        return None
    needle = process_name.lower()
    best = None
    best_area = 0
    for w in CGWindowListCopyWindowInfo(
        kCGWindowListOptionOnScreenOnly, kCGNullWindowID
    ):
        owner = (w.get("kCGWindowOwnerName") or "").lower()
        if needle not in owner and owner not in needle:
            continue
        b = w.get("kCGWindowBounds") or {}
        area = int(b.get("Width", 0) or 0) * int(b.get("Height", 0) or 0)
        if area > best_area:
            best_area = area
            best = {
                "x": int(b.get("X", 0) or 0),
                "y": int(b.get("Y", 0) or 0),
                "width": int(b.get("Width", 0) or 0),
                "height": int(b.get("Height", 0) or 0),
                "owner": w.get("kCGWindowOwnerName"),
            }
    return best


def window_on_display(process_name: str) -> dict | None:
    b = window_bounds_for_process(process_name)
    if not b:
        return None
    cx = b["x"] + b["width"] / 2
    cy = b["y"] + b["height"] / 2
    disp = display_for_point(cx, cy)
    return {"bounds": b, "display": disp, "center": (cx, cy)}


def _native_ax_window_candidates(process_name: str) -> list[dict]:
    """Return native AX windows even when System Events reports zero windows."""
    from AppKit import NSWorkspace
    from ApplicationServices import (
        AXUIElementCopyAttributeValue,
        AXUIElementCreateApplication,
        AXValueGetValue,
        kAXPositionAttribute,
        kAXSizeAttribute,
        kAXValueCGPointType,
        kAXValueCGSizeType,
        kAXWindowsAttribute,
    )

    needle = process_name.lower()
    candidates = []
    for application in NSWorkspace.sharedWorkspace().runningApplications():
        names = {str(application.localizedName() or "").lower()}
        executable_url = application.executableURL()
        if executable_url is not None:
            names.add(str(executable_url.lastPathComponent()).lower())
        if needle not in names:
            continue
        pid = int(application.processIdentifier())
        app_element = AXUIElementCreateApplication(pid)
        error, windows = AXUIElementCopyAttributeValue(
            app_element, kAXWindowsAttribute, None
        )
        if error != 0 or not windows:
            continue
        for window in windows:
            position_error, position = AXUIElementCopyAttributeValue(
                window, kAXPositionAttribute, None
            )
            size_error, size = AXUIElementCopyAttributeValue(
                window, kAXSizeAttribute, None
            )
            if position_error != 0 or size_error != 0:
                continue
            position_ok, point = AXValueGetValue(
                position, kAXValueCGPointType, None
            )
            size_ok, dimensions = AXValueGetValue(
                size, kAXValueCGSizeType, None
            )
            if not position_ok or not size_ok:
                continue
            bounds = {
                "x": int(round(point.x)),
                "y": int(round(point.y)),
                "width": int(round(dimensions.width)),
                "height": int(round(dimensions.height)),
                "owner": process_name,
            }
            candidates.append(
                {
                    "pid": pid,
                    "element": window,
                    "bounds": bounds,
                    "area": bounds["width"] * bounds["height"],
                }
            )
    return sorted(candidates, key=lambda item: item["area"], reverse=True)


def native_ax_window_bounds_for_process(process_name: str) -> dict | None:
    candidates = _native_ax_window_candidates(process_name)
    return candidates[0]["bounds"] if candidates else None


def set_native_ax_window_frame(
    process_name: str, *, x: int, y: int, width: int, height: int
) -> dict:
    """Move the largest AX window without activation or physical-pointer input."""
    from ApplicationServices import (
        AXUIElementSetAttributeValue,
        AXValueCreate,
        kAXPositionAttribute,
        kAXSizeAttribute,
        kAXValueCGPointType,
        kAXValueCGSizeType,
    )
    from Quartz import CGPoint, CGSize

    candidates = _native_ax_window_candidates(process_name)
    if not candidates:
        return {"ok": False, "method": "native-ax", "error": "no AX window"}
    candidate = candidates[0]
    position = AXValueCreate(kAXValueCGPointType, CGPoint(x, y))
    size = AXValueCreate(kAXValueCGSizeType, CGSize(width, height))
    size_error = AXUIElementSetAttributeValue(
        candidate["element"], kAXSizeAttribute, size
    )
    position_error = AXUIElementSetAttributeValue(
        candidate["element"], kAXPositionAttribute, position
    )
    return {
        "ok": size_error == 0 and position_error == 0,
        "method": "native-ax",
        "pid": candidate["pid"],
        "size_error": int(size_error),
        "position_error": int(position_error),
    }


def logical_window_bounds_for_process(process_name: str) -> dict | None:
    """Largest logical AX window frame, unaffected by Stage Manager thumbnails."""
    script = (
        'tell application "System Events"\n'
        f'  if not (exists process "{process_name}") then return "NO_PROC"\n'
        f'  tell process "{process_name}"\n'
        '    if (count of windows) is 0 then return "NO_WIN"\n'
        "    set targetWindow to window 1\n"
        "    set maxArea to 0\n"
        "    repeat with candidateWindow in windows\n"
        "      set candidateSize to size of candidateWindow\n"
        "      set candidateArea to (item 1 of candidateSize) * (item 2 of candidateSize)\n"
        "      if candidateArea > maxArea then\n"
        "        set maxArea to candidateArea\n"
        "        set targetWindow to candidateWindow\n"
        "      end if\n"
        "    end repeat\n"
        "    set targetPosition to position of targetWindow\n"
        "    set targetSize to size of targetWindow\n"
        '    return "FRAME," & (item 1 of targetPosition) & "," & '
        '(item 2 of targetPosition) & "," & (item 1 of targetSize) & "," & '
        "(item 2 of targetSize)\n"
        "  end tell\n"
        "end tell\n"
    )
    proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    values = re.findall(r"-?\d+(?:\.\d+)?", proc.stdout or "")
    if proc.returncode != 0 or len(values) != 4:
        return native_ax_window_bounds_for_process(process_name)
    x, y, width, height = (int(float(value)) for value in values)
    return {
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "owner": process_name,
    }


def logical_window_on_display(process_name: str) -> dict | None:
    bounds = logical_window_bounds_for_process(process_name)
    if not bounds:
        return None
    center = (
        bounds["x"] + bounds["width"] / 2,
        bounds["y"] + bounds["height"] / 2,
    )
    return {
        "bounds": bounds,
        "display": display_for_point(*center),
        "center": center,
    }


def virtual_screen_quartz() -> dict:
    """Quartz top-left bounds covering all displays (global cursor coordinate space)."""
    displays = list_displays()
    if not displays:
        return {"x": 0, "y": 0, "width": 0, "height": 0}
    min_x = min(d["x"] for d in displays)
    max_x = max(d["x"] + d["width"] for d in displays)
    # Quartz global Y: 0 at top of primary; height is max bottom edge in quartz space.
    max_top = max(d["y"] + d["height"] for d in displays)
    min_quartz_y = 0
    max_quartz_y = 0
    for d in displays:
        top = max_top - (d["y"] + d["height"])
        bottom = max_top - d["y"]
        min_quartz_y = min(min_quartz_y, top)
        max_quartz_y = max(max_quartz_y, bottom)
    return {
        "x": int(min_x),
        "y": int(min_quartz_y),
        "width": int(max_x - min_x),
        "height": int(max_quartz_y - min_quartz_y),
    }


def cocoa_to_quartz_y(cocoa_y: float) -> float:
    """Convert Cocoa global Y (bottom-left origin) to Quartz top-left Y."""
    max_top = max(d["y"] + d["height"] for d in list_displays())
    return max_top - cocoa_y


def quartz_to_applescript_y(quartz_y: float) -> float:
    """AppleScript/System Events window positions use top-left global Y."""
    return quartz_y


def applescript_position(display: dict, margin: int = 120) -> tuple[int, int]:
    """Top-left window position for AppleScript (global top-left origin)."""
    x = display["x"] + margin
    # NSScreen frame is Cocoa; top edge in quartz = cocoa_to_quartz_y(y + height)
    quartz_top = cocoa_to_quartz_y(display["y"] + display["height"])
    y_top = int(quartz_top + margin)
    return int(x), y_top


def frame_matches_requested(
    bounds: dict,
    *,
    x: int,
    y: int,
    width: int,
    height: int,
    position_tolerance: int = 32,
    size_tolerance: int = 3,
) -> bool:
    """Accept small WindowServer title-bar clamps, but require the requested size."""
    return (
        abs(float(bounds.get("x", -100000)) - float(x)) <= position_tolerance
        and abs(float(bounds.get("y", -100000)) - float(y)) <= position_tolerance
        and abs(float(bounds.get("width", -100000)) - float(width)) <= size_tolerance
        and abs(float(bounds.get("height", -100000)) - float(height)) <= size_tolerance
    )


def move_process_window(
    process_name: str,
    display_name: str | None = None,
    *,
    width: int = 800,
    height: int = 600,
    margin: int = 120,
) -> dict:
    if display_name is None:
        display_name = resolve_display_token()
    target = find_display(display_name)
    if not target:
        return {"ok": False, "error": f"display not found: {display_name}"}
    x, y = applescript_position(target, margin)
    script = (
        'tell application "System Events"\n'
        f'  if not (exists process "{process_name}") then return "NO_PROC"\n'
        f'  tell process "{process_name}"\n'
        '    if (count of windows) is 0 then return "NO_WIN"\n'
        "    set targetWindow to window 1\n"
        "    set maxArea to 0\n"
        "    repeat with candidateWindow in windows\n"
        "      set candidateSize to size of candidateWindow\n"
        "      set candidateArea to (item 1 of candidateSize) * (item 2 of candidateSize)\n"
        "      if candidateArea > maxArea then\n"
        "        set maxArea to candidateArea\n"
        "        set targetWindow to candidateWindow\n"
        "      end if\n"
        "    end repeat\n"
        f"    set size of targetWindow to {{{width}, {height}}}\n"
        "    delay 0.25\n"
        f"    set position of targetWindow to {{{x}, {y}}}\n"
        "    delay 0.25\n"
        '    return "OK"\n'
        "  end tell\n"
        "end tell\n"
    )
    proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    ok = proc.returncode == 0 and "OK" in (proc.stdout or "")
    move_method = "system-events"
    native_move = None
    if not ok:
        native_move = set_native_ax_window_frame(
            process_name, x=x, y=y, width=width, height=height
        )
        ok = native_move.get("ok") is True
        if ok:
            move_method = "native-ax"
    after = None
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        after = window_on_display(process_name)
        bounds = (after or {}).get("bounds") or {}
        if frame_matches_requested(bounds, x=x, y=y, width=width, height=height):
            break
        time.sleep(0.1)
    quartz_after = after
    verification = "quartz-onscreen"
    logical_after = logical_window_on_display(process_name)
    logical_bounds = (logical_after or {}).get("bounds") or {}
    logical_on_target = bool(
        logical_after
        and logical_after.get("display")
        and display_name.lower() in logical_after["display"]["name"].lower()
    )
    if logical_on_target and frame_matches_requested(
        logical_bounds, x=x, y=y, width=width, height=height
    ):
        after = logical_after
        verification = "accessibility-logical"

    on_target = (
        after
        and after.get("display")
        and display_name.lower() in after["display"]["name"].lower()
    )
    bounds = (after or {}).get("bounds") or {}
    frame_matches = frame_matches_requested(
        bounds, x=x, y=y, width=width, height=height
    )
    return {
        "ok": ok and on_target and frame_matches,
        "display": target["name"],
        "frame": [x, y, width, height],
        "window_after": after,
        "quartz_window_after": quartz_after,
        "frame_matches": frame_matches,
        "verification": verification,
        "detail": (proc.stdout or proc.stderr or "").strip(),
        "move_method": move_method,
        "native_move": native_move,
    }


def ensure_overlay_on_display(display_name: str | None = None) -> dict:
    """Position cua-driver overlay on one monitor; use local coords for move_cursor."""
    if display_name is None:
        display_name = resolve_display_token()
    target = find_display(display_name)
    if not target:
        return {"ok": False, "error": f"display not found: {display_name}"}
    quartz_top = int(cocoa_to_quartz_y(target["y"] + target["height"]))
    x = int(target["x"])
    w = int(target["width"])
    h = int(target["height"])
    process_name = "cua-driver"
    script = (
        'tell application "System Events"\n'
        f'  if not (exists process "{process_name}") then return "NO_PROC"\n'
        f'  tell process "{process_name}"\n'
        '    if (count of windows) is 0 then return "NO_WIN"\n'
        f"    set position of window 1 to {{{x}, {quartz_top}}}\n"
        f"    set size of window 1 to {{{w}, {h}}}\n"
        '    return "OK"\n'
        "  end tell\n"
        "end tell\n"
    )
    proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    command_ok = proc.returncode == 0 and "OK" in (proc.stdout or "")
    bounds = window_bounds_for_process(process_name) or {}
    actual = window_on_display(process_name)
    actual_display = (actual or {}).get("display", {}).get("name")
    on_target = bool(actual_display) and actual_display == target["name"]
    return {
        "ok": command_ok and on_target,
        "display": target["name"],
        "actual_display": actual_display,
        "origin": {"x": x, "y": quartz_top},
        "size": {"width": w, "height": h},
        "overlay_bounds": bounds,
        "detail": (proc.stdout or proc.stderr or "").strip(),
        "coord_mode": "local",
    }


def global_to_overlay_local(
    global_x: float, global_y: float, display_name: str | None = None
) -> tuple[float, float]:
    """Map global Quartz coords to overlay-local when driver screen is primary-only."""
    if display_name is None:
        display_name = resolve_display_token()
    target = find_display(display_name)
    if not target:
        return global_x, global_y
    quartz_top = cocoa_to_quartz_y(target["y"] + target["height"])
    return global_x - target["x"], global_y - quartz_top


def ensure_virtual_overlay(process_name: str = "cua-driver") -> dict:
    """Resize cua-driver overlay to span all monitors (global cursor coordinate space)."""
    vs = virtual_screen_quartz()
    w, h = vs["width"], vs["height"]
    if w <= 0 or h <= 0:
        return {"ok": False, "error": "no virtual screen"}
    script = (
        'tell application "System Events"\n'
        f'  if not (exists process "{process_name}") then return "NO_PROC"\n'
        f'  tell process "{process_name}"\n'
        '    if (count of windows) is 0 then return "NO_WIN"\n'
        f"    set position of window 1 to {{{vs['x']}, {vs['y']}}}\n"
        f"    set size of window 1 to {{{w}, {h}}}\n"
        '    return "OK"\n'
        "  end tell\n"
        "end tell\n"
    )
    proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    ok = proc.returncode == 0 and "OK" in (proc.stdout or "")
    bounds = window_bounds_for_process(process_name) or {}
    return {
        "ok": ok,
        "virtual_screen": vs,
        "overlay_bounds": bounds,
        "detail": (proc.stdout or proc.stderr or "").strip(),
    }


def ensure_on_test_display(
    process_name: str, display_name: str | None = None, **kwargs
) -> dict:
    if display_name is None:
        display_name = resolve_display_token()
    target = find_display(display_name)
    if not target:
        return {"ok": False, "error": f"display not found: {display_name}"}
    before = window_on_display(process_name)
    on_target = bool(
        before
        and before.get("display")
        and display_name.lower() in before["display"]["name"].lower()
    )
    bounds = (before or {}).get("bounds") or {}
    explicit_size = kwargs.get("width") is not None or kwargs.get("height") is not None
    if on_target and not explicit_size:
        return {
            "ok": True,
            "moved": False,
            "display": before["display"]["name"],
            "bounds": before["bounds"],
            "frame_matches": True,
            "reason": "already_on_target_display",
        }
    width = int(kwargs.get("width") or bounds.get("width") or 800)
    height = int(kwargs.get("height") or bounds.get("height") or 600)
    margin = int(kwargs.get("margin", 120))
    x, y = applescript_position(target, margin)
    frame_matches = frame_matches_requested(
        bounds, x=x, y=y, width=width, height=height
    )
    if on_target and frame_matches:
        return {
            "ok": True,
            "moved": False,
            "display": before["display"]["name"],
            "bounds": before["bounds"],
            "frame_matches": True,
        }
    moved = move_process_window(
        process_name,
        display_name,
        width=width,
        height=height,
        margin=margin,
    )
    moved["moved"] = True
    moved["reason"] = "wrong_display" if not on_target else "frame_mismatch"
    return moved
