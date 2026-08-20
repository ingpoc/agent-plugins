#!/usr/bin/env python3
"""Wall-clock CUAService timings. App-agnostic fixture: Calculator keypad."""
from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))
from cua_client import CUAClient  # noqa: E402

APP = "Calculator"
REPEAT = 6


def _ms(samples: list[float]) -> str:
    return f"{statistics.median(samples)*1000:.0f}ms med / {min(samples)*1000:.0f}–{max(samples)*1000:.0f}"


def _time(fn, n: int = REPEAT) -> list[float]:
    out: list[float] = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        out.append(time.perf_counter() - t0)
    return out


def main() -> int:
    label = sys.argv[1] if len(sys.argv) > 1 else "run"
    c = CUAClient()
    c.connect()
    c.get_app_state(APP, disableDiff=True)  # warmup
    split = c.get_app_state(APP, disableDiff=True)
    print("timings_ms", split.get("timings_ms") if isinstance(split, dict) else None)
    state = _time(lambda: c.get_app_state(APP, disableDiff=True))
    def _clear():
        try:
            return c.click(APP, label="All Clear")
        except Exception:
            return c.click(APP, label="Clear")

    click = _time(_clear)
    payload = "speed-probe-" + ("x" * 80)
    type_calc = c.type_text(APP, payload)
    type_app = "TextEdit"
    c.get_app_state(type_app, disableDiff=True)
    type_s = _time(lambda: c.type_text(type_app, payload), n=3)
    type_te = c.type_text(type_app, payload)
    print(f"label={label}")
    print(f"get_app_state  {_ms(state)}")
    print(f"click All Clear {_ms(click)}")
    print(
        f"type_text 84ch {_ms(type_s)} "
        f"calc={type_calc.get('method') if isinstance(type_calc, dict) else type_calc} "
        f"textedit={type_te.get('method') if isinstance(type_te, dict) else type_te}"
    )
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
