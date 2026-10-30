# Runtime dependencies are injected by the compatibility facade.
# ruff: noqa: F401, F821
"""Window capture geometry and app-state proof.

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



def _capture_ax_window_frames(raw):
    """Return distinct AX window frames without guessing which one is largest."""
    frames = []
    seen = set()
    for element in raw.get("elements", []):
        if element.get("role") != "AXWindow":
            continue
        frame = _frame_rect(element.get("frame"))
        if not frame:
            continue
        if (
            float(frame.get("width") or 0) <= 0
            or float(frame.get("height") or 0) <= 0
        ):
            continue
        key = tuple(
            round(float(frame[name]), 2)
            for name in ("x", "y", "width", "height")
        )
        if key in seen:
            continue
        seen.add(key)
        frames.append({"source": "ax-window", **frame})
    return frames


def _is_stage_manager_quartz_proxy_candidate(quartz, ax_window):
    """Flag a smaller proxy candidate; exact AX-to-window identity must confirm it."""
    quartz_width = float(quartz.get("width") or 0)
    quartz_height = float(quartz.get("height") or 0)
    ax_width = float(ax_window.get("width") or 0)
    ax_height = float(ax_window.get("height") or 0)
    if min(quartz_width, quartz_height, ax_width, ax_height) <= 0:
        return False
    width_ratio = quartz_width / ax_width
    height_ratio = quartz_height / ax_height
    area_ratio = (quartz_width * quartz_height) / (ax_width * ax_height)
    return width_ratio < 0.95 and height_ratio < 0.95 and area_ratio < 0.85


def _capture_size_candidates(expected, max_image_dimension):
    """Expected 1x/2x backing sizes after the driver's configured uniform cap."""
    width = float(expected["width"])
    height = float(expected["height"])
    candidates = []
    seen = set()
    for backing_scale in (1.0, 2.0):
        natural_width = width * backing_scale
        natural_height = height * backing_scale
        driver_scale = 1.0
        if max_image_dimension:
            longest = max(natural_width, natural_height)
            if longest > max_image_dimension:
                driver_scale = max_image_dimension / longest
        candidate = {
            "backing_scale": backing_scale,
            "driver_scale": round(driver_scale, 6),
            "width": natural_width * driver_scale,
            "height": natural_height * driver_scale,
        }
        key = (round(candidate["width"], 3), round(candidate["height"], 3))
        if key not in seen:
            seen.add(key)
            candidates.append(candidate)
    return candidates


def _capture_matches_candidate(width, height, candidate):
    expected_width = float(candidate["width"])
    expected_height = float(candidate["height"])
    width_tolerance = max(4.0, expected_width * 0.02)
    height_tolerance = max(4.0, expected_height * 0.02)
    if (
        abs(width - expected_width) > width_tolerance
        or abs(height - expected_height) > height_tolerance
    ):
        return False
    observed_aspect = width / height
    expected_aspect = expected_width / expected_height
    return abs(observed_aspect - expected_aspect) <= max(
        0.02, expected_aspect * 0.02
    )


def _capture_screen_region(raw, expected, output, source):
    try:
        x = int(round(float(expected["x"])))
        y = int(round(float(expected["y"])))
        width = int(round(float(expected["width"])))
        height = int(round(float(expected["height"])))
    except (KeyError, TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    command = [
        "/usr/sbin/screencapture",
        "-x",
        f"-R{x},{y},{width},{height}",
        output,
    ]
    header = b""
    for attempt in range(2):
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=5,
                stdin=subprocess.DEVNULL,
            )
            if completed.returncode == 0:
                with open(output, "rb") as stream:
                    header = stream.read(24)
                if header[:8] == b"\x89PNG\r\n\x1a\n":
                    break
        except (OSError, subprocess.TimeoutExpired):
            pass
        if attempt == 0:
            time.sleep(0.05)
    else:
        return None
    captured = dict(raw)
    captured["screenshot_width"] = int.from_bytes(header[16:20], "big")
    captured["screenshot_height"] = int.from_bytes(header[20:24], "big")
    captured["screenshot_mime_type"] = "image/png"
    captured["screenshot_file_path"] = output
    captured["snapshot_id"] = captured.get("snapshot_id") or f"native-{time.time_ns()}"
    captured["capture_source"] = source
    return captured


def _capture_native_window_region(raw, pid, window_id, output):
    expected, _ = _quartz_window_bounds(pid, window_id)
    if not expected:
        frames = _capture_ax_window_frames(raw)
        expected = frames[0] if len(frames) == 1 else None
    if not expected:
        return None
    return _capture_screen_region(
        raw, expected, output, "foreground_native_ax_screen_region"
    )


