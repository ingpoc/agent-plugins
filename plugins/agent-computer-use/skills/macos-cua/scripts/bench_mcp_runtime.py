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


ROOT = Path(__file__).resolve().parents[1]
COMPACT_MCP = ROOT / "scripts" / "compact_mcp.py"
MACOS_CUA = ROOT / "scripts" / "macos-cua.py"
SESSION = "acu-mcp-runtime-benchmark"
PLAN = {
    "pointer": False,
    "capture": "failures",
    "output": "compact",
    "max_elements": 50,
    "actions": [
        {"action": "click", "label": "Clear"},
        {
            "action": "click",
            "label": "7",
            "expect": {"text": "7", "role": "AXStaticText"},
        },
    ],
    "expect": {"text": "7", "role": "AXStaticText"},
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


def direct_once(mcp) -> dict[str, Any]:
    started = time.monotonic()
    state = mcp._state_payload({"app": "Calculator", "max": 80})
    state_seconds = time.monotonic() - started
    started = time.monotonic()
    act = mcp.handle_act({"app": "Calculator", "plan": PLAN})
    act_seconds = time.monotonic() - started
    return {
        "state_s": state_seconds,
        "act_s": act_seconds,
        "total_s": state_seconds + act_seconds,
        "ok": state.get("ok") is True and act.get("verified") is True,
    }


def subprocess_once(env: dict[str, str]) -> dict[str, Any]:
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
        json.dump(PLAN, handle)
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

    def live_identity():
        current = cua._running_app_identity(app) or {}
        pid = current.get("pid")
        return current if pid and cua._pid_alive(pid) else None

    if live_identity():
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
        while live_identity() and time.monotonic() < deadline:
            time.sleep(0.05)
        leftover = live_identity()
        if leftover:
            try:
                os.kill(int(leftover["pid"]), signal.SIGKILL)
            except (OSError, ProcessLookupError, TypeError, ValueError):
                pass
            deadline = time.monotonic() + 3
            while live_identity() and time.monotonic() < deadline:
                time.sleep(0.05)
        if live_identity():
            raise RuntimeError(f"{app} fixture did not terminate")
    cua.clear_resolution_cache()
    _, _, _, err = cua.resolve_app(app)
    if err:
        raise RuntimeError(err)


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
    start = mcp.handle_start_session({"session": SESSION})
    try:
        direct_once(mcp)
        subprocess_once(env)
        runners: dict[str, Callable[[], dict[str, Any]]] = {
            "direct": lambda: direct_once(mcp),
            "subprocess": lambda: subprocess_once(env),
        }
        for index in range(args.repeat):
            order = (
                ("direct", "subprocess")
                if index % 2 == 0
                else ("subprocess", "direct")
            )
            for name in order:
                rows[name].append(runners[name]())
    finally:
        end = mcp.handle_end_session({"session": SESSION})

    summary = {name: summarize(values) for name, values in rows.items()}
    baseline = summary["subprocess"]["p50_total_s"]
    current = summary["direct"]["p50_total_s"]
    improvement = round((baseline - current) / baseline * 100, 1)
    telemetry = mcp.telemetry_read()
    all_succeeded = all(
        values["successes"] == values["runs"] for values in summary.values()
    )
    passed = all(
        (
            start.get("ok") is True,
            end.get("ok") is True,
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
                "start_ok": start.get("ok"),
                "end_ok": end.get("ok"),
            },
            indent=2,
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
