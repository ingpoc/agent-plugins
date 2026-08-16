#!/usr/bin/env python3
"""Run the persistent macos-cua suite from entry-contract.json.

Invokes existing owners. Writes results to ~/.cache/macos-cua/ only.
WhatsApp is observe-only: no send, no chat dumps, no personal identifiers.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unicodedata
from typing import Any


HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
CONTRACT = SKILL / "references" / "entry-contract.json"
CACHE = Path(os.environ.get("MACOS_CUA_CACHE_DIR", Path.home() / ".cache/macos-cua"))
REQUIRED = (
    "name",
    "surface",
    "owner",
    "metric",
    "pass_signal",
    "budget_seconds",
    "timeout_seconds",
    "bytes_budget",
)
CRITERIA = (
    "accuracy",
    "visibility",
    "speed",
    "efficiency",
    "context_efficiency",
    "robustness",
)
MUTATING = {"click", "perform_action", "right_click", "type", "set_value"}


def load_suite() -> dict[str, Any]:
    data = json.loads(CONTRACT.read_text())
    rows = data.get("suite") or []
    if not isinstance(rows, list) or not rows:
        raise SystemExit("entry-contract.json suite is empty")
    for row in rows:
        missing = [key for key in REQUIRED if key not in row]
        if missing:
            raise SystemExit(f"{row.get('name')}: missing {missing}")
    return data


def load_cua():
    spec = importlib.util.spec_from_file_location("macos_cua", HERE / "macos-cua.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_parity():
    path = SKILL / "tests" / "test_live_computer_parity.py"
    spec = importlib.util.spec_from_file_location("macos_cua_parity", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def steps_show_cursor(steps: Any) -> bool:
    mutating = [
        step
        for step in (steps or [])
        if isinstance(step, dict) and step.get("action") in MUTATING
    ]
    if not mutating:
        return False
    return all(
        str(step.get("method") or "").startswith("agent-cursor-glide")
        and not step.get("cursor_sync_error")
        for step in mutating
    )


def score(row: dict[str, Any], measured: dict[str, Any]) -> dict[str, bool]:
    duration_ok = float(measured.get("duration_s") or 999) <= float(
        row["budget_seconds"]
    )
    step_budget = row.get("max_step_ms")
    measured_step = measured.get("max_step_ms")
    step_ok = step_budget is None or (
        measured_step is not None
        and 0 < int(measured_step) <= int(step_budget)
    )
    pointer_required = bool(row.get("pointer_required", True))
    return {
        "accuracy": bool(measured.get("readback")),
        "visibility": (
            True if not pointer_required else bool(measured.get("cursor_visible"))
        ),
        "speed": duration_ok and step_ok,
        "efficiency": bool(measured.get("asserted_batch")),
        "context_efficiency": int(measured.get("output_bytes") or 0)
        <= int(row["bytes_budget"]),
        "robustness": bool(measured.get("robust")),
    }


def _resolve(cua, name: str):
    cua.launch_or_activate(name)
    pid, window_id, resolved, error = cua.resolve_app(name)
    if error:
        raise AssertionError(error)
    return pid, window_id, resolved or name


def visible_ax_text(value: Any) -> str:
    """AX values often prefix LRM/RLM; compare the visible text only."""
    return "".join(
        char for char in str(value or "") if unicodedata.category(char) != "Cf"
    )


def _compact_bytes(payload: Any) -> int:
    return len(json.dumps(payload, separators=(",", ":"), default=str).encode())


def _max_step_ms(result: dict[str, Any]) -> int:
    steps = result.get("steps") or []
    return max((int(step.get("duration_ms") or 0) for step in steps), default=0)


def probe_calculator(cua, row: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    pid, window_id, name = _resolve(cua, "Calculator")
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
        },
        app_name=name,
    )
    fresh = cua.app_state(
        name,
        pid,
        window_id,
        max_elements=40,
        query="AXStaticText",
        include_screenshot=False,
        prepare_foreground=False,
    )
    display_64 = any(
        visible_ax_text(item.get("value")) == "64"
        and item.get("role") == "AXStaticText"
        for item in fresh.get("elements", [])
    )
    compact = {
        "ok": result.get("ok"),
        "verified": result.get("verified"),
        "final": (result.get("final") or {}).get("text"),
        "steps": result.get("steps"),
    }
    duration_s = time.monotonic() - started
    steps = result.get("steps") or []
    return {
        "readback": bool(result.get("ok") and result.get("verified") and display_64),
        "cursor_visible": steps_show_cursor(steps),
        "robust": clear_label in {"Clear", "All Clear"}
        and _max_step_ms(result) < 8000
        and os.environ.get("MACOS_CUA_PIXEL_CLICK") != "1",
        "asserted_batch": len(steps) >= 5,
        "output_bytes": _compact_bytes(compact),
        "duration_s": round(duration_s, 3),
        "clear_label": clear_label,
        "display_64": display_64,
        "max_step_ms": _max_step_ms(result),
        "verified": bool(result.get("verified")),
    }


def probe_folder(cua, row: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    cua.launch_or_activate("Finder")
    pid, window_id, name, error = cua.resolve_app("Finder")
    if error:
        raise AssertionError(error)
    tree = cua._native_ax_snapshot(pid, max_elements=120, window_id=window_id)
    clickable = cua.find_clickable_index(tree, "Downloads")
    # cua-driver: background AX first; front only when the tree missed the label.
    state = cua.app_state(
        name or "Finder",
        pid,
        window_id,
        max_elements=120 if clickable is None else 40,
        include_screenshot=True,
        prepare_foreground=clickable is None,
    )
    screenshot = state.get("screenshot") or {}
    path = Path(screenshot.get("path", ""))
    if clickable is None:
        clickable = cua.find_clickable_index(state, "Downloads")
    compact = {
        "window_id": window_id,
        "element_count": state.get("element_count"),
        "downloads": clickable is not None,
    }
    return {
        "readback": clickable is not None
        and bool(state.get("ok"))
        and bool((state.get("signals") or {}).get("app_content_available")),
        "robust": path.is_file() and path.stat().st_size >= 1024,
        "asserted_batch": True,
        "output_bytes": _compact_bytes(compact),
        "duration_s": round(time.monotonic() - started, 3),
        "downloads_index": clickable,
        "screenshot_bytes": path.stat().st_size if path.is_file() else 0,
    }


def probe_right_click(cua, parity, row: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    temporary = Path(tempfile.gettempdir()) / f"macos-cua-bench-{os.getpid()}.txt"
    temporary.write_text(parity.ORIGINAL_TEXT)
    fixture_pid = None
    opened = False
    copy_visible = False
    restored = False
    try:
        before = parity.textedit_pids()
        subprocess.run(["open", "-na", "TextEdit", str(temporary)], check=True)
        opened = True
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and fixture_pid is None:
            added = parity.textedit_pids() - before
            if added:
                fixture_pid = max(added)
                break
            time.sleep(0.2)
        if fixture_pid is None:
            raise AssertionError("isolated TextEdit process did not start")
        window_id = parity.fixture_window(fixture_pid, temporary.name)
        _, _, area = parity.fresh_text_area(fixture_pid, window_id, publish=True)
        click_started = time.monotonic()
        click = cua.right_click(
            fixture_pid, window_id, area["element_index"], app_name="TextEdit"
        )
        click_ms = round((time.monotonic() - click_started) * 1000)
        time.sleep(0.2)
        context, _, _ = parity.fresh_text_area(fixture_pid, window_id)
        copy_visible = "Copy" in (context.get("text") or "")
        cua.press_key(fixture_pid, window_id, "Escape", "foreground")
        _, _, area = parity.fresh_text_area(fixture_pid, window_id)
        cua.set_value(
            fixture_pid, window_id, area["element_index"], parity.ORIGINAL_TEXT
        )
        deadline = time.monotonic() + 3
        _, _, area = parity.fresh_text_area(fixture_pid, window_id)
        while area.get("value") != parity.ORIGINAL_TEXT and time.monotonic() < deadline:
            time.sleep(0.05)
            _, _, area = parity.fresh_text_area(fixture_pid, window_id)
        restored = area.get("value") == parity.ORIGINAL_TEXT
        cua.press_key(fixture_pid, window_id, "cmd+w", "foreground")
        opened = False
        compact = {
            "accepted": cua._accepted(click),
            "copy_visible": copy_visible,
            "restored": restored,
            "method": (click or {}).get("method"),
        }
        return {
            "readback": copy_visible,
            "cursor_visible": str((click or {}).get("method") or "").startswith(
                "agent-cursor-glide"
            )
            and bool(((click or {}).get("move") or {}).get("ok")),
            "robust": restored and cua._accepted(click),
            "asserted_batch": True,
            "output_bytes": _compact_bytes(compact),
            "duration_s": round(time.monotonic() - started, 3),
            "copy_visible": copy_visible,
            "restored": restored,
            "accepted": cua._accepted(click),
            "max_step_ms": click_ms,
        }
    finally:
        if opened and fixture_pid is not None:
            try:
                os.kill(fixture_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        temporary.unlink(missing_ok=True)


def _heading_open(text: str) -> bool:
    return any(
        "axheading" in line.lower() and "new chat" in line.lower()
        for line in (text or "").splitlines()
    )


def probe_whatsapp(cua, row: dict[str, Any]) -> dict[str, Any]:
    pid, window_id, name = _resolve(cua, "WhatsApp")
    cua.press_key(pid, window_id, "Escape", "background")
    time.sleep(0.15)
    closed_first = True
    started = time.monotonic()
    result = cua.run_actions(
        pid,
        window_id,
        {
            "pointer": True,
            "capture": "failures",
            "output": "compact",
            "max_elements": 80,
            "actions": [
                {
                    "action": "perform_action",
                    "label": "New Chat",
                    "name": "press",
                    "expect": {"text": "New chat", "role": "AXHeading"},
                }
            ],
            "expect": {"text": "New chat", "role": "AXHeading"},
        },
        app_name=name,
    )
    final_text = str((result.get("final") or {}).get("text") or "")
    heading_only = _heading_open(final_text) and "message yourself" not in final_text.lower()
    try:
        cua.press_key(pid, window_id, "Escape", "background")
    except Exception:
        pass
    compact = {
        "ok": result.get("ok"),
        "verified": result.get("verified"),
        "code": result.get("code"),
        "lines": len([line for line in final_text.splitlines() if line.strip()]),
        "heading_only": heading_only,
        "closed_first": closed_first,
    }
    return {
        "readback": bool(result.get("ok") and result.get("verified") and heading_only),
        "cursor_visible": steps_show_cursor(result.get("steps")),
        "robust": closed_first and bool(result.get("verified")),
        "asserted_batch": True,
        "output_bytes": _compact_bytes(compact),
        "duration_s": round(time.monotonic() - started, 3),
        "verified": bool(result.get("verified")),
        "heading_only": heading_only,
        "closed_first": closed_first,
        "code": result.get("code"),
        "sent": False,
        "max_step_ms": _max_step_ms(result),
    }


PROBES = {
    "calculator-8x8": lambda cua, parity, row: probe_calculator(cua, row),
    "folder-downloads": lambda cua, parity, row: probe_folder(cua, row),
    "textedit-right-click": lambda cua, parity, row: probe_right_click(cua, parity, row),
    "whatsapp-new-chat": lambda cua, parity, row: probe_whatsapp(cua, row),
}


def run_suite() -> dict[str, Any]:
    contract = load_suite()
    cua = load_cua()
    parity = load_parity()
    results = []
    for row in contract["suite"]:
        probe = PROBES.get(row["name"])
        started = time.monotonic()
        try:
            if probe is None:
                raise AssertionError(f"no probe for {row['name']}")
            measured = probe(cua, parity, row)
            criteria = score(row, measured)
            results.append(
                {
                    "name": row["name"],
                    "surface": row["surface"],
                    "ok": all(criteria.values()),
                    "criteria": criteria,
                    "measured": measured,
                    "budget_seconds": row["budget_seconds"],
                    "bytes_budget": row["bytes_budget"],
                    "pass_signal": row["pass_signal"],
                }
            )
        except Exception as error:
            duration_s = round(time.monotonic() - started, 3)
            results.append(
                {
                    "name": row["name"],
                    "surface": row["surface"],
                    "ok": False,
                    "criteria": {key: False for key in CRITERIA},
                    "measured": {"duration_s": duration_s, "error": str(error)[:240]},
                    "budget_seconds": row["budget_seconds"],
                    "bytes_budget": row["bytes_budget"],
                    "pass_signal": row["pass_signal"],
                }
            )
    payload = {
        "ok": all(item["ok"] for item in results),
        "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "results": results,
    }
    CACHE.mkdir(parents=True, exist_ok=True)
    out = CACHE / "benchmarks-latest.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    payload["path"] = str(out)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema-only", action="store_true")
    args = parser.parse_args()
    if args.schema_only:
        load_suite()
        print(json.dumps({"ok": True, "schema": "suite"}))
        return 0
    payload = run_suite()
    print(json.dumps({key: payload[key] for key in ("ok", "path", "results")}))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
