# Runtime dependencies are injected by the compatibility facade.
# ruff: noqa: F401, F821
"""Snapshots, compact state, screenshots, and cursor raster assets."""
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

def snapshot(
    pid,
    window_id,
    max_elements=20,
    mode="som",
    retries=2,
    delay=0.4,
    *,
    include_screenshot=False,
    screenshot_out_file=None,
    query=None,
):
    """Get a window tree. Retries when the tree comes back empty, which
    happens when the app is mid-navigation/animation and cua-driver's
    internal AX walk races the UI.

    Note: `element_count` only counts interactive nodes (buttons/menus), so a
    content-rich screen with few buttons can legitimately report a small
    element_count. We retry only when the tree is genuinely empty.
    """
    last = None
    for _ in range(retries + 1):
        params = {
            "pid": pid,
            "window_id": window_id,
            "max_elements": max_elements,
            "include_screenshot": include_screenshot,
            # cua-driver 0.7.x rejects unknown fields because the tool schema
            # has additionalProperties=false. Its accepted compatibility
            # field is capture_mode (som maps to the normal AX snapshot).
            "capture_mode": "vision" if mode == "vision" else "ax",
        }
        if screenshot_out_file:
            params["screenshot_out_file"] = screenshot_out_file
        if query:
            params["query"] = query
        last = call_driver(
            "get_window_state",
            params,
            timeout=int(os.environ.get("MACOS_CUA_STATE_TIMEOUT", "12")),
        )
        if isinstance(last, dict) and last.get("error"):
            return last
        if isinstance(last, dict):
            last.setdefault("source", f"driver_{mode}")
        if isinstance(last, dict) and last.get("degraded"):
            tree = ""
        else:
            tree = last.get("tree_markdown", "") if isinstance(last, dict) else ""
        captured = last.get("screenshot_file_path") if isinstance(last, dict) else None
        capture_ready = not include_screenshot
        if include_screenshot and captured:
            materialize_deadline = time.monotonic() + 1.0
            while time.monotonic() < materialize_deadline:
                try:
                    if Path(captured).is_file() and Path(captured).stat().st_size > 0:
                        capture_ready = True
                        break
                except OSError:
                    pass
                time.sleep(0.05)
        if len(tree) > 80 and capture_ready:  # non-empty tree + required proof
            _enrich_elements(last)
            return last
        time.sleep(delay)
    if isinstance(last, dict):
        _enrich_elements(last)
        if include_screenshot:
            captured = last.get("screenshot_file_path")
            try:
                capture_ready = bool(
                    captured
                    and Path(captured).is_file()
                    and Path(captured).stat().st_size > 0
                )
            except OSError:
                capture_ready = False
            if not capture_ready:
                last["error"] = "screenshot artifact did not materialize"
                last["capture_path"] = captured or screenshot_out_file
    return last


ACTIVE_SURFACE_ROLES = frozenset({"AXSheet", "AXPopover", "AXDialog"})
ROW_NAME_CHILD_ROLES = frozenset({"AXStaticText", "AXTextField"})


def choose_walk_roots(windows, surfaces, menus=None):
    """Modals only; else open app-level menus first, then one window.

    Never pass or walk AXMenuBar. Closed Apple-menu BFS floods --max.
    """
    if surfaces:
        return list(surfaces)
    return list(menus or []) + list(windows[:1] or [])


def _has_ax_frame(frame):
    if not isinstance(frame, dict):
        return False
    return all(isinstance(frame.get(key), (int, float)) for key in ("x", "y")) and any(
        float(frame.get(key, 0) or 0) > 0 for key in ("w", "h", "width", "height")
    )


def _open_ax_menus(app, services):
    """App-level context menus only. Do not walk the menu bar here."""
    return [
        child
        for child in _ax_sequence(_ax_value(app, "AXChildren", services))
        if str(_ax_value(child, "AXRole", services) or "") == "AXMenu"
        and _ax_frame(child, services)
    ]


def typed_text_is_proven(text, observed, selected=None):
    return bool(text) and any(
        isinstance(candidate, str) and text in candidate
        for candidate in (observed, selected)
    )


