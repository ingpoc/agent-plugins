#!/usr/bin/env python3
"""Compact, typed failure feedback for macos-cua act results."""

from __future__ import annotations

import re
from typing import Any

# Stable reason codes for the model (actionable, not tree dumps).
TARGET_MISSING = "target_missing"
STALE_ID = "stale_id"
OBSERVATION_INCOMPLETE = "observation_incomplete"
ACTION_FAILED = "action_failed"
EXPECT_UNVERIFIED = "expect_unverified"
VERIFICATION_REQUIRED = "verification_required"

_REASON_HINTS = {
    TARGET_MISSING: "No matching control; fresh state by label, then retry or use coordinates.",
    STALE_ID: "Element index/id is stale; discard ids and resolve by label via state.",
    OBSERVATION_INCOMPLETE: "No current observation; discard old IDs and get state before deciding. Do not replay uncertain actions.",
    ACTION_FAILED: "Native step failed; change approach (AX → coord) or raise the app.",
    EXPECT_UNVERIFIED: "Dispatch ran but expect was not new in the settled tree; do not claim done.",
    VERIFICATION_REQUIRED: "Mutating act needs expect (or allow_unverified for dispatch-only).",
}

_LABEL_RE = re.compile(r'"(.*?)"')
_INDEX_RE = re.compile(r"\[(\d+)\]")


def _err_blob(results: list[dict[str, Any]] | None, error: str | None) -> str:
    parts = [str(error or "")]
    for item in results or []:
        if not isinstance(item, dict):
            continue
        for key in ("error", "message", "reason"):
            if item.get(key):
                parts.append(str(item[key]))
        nested = item.get("result")
        if isinstance(nested, dict) and nested.get("error"):
            parts.append(str(nested["error"]))
    return " ".join(parts).lower()


def classify_failure(
    *,
    dispatched: bool,
    verified: bool,
    before_text: str,
    after_text: str,
    results: list[dict[str, Any]] | None,
    error: str | None,
    error_type: str | None = None,
) -> str:
    """Return one taxonomy reason for a non-ok act."""
    if error_type == VERIFICATION_REQUIRED or (
        error and "verification_required" in str(error)
    ):
        return VERIFICATION_REQUIRED

    blob = _err_blob(results, error)
    after = (after_text or "").strip()
    before = (before_text or "").strip()

    if not after:
        return OBSERVATION_INCOMPLETE
    if any(
        token in blob
        for token in (
            "empty cuaservice state",
            "empty state",
            "screenshot missing",
            "observation",
            "no accessibility",
            "ax tree empty",
        )
    ):
        return OBSERVATION_INCOMPLETE

    if any(
        token in blob
        for token in (
            "stale",
            "out of range",
            "index",
            "detached",
            "invalid element",
            "element_index",
            "no longer",
        )
    ) and any(token in blob for token in ("stale", "range", "detach", "invalid", "index")):
        # Prefer stale when index/id language dominates over pure "not found".
        if any(token in blob for token in ("stale", "detached", "out of range", "no longer")):
            return STALE_ID

    if not dispatched:
        if any(
            token in blob
            for token in (
                "not found",
                "label not found",
                "no match",
                "no element",
                "element_not_found",
                "could not resolve",
                "unknown label",
                "target_missing",
            )
        ):
            return TARGET_MISSING
        if any(token in blob for token in ("stale", "detached", "out of range")):
            return STALE_ID
        return ACTION_FAILED

    if dispatched and not verified:
        return EXPECT_UNVERIFIED

    return ACTION_FAILED


