# Runtime dependencies are supplied by validate-macos-cua.py.
# ruff: noqa: F401, F821
"""Opt-in live native-app acceptance scenarios for macos-cua."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time


def doctor_ready(cua):
    """Gate on the shared read-only doctor (CUAService and TCC)."""
    report = cua.doctor()
    if report.get("ok") is not True:
        raise AssertionError(f"doctor failed: {report}")
    return {"doctor": "ok"}


def live_checks(*, progress=False):
    cua = load_main()
    checks = []

    def timed(name, fn):
        started = time.monotonic()
        if progress:
            print(f"START {name}", file=sys.stderr, flush=True)
        try:
            detail = fn()
            checks.append(
                {
                    "name": name,
                    "ok": True,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                    "detail": detail,
                }
            )
            if progress:
                print(f"PASS {name}", file=sys.stderr, flush=True)
        except Exception as error:  # acceptance harness must report every gate
            checks.append(
                {
                    "name": name,
                    "ok": False,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                    "detail": str(error),
                }
            )
            if progress:
                print(f"FAIL {name}: {error}", file=sys.stderr, flush=True)

    def permissions():
        return doctor_ready(cua)

    def finder_state():
        cua.launch_or_activate("Finder")
        pid, window_id, name, error = cua.resolve_app("Finder")
        if error:
            raise AssertionError(error)
        tree = cua._native_ax_snapshot(pid, max_elements=240, window_id=window_id)
        clickable = cua.find_clickable_index(tree, "Downloads")
        if clickable is not None:
            return {
                "window_id": window_id,
                "element_count": tree.get("element_count", 0),
                "actionable_label_resolution": True,
            }
        state = cua.app_state(
            name or "Finder",
            pid,
            window_id,
            max_elements=240,
            include_screenshot=True,
            prepare_foreground=True,
        )
        if not state.get("ok") or not state.get("signals", {}).get(
            "app_content_available"
        ):
            raise AssertionError(state)
        screenshot = state.get("screenshot") or {}
        path = Path(screenshot.get("path", ""))
        if not path.is_file() or path.stat().st_size < 1024:
            raise AssertionError(f"invalid screenshot: {path}")
        clickable = cua.find_clickable_index(state, "Downloads")
        if clickable is None:
            raise AssertionError(
                "Finder row text was not resolved to an actionable ancestor"
            )
        return {
            "window_id": window_id,
            "element_count": state["element_count"],
            "screenshot": str(path),
            "actionable_label_resolution": True,
        }

    def finder_sidebar_click():
        cua.launch_or_activate("Finder")
        pid, window_id, name, error = cua.resolve_app("Finder")
        if error:
            raise AssertionError(error)
        app = name or "Finder"

        def title(tree):
            return next(
                (
                    str(item.get("label") or "")
                    for item in tree.get("elements") or []
                    if item.get("role") == "AXWindow"
                ),
                "",
            )

        tree = cua._native_ax_snapshot(pid, max_elements=240, window_id=window_id)
        if cua.find_clickable_index(tree, "Downloads") is None:
            raise AssertionError("Downloads row missing")
        if "Downloads" in title(tree) and cua.find_clickable_index(tree, "Recents"):
            recents = cua.click_label_pointer(
                pid, window_id, "Recents", snapshot_data=tree, app_name=app
            )
            if not recents.get("ok"):
                raise AssertionError(recents)
            time.sleep(0.4)
            tree = cua._native_ax_snapshot(pid, max_elements=240, window_id=window_id)
        click = cua.click_label_pointer(
            pid, window_id, "Downloads", snapshot_data=tree, app_name=app
        )
        time.sleep(0.4)
        after = title(cua._native_ax_snapshot(pid, max_elements=80, window_id=window_id))
        if not click.get("ok") or "Downloads" not in after:
            raise AssertionError({"click": click, "title": after})
        return {"method": click.get("method"), "title": after}

    def calculator_plan():
        cua.launch_or_activate("Calculator")
        time.sleep(0.2)
        pid, window_id, name, error = cua.resolve_app("Calculator")
        if error:
            raise AssertionError(error)
        state = cua._native_ax_snapshot(pid, max_elements=80, window_id=window_id)
        clear_label = (
            "All Clear"
            if cua.find_clickable_index(state, "All Clear") is not None
            else "Clear"
        )
        result = cua.run_actions(
            pid,
            window_id,
            {
                "pointer": True,
                "capture": "always",
                "max_elements": 50,
                "actions": [
                    {"action": "click", "label": clear_label},
                    {"action": "click", "label": "7"},
                    {"action": "click", "label": "Multiply"},
                    {"action": "click", "label": "8"},
                    {"action": "click", "label": "Equals", "expect": {"text": "56"}},
                ],
                "expect": {"text": "56"},
            },
            app_name=name or "Calculator",
        )
        if not result.get("ok"):
            failed_steps = [
                {
                    "step": step.get("step"),
                    "action": step.get("action"),
                    "accepted": step.get("accepted"),
                    "result": step.get("result"),
                    "verification": step.get("verification"),
                }
                for step in result.get("steps", [])
                if not step.get("accepted")
            ]
            raise AssertionError(
                {
                    "failed_steps": failed_steps,
                    "final_assertions": result.get("assertions"),
                    "final_text_head": result.get("final", {}).get("text", "")[:200],
                }
            )
        path = Path((result.get("final", {}).get("screenshot") or {}).get("path", ""))
        screenshot = result.get("final", {}).get("screenshot") or {}
        if not path.is_file() or path.stat().st_size < 1024:
            raise AssertionError(f"invalid final screenshot: {path}")
        if not screenshot.get("cursor_included") or not path.name.endswith(
            "-cursor.png"
        ):
            raise AssertionError(f"cursor missing from proof screenshot: {screenshot}")
        return {
            "result": "56",
            "steps": len(result["steps"]),
            "plan_duration_ms": result["duration_ms"],
            "screenshot": str(path),
            "cursor_included": True,
            "cursor_asset": str(HERMES_CURSOR),
            "exact_window_actions": True,
        }

    def textedit_selection():
        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", prefix="macos-cua-selection-", delete=False
        )
        path = Path(handle.name)
        title = path.name
        fixture_pid = None
        try:
            handle.write("alpha target omega\n")
            handle.close()
            before = {
                int(line)
                for line in subprocess.run(
                    ["pgrep", "-x", "TextEdit"], capture_output=True, text=True
                ).stdout.splitlines()
                if line.strip().isdigit()
            }
            subprocess.run(["open", "-na", "TextEdit", str(path)], check=True)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and fixture_pid is None:
                after = {
                    int(line)
                    for line in subprocess.run(
                        ["pgrep", "-x", "TextEdit"], capture_output=True, text=True
                    ).stdout.splitlines()
                    if line.strip().isdigit()
                }
                added = after - before
                if added:
                    fixture_pid = max(added)
                    break
                time.sleep(0.2)
            if fixture_pid is None:
                raise AssertionError("isolated TextEdit process did not start")
            candidates = []
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and not candidates:
                candidates = [
                    window
                    for window in cua.list_windows().get("windows", [])
                    if window.get("pid") == fixture_pid
                    and title in (window.get("title") or "")
                ]
                if not candidates:
                    time.sleep(0.2)
            if not candidates:
                raise AssertionError(f"TextEdit window not found: {title}")
            window = candidates[0]
            pid, window_id = window["pid"], window["window_id"]
            state = cua._native_ax_snapshot(
                pid, max_elements=200, window_id=window_id
            )
            element = cua.find_field_index(state, "alpha target omega")
            if element is None:
                raise AssertionError("TextEdit field not found")
            selected = cua.select_text_action(
                pid,
                state,
                element,
                "target",
                prefix="alpha ",
                suffix=" omega",
            )
            if not selected.get("ok"):
                raise AssertionError(selected)
            cua.press_key(pid, window_id, "cmd+w", "foreground")
            return {
                "selection_range": selected["range"],
                "verified_range": selected["verified_range"],
                "temporary_file": True,
            }
        finally:
            if not handle.closed:
                handle.close()
            if fixture_pid is not None:
                try:
                    os.kill(fixture_pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            path.unlink(missing_ok=True)


    timed("daemon and permissions", permissions)
    timed("Finder AX plus screenshot and label resolution", finder_state)
    timed("Finder sidebar row click changes window title", finder_sidebar_click)
    timed("Calculator comprehensive asserted plan", calculator_plan)
    timed("TextEdit native substring selection", textedit_selection)
    hidden = cua.cursor("hide")
    if hidden.get("ok") is not True:
        raise AssertionError(f"CUAService hide_agent_cursor failed: {hidden}")
    return checks
