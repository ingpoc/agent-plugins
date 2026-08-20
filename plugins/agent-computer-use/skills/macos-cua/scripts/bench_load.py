#!/usr/bin/env python3
"""Multi-app, multi-step CUAService load bench.

Single-hit timings hide jitter. Each app runs several rounds of a
multi-step scenario. Never prints AX values or document bodies.
"""
from __future__ import annotations

import statistics
import subprocess
import sys
import time
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))
from cua_client import CUAClient, RPCError  # noqa: E402

ROUNDS = 5
SCRATCH = Path("/tmp/cua-speed-probe.txt")


def _ms(xs: list[float]) -> str:
    if not xs:
        return "n/a"
    med = statistics.median(xs) * 1000
    p95 = sorted(xs)[min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))] * 1000
    return f"{med:.0f}ms med / {min(xs)*1000:.0f}–{max(xs)*1000:.0f} p95={p95:.0f}"


def _open(app: str, path: Path | None = None) -> None:
    cmd = ["open", "-a", app]
    if path:
        cmd.append(str(path))
    subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _click(c: CUAClient, app: str, label: str) -> dict:
    try:
        return c.click(app, label=label)
    except RPCError:
        if label == "All Clear":
            return c.click(app, label="Clear")
        raise


def _timed(fn) -> tuple[float, object, str | None]:
    t0 = time.perf_counter()
    err = None
    out: object = None
    try:
        out = fn()
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"[:180]
    return time.perf_counter() - t0, out, err


def run_steps(steps: list[tuple[str, object]]) -> list[tuple[str, float, str | None, object]]:
    rows = []
    for name, fn in steps:
        dt, result, err = _timed(fn)
        rows.append((name, dt, err, result))
    return rows


def scenario_calculator(c: CUAClient) -> list[tuple[str, object]]:
    app = "Calculator"
    return [
        ("state", lambda: c.get_app_state(app, disableDiff=True)),
        ("click:All Clear", lambda: _click(c, app, "All Clear")),
        ("click:7", lambda: _click(c, app, "7")),
        ("click:Add", lambda: _click(c, app, "Add")),
        ("click:8", lambda: _click(c, app, "8")),
        ("click:Equals", lambda: _click(c, app, "Equals")),
        ("click:All Clear", lambda: _click(c, app, "All Clear")),
        ("state", lambda: c.get_app_state(app, disableDiff=True)),
    ]


def scenario_textedit(c: CUAClient) -> list[tuple[str, object]]:
    app = "TextEdit"
    blob = "speed-probe-" + ("x" * 80)
    return [
        ("state", lambda: c.get_app_state(app, disableDiff=True)),
        ("type:84", lambda: c.type_text(app, blob)),
        ("type:84", lambda: c.type_text(app, blob)),
        ("type:84", lambda: c.type_text(app, blob)),
        ("key:left", lambda: c.press_key(app, "left")),
        ("type:8", lambda: c.type_text(app, "INS-MARK")),
        ("scroll:down", lambda: c.scroll(app, "down", pages=1)),
        ("state", lambda: c.get_app_state(app, disableDiff=True)),
    ]


def scenario_settings(c: CUAClient) -> list[tuple[str, object]]:
    # Never click Displays — resolution clicks mutate the panel.
    app = "System Settings"
    labels = ("Wi-Fi", "Bluetooth", "Sound", "Network", "Wi-Fi")
    steps: list[tuple[str, object]] = [
        ("state", lambda: c.get_app_state(app, disableDiff=True)),
    ]
    for lab in labels:
        steps.append((f"click:{lab}", lambda lab=lab: c.click(app, label=lab)))
        steps.append(("state", lambda: c.get_app_state(app, disableDiff=True)))
    return steps


def scenario_dictionary(c: CUAClient) -> list[tuple[str, object]]:
    app = "Dictionary"
    return [
        ("state", lambda: c.get_app_state(app, disableDiff=True)),
        ("type:q", lambda: c.type_text(app, "quasar")),
        ("state", lambda: c.get_app_state(app, disableDiff=True)),
        ("type:q", lambda: c.type_text(app, "neutron")),
        ("key:return", lambda: c.press_key(app, "return")),
        ("state", lambda: c.get_app_state(app, disableDiff=True)),
        ("scroll:down", lambda: c.scroll(app, "down", pages=2)),
        ("state", lambda: c.get_app_state(app, disableDiff=True)),
    ]


def scenario_preview(c: CUAClient) -> list[tuple[str, object]]:
    app = "Preview"
    return [
        ("state", lambda: c.get_app_state(app, disableDiff=True)),
        ("scroll:down", lambda: c.scroll(app, "down", pages=1)),
        ("scroll:up", lambda: c.scroll(app, "up", pages=1)),
        ("key:left", lambda: c.press_key(app, "left")),
        ("key:right", lambda: c.press_key(app, "right")),
        ("state", lambda: c.get_app_state(app, disableDiff=True)),
    ]