def _target_hints(arguments: dict[str, Any], results: list[dict[str, Any]] | None) -> list[str]:
    hints: list[str] = []
    for key in ("label", "text", "key", "op", "path", "url"):
        if arguments.get(key) is not None:
            hints.append(f"{key}={arguments.get(key)!s}"[:120])
    if arguments.get("element") is not None:
        hints.append(f"element={arguments.get('element')}")
    if arguments.get("expect") is not None:
        hints.append(f"expect={arguments.get('expect')!s}"[:120])
    steps = arguments.get("steps")
    if isinstance(steps, list) and steps:
        labels = []
        for step in steps[:6]:
            if isinstance(step, dict) and step.get("label"):
                labels.append(str(step["label"])[:40])
        if labels:
            hints.append("steps=" + ">".join(labels))
    if results:
        last = results[-1] if isinstance(results[-1], dict) else {}
        method = last.get("method")
        if method:
            hints.append(f"method={method}")
    return hints


def _nearby_lines(tree: str, arguments: dict[str, Any], *, limit: int = 8) -> list[str]:
    if not tree:
        return []
    lines = tree.splitlines()
    needles: list[str] = []
    for key in ("label", "text", "expect"):
        val = arguments.get(key)
        if val:
            needles.append(str(val))
    steps = arguments.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if isinstance(step, dict) and step.get("label"):
                needles.append(str(step["label"]))
    needles = [n for n in needles if n]
    hits: list[str] = []
    if lines:
        hits.append(lines[0][:160])  # window header
    for line in lines[1:]:
        if any(n.lower() in line.lower() for n in needles):
            hits.append(line[:160])
        if len(hits) >= limit:
            break
    if len(hits) == 1 and len(lines) > 1:
        # No needle hits — include a few static text / value lines.
        for line in lines[1:]:
            if "value=" in line or "AXStaticText" in line or "AXText" in line:
                hits.append(line[:160])
            if len(hits) >= min(limit, 4):
                break
    return hits


def format_failure_text(
    *,
    reason: str,
    arguments: dict[str, Any],
    before_text: str,
    after_text: str,
    results: list[dict[str, Any]] | None,
    error: str | None,
) -> str:
    """Human/model-facing compact failure body (not a full AX dump)."""
    tried = _target_hints(arguments, results)
    nearby = _nearby_lines(after_text, arguments)
    err = str(error or "").strip()
    if err and len(err) > 200:
        err = err[:200] + "…"
    lines = [
        f"reason: {reason}",
        f"hint: {_REASON_HINTS.get(reason, 'Inspect failure and change strategy.')}",
    ]
    if tried:
        lines.append("tried: " + "; ".join(tried))
    if err:
        lines.append(f"error: {err}")
    if nearby:
        lines.append("nearby:")
        lines.extend(f"  {ln}" for ln in nearby)
    text = "\n".join(lines)
    # Hard cap so failures stay smaller than typical full trees.
    if len(text) > 900:
        text = text[:897] + "…"
    return text


def apply_failure_feedback(
    payload: dict[str, Any],
    *,
    arguments: dict[str, Any],
    before_text: str,
    after_text: str,
) -> dict[str, Any]:
    """Mutate an act payload so non-ok results expose taxonomy + compact text."""
    if payload.get("ok") is True:
        return payload
    results = payload.get("results") if isinstance(payload.get("results"), list) else []
    reason = classify_failure(
        dispatched=bool(payload.get("dispatched")),
        verified=bool(payload.get("verified")),
        before_text=before_text,
        after_text=after_text,
        results=results,
        error=str(payload.get("error") or "") or None,
        error_type=str(payload.get("error_type") or "") or None,
    )
    payload["error_type"] = reason
    payload["failure"] = {
        "completed_steps": sum(item.get("ok") is True for item in results),
        "failed_step": next((i + 1 for i, item in enumerate(results) if item.get("ok") is not True), None),
    }
    payload["text"] = format_failure_text(
        reason=reason,
        arguments=arguments,
        before_text=before_text,
        after_text=after_text,
        results=results,
        error=str(payload.get("error") or "") or None,
    )
    # Text owns recovery details; retain only nonduplicated step metadata.
    for key in ("error", "results", "expect"):
        payload.pop(key, None)
    # Keep full tree off the model path; callers that need proof can re-state.
    payload["full_text_omitted"] = True
    return payload
