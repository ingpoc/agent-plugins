#!/usr/bin/env python3
"""Compact, typed failure feedback for macos-cua act results."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
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
    EXPECT_UNVERIFIED: "Dispatch ran but expect was not new in the settled observation; read the settled state below (no cua_state needed); do not claim done unless it shows the result.",
    VERIFICATION_REQUIRED: "Mutating act needs expect (or allow_unverified for dispatch-only).",
}
_CREDENTIAL_TARGET = re.compile(r"(?i)(password|secret|credential|api[_ -]?key|authorization|auth token|securetextfield)")


def _has_credential_target(arguments: dict[str, Any]) -> bool:
    rows = [arguments]
    for key in ("steps", "actions"):
        if isinstance(arguments.get(key), list):
            rows.extend(row for row in arguments[key] if isinstance(row, dict))
    return any(_CREDENTIAL_TARGET.search(" ".join(
        str(row.get(key) or "") for key in ("label", "role", "field", "name", "target")
    )) for row in rows)

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
    credential_target = _has_credential_target(arguments)
    for key in ("label", "text", "key", "op", "path", "url"):
        if credential_target and key == "text":
            continue
        if arguments.get(key) is not None:
            hints.append(f"{key}={arguments.get(key)!s}"[:120])
    if arguments.get("element") is not None:
        hints.append(f"element={arguments.get('element')}")
    if arguments.get("expect") is not None and not credential_target:
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


def _nearby_lines(tree: str, arguments: dict[str, Any], *, limit: int = 4) -> list[str]:
    if not tree:
        return []
    lines = tree.splitlines()
    if _has_credential_target(arguments):
        return [lines[0][:160]] if lines and re.search(r"(?i)(?:\bAXWindow\b|^Window:|^Window\s)", lines[0]) else []
    label_needles: list[str] = []
    needles: list[str] = []
    for key in ("text", "expect", "query"):
        val = arguments.get(key)
        if val:
            needles.append(str(val))
    if arguments.get("label"):
        label_needles.append(str(arguments["label"]))
    steps = arguments.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if isinstance(step, dict):
                if step.get("label"):
                    label_needles.append(str(step["label"]))
                for key in ("text", "expect", "query"):
                    if step.get(key):
                        needles.append(str(step[key]))
    label_needles = list(dict.fromkeys(n for n in label_needles if n))
    needles = list(dict.fromkeys(n for n in needles if n))
    hits: list[str] = []
    if lines and re.search(r"(?i)(?:\bAXWindow\b|^Window:|^Window\s)", lines[0]):
        hits.append(lines[0][:160])  # window header
    near: list[tuple[float, str]] = []
    for line in lines[1:]:
        labels = re.findall(r'["\']([^"\']{1,120})["\']', line)
        exact_label = any(label.casefold() == target.casefold()
                          for label in labels for target in label_needles)
        exact_text = any(re.search(rf"(?<!\w){re.escape(n)}(?!\w)", line, re.I) for n in needles)
        if exact_label or exact_text:
            hits.append(line[:160])
            continue
        for target in label_needles:
            score = max((SequenceMatcher(None, target.casefold(), label.casefold()).ratio()
                         for label in labels), default=0.0)
            if score >= 0.55 and not any(re.fullmatch(re.escape(target) + r"\d+", label, re.I)
                                         for label in labels):
                near.append((score, line[:160]))
                break
    hits.extend(line for _, line in sorted(near, reverse=True)[:max(0, limit - len(hits))])
    hits = hits[:limit]
    return hits


def _failure_facts(
    *, arguments: dict[str, Any], before_text: str, after_text: str,
    results: list[dict[str, Any]], error: str | None,
) -> dict[str, Any]:
    failed_step = next((i for i, item in enumerate(results) if item.get("ok") is not True), None)
    steps = arguments.get("steps")
    expected_step = (steps[failed_step] if failed_step is not None and isinstance(steps, list)
                     and failed_step < len(steps) and isinstance(steps[failed_step], dict) else arguments)
    target_keys = ("op", "label", "text", "query", "selector", "element", "element_index", "role", "expect")
    expected_target = {key: expected_step[key] for key in target_keys if key in expected_step}
    if arguments.get("app"):
        expected_target["app"] = arguments["app"]
    credential_target = _has_credential_target(arguments)
    if credential_target:
        for key in ("text", "value", "expect"):
            if key in expected_target:
                expected_target[key] = "[redacted]"
    failed_result = results[failed_step] if failed_step is not None else {}
    found_keys = ("element_index", "role", "label", "value", "method", "error_type", "error")
    found_target = {key: failed_result[key] for key in found_keys if key in failed_result}
    if credential_target and "value" in found_target:
        found_target["value"] = "[redacted]"
    lines = [line.strip() for line in str(after_text or "").splitlines() if line.strip()]
    label_terms = [str(expected_target["label"]).strip()] if expected_target.get("label") else []
    terms = [str(expected_target[key]).strip() for key in ("text", "query", "selector", "role")
             if expected_target.get(key)]
    matching: list[str] = []
    near: list[tuple[float, str]] = []
    for line in lines[1:]:
        labels = re.findall(r'["\']([^"\']{1,120})["\']', line) or [line]
        exact_label = any(target.casefold() == label.casefold()
                          for target in label_terms for label in labels)
        exact_text = any(re.search(rf"(?<!\w){re.escape(term)}(?!\w)", line, re.I) for term in terms)
        if exact_label or exact_text:
            matching.append(line)
            continue
        score = max((SequenceMatcher(None, term.casefold(), label.casefold()).ratio()
                     for term in label_terms for label in labels), default=0.0)
        if score >= 0.55 and not any(re.fullmatch(re.escape(term) + r"\d+", label, re.I)
                                     for term in label_terms for label in labels):
            near.append((score, line))
    near.sort(reverse=True)
    candidates = [] if credential_target else (matching[:4] or [line for _, line in near[:4]])
    window = lines[0][:160] if lines and re.search(r"(?i)(?:\bAXWindow\b|^Window:|^Window\s)", lines[0]) else None
    return {
        "completed_steps": sum(item.get("ok") is True for item in results),
        "failed_step": failed_step + 1 if failed_step is not None else None,
        "error": str(error or "") or None,
        "expected_target": expected_target,
        "found_target": found_target or None,
        "observation": {
            "state": "available" if lines else "empty",
            "window": window,
            "line_count": len(lines),
            "match_count": len(matching),
            "near_miss_count": len(near),
            "candidates": [line[:200] for line in candidates],
        },
        "before_observation_state": "available" if str(before_text or "").strip() else "empty",
    }


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
    payload["failure"] = _failure_facts(
        arguments=arguments, before_text=before_text, after_text=after_text,
        results=results, error=str(payload.get("error") or "") or None,
    )
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
    if reason == EXPECT_UNVERIFIED and after_text.strip():
        # Keep only target-relevant settled facts; the full AX tree never reaches the task view.
        observation = payload["failure"]["observation"]
        payload["text"] += "\nsettled observation: " + "; ".join(
            f"{key}={value}" for key, value in observation.items()
            if value not in (None, [], "")
        )
        payload["full_text_omitted"] = True
        return payload
    # Keep full tree off the model path; callers that need proof can re-state.
    payload["full_text_omitted"] = True
    return payload
