#!/usr/bin/env python3
"""Live A/B grader for persistent MCP dispatch versus the retired CLI path."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable

SCRIPTS = str(Path(__file__).resolve().parent)
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from runtime_telemetry import telemetry_read, telemetry_reset


ROOT = Path(__file__).resolve().parents[1]
COMPACT_MCP = ROOT / "scripts" / "compact_mcp.py"
MACOS_CUA = ROOT / "scripts" / "macos-cua.py"
SESSION = "acu-mcp-runtime-benchmark"
def plan_for(index: int) -> dict[str, Any]:
    """Calculator plan whose postcondition is new on every repeat.

    `expect` must be absent from the settled pre-state, and the display carries
    the previous result forward, so a single fixed operand can only verify on
    the first run. Alternating operands keeps the pre-state different from the
    expectation on every measurement.
    """
    digit = "7" if index % 2 == 0 else "8"
    return {
        "app": "Calculator",
        "steps": [
            {"label": "All Clear"},
            {"label": digit},
        ],
        "expect": digit,
    }


def load_compact_mcp():
    spec = importlib.util.spec_from_file_location("compact_mcp_benchmark", COMPACT_MCP)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {COMPACT_MCP}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_macos_cua():
    spec = importlib.util.spec_from_file_location("macos_cua_fixture", MACOS_CUA)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {MACOS_CUA}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def subprocess_payload(command: list[str], *, timeout: float, env: dict[str, str]):
    started = time.monotonic()
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        env=env,
    )
    elapsed = time.monotonic() - started
    try:
        payload = json.loads((result.stdout or "").strip())
    except json.JSONDecodeError:
        payload = {
            "ok": False,
            "error": (result.stderr or result.stdout or "invalid JSON")[:1000],
        }
    if result.returncode != 0:
        payload.setdefault("ok", False)
    return payload, elapsed


def direct_once(mcp, plan: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    state = mcp._state_payload({"app": "Calculator", "max": 80})
    state_seconds = time.monotonic() - started
    started = time.monotonic()
    act = mcp.handle_act(plan)
    act_seconds = time.monotonic() - started
    return {
        "state_s": state_seconds,
        "act_s": act_seconds,
        "total_s": state_seconds + act_seconds,
        "ok": state.get("ok") is True and act.get("verified") is True,
    }


def cli_form(plan: dict[str, Any]) -> dict[str, Any]:
    """Translate an MCP act plan into the harness/CLI plan schema.

    The per-call CLI validates against plan_contract, which requires an
    `actions` array where every entry names its `action`; the MCP act surface
    infers the action from the step's fields.
    """
    return {
        "app": plan["app"],
        "actions": [{"action": "click", **step} for step in plan.get("steps", [])],
        "expect": plan["expect"],
    }


def subprocess_once(env: dict[str, str], plan: dict[str, Any]) -> dict[str, Any]:
    state, state_seconds = subprocess_payload(
        [
            sys.executable,
            str(MACOS_CUA),
            "state",
            "Calculator",
            "--compact",
            "--no-screenshot",
            "--max",
            "80",
        ],
        timeout=30,
        env=env,
    )
    with tempfile.NamedTemporaryFile("w", suffix=".json") as handle:
        json.dump(cli_form(plan), handle)
        handle.flush()
        act, act_seconds = subprocess_payload(
            [
                sys.executable,
                str(MACOS_CUA),
                "run",
                "Calculator",
                f"@{handle.name}",
            ],
            timeout=60,
            env=env,
        )
    return {
        "state_s": state_seconds,
        "act_s": act_seconds,
        "total_s": state_seconds + act_seconds,
        "ok": state.get("ok") is True and act.get("verified") is True,
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "p50_state_s": round(statistics.median(row["state_s"] for row in rows), 3),
        "p50_act_s": round(statistics.median(row["act_s"] for row in rows), 3),
        "p50_total_s": round(statistics.median(row["total_s"] for row in rows), 3),
        "successes": sum(row["ok"] for row in rows),
        "runs": len(rows),
    }


def reset_app_fixture(app_name: str) -> None:
    """Quit and relaunch any native app, then drop stale PID/window cache."""
    app = str(app_name or "").strip()
    if not app:
        raise ValueError("app_name is required")
    cua = load_macos_cua()

    def live_pids() -> list[int]:
        """Every live instance: a duplicate must be reaped, never inherited."""
        return [
            int(instance["pid"])
            for instance in cua._live_app_instances(app)
            if cua._pid_alive(int(instance["pid"]))
        ]

    def survivors(pids: list[int]) -> list[int]:
        """Cheap liveness poll: a process check costs no service round trip."""
        return [pid for pid in pids if cua._pid_alive(pid)]

    pids = live_pids()
    if pids:
        try:
            subprocess.run(
                ["osascript", "-e", f"tell application {json.dumps(app)} to quit"],
                capture_output=True,
                timeout=5,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            pass
        deadline = time.monotonic() + 5
        while survivors(pids) and time.monotonic() < deadline:
            time.sleep(0.05)
        for pid in survivors(pids):
            try:
                os.kill(pid, signal.SIGKILL)
            except (OSError, ProcessLookupError, TypeError, ValueError):
                pass
        deadline = time.monotonic() + 3
        while survivors(pids) and time.monotonic() < deadline:
            time.sleep(0.05)
        # One enumeration at the end catches a copy that started in the interim.
        leftover = survivors(pids) or [pid for pid in live_pids() if pid not in pids]
        if leftover:
            raise RuntimeError(f"{app} fixture did not terminate: {leftover}")
    cua.clear_resolution_cache()
    pid, _, _, err = cua.resolve_app(app)
    if err:
        raise RuntimeError(err)
    # The next click resolves this fixture by pid, and a pid the registry has not
    # published yet is rejected as "no running app with pid", so wait for the app
    # to show up. Poll the process (free) between slower registry checks, and
    # treat the wait as best effort: a live pid already proves the fixture ran.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and cua._pid_alive(pid) and not live_pids():
        time.sleep(0.2)
    if not cua._pid_alive(pid):
        raise RuntimeError(f"{app} fixture exited right after launch (pid {pid})")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--min-improvement", type=float, default=10.0)
    args = parser.parse_args()
    if args.repeat < 3:
        parser.error("--repeat must be at least 3")

    reset_app_fixture("Calculator")
    mcp = load_compact_mcp()
    env = os.environ | {
        "CUA_DRIVER_RS_UPDATE_CHECK": "0",
        "MACOS_CUA_SESSION": SESSION,
    }
    rows: dict[str, list[dict[str, Any]]] = {"direct": [], "subprocess": []}
    runners: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
        "direct": lambda plan: direct_once(mcp, plan),
        "subprocess": lambda plan: subprocess_once(env, plan),
    }
    telemetry_reset()
    step = 0
    for name in ("direct", "subprocess"):
        runners[name](plan_for(step))  # warmup, not measured
        step += 1
    for index in range(args.repeat):
        order = (
            ("direct", "subprocess")
            if index % 2 == 0
            else ("subprocess", "direct")
        )
        for name in order:
            rows[name].append(runners[name](plan_for(step)))
            step += 1

    summary = {name: summarize(values) for name, values in rows.items()}
    baseline = summary["subprocess"]["p50_total_s"]
    current = summary["direct"]["p50_total_s"]
    improvement = round((baseline - current) / baseline * 100, 1)
    telemetry = telemetry_read()
    all_succeeded = all(
        values["successes"] == values["runs"] for values in summary.values()
    )
    passed = all(
        (
            all_succeeded,
            telemetry.get("cli_invocations") == 0,
            improvement >= args.min_improvement,
        )
    )
    print(
        json.dumps(
            {
                "ok": passed,
                "decision": "keep" if passed else "revert_or_investigate",
                "minimum_improvement_percent": args.min_improvement,
                "improvement_percent": improvement,
                "summary": summary,
                "runtime_telemetry": telemetry,
            },
            indent=2,
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
