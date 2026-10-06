#!/usr/bin/env python3
"""Agent-friendly CLI contract shared by comet-control scripts.

- Every result is one JSON object on stdout (also for usage errors).
- Known errors carry a one-line ``hint`` with the exact next step.
- Exit codes are fixed: see ``EXIT_CODES``.
- Hints never suggest a second session: on a miss the lease stays the same and
  the job escalates 1:1 to ACU.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, NoReturn

EXIT_OK = 0
EXIT_FAIL = 1  # command ran, outcome failed (dead controller, timeout, unverified closeout)
EXIT_LEASE = 2  # start: lease never became ready (kept for compatibility)
EXIT_USAGE = 64  # bad flags or payload; nothing was sent (BSD EX_USAGE)

EXIT_CODES = {
    EXIT_OK: "ok",
    EXIT_FAIL: "failed",
    EXIT_LEASE: "lease_not_ready",
    EXIT_USAGE: "usage",
}

_NO_REMINT = "Do not start a second session for this job."

HINTS: dict[str, str] = {
    "usage_error": (
        "Run `durable_lease_controller.py <start|send|status|closeout> --help`. "
        "Start here: start --session-id ID --label L --url URL --workdir W."
    ),
    "empty_payload": (
        "Pass one JSON object: send --workdir W '{\"actions\":[{\"type\":\"page_context\"}]}' "
        "or --payload-file F."
    ),
    "invalid_payload_json": (
        "Payload must be one JSON object, e.g. '{\"actions\":[{\"type\":\"page_context\"}]}'. "
        "Check with send --dry-run first."
    ),
    "payload_not_object": (
        "Payload must be a JSON object with \"actions\" (list) or \"command\"."
    ),
    "actions_missing": (
        "A run payload needs a non-empty \"actions\" list, or use "
        "{\"command\": \"closeout\"}."
    ),
    "controller_not_alive": (
        "No live controller for this workdir. Run status --workdir W. "
        + _NO_REMINT
        + " If this lease was serving a job, escalate to ACU."
    ),
    "timeout_waiting_request_slot": (
        "A previous request is still pending. Wait or run status --workdir W; "
        "do not resend. " + _NO_REMINT
    ),
    "timeout_waiting_response": (
        "The request may still run. Do not resend mutations; run status --workdir W, "
        "then escalate to ACU if it stays pending. " + _NO_REMINT
    ),
    "controller_died": (
        "The controller exited mid-request; the action may or may not have happened. "
        "Run closeout --workdir W (safe to repeat) and escalate to ACU. " + _NO_REMINT
    ),
    "controller_exited_before_ready": (
        "The lease never became ready. Read W/controller.stderr and run "
        "./scripts/ensure-broker.sh probe --json before one new start."
    ),
    "timeout_waiting_ready.json": (
        "The lease never became ready. Read W/controller.stderr and run "
        "./scripts/ensure-broker.sh probe --json before one new start."
    ),
    "closeout_unverified": (
        "Closeout did not prove verified_absent. Repeat closeout --workdir W once "
        "(safe to repeat), then escalate to ACU. " + _NO_REMINT
    ),
    "status_unhealthy": (
        "Controller is not healthy. " + _NO_REMINT
        + " Escalate to ACU with this status JSON."
    ),
    "session_mismatch": (
        "This workdir already holds a live controller for another session; it was "
        "reused, not replaced. Use that session_id, or a new --workdir for a new job."
    ),
    "would_spawn": (
        "start would mint a new lease. Do this only for a new job, never after "
        "LEASE_HELD, a timeout, or a click miss on an existing one."
    ),
}


def error_key(error: Any) -> str:
    """Map an error string such as ``invalid_payload_json: ...`` to its hint key."""
    text = str(error or "")
    return text.split(":", 1)[0].strip()


def with_hint(payload: dict[str, Any], key: str | None = None) -> dict[str, Any]:
    """Add ``hint`` to a result dict if a known key applies and none is set."""
    if not isinstance(payload, dict) or payload.get("hint"):
        return payload
    k = key or error_key(payload.get("error"))
    hint = HINTS.get(k)
    if hint:
        payload["hint"] = hint
    return payload


def emit(payload: dict[str, Any], code: int, *, key: str | None = None) -> int:
    """Print one JSON result (with hint when known) and return ``code``."""
    print(json.dumps(with_hint(payload, key), ensure_ascii=False))
    return code


def summarize_payload(body: Any) -> dict[str, Any]:
    """Secret-safe shape of a send payload: action types only, never values."""
    if not isinstance(body, dict):
        return {"kind": type(body).__name__}
    if "command" in body:
        return {"command": str(body.get("command"))}
    actions = body.get("actions")
    if not isinstance(actions, list):
        return {"keys": sorted(str(k) for k in body)}
    types = []
    for item in actions:
        if isinstance(item, dict):
            types.append(str(item.get("type") or item.get("action") or "?"))
        else:
            types.append("?")
    return {"action_count": len(types), "action_types": types}


class JsonArgumentParser(argparse.ArgumentParser):
    """argparse that reports usage errors as one JSON object, exit 64."""

    def error(self, message: str) -> NoReturn:  # type: ignore[override]
        payload = {
            "ok": False,
            "error": "usage_error",
            "detail": message,
            "usage": self.format_usage().strip(),
            "exit_code": EXIT_USAGE,
        }
        print(json.dumps(with_hint(payload), ensure_ascii=False))
        sys.exit(EXIT_USAGE)
