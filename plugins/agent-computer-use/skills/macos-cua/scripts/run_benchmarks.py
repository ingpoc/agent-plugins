#!/usr/bin/env python3
"""Run the persistent macos-cua suite from entry-contract.json.

Invokes existing owners. Writes results to ~/.cache/macos-cua/ only.
WhatsApp is observe-only: no send, no chat dumps, no personal identifiers.
"""
from __future__ import annotations

import argparse
import hashlib
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
_RATING_SPEC = importlib.util.spec_from_file_location(
    "macos_cua_bench_rating", HERE / "bench_rating.py"
)
bench_rating = importlib.util.module_from_spec(_RATING_SPEC)
_RATING_SPEC.loader.exec_module(bench_rating)
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
        "display_64": display_64,
        "glided_steps": sum(
            1
            for step in result.get("steps") or []
            if str(step.get("method") or "").startswith("agent-cursor-glide")
        ),
    }
    duration_s = time.monotonic() - started
    steps = result.get("steps") or []
    return {
        "readback": bool(result.get("ok") and result.get("verified") and display_64),
        "cursor_visible": steps_show_cursor(steps),
        "robust": clear_label in {"Clear", "All Clear"}
        and _max_step_ms(result) < 8000
        and os.environ.get("MACOS_CUA_PIXEL_CLICK") != "1",
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
    compact = {
        "downloads_index": clickable,
        "actionable": clickable is not None,
    }
    found = clickable is not None
    return {
        "readback": found,
        "robust": found,
        "output_bytes": _compact_bytes(compact),
        "duration_s": round(time.monotonic() - started, 3),
        "downloads_index": clickable,
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
        _, _, area = parity.fresh_text_area(fixture_pid, window_id)
        click_started = time.monotonic()
        click = cua.right_click(
            fixture_pid, window_id, area["element_index"], app_name="TextEdit"
        )
        click_ms = round((time.monotonic() - click_started) * 1000)
        time.sleep(0.2)
        context, _, area = parity.fresh_text_area(fixture_pid, window_id)
        copy_visible = "Copy" in (context.get("text") or "")
        cua.press_key(fixture_pid, window_id, "Escape", "foreground")
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


def _heading_open_snapshot(snap: dict[str, Any]) -> bool:
    blob = str(snap.get("tree_markdown") or snap.get("text") or "")
    if _heading_open(blob):
        return True
    for item in snap.get("elements") or []:
        line = f"{item.get('role')} {item.get('label')} {item.get('value')}"
        if "heading" in line.lower() and "new chat" in line.lower():
            return True
    return False


def _close_whatsapp_panel(cua, name, pid, window_id) -> bool:
    """Observe first. Press Escape only when the New chat heading is open."""
    del name
    snap = cua._native_ax_snapshot(pid, max_elements=90, window_id=window_id)
    if not _heading_open_snapshot(snap):
        return True
    for delivery in ("background", "foreground"):
        cua.press_key(pid, window_id, "Escape", delivery)
        time.sleep(0.12)
        snap = cua._native_ax_snapshot(pid, max_elements=90, window_id=window_id)
        if not _heading_open_snapshot(snap):
            return True
    return False


def probe_whatsapp(cua, row: dict[str, Any]) -> dict[str, Any]:
    pid, window_id, name = _resolve(cua, "WhatsApp")
    closed_first = _close_whatsapp_panel(cua, name, pid, window_id)
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
    elapsed_s = round(time.monotonic() - started, 3)
    heading_only = _heading_open(final_text) and "message yourself" not in final_text.lower()
    if heading_only:
        cua.press_key(pid, window_id, "Escape", "background")
    else:
        _close_whatsapp_panel(cua, name, pid, window_id)
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
        "output_bytes": _compact_bytes(compact),
        "duration_s": elapsed_s,
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


_HASH_IGNORE = frozenset(
    {"__pycache__", ".DS_Store", ".ruff_cache", ".build", ".git"}
)


def source_hash(root: Path) -> str:
    files: list[Path] = []
    for rel in ("SKILL.md", "hardening-contract.json"):
        path = root / rel
        if path.is_file():
            files.append(path)
    for folder in ("operator", "references", "scripts", "tests"):
        base = root / folder
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.is_file() and not any(part in _HASH_IGNORE for part in path.parts):
                files.append(path)
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _attach_telemetry(cua, measured: dict[str, Any]) -> dict[str, Any]:
    for key, value in cua.telemetry_read().items():
        measured.setdefault(key, value)
    if hasattr(cua, "driver_call_stats"):
        stats = cua.driver_call_stats()
        measured.setdefault("round_trips", int(stats.get("calls") or 0))
        measured.setdefault("driver_bytes", int(stats.get("stdout_bytes") or 0))
    return measured


def _aggregate_measured(repeats: list[dict[str, Any]]) -> dict[str, Any]:
    keys = (
        "duration_s",
        "max_step_ms",
        "output_bytes",
        "driver_calls",
        "driver_seconds",
        "ax_snapshots",
    )
    measured: dict[str, Any] = {}
    for key in keys:
        samples = bench_rating.numeric_samples(repeats, key)
        if not samples:
            continue
        value = bench_rating.percentile(samples, 50)
        measured[key] = (
            round(value, 3) if key in {"duration_s", "driver_seconds"} else int(round(value))
        )
    return measured


def _execute_row(cua, parity, row: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    cua.telemetry_reset()
    if hasattr(cua, "reset_driver_call_stats"):
        cua.reset_driver_call_stats()
    try:
        probe = PROBES.get(row["name"])
        if probe is None:
            raise AssertionError(f"no probe for {row['name']}")
        measured = probe(cua, parity, row)
        _attach_telemetry(cua, measured)
        criteria = score(row, measured)
        return {"ok": all(criteria.values()), "criteria": criteria, "measured": measured}
    except Exception as error:
        measured = {
            "duration_s": round(time.monotonic() - started, 3),
            "error": str(error)[:240],
        }
        try:
            _attach_telemetry(cua, measured)
        except Exception:
            pass
        return {
            "ok": False,
            "criteria": {key: False for key in CRITERIA},
            "measured": measured,
        }


def run_suite(
    repeat: int = 1,
    rate: bool = False,
    write_baseline: bool = False,
    freeze_baseline: bool = False,
    compare: Path | None = None,
) -> dict[str, Any]:
    if repeat < 1:
        raise SystemExit("--repeat must be >= 1")
    write_baseline = write_baseline or freeze_baseline
    contract = load_suite()
    cua = load_cua()
    parity = load_parity()
    prior_by_name = {}
    if compare is not None:
        prior = json.loads(Path(compare).read_text())
        prior_by_name = {item["name"]: item for item in prior.get("results") or []}
    collected: dict[str, list[dict[str, Any]]] = {
        row["name"]: [] for row in contract["suite"]
    }
    for _ in range(repeat):
        for row in contract["suite"]:
            collected[row["name"]].append(_execute_row(cua, parity, row))
    results = []
    for row in contract["suite"]:
        repeats = collected[row["name"]]
        item = {
            "name": row["name"],
            "surface": row["surface"],
            "ok": all(rep["ok"] for rep in repeats),
            "criteria": {
                key: all(rep["criteria"].get(key) for rep in repeats) for key in CRITERIA
            },
            "measured": repeats[0]["measured"] if repeat == 1 else _aggregate_measured(repeats),
            "budget_seconds": row["budget_seconds"],
            "bytes_budget": row["bytes_budget"],
            "floor_seconds": row.get("floor_seconds"),
            "floor_max_step_ms": row.get("floor_max_step_ms"),
            "floor_bytes": row.get("floor_bytes"),
            "floor_driver_calls": row.get("floor_driver_calls"),
            "pass_signal": row["pass_signal"],
        }
        if repeat > 1:
            item["repeats"] = repeats
        if rate:
            item["ratings"] = bench_rating.rate_row(row, repeats)
        if compare is not None:
            item["compare"] = bench_rating.compare_row(
                repeats,
                bench_rating.repeats_from_result(prior_by_name.get(row["name"]) or {}),
            )
        results.append(item)
    payload: dict[str, Any] = {
        "ok": all(item["ok"] for item in results),
        "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "repeat": repeat,
        "results": results,
    }
    if rate:
        suite_rated = bench_rating.rate_suite(
            [
                {
                    "name": row["name"],
                    "contract": row,
                    "repeats": collected[row["name"]],
                }
                for row in contract["suite"]
            ]
        )
        payload["ratings"] = suite_rated["overall"]
        payload["trust_failures"] = suite_rated["trust_failures"]
    CACHE.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2) + "\n"
    out = CACHE / "benchmarks-latest.json"
    out.write_text(text)
    if write_baseline:
        baseline = dict(payload)
        baseline["source_hash"] = source_hash(SKILL)
        (CACHE / "benchmarks-baseline.json").write_text(
            json.dumps(baseline, indent=2) + "\n"
        )
    payload["path"] = str(out)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema-only", action="store_true")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--rate", action="store_true")
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--freeze-baseline", action="store_true")
    parser.add_argument("--compare", type=Path, default=None)
    args = parser.parse_args()
    if args.schema_only:
        load_suite()
        print(json.dumps({"ok": True, "schema": "suite"}))
        return 0
    payload = run_suite(
        repeat=args.repeat,
        rate=args.rate,
        write_baseline=args.baseline,
        freeze_baseline=args.freeze_baseline,
        compare=args.compare,
    )
    print(json.dumps({key: payload[key] for key in ("ok", "path", "results")}))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
