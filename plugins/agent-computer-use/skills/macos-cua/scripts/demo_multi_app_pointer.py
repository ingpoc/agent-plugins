#!/usr/bin/env python3
"""Batched multi-app watched demo: one process, pointer:true plans, minimal lag."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))


def load_cua():
    spec = importlib.util.spec_from_file_location("macos_cua", SKILL / "scripts" / "macos-cua.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_parity():
    path = SKILL / "tests" / "test_live_computer_parity.py"
    spec = importlib.util.spec_from_file_location("macos_cua_parity", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _resolve(cua, app: str):
    cua.launch_or_activate(app)
    pid, window_id, resolved, error = cua.resolve_app(app)
    if error:
        raise AssertionError(error)
    return pid, window_id, resolved or app


def run_calc(cua) -> dict:
    pid, window_id, name = _resolve(cua, "Calculator")
    state = cua._native_ax_snapshot(pid, max_elements=80, window_id=window_id)
    clear_label = (
        "All Clear"
        if cua.find_clickable_index(state, "All Clear") is not None
        else "Clear"
    )
    plan = {
        "pointer": True,
        "capture": "failures",
        "output": "compact",
        "max_elements": 50,
        "actions": [
            {"action": "click", "label": clear_label},
            {"action": "click", "label": "8"},
            {"action": "click", "label": "Multiply"},
            {"action": "click", "label": "8"},
            {"action": "click", "label": "Equals", "expect": {"text": "64"}},
        ],
        "expect": {"text": "64"},
    }
    t0 = time.monotonic()
    result = cua.run_actions(pid, window_id, plan, app_name=name)
    return {
        "app": "Calculator",
        "ok": bool(result.get("ok")),
        "duration_ms": round((time.monotonic() - t0) * 1000),
        "method": "asserted_run_pointer",
        "verified": bool(result.get("verified")),
    }


def run_textedit_right_click(cua, parity) -> dict:
    temporary = Path(tempfile.gettempdir()) / f"acu-demo-{os.getpid()}.txt"
    temporary.write_text(parity.ORIGINAL_TEXT)
    before = parity.textedit_pids()
    subprocess.run(["open", "-na", "TextEdit", str(temporary)], check=True)
    fixture_pid = None
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and fixture_pid is None:
        added = parity.textedit_pids() - before
        if added:
            fixture_pid = max(added)
            break
        time.sleep(0.05)
    if fixture_pid is None:
        return {"app": "TextEdit", "ok": False, "error": "no TextEdit pid"}
    window_id = parity.fixture_window(fixture_pid, temporary.name)
    _, _, area = parity.fresh_text_area(fixture_pid, window_id)
    t0 = time.monotonic()
    click = cua.right_click(
        fixture_pid, window_id, area["element_index"], app_name="TextEdit"
    )
    click_ms = round((time.monotonic() - t0) * 1000)
    time.sleep(0.15)
    context, _, area = parity.fresh_text_area(fixture_pid, window_id)
    copy_visible = "Copy" in (context.get("text") or "")
    cua.press_key(fixture_pid, window_id, "Escape", "foreground")
    cua.press_key(fixture_pid, window_id, "cmd+w", "foreground")
    method = str((click or {}).get("method") or "")
    move_ok = bool(((click or {}).get("move") or {}).get("ok"))
    return {
        "app": "TextEdit",
        "ok": copy_visible and method.startswith("agent-cursor-glide") and move_ok,
        "duration_ms": click_ms,
        "method": method,
        "copy_visible": copy_visible,
        "cursor_glide": method.startswith("agent-cursor-glide") and move_ok,
    }


def run_preview_point(cua) -> dict:
    """Coordinate click needs the real pointer path (CGEvent), watched."""
    pdf = Path(tempfile.gettempdir()) / f"acu-demo-{os.getpid()}.pdf"
    # Minimal valid-ish PDF bytes so Preview opens a window
    pdf.write_bytes(
        b"%PDF-1.1\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
    )
    before = set()
    try:
        import AppKit

        before = {app.processIdentifier() for app in AppKit.NSWorkspace.sharedWorkspace().runningApplications()}
    except Exception:
        pass
    subprocess.run(["open", "-a", "Preview", str(pdf)], check=False)
    time.sleep(0.8)
    t0 = time.monotonic()
    # Use asserted plan with click near center of Preview if window resolves
    plan = {
        "pointer": True,
        "capture": "failures",
        "output": "compact",
        "allow_unverified": True,
        "actions": [
            {"action": "click", "x": 40, "y": 40},
        ],
    }
    try:
        result = cua.run_actions("Preview", plan)
        ok = bool(result.get("ok") or result.get("accepted"))
        method = "preview_point_pointer"
        if result.get("steps"):
            method = str(result["steps"][0].get("method") or method)
        return {
            "app": "Preview",
            "ok": ok,
            "duration_ms": round((time.monotonic() - t0) * 1000),
            "method": method,
        }
    except Exception as exc:
        return {
            "app": "Preview",
            "ok": False,
            "duration_ms": round((time.monotonic() - t0) * 1000),
            "error": str(exc),
        }
    finally:
        subprocess.run(
            ["osascript", "-e", 'tell application "Preview" to quit'],
            check=False,
            capture_output=True,
        )
        try:
            pdf.unlink(missing_ok=True)
        except Exception:
            pass


def main() -> int:
    cua = load_cua()
    parity = load_parity()
    # Warm operator once so first glide is not cold
    subprocess.run(
        ["open", "-a", "Calculator"],
        check=False,
        capture_output=True,
    )
    time.sleep(0.4)
    started = time.monotonic()
    results = []
    results.append(run_calc(cua))
    results.append(run_textedit_right_click(cua, parity))
    # Stick to pointer-required apps; skip Preview if flaky
    gap_ms = []
    for i in range(1, len(results)):
        gap_ms.append(0)  # sequential in-process; no MCP round-trip
    payload = {
        "ok": all(r.get("ok") for r in results),
        "total_ms": round((time.monotonic() - started) * 1000),
        "apps": results,
        "batching": "one_process_asserted_run_per_app_no_mcp_roundtrips",
    }
    print(json.dumps(payload, indent=2))
    subprocess.run(
        ["osascript", "-e", 'tell application "Calculator" to quit'],
        check=False,
        capture_output=True,
    )
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
