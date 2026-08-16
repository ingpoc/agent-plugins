"""Process-local counters for one completed macos-cua task.

Cheap integer/float accumulation only. No file I/O. No locks.
Read via telemetry_read(); never splice these keys into command payloads.

The counters hang off builtins, not this module's globals. The facade loads each
runtime module with importlib and overwrites its own sys.modules entry, so a
second facade load in the same process creates a second module object. Anything
counted through one instance is then invisible to the other, which silently
undercounts work done through a separately loaded facade.
"""
from __future__ import annotations

import builtins

_STORE = "_macos_cua_telemetry_counts"
_ZERO = {
    "driver_calls": 0,
    "driver_seconds": 0.0,
    "ax_snapshots": 0,
    "cli_invocations": 0,
    "cli_response_bytes": 0,
}
_COUNTS = getattr(builtins, _STORE, None)
if not isinstance(_COUNTS, dict):
    _COUNTS = dict(_ZERO)
    setattr(builtins, _STORE, _COUNTS)


def telemetry_reset() -> None:
    _COUNTS["driver_calls"] = 0
    _COUNTS["driver_seconds"] = 0.0
    _COUNTS["ax_snapshots"] = 0
    _COUNTS["cli_invocations"] = 0
    _COUNTS["cli_response_bytes"] = 0


def telemetry_read() -> dict[str, float | int]:
    return {
        "driver_calls": int(_COUNTS["driver_calls"]),
        "driver_seconds": round(float(_COUNTS["driver_seconds"]), 3),
        "ax_snapshots": int(_COUNTS["ax_snapshots"]),
        "cli_invocations": int(_COUNTS["cli_invocations"]),
        "cli_response_bytes": int(_COUNTS["cli_response_bytes"]),
    }


def telemetry_record_driver(seconds: float) -> None:
    _COUNTS["driver_calls"] += 1
    _COUNTS["driver_seconds"] += float(seconds)


def telemetry_record_ax() -> None:
    _COUNTS["ax_snapshots"] += 1


def telemetry_record_cli(response_bytes: int = 0) -> None:
    _COUNTS["cli_invocations"] += 1
    _COUNTS["cli_response_bytes"] += int(response_bytes)
