#!/usr/bin/env python3
"""Ambiguity-only Jev Choice → validated macos-cua `act` payload.

Port of the jev-use *pattern* onto macos-cua (CUAService). Does not touch MCP
catalog or CUAService. When labels already determine the action, call `act`
directly — do not route through this helper.

Flow: immutable candidate menu (+ __reobserve__ / __abstain__) → TypeSafe Jev
Choice → validate ID → emit exact `act` args. Proof remains expect after act;
confidence is not success.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REOBSERVE = "__reobserve__"
ABSTAIN = "__abstain__"
CONTROLS = (REOBSERVE, ABSTAIN)
DEFAULT_MIN_CONFIDENCE = 0.7
EVALUATE = Path(
    os.environ.get(
        "MACOS_CUA_JEV_EVALUATE",
        Path.home() / ".agents/skills/jev/scripts/evaluate.py",
    )
).expanduser()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def act_id(act_payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(canonical_json(act_payload).encode("utf-8")).hexdigest()
    return f"act_{digest[:12]}"


def build_menu(
    candidates: list[dict[str, Any]],
    *,
    include_controls: bool = True,
) -> dict[str, dict[str, Any]]:
    """Return id → {description, act|None, control?}."""
    menu: dict[str, dict[str, Any]] = {}
    for raw in candidates:
        if not isinstance(raw, dict):
            raise ValueError("each candidate must be an object")
        description = str(raw.get("description") or "").strip()
        act = raw.get("act")
        if not description:
            raise ValueError("candidate description is required")
        if not isinstance(act, dict):
            raise ValueError("candidate act must be an object")
        if not str(act.get("app") or "").strip():
            raise ValueError("candidate act.app is required")
        cid = act_id(act)
        if cid in menu:
            raise ValueError(f"duplicate candidate id {cid}")
        menu[cid] = {"description": description, "act": act, "control": False}
    if include_controls:
        menu[REOBSERVE] = {
            "description": "State is stale or incomplete; take one fresh compact state then rebuild",
            "act": None,
            "control": True,
        }
        menu[ABSTAIN] = {
            "description": "No safe action among the candidates; stop and ask the user",
            "act": None,
            "control": True,
        }
    return menu


def choice_questions(menu: dict[str, dict[str, Any]], goal: str) -> dict[str, Any]:
    criteria = {cid: entry["description"] for cid, entry in menu.items()}
    instructions = (
        "Pick exactly one action ID for the next macos-cua step. "
        "Only choose an act_* ID when that plan is the correct complete action. "
        f"Prefer {REOBSERVE} when state is insufficient. "
        f"Prefer {ABSTAIN} when none of the act plans are correct. "
        f"Goal: {goal}"
    )
    return {
        "next": {
            "type": "choice",
            "instructions": instructions,
            "criteria": criteria,
        }
    }


def run_evaluate(
    *,
    questions: dict[str, Any],
    state: Any,
    min_confidence: float,
    timeout: float,
    mock_choice: str | None,
) -> dict[str, Any]:
    if mock_choice is not None:
        return {
            "ok": True,
            "model": "mock",
            "answers": {
                "next": {
                    "type": "choice",
                    "choice": mock_choice,
                    "probabilities": {mock_choice: 1.0},
                    "confidence": 1.0,
                }
            },
            "usage": {"inputTokens": 0, "outputTokens": 0},
            "recommend": {"ask_user": False, "next": mock_choice},
            "min_confidence": min_confidence,
        }
    if not EVALUATE.is_file():
        return {"ok": False, "error": f"evaluate.py missing: {EVALUATE}"}
    cmd = [
        sys.executable,
        str(EVALUATE),
        "--questions",
        json.dumps(questions, ensure_ascii=False),
        "--state",
        json.dumps(state, ensure_ascii=False),
        "--min-confidence",
        str(min_confidence),
        "--timeout",
        str(timeout),
    ]
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout + 5,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "jev evaluate timed out", "latency_ms": round((time.perf_counter() - started) * 1000)}
    latency_ms = round((time.perf_counter() - started) * 1000)
    try:
        payload = json.loads((proc.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        return {
            "ok": False,
            "error": (proc.stderr or proc.stdout or "invalid JSON")[:500],
            "latency_ms": latency_ms,
        }
    if not isinstance(payload, dict):
        return {"ok": False, "error": "evaluate returned non-object", "latency_ms": latency_ms}
    payload["latency_ms"] = latency_ms
    if proc.returncode != 0:
        payload.setdefault("ok", False)
        payload.setdefault("error", (proc.stderr or "evaluate failed")[:500])
    return payload


def decide(
    *,
    goal: str,
    candidates: list[dict[str, Any]],
    state: Any = None,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    timeout: float = 30.0,
    mock_choice: str | None = None,
    include_controls: bool = True,
) -> dict[str, Any]:
    try:
        menu = build_menu(candidates, include_controls=include_controls)
    except ValueError as exc:
        return {"ok": False, "auto_act": False, "error": str(exc)}

    packed_state = {
        "goal": goal,
        "observation": state,
        "candidate_ids": list(menu.keys()),
    }
    questions = choice_questions(menu, goal)
    evaluated = run_evaluate(
        questions=questions,
        state=packed_state,
        min_confidence=min_confidence,
        timeout=timeout,
        mock_choice=mock_choice,
    )
    if not evaluated.get("ok"):
        return {
            "ok": False,
            "auto_act": False,
            "error": evaluated.get("error") or "jev evaluate failed",
            "latency_ms": evaluated.get("latency_ms"),
            "usage": evaluated.get("usage"),
            "menu_ids": list(menu.keys()),
        }

    answers = evaluated.get("answers") or {}
    next_ans = answers.get("next") if isinstance(answers, dict) else None
    choice = None
    confidence = None
    if isinstance(next_ans, dict):
        choice = next_ans.get("choice")
        confidence = next_ans.get("confidence")
    recommend = evaluated.get("recommend") or {}
    ask_user = bool(recommend.get("ask_user"))

    if choice not in menu:
        return {
            "ok": False,
            "auto_act": False,
            "error": "invented_or_unknown_id",
            "choice": choice,
            "confidence": confidence,
            "latency_ms": evaluated.get("latency_ms"),
            "usage": evaluated.get("usage"),
            "menu_ids": list(menu.keys()),
        }

    if confidence is not None and float(confidence) < float(min_confidence):
        ask_user = True

    entry = menu[choice]
    if entry.get("control") or ask_user or choice in CONTROLS:
        return {
            "ok": True,
            "auto_act": False,
            "choice": choice,
            "confidence": confidence,
            "control": choice if choice in CONTROLS else None,
            "ask_user": ask_user or choice == ABSTAIN,
            "reobserve": choice == REOBSERVE,
            "act": None,
            "latency_ms": evaluated.get("latency_ms"),
            "usage": evaluated.get("usage"),
            "menu_ids": list(menu.keys()),
        }

    return {
        "ok": True,
        "auto_act": True,
        "choice": choice,
        "confidence": confidence,
        "ask_user": False,
        "act": entry["act"],
        "latency_ms": evaluated.get("latency_ms"),
        "usage": evaluated.get("usage"),
        "menu_ids": list(menu.keys()),
    }


def _load_json(path: str | None, raw: str | None, default: Any) -> Any:
    if path:
        return json.loads(Path(path).read_text())
    if raw:
        return json.loads(raw)
    return default


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--goal", required=True)
    parser.add_argument("--candidates", help="JSON array of {description, act}")
    parser.add_argument("--candidates-file")
    parser.add_argument("--state", help="JSON observation (compact state text/object)")
    parser.add_argument("--state-file")
    parser.add_argument("--min-confidence", type=float, default=DEFAULT_MIN_CONFIDENCE)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--mock-choice", help="CI/mock: force this menu id")
    parser.add_argument("--no-controls", action="store_true")
    args = parser.parse_args(argv)

    candidates = _load_json(args.candidates_file, args.candidates, None)
    if not isinstance(candidates, list) or not candidates:
        print(json.dumps({"ok": False, "auto_act": False, "error": "candidates required"}, ensure_ascii=False))
        return 2
    state = _load_json(args.state_file, args.state, None)
    result = decide(
        goal=args.goal,
        candidates=candidates,
        state=state,
        min_confidence=args.min_confidence,
        timeout=args.timeout,
        mock_choice=args.mock_choice,
        include_controls=not args.no_controls,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
