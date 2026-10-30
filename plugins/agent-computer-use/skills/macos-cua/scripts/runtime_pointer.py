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












def right_click(pid, window_id, element_index, *, app_name=None, snapshot_data=None):
    """AXShowMenu on the CUAService element (same index space as snap/state)."""
    refused = require_cuaservice_window(pid, window_id, max(80, int(element_index) + 1))
    if refused:
        return {**refused, "element": int(element_index)}
    return cua_right_click(pid, element_index, window_id=window_id)