def _attach_static_child_text(elements):
    """Copy AXStaticText/AXTextField onto unlabeled AXRow/AXCell ancestors."""
    if not isinstance(elements, list):
        return elements
    by_index = {item.get("element_index"): item for item in elements}
    for item in elements:
        if item.get("role") not in ROW_NAME_CHILD_ROLES:
            continue
        text = str(item.get("value") or item.get("label") or "").strip()
        if not text:
            continue
        parent = by_index.get(item.get("parent_index"))
        hops = 0
        while parent is not None and hops < 6:
            if parent.get("role") in {"AXRow", "AXCell"}:
                existing = [
                    line
                    for line in str(parent.get("derived_text") or "").splitlines()
                    if line
                ]
                parent["derived_text"] = "\n".join(dict.fromkeys(existing + [text]))
                if not parent.get("label") and not parent.get("value"):
                    parent["value"] = text
                if parent.get("role") == "AXRow":
                    break
            parent = by_index.get(parent.get("parent_index"))
            hops += 1
    return elements


def _enrich_elements(snapshot_data):
    """Attach unindexed static child text to clickable AX ancestors."""
    if not isinstance(snapshot_data, dict):
        return snapshot_data
    elements = snapshot_data.get("elements", [])
    _attach_static_child_text(elements)
    by_index = {e.get("element_index"): e for e in elements}
    stack = []
    direct_text = {}
    for line in snapshot_data.get("tree_markdown", "").splitlines():
        indent = len(line) - len(line.lstrip(" "))
        indexed = re.search(r"\[(\d+)\]", line)
        if indexed:
            index = int(indexed.group(1))
            actions = re.search(r"actions=\[([^\]]*)\]", line)
            if actions and index in by_index:
                by_index[index]["actions"] = [
                    item.strip() for item in actions.group(1).split(",") if item.strip()
                ]
            while stack and stack[-1][0] >= indent:
                stack.pop()
            stack.append((indent, index))
            continue
        static = re.search(r'AXStaticText\s*=\s*"(.*)"', line)
        if static and stack:
            text = static.group(1).strip()
            if text:
                direct_text.setdefault(stack[-1][1], []).append(text)
    for index, texts in direct_text.items():
        values = list(dict.fromkeys(texts))
        target = by_index.get(index)
        if target is None:
            continue
        target["derived_text"] = "\n".join(values)
        parent = by_index.get(target.get("parent_index"))
        if parent is not None and not parent.get("label") and not parent.get("value"):
            existing = parent.get("derived_text", "").splitlines()
            parent["derived_text"] = "\n".join(dict.fromkeys(existing + values))
    return snapshot_data


def _state_elements(elements):
    """Hide closed-menu descendants. Keep items under a framed open AXMenu."""
    by_index = {item.get("element_index"): item for item in elements}
    open_menus = {
        item.get("element_index")
        for item in elements
        if item.get("role") == "AXMenu" and _has_ax_frame(item.get("frame"))
    }
    compact = []
    for element in elements:
        role = element.get("role")
        if role in {"AXMenu", "AXMenuItem"} and not _has_ax_frame(element.get("frame")):
            parent = element.get("parent_index")
            hops = 0
            keep = False
            while parent is not None and hops < 4:
                if parent in open_menus:
                    keep = True
                    break
                parent = (by_index.get(parent) or {}).get("parent_index")
                hops += 1
            if not keep:
                continue
        compact.append(element)
    return compact


