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
        status = cua.driver_status()
        perms = status.get("permissions", {})
        if not status.get("daemon", {}).get("running"):
            raise AssertionError("daemon not running")
        if not perms.get("accessibility") or not perms.get("screen_recording"):
            raise AssertionError(f"permissions incomplete: {perms}")
        return {"daemon": True, "accessibility": True, "screen_recording": True}

    def finder_state():
        cua.launch_or_activate("Finder")
        pid, window_id, name, error = cua.resolve_app("Finder")
        if error:
            raise AssertionError(error)
        tree = cua._native_ax_snapshot(pid, max_elements=120, window_id=window_id)
        clickable = cua.find_clickable_index(tree, "Downloads")
        state = cua.app_state(
            name or "Finder",
            pid,
            window_id,
            max_elements=120 if clickable is None else 40,
            include_screenshot=True,
            prepare_foreground=clickable is None,
        )
        if not state.get("ok") or not state.get("signals", {}).get(
            "app_content_available"
        ):
            raise AssertionError(state)
        screenshot = state.get("screenshot") or {}
        path = Path(screenshot.get("path", ""))
        if not path.is_file() or path.stat().st_size < 1024:
            raise AssertionError(f"invalid screenshot: {path}")
        if clickable is None:
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

    def operator_visibility():
        controller = cua._operator_ui()
        running = controller.status()
        if not running.get("service", {}).get("installed"):
            installed = controller.install_service()
            if not installed.get("ok"):
                raise AssertionError(installed)
            running = controller.status()
        state = running.get("state", {})
        screenshot = Path(state.get("screenshot_path") or "")
        if not running.get("running") or state.get("app") != "Calculator":
            raise AssertionError(running)
        if not running.get("service", {}).get("running"):
            raise AssertionError(f"launchd service is not running: {running}")
        signing = running.get("signing", {})
        if not signing.get("ok") or signing.get("mode") != "identity":
            raise AssertionError(f"operator is not identity-signed: {signing}")
        expected_cursor = Path(cua.cursor_raster_path())
        if (
            not state.get("cursor_visible")
            or Path(state.get("cursor_image_path") or "") != expected_cursor
            or not HERMES_CURSOR.is_file()
        ):
            raise AssertionError(f"PiP cursor state missing: {state}")
        if not screenshot.is_file():
            raise AssertionError(f"operator screenshot missing: {screenshot}")

        from Quartz import (
            CGWindowListCopyWindowInfo,
            kCGNullWindowID,
            kCGWindowListOptionOnScreenOnly,
        )

        def operator_windows():
            operator_pid = running.get("service", {}).get("pid") or running.get("pid")
            return [
                window
                for window in CGWindowListCopyWindowInfo(
                    kCGWindowListOptionOnScreenOnly, kCGNullWindowID
                )
                if int(window.get("kCGWindowOwnerPID", 0)) == int(operator_pid or 0)
            ]

        def preview_windows():
            return [
                window
                for window in operator_windows()
                if (window.get("kCGWindowName") or "").startswith("macos-cua — ")
                and int(window.get("kCGWindowLayer", 0)) == 3
            ]

        def wait_for_visibility(visible, timeout=3):
            deadline = time.monotonic() + timeout
            windows = preview_windows()
            while bool(windows) != visible and time.monotonic() < deadline:
                time.sleep(0.1)
                windows = preview_windows()
            return windows

        prior_pip_visibility = bool(running.get("pip_visible"))
        controller.set_pip_visible(True)

        windows = wait_for_visibility(True)
        preview = next(
            (
                window
                for window in windows
                if "Calculator" in (window.get("kCGWindowName") or "")
                and int(window.get("kCGWindowLayer", 0)) >= 3
            ),
            None,
        )
        if preview is None:
            raise AssertionError(f"PiP window not visible: {windows}")

        script = (
            'tell application "System Events" to tell process '
            '"macos-cua-operator" to get {title, help, description} of '
            "menu bar item 1 of menu bar 1"
        )
        menu = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        )
        if menu.returncode != 0 or "Calculator" not in menu.stdout:
            raise AssertionError(menu.stderr or menu.stdout)

        # Harness identity is runtime metadata, not a reason to depend on a
        # harness-specific installation path. Exercise Cursor attribution
        # through the canonical app-agnostic skill owner.
        cursor_script = OPERATOR
        cursor_env = dict(os.environ, MACOS_CUA_HARNESS="Cursor")
        process = subprocess.run(
            [
                sys.executable,
                str(cursor_script),
                "update",
                "--app",
                "Calculator",
                "--pid",
                str(state["pid"]),
                "--window-id",
                str(state["window_id"]),
                "--screenshot",
                str(screenshot),
                "--status",
                "complete",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            stdin=subprocess.DEVNULL,
            env=cursor_env,
        )
        if process.returncode != 0:
            raise AssertionError(process.stderr or process.stdout)
        cursor_update = json.loads(process.stdout)
        if cursor_update.get("state", {}).get("harness") != "Cursor":
            raise AssertionError(cursor_update)
        time.sleep(0.5)
        menu_cursor = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        )
        if "Cursor" not in menu_cursor.stdout:
            raise AssertionError(menu_cursor.stderr or menu_cursor.stdout)

        operator_pid, operator_window, _, operator_error = cua.resolve_app(
            "macos-cua Operator"
        )
        if operator_error:
            raise AssertionError(operator_error)
        operator_ax = cua.snapshot(operator_pid, operator_window, max_elements=40)
        panel_controls = {
            element.get("label")
            for element in operator_ax.get("elements", [])
            if element.get("label")
        }
        if not {"Hide", "Refresh", "End"}.issubset(panel_controls):
            raise AssertionError(f"PiP controls missing: {sorted(panel_controls)}")

        controller.set_pip_visible(False)
        hidden = wait_for_visibility(False)
        if hidden:
            raise AssertionError(f"PiP remained visible after Hide: {hidden}")
        controller.set_pip_visible(True)
        shown = wait_for_visibility(True)
        if not shown:
            raise AssertionError("PiP did not return after Show")
        if not prior_pip_visibility:
            controller.set_pip_visible(False)
            wait_for_visibility(False)
        old_pid = running["pid"]
        os.kill(old_pid, signal.SIGKILL)
        restarted = None
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            candidate = controller._service_status()
            if candidate.get("running") and candidate.get("pid") != old_pid:
                restarted = candidate
                break
            time.sleep(0.2)
        if restarted is None:
            raise AssertionError("launchd did not self-heal the operator after SIGKILL")
        return {
            "pid": restarted["pid"],
            "panel": preview.get("kCGWindowName"),
            "layer": preview.get("kCGWindowLayer"),
            "menu": menu_cursor.stdout.strip(),
            "panel_controls": sorted(panel_controls),
            "menu_toggle": True,
            "launchd_self_heal": {"old_pid": old_pid, "new_pid": restarted["pid"]},
            "signing": signing,
            "cursor_link": str(CURSOR_LINK),
        }

    timed("daemon and permissions", permissions)
    timed("Finder AX plus screenshot and label resolution", finder_state)
    timed("Calculator comprehensive asserted plan", calculator_plan)
    timed("TextEdit native substring selection", textedit_selection)
    timed("operator PiP menu bar and Cursor link", operator_visibility)
    cua.operator_update(status="idle", active=False, message="Validation complete")
    cua.call_driver(
        "set_agent_cursor_enabled", {"enabled": False, "session": cua.CUA_SESSION}
    )
    cua.call_driver("end_session", {"session": cua.CUA_SESSION})
    return checks
