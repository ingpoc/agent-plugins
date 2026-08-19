#!/usr/bin/env python3
"""Live A/B for MCP session shape: within-app vs cross-app wall clocks.

Measures in-process dispatch (held MCP runtime). Reports tool-call budgets
for agent round-trips separately — those dominate cross-app latency.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
COMPACT = ROOT / "scripts" / "compact_mcp.py"
WORKFLOW = ROOT / "scripts" / "workflow.py"
SESSION = "acu-session-shape-bench"
# Rough agent turn overhead when each MCP call is a separate model hop (seconds).
AGENT_TURN_S = 4.0

CALC_PLAN = {
    "pointer": True,
    "capture": "failures",
    "max_elements": 80,
    "actions": [
        {"action": "click", "label": "All Clear"},
        {"action": "click", "label": "7"},
        {"action": "click", "label": "Multiply"},
        {"action": "click", "label": "7"},
        {
            "action": "click",
            "label": "Equals",
            "expect": {"text": "49", "role": "AXStaticText"},
        },
    ],
    "expect": {"text": "49", "role": "AXStaticText"},
}

FINDER_PLAN = {
    "pointer": True,
    "capture": "failures",
    "max_elements": 120,
    "actions": [
        {"action": "click", "label": "Recents"},
        {"action": "click", "label": "Downloads"},
    ],
    "expect": {"text": "Downloads"},
}


def load_mcp():
    spec = importlib.util.spec_from_file_location("compact_mcp_shape", COMPACT)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {COMPACT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def timed(label: str, fn: Callable[[], Any]) -> dict[str, Any]:
    started = time.monotonic()
    payload = fn()
    elapsed_ms = round((time.monotonic() - started) * 1000)
    ok = payload.get("ok") is True or payload.get("verified") is True
    if payload.get("verified") is False:
        ok = False
    return {
        "label": label,
        "ok": ok,
        "dispatch_ms": elapsed_ms,
        "tool_calls": 1,
        "payload": payload,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--agent-turn-s",
        type=float,
        default=AGENT_TURN_S,
        help="Estimated seconds per extra MCP tool hop (default 4)",
    )
    args = parser.parse_args(argv)
    agent_turn_s = float(args.agent_turn_s)

    def wall_for(dispatch_ms: int, tool_calls: int) -> dict[str, float]:
        dispatch_s = dispatch_ms / 1000.0
        agent_s = max(0, tool_calls - 1) * agent_turn_s
        return {
            "dispatch_s": round(dispatch_s, 3),
            "agent_turns_s": round(agent_s, 3),
            "estimated_total_s": round(dispatch_s + agent_s, 3),
        }

    def run_variant_local(
        name: str, steps: list[Callable[[], dict[str, Any]]]
    ) -> dict[str, Any]:
        rows = [step() for step in steps]
        dispatch_ms = sum(row["dispatch_ms"] for row in rows)
        tool_calls = sum(row["tool_calls"] for row in rows)
        ok = all(row["ok"] for row in rows)
        return {
            "name": name,
            "ok": ok,
            "dispatch_ms": dispatch_ms,
            "tool_calls": tool_calls,
            **wall_for(dispatch_ms, tool_calls),
            "steps": [
                {"label": r["label"], "ok": r["ok"], "dispatch_ms": r["dispatch_ms"]}
                for r in rows
            ],
        }

    preflight = subprocess.run(
        [sys.executable, str(WORKFLOW), "preflight"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if preflight.returncode != 0:
        print(json.dumps({"ok": False, "error": "preflight failed", "stderr": preflight.stderr}))
        return 1

    mcp = load_mcp()
    mcp._SESSION.clear()
    mcp.handle_start_session({"session": SESSION})

    calc_state = lambda: timed(
        "state Calculator",
        lambda: mcp._state_payload({"app": "Calculator", "max": 80}),
    )
    calc_act = lambda: timed(
        "act Calculator 7x7",
        lambda: mcp.handle_act({"app": "Calculator", "plan": CALC_PLAN}),
    )
    calc_verify = lambda: timed(
        "verify Calculator 49",
        lambda: mcp.handle_verify({"app": "Calculator", "expect": "49"}),
    )

    finder_state_dl = lambda: timed(
        "state Finder Downloads query",
        lambda: mcp._state_payload({"app": "Finder", "max": 80, "query": "Downloads"}),
    )
    finder_state_rc = lambda: timed(
        "state Finder Recents query",
        lambda: mcp._state_payload({"app": "Finder", "max": 80, "query": "Recents"}),
    )
    finder_act = lambda: timed(
        "act Finder sidebar",
        lambda: mcp.handle_act({"app": "Finder", "plan": FINDER_PLAN}),
    )
    finder_verify = lambda: timed(
        "verify Finder Downloads",
        lambda: mcp.handle_verify({"app": "Finder", "expect": "Downloads"}),
    )

    calendar_hop = lambda: timed(
        "state Calendar compact",
        lambda: mcp._state_payload({"app": "Calendar", "max": 40, "query": "Calendar"}),
    )

    variants = [
        run_variant_local(
            "within_app_pre_state",
            [calc_state, calc_act],
        ),
        run_variant_local("within_app_act_first", [calc_act]),
        run_variant_local(
            "within_app_act_plus_redundant_verify",
            [calc_act, calc_verify],
        ),
        run_variant_local(
            "finder_probe_then_act",
            [finder_state_dl, finder_state_rc, finder_act],
        ),
        run_variant_local("finder_act_first", [finder_act]),
        run_variant_local(
            "finder_act_plus_redundant_verify",
            [finder_act, finder_verify],
        ),
        run_variant_local(
            "cross_app_state_act_verify_each",
            [
                calc_state,
                calc_act,
                calc_verify,
                finder_state_dl,
                finder_act,
                finder_verify,
                calendar_hop,
            ],
        ),
        run_variant_local(
            "cross_app_act_only",
            [calc_act, finder_act, calendar_hop],
        ),
    ]

    mcp.handle_end_session({})

    winners: dict[str, str] = {}
    by = {row["name"]: row for row in variants}

    def pick(slow: str, fast: str, key: str) -> None:
        if by[slow]["ok"] and by[fast]["ok"]:
            winners[key] = fast if by[fast]["estimated_total_s"] < by[slow]["estimated_total_s"] else slow

    pick("within_app_pre_state", "within_app_act_first", "within_app")
    pick("within_app_act_first", "within_app_act_plus_redundant_verify", "skip_verify_when_verified")
    pick("finder_probe_then_act", "finder_act_first", "finder_within_app")
    pick("cross_app_state_act_verify_each", "cross_app_act_only", "cross_app")

    savings = {}
    for key, winner in winners.items():
        if key == "within_app":
            slow, fast = "within_app_pre_state", "within_app_act_first"
        elif key == "skip_verify_when_verified":
            slow, fast = "within_app_act_plus_redundant_verify", "within_app_act_first"
        elif key == "finder_within_app":
            slow, fast = "finder_probe_then_act", "finder_act_first"
        else:
            slow, fast = "cross_app_state_act_verify_each", "cross_app_act_only"
        if winner == fast:
            savings[key] = round(by[slow]["estimated_total_s"] - by[fast]["estimated_total_s"], 3)

    payload = {
        "ok": all(row["ok"] for row in variants),
        "agent_turn_s": agent_turn_s,
        "variants": variants,
        "winners": winners,
        "estimated_savings_s": savings,
        "keep": [
            "act-first: skip pre-act state when labels are known",
            "one batched act per app surface; plan carries expects",
            "skip verify when act.verified is true",
            "cross-app: act-only hops; no state/verify between apps unless act failed",
        ],
        "remove": [
            "probe labels with separate state calls before act",
            "verify after verified act",
            "state+verify between every app switch",
        ],
    }
    print(json.dumps(payload, indent=2))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