def _state_text(app_name, elements, query=None):
    """Render a compact, index-stable accessibility view for agent context."""
    window = next((e for e in elements if e.get("role") == "AXWindow"), None)
    title = (window or {}).get("label") or app_name
    lines = [f'Window: "{title}", App: {app_name}.']
    matcher = None
    if query:
        try:
            matcher = re.compile(str(query), re.IGNORECASE)
        except re.error:
            matcher = re.compile(re.escape(str(query)), re.IGNORECASE)
    for element in elements:
        index = element.get("element_index")
        if index is None:
            continue
        role = str(element.get("role") or "AXUnknown")
        label = str(element.get("label") or "").strip()
        value = str(element.get("value") or "").strip()
        derived = str(element.get("derived_text") or "").strip()
        pieces = [f"[{index}] {role}"]
        if label:
            pieces.append(json.dumps(label, ensure_ascii=False))
        if value and value != label:
            pieces.append(f"value={json.dumps(value, ensure_ascii=False)}")
        if derived and derived not in {label, value}:
            pieces.append(f"text={json.dumps(derived, ensure_ascii=False)}")
        actions = element.get("actions") or []
        if actions:
            pieces.append(f"actions={json.dumps(actions, ensure_ascii=False)}")
        line = "  " * max(0, int(element.get("depth", 0) or 0)) + " ".join(pieces)
        if matcher is None or matcher.search(line) or role == "AXWindow":
            lines.append(line)
    return "\n".join(lines)


def _default_screenshot_path(app_name):
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", app_name)
    return os.path.join(SCREENSHOT_DIR, f"{safe}-{time.time_ns()}.png")


def _absolute_output_path(path):
    """Normalize driver output paths; cua-driver silently ignores relative paths."""
    if not path:
        return path
    absolute = os.path.abspath(os.path.expanduser(str(path)))
    os.makedirs(os.path.dirname(absolute), exist_ok=True)
    return absolute


def _cursor_proof_path(screenshot_path):
    root, extension = os.path.splitext(screenshot_path)
    return f"{root}-cursor{extension or '.png'}"


def annotate_cursor_screenshot(
    screenshot_path, cursor_x, cursor_y, output_path=None, cursor_path=None
):
    """Draw the agent cursor into a proof PNG using normalized top-left coords."""
    try:
        from AppKit import (
            NSBitmapImageFileTypePNG,
            NSBitmapImageRep,
            NSBezierPath,
            NSColor,
            NSCompositingOperationSourceOver,
            NSImage,
            NSMakeRect,
        )

        image = NSImage.alloc().initWithContentsOfFile_(str(screenshot_path))
        if image is None:
            raise ValueError("screenshot is not a readable image")
        asset = str(cursor_path or CURSOR_ICON)
        native_asset = cursor_raster_path(asset)
        pointer = NSImage.alloc().initWithContentsOfFile_(native_asset)
        if pointer is None:
            raise ValueError(f"cursor asset is not readable: {native_asset}")
        x = min(1.0, max(0.0, float(cursor_x)))
        y = min(1.0, max(0.0, float(cursor_y)))
        size = image.size()
        target = str(output_path or _cursor_proof_path(str(screenshot_path)))
        rendered = NSImage.alloc().initWithSize_(size)
        rendered.lockFocus()
        image.drawInRect_(NSMakeRect(0, 0, size.width, size.height))

        # Normalized coordinates use screenshot top-left; AppKit draws bottom-left.
        pointer_size = max(24.0, min(34.0, min(size.width, size.height) * 0.12))
        target_x = x * size.width
        target_y = (1.0 - y) * size.height
        destination = NSMakeRect(
            min(max(0.0, target_x), max(0.0, size.width - pointer_size)),
            min(
                max(0.0, target_y - pointer_size), max(0.0, size.height - pointer_size)
            ),
            pointer_size,
            pointer_size,
        )
        pointer.drawInRect_fromRect_operation_fraction_(
            destination,
            NSMakeRect(0, 0, pointer.size().width, pointer.size().height),
            NSCompositingOperationSourceOver,
            1.0,
        )
        marker = NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(
                min(max(0.0, target_x - 4.0), max(0.0, size.width - 8.0)),
                min(max(0.0, target_y - 4.0), max(0.0, size.height - 8.0)),
                8.0,
                8.0,
            )
        )
        NSColor.systemCyanColor().setStroke()
        marker.setLineWidth_(2.0)
        marker.stroke()

        bounds = NSMakeRect(0, 0, size.width, size.height)
        representation = NSBitmapImageRep.alloc().initWithFocusedViewRect_(bounds)
        rendered.unlockFocus()
        data = representation.representationUsingType_properties_(
            NSBitmapImageFileTypePNG, {}
        )
        if not data.writeToFile_atomically_(target, True):
            raise OSError(f"could not write {target}")
        return {
            "ok": True,
            "path": target,
            "cursor": {"x": x, "y": y},
            "cursor_asset": asset,
        }
    except (AttributeError, ImportError, OSError, TypeError, ValueError) as error:
        return {"ok": False, "error": str(error), "path": screenshot_path}