def scenario_stickies(c: CUAClient) -> list[tuple[str, object]]:
    app = "Stickies"
    return [
        ("state", lambda: c.get_app_state(app, disableDiff=True)),
        ("type:8", lambda: c.type_text(app, "STICK-OK")),
        ("key:left", lambda: c.press_key(app, "left")),
        ("type:8", lambda: c.type_text(app, "STICK-OK")),
        ("state", lambda: c.get_app_state(app, disableDiff=True)),
    ]


def scenario_textedit_new(c: CUAClient) -> list[tuple[str, object]]:
    """cmd+n then type into focused untitled; scratch body must stay untouched."""
    app = "TextEdit"
    return [
        ("state", lambda: c.get_app_state(app, disableDiff=True)),
        ("key:cmd+n", lambda: c.press_key(app, "cmd+n")),
        ("type:new", lambda: c.type_text(app, "NEWTOK12", after_new_document=True)),
        ("state", lambda: c.get_app_state(app, disableDiff=True)),
    ]


def summarize(app: str, rounds: list[list[tuple[str, float, str | None, object]]]) -> None:
    by_step: dict[str, list[float]] = {}
    methods: dict[str, list[str]] = {}
    fail_examples: list[str] = []
    fails = 0
    round_tot: list[float] = []
    n_el: list[int] = []
    splits: list[dict] = []
    for round_rows in rounds:
        round_tot.append(sum(dt for _, dt, _, _ in round_rows))
        for name, dt, err, result in round_rows:
            by_step.setdefault(name, []).append(dt)
            if err:
                fails += 1
                if len(fail_examples) < 6:
                    fail_examples.append(f"{name}: {err}")
            if not isinstance(result, dict):
                continue
            if result.get("ok") is False:
                fails += 1
                if len(fail_examples) < 6:
                    fail_examples.append(f"{name}: ok=false {result.get('error')}")
            m = result.get("method")
            if isinstance(m, str):
                methods.setdefault(name, []).append(m)
            if name == "state":
                if isinstance(result.get("elementCount"), int):
                    n_el.append(result["elementCount"])
                if isinstance(result.get("timings_ms"), dict):
                    splits.append(result["timings_ms"])
    print(f"\n== {app}  rounds={len(rounds)} step_fails={fails}")
    print(f"   round total {_ms(round_tot)}")
    if n_el:
        print(f"   elementCount med={statistics.median(n_el):.0f} ({min(n_el)}–{max(n_el)})")
    if splits:
        for key in ("resolve", "walk", "capture"):
            xs = [s[key] for s in splits if isinstance(s.get(key), (int, float))]
            if xs:
                retries = sum(1 for s in splits if s.get("capture_retry"))
                print(
                    f"   split {key} {statistics.median(xs):.0f}ms med "
                    f"({min(xs)}–{max(xs)}) capture_retry={retries}/{len(splits)}"
                )
    for name, xs in by_step.items():
        meth = methods.get(name, [])
        uniq = ",".join(sorted(set(meth))) if meth else "-"
        print(f"   {name:18} {_ms(xs)}  method={uniq}")
    for ex in fail_examples:
        print(f"   FAIL {ex}")


def main() -> int:
    SCRATCH.write_text("", encoding="utf-8")
    preview = Path("/tmp/cua-preview-probe.png")
    if not preview.exists():
        # 1×1 PNG
        preview.write_bytes(
            bytes.fromhex(
                "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
            )
        )
    _open("Calculator")
    _open("TextEdit", SCRATCH)
    _open("System Settings")
    _open("Dictionary")
    _open("Preview", preview)
    _open("Stickies")
    time.sleep(1.5)

    c = CUAClient()
    c.connect()
    probe = c.get_app_state("Calculator", disableDiff=True)
    if not probe.get("axTrusted"):
        print("axTrusted=false; grant Accessibility to CUAService and rerun")
        c.close()
        return 2

    fixtures = [
        ("Calculator", scenario_calculator),
        ("TextEdit", scenario_textedit),
        ("TextEdit-new", scenario_textedit_new),
        ("System Settings", scenario_settings),
        ("Dictionary", scenario_dictionary),
        ("Preview", scenario_preview),
        ("Stickies", scenario_stickies),
    ]
    print(f"axTrusted=true  rounds={ROUNDS} apps={len(fixtures)}")
    scratch_before = SCRATCH.read_bytes()
    for app, scen in fixtures:
        c.get_app_state(
            "TextEdit" if app.startswith("TextEdit") else app, disableDiff=True
        )
        rounds = [run_steps(scen(c)) for _ in range(ROUNDS)]
        summarize(app, rounds)
    if SCRATCH.read_bytes() != scratch_before:
        print("\nFAIL scratch mutated after TextEdit-new rounds")
        c.close()
        return 1
    print("\nscratch unchanged after TextEdit-new rounds")
    subprocess.run(
        [
            "osascript",
            "-e",
            'tell application "TextEdit" to close '
            '(documents whose name is "Untitled") saving no',
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
