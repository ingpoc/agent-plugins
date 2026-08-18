#!/usr/bin/env python3
"""Live parity gate for macos-cua against the bundled Computer Use contract.

This test only manipulates Calculator and a uniquely named temporary TextEdit
document. It restores the document before closing and never saves user data.
"""

from __future__ import annotations

import json
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time


ROOT = Path(__file__).parents[1]
CLI = ROOT / "scripts" / "macos-cua.py"
WORKFLOW = ROOT / "scripts" / "workflow.py"
DRAG_FIXTURE = ROOT / "tests" / "fixtures" / "drag_fixture.swift"
ORIGINAL_TEXT = "\n".join(["alpha beta gamma", *[f"line {n}" for n in range(2, 121)]])
SPEC = importlib.util.spec_from_file_location("macos_cua_live", CLI)
MACOS_CUA = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MACOS_CUA)


def invoke(*arguments: str, timeout: int = 40) -> dict:
    completed = subprocess.run(
        ["python3", str(CLI), *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"macos-cua {' '.join(arguments)} failed ({completed.returncode}): "
            f"{completed.stderr.strip()}\n{completed.stdout.strip()}"
        )
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise AssertionError(
            f"macos-cua {' '.join(arguments)} returned non-JSON: {completed.stdout}"
        ) from error
    if isinstance(result, dict) and (result.get("error") or result.get("ok") is False):
        raise AssertionError(f"macos-cua {' '.join(arguments)}: {result}")
    return result


def check(condition: bool, label: str, detail=None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"  [PASS] {label}" + (f" — {detail}" if detail is not None else ""))


def fresh_text_area(
    pid=None, window_id=None, *, publish=False
) -> tuple[dict, dict, dict]:
    if pid is not None and window_id is not None:
        if publish:
            state = MACOS_CUA.app_state(
                "TextEdit", pid, window_id, max_elements=160, include_screenshot=True
            )
        else:
            raw = MACOS_CUA._native_ax_snapshot(
                pid, max_elements=160, window_id=window_id
            )
            roles = {element.get("role") for element in raw.get("elements", [])}
            if "AXWindow" not in roles or "AXTextArea" not in roles:
                raw = MACOS_CUA.snapshot(pid, window_id, max_elements=160)
            elements = MACOS_CUA._state_elements(raw.get("elements", []))
            state = {
                "elements": elements,
                "text": MACOS_CUA._state_text("TextEdit", elements),
            }
    else:
        state = invoke("state", "TextEdit", "--no-screenshot")
    if not isinstance(state.get("elements"), list):
        raise AssertionError(f"TextEdit state has no accessibility elements: {state}")
    window = next(e for e in state["elements"] if e.get("role") == "AXWindow")
    area = next(e for e in state["elements"] if e.get("role") == "AXTextArea")
    return state, window, area


def textedit_pids() -> set[int]:
    completed = subprocess.run(
        ["pgrep", "-x", "TextEdit"], capture_output=True, text=True
    )
    return {
        int(line) for line in completed.stdout.splitlines()
        if line.strip().isdigit()
    }


def fixture_window(pid: int, title: str) -> int:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        candidates = MACOS_CUA._ax_window_candidates(
            pid, MACOS_CUA.list_windows().get("windows", [])
        )
        match = next((candidate for candidate in candidates if candidate.get("title") == title), None)
        if match:
            return int(match["window_id"])
        time.sleep(0.2)
    raise AssertionError(f"TextEdit fixture window not found for pid {pid}: {title}")


def accepted(result, label: str) -> dict:
    if not MACOS_CUA._accepted(result):
        raise AssertionError(f"{label}: {result}")
    check(True, label)
    return result


def hardware_pointer() -> tuple[float, float]:
    from Quartz import CGEventCreate, CGEventGetLocation

    point = CGEventGetLocation(CGEventCreate(None))
    return float(point.x), float(point.y)


def visible_window_local_point(window: dict, element: dict) -> tuple[float, float]:
    """Return the center of the element portion that is actually inside its window."""
    window_frame = window["frame"]
    element_frame = element["frame"]
    left = max(float(window_frame["x"]), float(element_frame["x"]))
    top = max(float(window_frame["y"]), float(element_frame["y"]))
    right = min(
        float(window_frame["x"] + window_frame["w"]),
        float(element_frame["x"] + element_frame["w"]),
    )
    bottom = min(
        float(window_frame["y"] + window_frame["h"]),
        float(element_frame["y"] + element_frame["h"]),
    )
    if right <= left or bottom <= top:
        raise AssertionError("text area has no visible intersection with its window")
    return (
        (left + right) / 2 - float(window_frame["x"]),
        (top + bottom) / 2 - float(window_frame["y"]),
    )


def native_text_range(pid: int, state: dict, area: dict, attribute: str) -> dict:
    resolved, error = MACOS_CUA._resolve_native_ax_element(
        pid, state, area["element_index"]
    )
    if error:
        raise AssertionError(error)
    native_area, services = resolved
    raw = MACOS_CUA._ax_value(
        native_area, getattr(services, attribute), services
    )
    ok, value = services.AXValueGetValue(
        raw, services.kAXValueCFRangeType, None
    )
    if not ok:
        raise AssertionError(f"{attribute} is unavailable")
    location = value[0] if isinstance(value, tuple) else value.location
    length = value[1] if isinstance(value, tuple) else value.length
    return {"location": int(location), "length": int(length)}


def wait_for_text_range(pid: int, window_id: int, attribute: str, predicate) -> dict:
    deadline = time.monotonic() + 2
    last = None
    while time.monotonic() < deadline:
        state, _, area = fresh_text_area(pid, window_id)
        last = native_text_range(pid, state, area, attribute)
        if predicate(last):
            return last
        time.sleep(0.05)
    raise AssertionError(f"{attribute} postcondition not observed: {last}")


def prove_drag() -> None:
    from PIL import Image

    with tempfile.TemporaryDirectory(prefix="macos-cua-drag-") as directory:
        directory_path = Path(directory)
        binary = Path(directory) / "macos-cua-drag-fixture"
        compiled = subprocess.run(
            [
                "swiftc",
                str(DRAG_FIXTURE),
                "-o",
                str(binary),
                "-framework",
                "AppKit",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        check(compiled.returncode == 0, "isolated drag fixture compiles", compiled.stderr)
        process = subprocess.Popen(
            [str(binary)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 12
            window_id = None
            while time.monotonic() < deadline and window_id is None:
                if process.poll() is not None:
                    break
                candidates = MACOS_CUA._ax_window_candidates(
                    process.pid, MACOS_CUA.list_windows().get("windows", [])
                )
                if candidates:
                    window_id = int(candidates[0]["window_id"])
                    break
                time.sleep(0.1)
            check(
                window_id is not None,
                "isolated drag fixture window resolves",
                {
                    "returncode": process.poll(),
                    "stderr": process.stderr.read() if process.poll() is not None else "",
                },
            )

            before_path = directory_path / "before.png"
            state = MACOS_CUA.app_state(
                "macos-cua Drag Fixture",
                process.pid,
                window_id,
                max_elements=80,
                include_screenshot=True,
                screenshot_out_file=str(before_path),
            )
            window = next(
                element for element in state["elements"]
                if element.get("role") == "AXWindow"
            )
            def indicator_pixels(path: Path) -> tuple[int, int]:
                pixels = Image.open(path).convert("RGB").get_flattened_data()
                red = sum(1 for r, g, b in pixels if r > 150 and g < 120 and b < 120)
                green = sum(1 for r, g, b in pixels if g > 100 and r < 160 and b < 160)
                return red, green

            before_red, before_green = indicator_pixels(before_path)
            check(
                before_red > 500 and before_green < before_red,
                "drag fixture starts with the red rendered indicator",
                {"red": before_red, "green": before_green},
            )
            slider = next(
                element for element in state["elements"]
                if element.get("role") == "AXSlider"
            )
            slider_frame = slider["frame"]
            window_frame = window["frame"]
            local_x = float(
                slider_frame["x"] - window_frame["x"] + slider_frame["w"] * 0.05
            )
            local_y = float(
                slider_frame["y"] - window_frame["y"] + slider_frame["h"] / 2
            )
            target_x = float(
                slider_frame["x"] - window_frame["x"] + slider_frame["w"] * 0.95
            )
            check(
                0 <= local_y <= window_frame["h"],
                "drag cursor path uses the live AXSlider centerline",
                {"local_y": local_y, "slider": slider_frame},
            )

            def prove_cursor_screenshot(
                point_x: float, point_y: float, path: Path, label: str
            ) -> dict:
                proof_state = MACOS_CUA.app_state(
                    "macos-cua Drag Fixture",
                    process.pid,
                    window_id,
                    max_elements=80,
                    include_screenshot=True,
                    screenshot_out_file=str(path),
                )
                proof = proof_state.get("screenshot") or {}
                check(
                    proof.get("cursor_included") is True,
                    f"{label} screenshot contains the agent cursor",
                    proof,
                )
                expected = {
                    "x": point_x / float(proof["width"]),
                    "y": point_y / float(proof["height"]),
                }
                actual = proof.get("cursor") or {}
                pixel_error = {
                    "x": (float(actual.get("x", -1)) - expected["x"])
                    * float(proof["width"]),
                    "y": (float(actual.get("y", -1)) - expected["y"])
                    * float(proof["height"]),
                }
                check(
                    max(abs(pixel_error["x"]), abs(pixel_error["y"])) <= 1,
                    f"{label} screenshot hotspot matches AXSlider within one pixel",
                    pixel_error,
                )
                proof_path = Path(proof["path"])
                check(proof_path.is_file(), f"{label} screenshot is materialized")
                with Image.open(proof_path).convert("RGB") as image:
                    target_x = round(expected["x"] * image.width)
                    target_y = round(expected["y"] * image.height)
                    cyan = 0
                    for px in range(max(0, target_x - 6), min(image.width, target_x + 7)):
                        for py in range(max(0, target_y - 6), min(image.height, target_y + 7)):
                            red, green, blue = image.getpixel((px, py))
                            if blue > 150 and green > 100 and red < 160:
                                cyan += 1
                check(cyan > 0, f"{label} screenshot ring is on the AXSlider", cyan)
                return proof

            source_move = MACOS_CUA._move_operator_cursor_to_point(
                "macos-cua Drag Fixture",
                process.pid,
                window_id,
                local_x,
                local_y,
            )
            check(source_move.get("ok") is True, "cursor reaches live slider source")
            prove_cursor_screenshot(
                local_x,
                local_y,
                directory_path / "source-cursor.png",
                "drag source",
            )
            first_drag = accepted(
                MACOS_CUA.drag(
                    process.pid,
                    window_id,
                    local_x,
                    local_y,
                    target_x,
                    local_y,
                    delivery_mode="foreground",
                    duration_ms=1000,
                    steps=40,
                    app_name="macos-cua Drag Fixture",
                ),
                "drag is accepted",
            )
            check(
                first_drag.get("path") == "native_ax_slider"
                and not first_drag.get("system_cursor_used"),
                "drag uses verified native AX slider semantics without the user cursor",
                {
                    "requested": first_drag.get("requested_value"),
                    "actual": first_drag.get("actual_value"),
                    "element": first_drag.get("element"),
                },
            )
            check(
                first_drag.get("move", {}).get("source", {}).get("sync", {}).get("ok")
                and first_drag.get("move", {}).get("destination", {}).get("sync", {}).get("ok"),
                "macos-cua cursor visibly tracks the drag trajectory",
                {
                    "source_ms": first_drag["move"]["source"]["sync"]["duration_ms"],
                    "destination_ms": first_drag["move"]["destination"]["sync"]["duration_ms"],
                },
            )
            time.sleep(0.3)
            destination_proof = prove_cursor_screenshot(
                target_x,
                local_y,
                directory_path / "destination-cursor.png",
                "drag destination",
            )
            after_red, after_green = indicator_pixels(
                Path(destination_proof["raw_path"])
            )
            check(
                after_green > 500 and after_green > after_red,
                "drag changes the isolated surface's rendered indicator",
                {
                    "before": {"red": before_red, "green": before_green},
                    "after": {"red": after_red, "green": after_green},
                },
            )
            accepted(
                MACOS_CUA.drag(
                    process.pid,
                    window_id,
                    target_x,
                    local_y,
                    local_x,
                    local_y,
                    delivery_mode="foreground",
                    duration_ms=1000,
                    steps=40,
                    app_name="macos-cua Drag Fixture",
                ),
                "second drag is accepted",
            )
            time.sleep(0.3)
            second_path = directory_path / "second.png"
            second_state = MACOS_CUA.app_state(
                "macos-cua Drag Fixture",
                process.pid,
                window_id,
                max_elements=80,
                include_screenshot=True,
                screenshot_out_file=str(second_path),
            )
            check(
                second_path.is_file(),
                "second drag screenshot is materialized",
                second_state,
            )
            second_red, second_green = indicator_pixels(second_path)
            check(
                second_red > 500 and second_red > second_green,
                "second drag independently changes the rendered indicator",
                {"red": second_red, "green": second_green},
            )
        finally:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            focus_deadline = time.monotonic() + 1.5
            while (
                MACOS_CUA._frontmost_pid() == process.pid
                and time.monotonic() < focus_deadline
            ):
                time.sleep(0.02)


def main() -> int:
    print("macos-cua live bundled Computer Use parity gate")
    preflight = subprocess.run(
        ["python3", str(WORKFLOW), "preflight"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    check(preflight.returncode == 0, "preflight passes", preflight.stderr.strip())
    preflight_json = json.loads(preflight.stdout)
    check(preflight_json.get("ready") is True, "permissions, daemon, signed operator ready")
    check(len(preflight.stdout) < 600, "healthy preflight output stays context-compact", len(preflight.stdout))

    activation = MACOS_CUA.launch_or_activate("Calculator")
    check(not activation.get("error"), "Calculator fixture is visible", activation)
    time.sleep(0.2)
    MACOS_CUA.clear_resolution_cache()
    displays = invoke("displays")
    rows = displays.get("displays") if isinstance(displays, dict) else displays
    secondary = next((display for display in rows if not display.get("main")), None)
    if secondary:
        placement = invoke(
            "ensure-display", "Calculator", "--display", secondary["name"]
        )
        check(
            placement.get("ok") is True,
            "Calculator fixture is placed on secondary display",
        )
    else:
        print(
            "  [SKIP] secondary-display placement — "
            "single-monitor runtime remains supported"
        )

    calculator = invoke("state", "Calculator", "--no-screenshot")
    deadline = time.monotonic() + 3
    while (
        not any(e.get("role") == "AXWindow" for e in calculator["elements"])
        and time.monotonic() < deadline
    ):
        time.sleep(0.1)
        calculator = invoke("state", "Calculator", "--no-screenshot")
    window = next(e for e in calculator["elements"] if e.get("role") == "AXWindow")
    frame = window["frame"]
    if secondary:
        check(
            frame["x"] >= secondary["x"],
            "Calculator remains on the secondary display",
            frame,
        )
    check("Recent Items" not in calculator["text"], "compact state excludes hidden recent items")
    labels = {element.get("label") for element in calculator["elements"]}
    clear_label = next(
        (label for label in ("All Clear", "Clear") if label in labels),
        None,
    )
    check(
        clear_label is not None,
        "fresh state exposes a valid clear action",
        sorted(label for label in labels if label),
    )

    plan = {
        "pointer": True,
        "capture": "always",
        "hide_cursor": False,
        "max_elements": 80,
        "actions": [
            {"action": "click", "label": clear_label},
            {"action": "click", "label": "7", "expect": {"text": "7"}},
        ],
        "expect": {"text": "7"},
    }
    calculation = invoke("run", "Calculator", json.dumps(plan), timeout=60)
    check(calculation["ok"], "asserted multi-step action plan passes")
    alias_plan = {
        "pointer": True,
        "capture": "failures",
        "max_elements": 80,
        "actions": [
            {"action": "click", "label": "All Clear"},
            {"action": "click", "label": "8"},
            {"action": "click", "label": "Multiply"},
            {"action": "click", "label": "8"},
            {
                "action": "click",
                "label": "Equals",
                "expect": {"text": "64", "role": "AXStaticText"},
            },
            {"action": "click", "label": "Clear"},
            {
                "action": "click",
                "label": "7",
                "expect": {"text": "7", "role": "AXStaticText"},
            },
        ],
        "expect": {"text": "7", "role": "AXStaticText"},
    }
    alias = invoke("run", "Calculator", json.dumps(alias_plan), timeout=60)
    check(alias["ok"], "Clear/All Clear aliases continue a batch after the first result")
    proof = calculation["final"]["screenshot"]
    check(proof.get("cursor_included") is True, "proof PNG contains Hermes pointer")
    check(Path(proof["path"]).is_file(), "proof PNG is materialized", proof["path"])
    check("Recent Items" not in calculation["final"]["text"], "run output remains compact")
    check("elements" not in calculation["final"], "compact run omits duplicate structured elements")
    check(
        len(json.dumps(calculation, separators=(",", ":"))) < 12000,
        "asserted run output stays within the context budget",
        len(json.dumps(calculation, separators=(",", ":"))),
    )

    operator = invoke("operator", "status")
    check(operator["running"], "menu-bar and PiP operator is running")
    check(operator["signing"].get("mode") == "identity", "operator has identity signature")
    check(operator["state"].get("cursor_visible") is True, "operator publishes live pointer state")
    check(
        Path(operator["state"]["cursor_image_path"]).name == "hermes-pointer.png",
        "signed operator uses the Hermes pointer raster",
    )
    prior_pip_visibility = operator["pip_visible"]
    invoke("operator", "show-pip")
    time.sleep(0.5)
    try:
        operator_pid, operator_window, _, operator_error = MACOS_CUA.resolve_app(
            "macos-cua Operator"
        )
        check(operator_error is None, "native operator window resolves", operator_error)
        operator_ax = MACOS_CUA._native_ax_snapshot(
            operator_pid,
            max_elements=40,
            window_id=operator_window,
        )
        check(
            not operator_ax.get("error"),
            "native operator accessibility state resolves",
            operator_ax.get("error"),
        )
        labels = {element.get("label") for element in operator_ax["elements"]}
        check({"Hide", "Refresh", "End"}.issubset(labels), "PiP exposes Hide, Refresh, and End controls")
    finally:
        if not prior_pip_visibility:
            invoke("operator", "hide-pip")

    prove_drag()

    temporary = Path(tempfile.gettempdir()) / f"macos-cua-parity-{os.getpid()}.txt"
    temporary.write_text(ORIGINAL_TEXT)
    opened = False
    fixture_pid = None
    fixture_window_id = None
    try:
        before = textedit_pids()
        subprocess.run(["open", "-na", "TextEdit", str(temporary)], check=True)
        opened = True
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and fixture_pid is None:
            added = textedit_pids() - before
            if added:
                fixture_pid = max(added)
                break
            time.sleep(0.2)
        check(fixture_pid is not None, "isolated TextEdit process starts")
        fixture_window_id = fixture_window(fixture_pid, temporary.name)
        state, text_window, area = fresh_text_area(
            fixture_pid, fixture_window_id, publish=True
        )
        check(text_window.get("label") == temporary.name, "isolated TextEdit fixture is controlled")
        operator = MACOS_CUA._operator_ui().status()
        check(
            operator["state"].get("app") == "TextEdit"
            and operator["state"].get("raw_screenshot_path")
            == state["screenshot"]["raw_path"],
            "PiP preview follows the currently controlled app",
            operator["state"].get("app"),
        )
        index = area["element_index"]

        accepted(
            MACOS_CUA.set_value(fixture_pid, fixture_window_id, index, "alpha parity omega"),
            "set_value is accepted",
        )
        state, _, area = fresh_text_area(fixture_pid, fixture_window_id)
        check(area.get("value") == "alpha parity omega", "set_value changes editable content")
        index = area["element_index"]
        selection = MACOS_CUA.select_text_action(
            fixture_pid, state, index, "parity", prefix="alpha ", suffix=" omega"
        )
        check(selection.get("verified_range", {}).get("length") == 6, "select_text verifies native selection")
        accepted(
            MACOS_CUA.type_text(fixture_pid, fixture_window_id, None, "BETA"),
            "type_text is accepted",
        )
        _, _, area = fresh_text_area(fixture_pid, fixture_window_id)
        check(area.get("value") == "alpha BETA omega", "type_text replaces selected text")
        accepted(
            MACOS_CUA.press_key(fixture_pid, fixture_window_id, "cmd+a", "background"),
            "press_key is accepted",
        )
        state, _, area = fresh_text_area(fixture_pid, fixture_window_id)
        resolved, selection_error = MACOS_CUA._resolve_native_ax_element(
            fixture_pid, state, area["element_index"]
        )
        check(selection_error is None, "selected text area resolves natively", selection_error)
        native_area, services = resolved
        selected = MACOS_CUA._ax_value(
            native_area, services.kAXSelectedTextRangeAttribute, services
        )
        selected_ok, selected_range = services.AXValueGetValue(
            selected, services.kAXValueCFRangeType, None
        )
        selected_location = (
            selected_range[0] if isinstance(selected_range, tuple) else selected_range.location
        )
        selected_length = (
            selected_range[1] if isinstance(selected_range, tuple) else selected_range.length
        )
        check(
            selected_ok
            and int(selected_location) == 0
            and int(selected_length) == len(area.get("value") or ""),
            "press_key selects the complete document",
            {"location": selected_location, "length": selected_length},
        )

        index = area["element_index"]
        menu = MACOS_CUA.perform_action(
            fixture_pid, fixture_window_id, index, "showmenu", snapshot_data=state
        )
        check(
            menu.get("path") == "native_ax",
            "advertised secondary action uses native AX",
            menu,
        )
        time.sleep(0.2)
        menu_state, _, _ = fresh_text_area(fixture_pid, fixture_window_id)
        check("Copy" in menu_state["text"], "opened menus disclose their visible items on demand")
        accepted(
            MACOS_CUA.press_key(fixture_pid, fixture_window_id, "Escape", "foreground"),
            "menu dismiss key is accepted",
        )
        deadline = time.monotonic() + 3
        dismissed_state, _, _ = fresh_text_area(fixture_pid, fixture_window_id)
        while "Copy" in dismissed_state["text"] and time.monotonic() < deadline:
            time.sleep(0.05)
            dismissed_state, _, _ = fresh_text_area(fixture_pid, fixture_window_id)
        check("Copy" not in dismissed_state["text"], "menu dismiss key closes the visible menu")

        state, text_window, area = fresh_text_area(fixture_pid, fixture_window_id)
        accepted(
            MACOS_CUA.set_value(
                fixture_pid,
                fixture_window_id,
                area["element_index"],
                ORIGINAL_TEXT,
            ),
            "long navigation fixture is restored",
        )
        state, text_window, area = fresh_text_area(fixture_pid, fixture_window_id)
        check(area.get("value") == ORIGINAL_TEXT, "navigation fixture is readable")
        scroll_x = float(area["frame"]["x"] - text_window["frame"]["x"] + 30)
        scroll_y = float(area["frame"]["y"] - text_window["frame"]["y"] + 50)
        visible_before = native_text_range(
            fixture_pid, state, area, "kAXVisibleCharacterRangeAttribute"
        )
        accepted(
            MACOS_CUA.scroll(
                fixture_pid,
                fixture_window_id,
                "down",
                1,
                by="page",
                x=scroll_x,
                y=scroll_y,
                delivery_mode="foreground",
            ),
            "scroll is accepted",
        )
        visible_after = wait_for_text_range(
            fixture_pid,
            fixture_window_id,
            "kAXVisibleCharacterRangeAttribute",
            lambda value: value["location"] > visible_before["location"],
        )
        check(
            visible_after["location"] > visible_before["location"],
            "scroll advances the visible document range",
            {"before": visible_before, "after": visible_after},
        )

        state, text_window, area = fresh_text_area(fixture_pid, fixture_window_id)
        selected_before_double = native_text_range(
            fixture_pid, state, area, "kAXSelectedTextRangeAttribute"
        )
        double_result = accepted(
            MACOS_CUA.double_click(
                fixture_pid,
                fixture_window_id,
                element_index=area["element_index"],
                delivery_mode="foreground",
                snapshot_data=state,
            ),
            "double click is accepted",
        )
        try:
            selected_after_double = wait_for_text_range(
                fixture_pid,
                fixture_window_id,
                "kAXSelectedTextRangeAttribute",
                lambda value: value != selected_before_double and value["length"] > 0,
            )
        except AssertionError as error:
            raise AssertionError(f"{error}; double_click={double_result}") from error
        check(
            0 < selected_after_double["length"] < len(ORIGINAL_TEXT),
            "double click selects a bounded text range",
            selected_after_double,
        )

        state, text_window, area = fresh_text_area(
            fixture_pid, fixture_window_id, publish=True
        )
        local_x, local_y = visible_window_local_point(text_window, area)
        selection = MACOS_CUA.select_text_action(
            fixture_pid,
            state,
            area["element_index"],
            "beta",
            prefix="alpha ",
            suffix=" gamma",
        )
        selected_before_click = selection["verified_range"]
        click_result = accepted(
            MACOS_CUA.click_point(
                fixture_pid,
                fixture_window_id,
                local_x,
                local_y,
                delivery_mode="foreground",
                app_name="TextEdit",
            ),
            "coordinate click is accepted",
        )
        try:
            selected_after_click = wait_for_text_range(
                fixture_pid,
                fixture_window_id,
                "kAXSelectedTextRangeAttribute",
                lambda value: value["length"] == 0,
            )
        except AssertionError as error:
            raise AssertionError(f"{error}; click_point={click_result}") from error
        check(
            selected_after_click["length"] == 0
            and selected_after_click != selected_before_click,
            "coordinate click collapses the selected text range",
            {
                "before": selected_before_click,
                "after": selected_after_click,
                "point": {"x": local_x, "y": local_y},
                "path": click_result.get("path", "window-local"),
            },
        )

        selected_before_hold = selected_after_click
        accepted(
            MACOS_CUA.hold_key(
                fixture_pid,
                "right",
                0.1,
                window_id=fixture_window_id,
                foreground=True,
            ),
            "foreground hold-key is accepted",
        )
        selected_after_hold = wait_for_text_range(
            fixture_pid,
            fixture_window_id,
            "kAXSelectedTextRangeAttribute",
            lambda value: value["length"] == 0,
        )
        check(
            selected_after_hold["length"] == 0
            and selected_after_hold["location"]
            > selected_before_hold["location"],
            "foreground hold-key changes the native selection",
            {"before": selected_before_hold, "after": selected_after_hold},
        )

        _, _, area = fresh_text_area(fixture_pid, fixture_window_id)
        accepted(
            MACOS_CUA.right_click(fixture_pid, fixture_window_id, area["element_index"]),
            "right click is accepted",
        )
        time.sleep(0.2)
        context, _, _ = fresh_text_area(fixture_pid, fixture_window_id)
        check("Copy" in context["text"], "right click opens a visible context menu")
        MACOS_CUA.press_key(fixture_pid, fixture_window_id, "Escape", "foreground")

        _, _, area = fresh_text_area(fixture_pid, fixture_window_id)
        accepted(
            MACOS_CUA.set_value(
                fixture_pid, fixture_window_id, area["element_index"], ORIGINAL_TEXT
            ),
            "temporary document restore is accepted",
        )
        deadline = time.monotonic() + 3
        _, _, area = fresh_text_area(fixture_pid, fixture_window_id)
        while area.get("value") != ORIGINAL_TEXT and time.monotonic() < deadline:
            time.sleep(0.05)
            _, _, area = fresh_text_area(fixture_pid, fixture_window_id)
        check(area.get("value") == ORIGINAL_TEXT, "temporary document is restored before closeout")
        MACOS_CUA.press_key(fixture_pid, fixture_window_id, "cmd+w", "foreground")
        time.sleep(0.5)
        opened = False
    finally:
        if opened and fixture_pid is not None and fixture_window_id is not None:
            try:
                MACOS_CUA.press_key(fixture_pid, fixture_window_id, "Escape", "foreground")
                _, _, area = fresh_text_area(fixture_pid, fixture_window_id)
                MACOS_CUA.set_value(
                    fixture_pid, fixture_window_id, area["element_index"], ORIGINAL_TEXT
                )
                MACOS_CUA.press_key(fixture_pid, fixture_window_id, "cmd+w", "foreground")
            except Exception:
                pass
        if fixture_pid is not None:
            try:
                os.kill(fixture_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        temporary.unlink(missing_ok=True)

    closeout = subprocess.run(
        ["python3", str(WORKFLOW), "closeout"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=40,
    )
    check(closeout.returncode == 0, "closeout succeeds", closeout.stderr.strip())
    closeout_json = json.loads(closeout.stdout)
    check(closeout_json.get("success") is True, "cursor sessions end and operator becomes idle")
    check(closeout_json.get("daemon_ready") is True, "driver remains ready after closeout")
    print("\n=== SUMMARY ===")
    print("  [PASS] live macos-cua parity primitives, operator UI, cursor, display, and cleanup")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