class CursorRasterError(OSError):
    """Raised when the native cursor raster cannot be produced safely."""


def _validate_cursor_raster(path, size):
    """Require a complete, visible PNG decoded at the requested square size."""
    try:
        with Path(path).open("rb") as stream:
            header = stream.read(24)
    except FileNotFoundError:
        raise ValueError(f"missing PNG output: {path}") from None
    except OSError as error:
        raise ValueError(f"unreadable PNG output {path}: {error}") from error
    if len(header) < 24:
        raise ValueError(f"truncated PNG output: {len(header)} header bytes")
    if header[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("invalid PNG signature")
    if header[12:16] != b"IHDR":
        raise ValueError("missing PNG IHDR header")
    width = int.from_bytes(header[16:20], "big")
    height = int.from_bytes(header[20:24], "big")
    if (width, height) != (size, size):
        raise ValueError(
            f"unexpected cursor raster size: {width}x{height}; expected {size}x{size}"
        )

    try:
        from AppKit import NSBitmapFormatAlphaFirst, NSBitmapImageRep
    except ImportError as error:
        raise ValueError(f"AppKit PNG validation is unavailable: {error}") from error
    try:
        representation = NSBitmapImageRep.imageRepWithContentsOfFile_(str(path))
    except Exception as error:
        raise ValueError(f"undecodable PNG output: {error}") from error
    if representation is None:
        raise ValueError("undecodable PNG output")
    decoded_width = int(representation.pixelsWide())
    decoded_height = int(representation.pixelsHigh())
    if (decoded_width, decoded_height) != (size, size):
        raise ValueError(
            f"decoded cursor raster size mismatch: {decoded_width}x{decoded_height}; "
            f"expected {size}x{size}"
        )
    if representation.hasAlpha():
        visible = False
        bits_per_sample = int(representation.bitsPerSample())
        samples_per_pixel = int(representation.samplesPerPixel())
        if (
            bits_per_sample == 8
            and samples_per_pixel > 1
            and not representation.isPlanar()
            and representation.bitmapData() is not None
        ):
            pixels = representation.bitmapData().tobytes()
            bytes_per_row = int(representation.bytesPerRow())
            alpha_first = bool(
                int(representation.bitmapFormat()) & int(NSBitmapFormatAlphaFirst)
            )
            alpha_offset = 0 if alpha_first else samples_per_pixel - 1
            for row in range(decoded_height):
                row_start = row * bytes_per_row
                alpha_start = row_start + alpha_offset
                alpha_stop = row_start + decoded_width * samples_per_pixel
                if any(pixels[alpha_start:alpha_stop:samples_per_pixel]):
                    visible = True
                    break
        else:
            for y in range(decoded_height):
                if visible:
                    break
                for x in range(decoded_width):
                    color = representation.colorAtX_y_(x, y)
                    if color is not None and color.alphaComponent() > 0:
                        visible = True
                        break
        if not visible:
            raise ValueError("cursor raster has no visible pixels")
    return width, height


def _rasterize_cursor_with_appkit(source_path, output_path, size):
    """Render one cursor asset into an exact-size PNG through native AppKit."""
    try:
        from AppKit import (
            NSBitmapImageFileTypePNG,
            NSBitmapImageRep,
            NSCompositingOperationSourceOver,
            NSImage,
            NSMakeRect,
        )
    except ImportError as error:
        raise OSError(f"AppKit is unavailable: {error}") from error

    source = NSImage.alloc().initWithContentsOfFile_(str(source_path))
    if source is None:
        raise ValueError(f"AppKit could not read cursor asset: {source_path}")
    source_size = source.size()
    if source_size.width <= 0 or source_size.height <= 0:
        raise ValueError(
            f"AppKit cursor asset has invalid size: "
            f"{source_size.width}x{source_size.height}"
        )

    bounds = NSMakeRect(0, 0, size, size)
    rendered = NSImage.alloc().initWithSize_((size, size))
    if rendered is None:
        raise OSError(f"AppKit could not allocate {size}x{size} cursor canvas")
    representation = None
    rendered.lockFocus()
    try:
        source.drawInRect_fromRect_operation_fraction_(
            bounds,
            NSMakeRect(0, 0, source_size.width, source_size.height),
            NSCompositingOperationSourceOver,
            1.0,
        )
        representation = NSBitmapImageRep.alloc().initWithFocusedViewRect_(bounds)
    finally:
        rendered.unlockFocus()
    if representation is None:
        raise OSError("AppKit could not create a cursor bitmap representation")
    data = representation.representationUsingType_properties_(
        NSBitmapImageFileTypePNG, {}
    )
    if data is None or not data.writeToFile_atomically_(str(output_path), True):
        raise OSError(f"AppKit could not write cursor PNG: {output_path}")
    return str(output_path)


def cursor_raster_path(source_path=None, output_path=None, size=96):
    """Materialize the Hermes SVG as a validated native PNG or fail closed."""
    source = str(source_path or CURSOR_ICON)
    output = str(output_path or CURSOR_RASTER)
    try:
        requested_size = int(size)
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid cursor raster size: {size!r}") from error
    if requested_size <= 0:
        raise ValueError(f"invalid cursor raster size: {requested_size}")

    if os.path.isfile(output):
        try:
            cache_is_fresh = os.path.getmtime(output) >= os.path.getmtime(source)
            if cache_is_fresh:
                _validate_cursor_raster(output, requested_size)
                return output
        except (OSError, ValueError):
            # A stale, corrupt, or wrong-size cache is never accepted as proof.
            pass

    output_directory = os.path.dirname(output)
    if output_directory:
        os.makedirs(output_directory, exist_ok=True)
    temporary_root = output_directory or "."
    attempt_details = []
    for attempt in range(1, 3):
        completed = None
        try:
            with tempfile.TemporaryDirectory(
                prefix=f".{Path(output).name}.tmp-{attempt}-",
                dir=temporary_root,
            ) as temporary_directory:
                temporary = os.path.join(temporary_directory, "cursor.png")
                completed = subprocess.run(
                    [
                        "/usr/bin/sips",
                        "-z",
                        str(requested_size),
                        str(requested_size),
                        "-s",
                        "format",
                        "png",
                        source,
                        "--out",
                        temporary,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    stdin=subprocess.DEVNULL,
                )
                if completed.returncode != 0:
                    raise OSError(f"sips exited {completed.returncode}")
                _validate_cursor_raster(temporary, requested_size)
                os.replace(temporary, output)
                return output
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            stderr = (getattr(completed, "stderr", "") or "").strip() or "<empty>"
            attempt_details.append(
                f"attempt {attempt}/2: {error}; stderr={stderr}"
            )
            if attempt == 1:
                time.sleep(0.05)

    try:
        with tempfile.TemporaryDirectory(
            prefix=f".{Path(output).name}.tmp-appkit-",
            dir=temporary_root,
        ) as temporary_directory:
            temporary = os.path.join(temporary_directory, "cursor.png")
            _rasterize_cursor_with_appkit(source, temporary, requested_size)
            _validate_cursor_raster(temporary, requested_size)
            os.replace(temporary, output)
            return output
    except Exception as error:
        # PyObjC can raise bridge-specific Exception subclasses; preserve all
        # framework diagnostics at this single fail-closed boundary.
        attempt_details.append(
            f"AppKit fallback: {type(error).__name__}: {error}"
        )

    raise CursorRasterError(
        f"cursor rasterization failed for {source} after 2 sips attempts: "
        + " | ".join(attempt_details)
    )
