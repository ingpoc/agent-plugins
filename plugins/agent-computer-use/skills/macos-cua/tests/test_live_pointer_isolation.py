#!/usr/bin/env python3
"""Live proof that macos-cua uses the Hermes agent cursor, not the user pointer."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import time

from PIL import Image
from Quartz import (
    CGEventCreate,
    CGEventGetLocation,
    CGWindowListCopyWindowInfo,
    kCGNullWindowID,
    kCGWindowListOptionOnScreenOnly,
)


ROOT = Path(__file__).parents[1]
CLI = ROOT / "scripts" / "macos-cua.py"
WORKFLOW = ROOT / "scripts" / "workflow.py"
CURSOR_ASSET = ROOT / "assets" / "pointer-shape-animated.svg"
REPORT = Path.home() / ".cache/macos-cua/live-pointer-isolation.json"
OVERLAY_CAPTURE = Path.home() / ".cache/macos-cua/live-pointer-overlay.png"

SPEC = importlib.util.spec_from_file_location("macos_cua", CLI)
MACOS_CUA = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MACOS_CUA)


def invoke(*args: str, timeout: int = 60) -> dict:
    completed = subprocess.run(
        ["python3", str(CLI), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"macos-cua {' '.join(args)} failed ({completed.returncode}): "
            f"{completed.stderr or completed.stdout}"
        )
    return json.loads(completed.stdout)


def hardware_pointer() -> tuple[float, float]:
    point = CGEventGetLocation(CGEventCreate(None))
    return float(point.x), float(point.y)


def check(condition: bool, label: str, detail=None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"PASS: {label}")


def visible_cursor_overlay(
    operator_pid: int, expected: dict, timeout: float = 8.0
) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        windows = CGWindowListCopyWindowInfo(
            kCGWindowListOptionOnScreenOnly, kCGNullWindowID
        )
        for window in windows:
            if window.get("kCGWindowOwnerName") != "macos-cua Operator":
                continue
            if int(window.get("kCGWindowOwnerPID", 0)) != operator_pid:
                continue
            bounds = dict(window.get("kCGWindowBounds") or {})
            width = round(float(bounds.get("Width", 0)))
            height = round(float(bounds.get("Height", 0)))
            layer = int(window.get("kCGWindowLayer", 0))
            if layer >= 100 and width == 210 and height == 90:
                candidate = {
                    "window_id": int(window["kCGWindowNumber"]),
                    "layer": layer,
                    "bounds": {key: float(value) for key, value in bounds.items()},
                }
                last = candidate
                error = max(
                    abs(candidate["bounds"]["X"] - expected["x"]),
                    abs(candidate["bounds"]["Y"] - expected["y"]),
                )
                if error <= 1:
                    return candidate
        time.sleep(0.1)
    raise AssertionError(
        f"signed operator cursor overlay did not converge: expected={expected}, last={last}"
    )


def main() -> None:
    preflight = subprocess.run(
        ["python3", str(WORKFLOW), "preflight"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )
    check(
        preflight.returncode == 0,
        "preflight passes",
        preflight.stderr or preflight.stdout,
    )
    check(Path(MACOS_CUA.CURSOR_ICON).resolve() == CURSOR_ASSET.resolve(),
          "macos-cua resolves its portable cursor asset")
    operator = MACOS_CUA._operator_ui().status()
    operator_pid = int(
        (operator.get("service") or {}).get("pid") or operator.get("pid") or 0
    )
    check(operator_pid > 0, "signed operator service PID resolves", operator)

    before = hardware_pointer()
    pid, window_id, app_name, error = MACOS_CUA.resolve_app("Calculator")
    check(error is None, "Calculator resolves for cursor tracking", error)
    tracking = []
    first = None
    overlay = None
    for choices in (("All Clear", "Clear"), ("Equals",)):
        label = None
        for _ in range(3):
            snapshot = MACOS_CUA._native_ax_snapshot(
                pid, max_elements=80, window_id=window_id
            )
            labels = {element.get("label") for element in snapshot.get("elements", [])}
            label = next((candidate for candidate in choices if candidate in labels), None)
            if label is not None:
                break
            time.sleep(0.4)
        check(label is not None, "fresh state exposes cursor target", choices)
        click = MACOS_CUA.click_label_pointer(
            pid,
            window_id,
            label,
            max_elements=80,
            snapshot_data=snapshot,
            app_name=app_name,
        )
        check(click.get("ok") is True, f"cursor-synchronized click reaches {label}", click)
        check((click.get("move") or {}).get("sync", {}).get("ok") is True,
              f"signed overlay acknowledges {label}", click.get("move"))
        expected = {
            "x": click["coords"]["x"] - 15,
            "y": click["coords"]["y"] - 14,
        }
        overlay = visible_cursor_overlay(operator_pid, expected)
        error = {
            "x": overlay["bounds"]["X"] - expected["x"],
            "y": overlay["bounds"]["Y"] - expected["y"],
        }
        check(max(abs(error["x"]), abs(error["y"])) <= 1,
              f"visible cursor tracks {label} within one pixel", error)
        raw_path = MACOS_CUA._default_screenshot_path(app_name)
        captured = MACOS_CUA._capture_native_window_region(
            snapshot, pid, window_id, raw_path
        )
        annotated = MACOS_CUA.annotate_cursor_screenshot(
            raw_path,
            click["cursor_normalized"]["x"],
            click["cursor_normalized"]["y"],
        )
        proof = {
            "cursor_included": bool(annotated and annotated.get("ok")),
            "cursor": click["cursor_normalized"],
            "path": (annotated or {}).get("path"),
            "width": (captured or {}).get("screenshot_width"),
            "height": (captured or {}).get("screenshot_height"),
        }
        check(
            proof.get("cursor_included") is True,
            f"grounded screenshot includes the cursor at {label}",
            proof,
        )
        proof_cursor = proof.get("cursor") or {}
        proof_error = {
            "x": (float(proof_cursor.get("x", -1)) - click["cursor_normalized"]["x"])
            * float(proof.get("width") or 0),
            "y": (float(proof_cursor.get("y", -1)) - click["cursor_normalized"]["y"])
            * float(proof.get("height") or 0),
        }
        check(
            max(abs(proof_error["x"]), abs(proof_error["y"])) <= 1,
            f"screenshot cursor hotspot matches {label} within one pixel",
            proof_error,
        )
        proof_path = Path(proof["path"])
        check(proof_path.is_file(), f"cursor screenshot for {label} is materialized")
        with Image.open(proof_path).convert("RGB") as proof_image:
            target_x = round(click["cursor_normalized"]["x"] * proof_image.width)
            target_y = round(click["cursor_normalized"]["y"] * proof_image.height)
            cyan = 0
            for px in range(max(0, target_x - 6), min(proof_image.width, target_x + 7)):
                for py in range(max(0, target_y - 6), min(proof_image.height, target_y + 7)):
                    red, green, blue = proof_image.getpixel((px, py))
                    if blue > 150 and green > 100 and red < 160:
                        cyan += 1
        check(cyan > 0, f"screenshot shows the cyan target ring at {label}", cyan)
        tracking.append({
            "label": label,
            "expected": expected,
            "actual": {
                "x": overlay["bounds"]["X"],
                "y": overlay["bounds"]["Y"],
            },
            "error": error,
            "sync": click["move"]["sync"],
            "proof_png": str(proof_path),
            "proof_error": proof_error,
        })
        first = first or click

    final_state = MACOS_CUA.app_state(
        app_name, pid, window_id, max_elements=80, include_screenshot=True
    )
    proof = final_state["screenshot"]
    OVERLAY_CAPTURE.parent.mkdir(parents=True, exist_ok=True)
    captured = subprocess.run(
        ["screencapture", "-x", "-l", str(overlay["window_id"]), str(OVERLAY_CAPTURE)],
        capture_output=True,
        text=True,
        timeout=20,
        stdin=subprocess.DEVNULL,
    )
    after = hardware_pointer()

    check(first.get("method") == "agent-cursor-glide+native-axpress-fallback",
          "click uses the native Accessibility path", first)
    check((first.get("result") or {}).get("path") == "native_ax",
          "click dispatch is native Accessibility", first)
    check(proof.get("cursor_included") is True,
          "proof PNG contains the Hermes cursor", proof)
    check(Path(proof["path"]).is_file(), "proof PNG is materialized", proof)
    check(captured.returncode == 0 and OVERLAY_CAPTURE.is_file(),
          "visible operator cursor window is captured", captured.stderr)
    logical_size = (
        round(overlay["bounds"]["Width"]),
        round(overlay["bounds"]["Height"]),
    )
    check(logical_size == (210, 90),
          "operator cursor overlay has deterministic logical bounds", logical_size)
    image = Image.open(OVERLAY_CAPTURE).convert("RGBA")
    check(image.width >= 180 and image.height >= 60,
          "captured overlay retains visible cursor and label", image.size)
    pointer_region = image.crop((0, 0, 70, 70))
    cyan_pixels = sum(
        1 for red, green, blue, alpha in pointer_region.get_flattened_data()
        if alpha > 20 and green > red + 35 and blue > green + 20
    )
    check(cyan_pixels >= 20, "on-screen pointer contains Hermes cyan glow", cyan_pixels)
    source = CLI.read_text()
    check("CGWarpMouseCursorPosition" not in source,
          "no hardware-pointer warp primitive exists")
    check("physical_mouse_look" not in source,
          "no physical-pointer command exists")
    delta = {"x": after[0] - before[0], "y": after[1] - before[1]}
    report = {
        "ok": True,
        "cursor_asset": str(CURSOR_ASSET),
        "click_method": first["method"],
        "click_path": first["result"]["path"],
        "proof_png": proof["path"],
        "overlay": overlay,
        "overlay_capture": str(OVERLAY_CAPTURE),
        "overlay_cyan_pixels": cyan_pixels,
        "tracking": tracking,
        "hardware_pointer_before": before,
        "hardware_pointer_after": after,
        "hardware_pointer_delta": delta,
        "hardware_pointer_note": (
            "Coordinates are diagnostic only because the operator may move the "
            "independent hardware pointer during the agent action."
        ),
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({**report, "report": str(REPORT)}, indent=2))


if __name__ == "__main__":
    try:
        main()
    finally:
        subprocess.run(
            ["python3", str(CLI), "operator", "stop"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=20,
            stdin=subprocess.DEVNULL,
        )
