# Runtime dependencies are injected by the compatibility facade.
# ruff: noqa: F401, F821
"""Element lookup and native Vision fallback snapshots.

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
import unicodedata
from pathlib import Path

def find_element_index(snapshot_data, text):
    tree = snapshot_data.get("tree_markdown", "")
    pattern = rf"\[(\d+)\][^\n]*{re.escape(text)}"
    m = re.search(pattern, tree, re.IGNORECASE)
    return int(m.group(1)) if m else None


CLICK_ROLES = {
    "AXButton",
    "AXCheckBox",
    "AXRadioButton",
    "AXLink",
    "AXPopUpButton",
    "AXMenuButton",
    "AXMenuBarItem",
    "AXMenuItem",
    "AXRow",
    "AXCell",
    "AXDisclosureTriangle",
    "AXImage",
}
# Prefer true controls over decorative/clickable chrome chrome when labels collide.
PRIMARY_CLICK_ROLES = {
    "AXButton",
    "AXLink",
    "AXCheckBox",
    "AXRadioButton",
    "AXPopUpButton",
    "AXMenuButton",
    "AXMenuItem",
    "AXMenuBarItem",
}
FIELD_ROLES = {"AXTextField", "AXTextArea"}
# In-place retitled controls resolve on the current tree. Do not re-observe to retitle.
LABEL_ALIASES = {
    "clear": ("all clear",),
    "all clear": ("clear",),
}


def _visible_text(value):
    return "".join(
        char for char in str(value or "") if unicodedata.category(char) != "Cf"
    ).strip()


SCAFFOLDING_ROLES = {
    "AXApplication",
    "AXMenuBar",
    "AXMenuBarItem",
    "AXMenu",
    "AXMenuItem",
}


def snapshot_content_error(snapshot_data):
    """Return structured failure metadata when AX state has no window content."""
    if not isinstance(snapshot_data, dict):
        return {"error": "invalid snapshot response"}
    if snapshot_data.get("error"):
        return {"error": str(snapshot_data["error"])}
    elements = snapshot_data.get("elements")
    if not isinstance(elements, list):
        return {"error": "snapshot response has no elements list"}
    if not elements and not str(snapshot_data.get("tree_markdown") or "").strip():
        return {"error": "snapshot response has an empty AX tree"}
    content_roles = sorted(
        {
            str(element.get("role") or "")
            for element in elements
            if element.get("role") and element.get("role") not in SCAFFOLDING_ROLES
        }
    )
    if content_roles:
        return None
    return {
        "error": "snapshot has no target-window AX content",
        "roles": sorted(
            {
                str(element.get("role") or "")
                for element in elements
                if element.get("role")
            }
        ),
        "element_count": len(elements),
    }


def emit_list_buttons(snapshot_data):
    """Print interactive controls and fail honestly when the snapshot failed."""
    error = snapshot_content_error(snapshot_data)
    if error:
        print(json.dumps(error), file=sys.stderr)
        return False
    elements = snapshot_data["elements"]
    visual = snapshot_data.get("source") in {"driver_vision", "native_vision"}
    for element in elements:
        labeled = (element.get("label") or "").strip()
        actionable = element.get("role") in CLICK_ROLES
        visual_target = visual and labeled and bool(element.get("frame"))
        if labeled and (actionable or visual_target):
            print(
                json.dumps(
                    {
                        "index": element.get("element_index"),
                        "label": element.get("label"),
                        "value": element.get("value", ""),
                    }
                )
            )
    return True


class AmbiguousLabelError(ValueError):
    """Multiple clickable elements match the requested label."""

    def __init__(self, label: str, matches: list[dict]):
        self.label = label
        self.matches = matches
        preview = ", ".join(
            f"#{m.get('element_index')}:{m.get('role')}:{str(m.get('label') or '')[:40]}"
            for m in matches[:5]
        )
        super().__init__(f"ambiguous label {label!r}: {preview}")


def _match_label(elements, text, roles, *, allow_ambiguous: bool = False):
    """Exact label match, then shortest actionable substring match.

    Multiple exact (or equal-shortest substring) matches fail closed unless a
    single PRIMARY_CLICK_ROLES element remains after role preference.
    """
    needle = _visible_text(text).lower()
    pool = [e for e in elements if e.get("role") in roles]

    def searchable(element):
        return "\n".join(
            _visible_text(element.get(key))
            for key in ("label", "value", "derived_text")
        ).strip()

    def pick(cands: list[dict]):
        if not cands:
            return None
        if len(cands) == 1:
            return cands[0].get("element_index")
        primary = [e for e in cands if e.get("role") in PRIMARY_CLICK_ROLES]
        # Fail closed only when multiple true controls collide (buttons/links).
        # Row/cell/image scaffolding often shares labels; keep shortest-pick.
        if len(primary) > 1 and not allow_ambiguous:
            raise AmbiguousLabelError(text, primary)
        if len(primary) == 1:
            return primary[0].get("element_index")
        ranked = sorted(cands, key=lambda e: len(searchable(e)))
        return ranked[0].get("element_index")

    exact = [e for e in pool if searchable(e).lower() == needle]
    if exact:
        return pick(exact)
    for alias in LABEL_ALIASES.get(needle, ()):
        aliased = [e for e in pool if searchable(e).lower() == alias]
        if aliased:
            return pick(aliased)
    cands = [e for e in pool if needle in searchable(e).lower()]
    if not cands:
        return None
    cands.sort(key=lambda e: len(searchable(e)))
    shortest = len(searchable(cands[0]))
    tied = [e for e in cands if len(searchable(e)) == shortest]
    return pick(tied)


def find_clickable_index(snapshot_data, text):
    idx = _match_label(snapshot_data.get("elements", []), text, CLICK_ROLES)
    if idx is not None:
        return idx
    # Older driver trees can carry the searchable label only in Markdown. Keep
    # that compatibility path role-scoped: an app/window title containing the
    # requested text is not a clickable target (for example an app window
    # containing "Window" vs the AXMenuBarItem named "Window").
    legacy_idx = find_element_index(snapshot_data, text)
    legacy = next(
        (
            element
            for element in snapshot_data.get("elements", [])
            if element.get("element_index") == legacy_idx
        ),
        None,
    )
    return legacy_idx if legacy and legacy.get("role") in CLICK_ROLES else None


def resolve_clickable_index(snapshot_data, text):
    """Like find_clickable_index but returns (index|None, error_dict|None)."""
    try:
        return find_clickable_index(snapshot_data, text), None
    except AmbiguousLabelError as exc:
        return None, {
            "ok": False,
            "error": str(exc),
            "error_code": "ambiguous_label",
            "matches": [
                {
                    "element_index": m.get("element_index"),
                    "role": m.get("role"),
                    "label": m.get("label"),
                }
                for m in exc.matches[:8]
            ],
        }


def find_visual_index(snapshot_data, text):
    """Match a rendered label with a concrete frame for pixel fallback."""
    framed = [
        element
        for element in snapshot_data.get("elements", [])
        if element.get("frame") and (element.get("label") or element.get("value"))
    ]
    roles = {element.get("role") for element in framed if element.get("role")}
    return _match_label(framed, text, roles) if roles else None


def _vision_snapshot_after_activation(pid, window_id, max_elements=120):
    """Ground labels in the rendered window when the app exposes no AX window."""
    activation = _activate_running_identity({"pid": int(pid)})
    screenshot_path = os.path.join(
        SCREENSHOT_DIR,
        f"vision-{int(pid)}-{int(window_id)}.png",
    )
    try:
        os.unlink(screenshot_path)
    except FileNotFoundError:
        pass
    visual = snapshot(
        pid,
        window_id,
        max_elements=max_elements,
        mode="vision",
        retries=0,
        include_screenshot=True,
        screenshot_out_file=screenshot_path,
    )
    framed = [
        element
        for element in (visual or {}).get("elements", [])
        if element.get("frame") and (element.get("label") or element.get("value"))
    ]
    if snapshot_content_error(visual) or not framed:
        captured = (visual or {}).get("screenshot_file_path") or screenshot_path
        visual = _native_vision_snapshot(
            pid,
            window_id,
            captured,
            max_elements=max_elements,
        )
    if isinstance(visual, dict) and activation.get("error"):
        visual["activation_warning"] = activation["error"]
    return visual


def _ensure_native_vision_binary():
    """Compile the small Vision helper only when its source is newer."""
    source = Path(VISION_OCR_SOURCE)
    binary = Path(VISION_OCR_BINARY)
    if not source.is_file():
        return None, f"native Vision source is missing: {source}"
    if binary.is_file() and binary.stat().st_mtime >= source.stat().st_mtime:
        return str(binary), None
    binary.parent.mkdir(parents=True, exist_ok=True)
    try:
        built = subprocess.run(
            [
                "xcrun",
                "swiftc",
                "-O",
                "-parse-as-library",
                str(source),
                "-o",
                str(binary),
            ],
            capture_output=True,
            text=True,
            timeout=45,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return None, "native Vision helper build timed out after 45s"
    if built.returncode != 0:
        return None, (built.stderr or built.stdout).strip()
    return str(binary), None


def _quartz_window_bounds(pid, window_id):
    """Return verified global logical bounds for one PID-owned Quartz window."""
    try:
        import Quartz
    except ImportError as import_error:
        return None, f"Quartz unavailable: {import_error}"
    windows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionAll | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    )
    for window in windows or ():
        if int(window.get("kCGWindowOwnerPID", 0)) != int(pid):
            continue
        if int(window.get("kCGWindowNumber", 0)) != int(window_id):
            continue
        raw = window.get("kCGWindowBounds") or {}
        bounds = {
            "x": float(raw.get("X", 0)),
            "y": float(raw.get("Y", 0)),
            "width": float(raw.get("Width", 0)),
            "height": float(raw.get("Height", 0)),
        }
        if bounds["width"] > 0 and bounds["height"] > 0:
            return bounds, None
    return None, f"Quartz window {window_id} is not owned by pid {pid}"


def _native_vision_snapshot(pid, window_id, screenshot_path, max_elements=120):
    """OCR a driver-owned screenshot using verified Quartz logical bounds."""
    if not screenshot_path or not Path(screenshot_path).is_file():
        return {
            "error": "cua-driver did not produce a screenshot for native Vision",
            "elements": [],
            "source": "native_vision",
        }
    bounds, bounds_error = _quartz_window_bounds(pid, window_id)
    if bounds_error:
        return {"error": bounds_error, "elements": [], "source": "native_vision"}
    binary, error = _ensure_native_vision_binary()
    if error:
        return {"error": error, "elements": [], "source": "native_vision"}
    try:
        result = subprocess.run(
            [
                binary,
                "--image",
                str(screenshot_path),
                "--origin-x",
                str(bounds["x"]),
                "--origin-y",
                str(bounds["y"]),
                "--logical-width",
                str(bounds["width"]),
                "--logical-height",
                str(bounds["height"]),
                "--max",
                str(int(max_elements)),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return {
            "error": "native Vision OCR timed out after 15s",
            "elements": [],
            "source": "native_vision",
        }
    if result.returncode != 0:
        return {
            "error": (result.stderr or result.stdout).strip(),
            "elements": [],
            "source": "native_vision",
        }
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as decode_error:
        return {
            "error": f"native Vision returned invalid JSON: {decode_error}",
            "elements": [],
            "source": "native_vision",
        }


def find_field_index(snapshot_data, text):
    idx = _match_label(snapshot_data.get("elements", []), text, FIELD_ROLES)
    if idx is not None:
        return idx
    return find_element_index(snapshot_data, text)


def element_center(snapshot_data, element_index):
    """Return (x, y) screen center from AX frame, or None."""
    for e in snapshot_data.get("elements", []):
        if e.get("element_index") != element_index:
            continue
        fr = e.get("frame") or {}
        x = fr.get("x")
        y = fr.get("y")
        w = fr.get("w", fr.get("width", 0))
        h = fr.get("h", fr.get("height", 0))
        if x is None or y is None:
            return None
        return float(x) + float(w) / 2, float(y) + float(h) / 2
    return None


def frame_app_window(
    app_name, *, width: int | None = None, height: int | None = None, margin: int = 120
):
    """Move an app to the configured display while preserving its size by default."""
    current = _displays().window_bounds_for_process(app_name) or {}
    if width is None:
        width = int(
            os.environ.get("MACOS_CUA_WINDOW_WIDTH")
            or current.get("width")
            or 800
        )
    if height is None:
        height = int(
            os.environ.get("MACOS_CUA_WINDOW_HEIGHT")
            or current.get("height")
            or 600
        )
    return _displays().ensure_on_test_display(
        app_name, width=width, height=height, margin=margin
    )