def _recapture_stage_manager_region(raw, capture_geometry):
    """Replace a Stage Manager proxy capture with verified foreground pixels."""
    identity = capture_geometry.get("identity") or {}
    expected = capture_geometry.get("expected") or {}
    output = raw.get("screenshot_file_path")
    if (
        identity.get("method") != "stage-manager-exact-ax-window-id-override"
        or not output
    ):
        return None
    return _capture_screen_region(raw, expected, output, "foreground_ax_screen_region")


def _capture_geometry_proof(
    raw,
    pid,
    window_id,
    *,
    max_image_dimension=None,
    driver_config_error=None,
):
    """Verify exact-window pixels while rejecting Stage Manager proxies."""
    width = float(raw.get("screenshot_width") or 0)
    height = float(raw.get("screenshot_height") or 0)
    quartz, quartz_error = _quartz_window_bounds(pid, window_id)
    if quartz and (
        float(quartz.get("width") or 0) <= 0
        or float(quartz.get("height") or 0) <= 0
    ):
        quartz = None
        quartz_error = quartz_error or "exact Quartz window has invalid geometry"
    ax_windows = _capture_ax_window_frames(raw)
    expected = None
    identity = {"status": "unresolved", "reason": "window_geometry_unavailable"}
    if quartz:
        expected = {"source": "quartz", **quartz}
        identity = {"status": "resolved", "method": "exact-quartz-window-id"}
        if len(ax_windows) == 1 and _is_stage_manager_quartz_proxy_candidate(
            quartz, ax_windows[0]
        ):
            exact_ax, exact_ax_error = _exact_ax_window_frame(pid, window_id)
            if exact_ax and _is_stage_manager_quartz_proxy_candidate(
                quartz, exact_ax
            ):
                expected = exact_ax
                identity = {
                    "status": "resolved",
                    "method": "stage-manager-exact-ax-window-id-override",
                    "quartz_proxy": {"source": "quartz", **quartz},
                }
            elif exact_ax:
                identity["ax_identity"] = "exact-window-not-proxy"
            else:
                expected = None
                identity = {
                    "status": "unresolved",
                    "reason": "suspected-quartz-proxy-without-exact-ax-identity",
                    "ax_identity_error": exact_ax_error,
                    "quartz_proxy": {"source": "quartz", **quartz},
                }
    elif len(ax_windows) == 1:
        expected = ax_windows[0]
        identity = {"status": "resolved", "method": "sole-ax-window-fallback"}
    elif len(ax_windows) > 1:
        identity = {"status": "unresolved", "reason": "ambiguous-ax-windows"}

    if max_image_dimension is None and driver_config_error is None:
        max_image_dimension, driver_config_error = _driver_max_image_dimension()
    if not expected or width <= 0 or height <= 0:
        return {
            "verified": None,
            "screenshot": {"width": width, "height": height},
            "expected": expected,
            "identity": identity,
            "ax_window_count": len(ax_windows),
            "driver": {
                "max_image_dimension": max_image_dimension,
                "config_error": driver_config_error,
            },
            "quartz_error": quartz_error,
        }
    scale_x = width / float(expected["width"])
    scale_y = height / float(expected["height"])
    candidates = _capture_size_candidates(expected, max_image_dimension)
    matched = next(
        (
            candidate
            for candidate in candidates
            if _capture_matches_candidate(width, height, candidate)
        ),
        None,
    )
    return {
        "verified": matched is not None,
        "screenshot": {"width": width, "height": height},
        "expected": expected,
        "identity": identity,
        "ax_window_count": len(ax_windows),
        "scale": {"x": round(scale_x, 4), "y": round(scale_y, 4)},
        "matched_candidate": matched,
        "size_candidates": candidates,
        "driver": {
            "max_image_dimension": max_image_dimension,
            "config_error": driver_config_error,
        },
        "quartz_error": quartz_error,
    }


def app_state(
    app_name,
    pid,
    window_id,
    *,
    max_elements=120,
    query=None,
    include_screenshot=True,
    screenshot_out_file=None,
    prepare_foreground=True,
    foreground_prepared=False,
):
    """One Computer Use-style state object from CUAService: AX text + elements + image.

    Indices are CUAService's, the same space click/set-value/scroll use.
    ``prepare_foreground`` raises the window for capture (interrupts the user).
    """
    return cua_app_state(
        app_name,
        pid,
        window_id,
        max_elements=max_elements,
        query=query,
        include_screenshot=include_screenshot,
        screenshot_out_file=screenshot_out_file,
        prepare_foreground=bool(prepare_foreground and not foreground_prepared),
    )


