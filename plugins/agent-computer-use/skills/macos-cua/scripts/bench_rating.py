"""Pure 0-10 rating and compare helpers for macos-cua benchmark repeats.

No I/O. No subprocess. Callers supply already-measured repeat dicts.
"""
from __future__ import annotations

import math
from typing import Any


COMPARE_KEYS = ("duration_s", "max_step_ms", "output_bytes", "driver_calls")
TRUST_GATES = ("accuracy", "visibility")
GRADED_KEYS = (
    "speed",
    "reliability",
    "robustness",
    "efficiency",
    "token_efficiency",
)
SCORE_KEYS = TRUST_GATES + GRADED_KEYS
RATING_DIMS = SCORE_KEYS


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def percentile(values: list[float], p: float) -> float:
    """Inclusive linear interpolation (Hyndman & Fan type 7 / NumPy default).

    For a sorted sample v of length n, the rank is h = (p/100)*(n-1) on
    0-based indices. The result is
    v[floor(h)] + (h-floor(h)) * (v[ceil(h)] - v[floor(h)]).
    Empty input raises ValueError. p is a percent in [0, 100].
    """
    if not values:
        raise ValueError("percentile() of empty sequence")
    if not 0.0 <= float(p) <= 100.0:
        raise ValueError("percentile p must be in [0, 100]")
    ordered = sorted(float(item) for item in values)
    count = len(ordered)
    if count == 1:
        return ordered[0]
    rank = (float(p) / 100.0) * (count - 1)
    low = int(math.floor(rank))
    high = int(math.ceil(rank))
    if low == high:
        return ordered[low]
    return ordered[low] + (rank - low) * (ordered[high] - ordered[low])


def p50(values: list[float]) -> float:
    return percentile(values, 50) if values else 0.0


def p95(values: list[float]) -> float:
    return percentile(values, 95) if values else 0.0


def trust_gate_zeros(scores: dict[str, Any]) -> list[str]:
    """Trust-gate names that scored 0. None is excluded, not treated as zero."""
    flags = []
    for name in TRUST_GATES:
        value = scores.get(name)
        if value is not None and float(value) == 0.0:
            flags.append(name)
    return flags


def repeat_measured(item: dict[str, Any]) -> dict[str, Any]:
    measured = item.get("measured")
    return measured if isinstance(measured, dict) else item


def score_measured(row: dict[str, Any], measured: dict[str, Any]) -> dict[str, bool]:
    duration_ok = float(measured.get("duration_s") or 999) <= float(row["budget_seconds"])
    step_budget = row.get("max_step_ms")
    measured_step = measured.get("max_step_ms")
    step_ok = step_budget is None or (
        measured_step is not None and 0 < int(measured_step) <= int(step_budget)
    )
    pointer_required = bool(row.get("pointer_required", True))
    return {
        "accuracy": bool(measured.get("readback")),
        "visibility": True if not pointer_required else bool(measured.get("cursor_visible")),
        "speed": duration_ok and step_ok,
        "context_efficiency": int(measured.get("output_bytes") or 0) <= int(row["bytes_budget"]),
        "robustness": bool(measured.get("robust")),
    }


def repeat_passed(item: dict[str, Any], row_contract: dict[str, Any] | None = None) -> bool:
    if "ok" in item:
        return bool(item["ok"])
    criteria = item.get("criteria")
    if isinstance(criteria, dict) and criteria:
        return all(criteria.values())
    if row_contract is not None:
        return all(score_measured(row_contract, repeat_measured(item)).values())
    return False


def numeric_samples(repeats: list[dict[str, Any]], key: str) -> list[float]:
    values = []
    for item in repeats:
        value = repeat_measured(item).get(key)
        if value is None:
            continue
        values.append(float(value))
    return values


def repeats_from_result(row: dict[str, Any]) -> list[dict[str, Any]]:
    raw = row.get("repeats")
    if isinstance(raw, list) and raw:
        return raw
    return [
        {
            "ok": row.get("ok"),
            "criteria": row.get("criteria") or {},
            "measured": row.get("measured") or {},
        }
    ]


def _ratio_score(p50_value: float, budget: float) -> float:
    if budget <= 0:
        return 0.0
    return 10.0 * clamp01(1.0 - p50_value / budget)


def _floor_score(p50_value: float, floor: float | None) -> float | None:
    """10 when at or below the irreducible floor, decaying as cost exceeds it.

    floor is None -> unrated. p50_value <= 0 -> 10 (nothing spent).
    floor <= 0 and p50_value > 0 -> 0 (spent against a zero floor).
    """
    if floor is None:
        return None
    if p50_value <= 0:
        return 10.0
    if floor <= 0:
        return 0.0
    return 10.0 * clamp01(float(floor) / p50_value)


def _round1(value: float) -> float:
    return round(float(value), 1)


def rate_row(row_contract: dict[str, Any], repeats: list[dict[str, Any]]) -> dict[str, Any]:
    if not repeats:
        raise ValueError("rate_row() requires at least one repeat")
    durations = numeric_samples(repeats, "duration_s") or [0.0]
    p50_duration = percentile(durations, 50)
    p95_duration = percentile(durations, 95)
    accuracy = 10.0 if all(repeat_measured(item).get("readback") for item in repeats) else 0.0
    if not bool(row_contract.get("pointer_required", True)):
        visibility: float | None = None
    else:
        visibility = (
            10.0
            if all(repeat_measured(item).get("cursor_visible") for item in repeats)
            else 0.0
        )
    speed = _floor_score(p50_duration, row_contract["floor_seconds"])
    step_floor = row_contract.get("floor_max_step_ms")
    steps = numeric_samples(repeats, "max_step_ms")
    if speed is not None and step_floor is not None and steps:
        step_score = _floor_score(percentile(steps, 50), step_floor)
        if step_score is not None:
            speed = (speed + step_score) / 2.0
    reliability = 10.0 * (
        sum(repeat_passed(item, row_contract) for item in repeats) / len(repeats)
    )
    if p50_duration > 0:
        spread = p95_duration / p50_duration
    else:
        spread = float("inf") if p95_duration > 0 else 1.0
    if spread > 1.5:
        reliability = max(0.0, reliability - 2.0)
    robustness = 10.0 * (
        sum(bool(repeat_measured(item).get("robust")) for item in repeats) / len(repeats)
    )
    efficiency = _floor_score(
        percentile(numeric_samples(repeats, "ax_snapshots") or [0.0], 50),
        row_contract.get("floor_ax_snapshots"),
    )
    token_efficiency = _floor_score(
        percentile(numeric_samples(repeats, "driver_calls") or [0.0], 50),
        row_contract.get("floor_driver_calls"),
    )
    scores: dict[str, Any] = {
        "accuracy": _round1(accuracy),
        "visibility": None if visibility is None else _round1(visibility),
        "speed": None if speed is None else _round1(speed),
        "reliability": _round1(reliability),
        "robustness": _round1(robustness),
        "efficiency": None if efficiency is None else _round1(efficiency),
        "token_efficiency": None if token_efficiency is None else _round1(token_efficiency),
    }
    present = [float(scores[key]) for key in GRADED_KEYS if scores[key] is not None]
    scores["overall"] = _round1(sum(present) / len(present)) if present else 0.0
    scores["unrated"] = sorted(key for key in GRADED_KEYS if scores[key] is None)
    scores["trust_gate_zeros"] = trust_gate_zeros(scores)
    scores["gated"] = bool(scores["trust_gate_zeros"])
    return scores


def compare_row(
    current_repeats: list[dict[str, Any]],
    baseline_repeats: list[dict[str, Any]],
) -> dict[str, Any]:
    """p50 deltas vs a previous row. Positive delta means worse (higher cost)."""
    payload: dict[str, Any] = {}
    regressed = False
    for key in COMPARE_KEYS:
        current = numeric_samples(current_repeats, key)
        baseline = numeric_samples(baseline_repeats, key)
        if not current or not baseline:
            payload[key] = None
            continue
        delta = percentile(current, 50) - percentile(baseline, 50)
        payload[key] = round(delta, 3)
        if delta > 0:
            regressed = True
    payload["regressed"] = regressed
    return payload


def rate_suite(rows: list[dict[str, Any]]) -> dict[str, Any]:
    per_row: dict[str, Any] = {}
    for item in rows:
        repeats = item.get("repeats") or []
        contract = item.get("contract") or {
            key: value for key, value in item.items() if key != "repeats"
        }
        name = str(item.get("name") or contract.get("name") or "row")
        per_row[name] = rate_row(contract, repeats)
    overall: dict[str, Any] = {}
    for dim in SCORE_KEYS:
        values = [rated[dim] for rated in per_row.values() if rated.get(dim) is not None]
        overall[dim] = _round1(sum(values) / len(values)) if values else None
    trust_failures = [name for name, rated in per_row.items() if rated.get("trust_gate_zeros")]
    return {"per_row": per_row, "overall": overall, "trust_failures": trust_failures}


def format_rating_summary(
    per_row: dict[str, Any],
    overall: dict[str, Any],
    trust_failures: list[str],
) -> str:
    labels = (
        ("accuracy", "acc"),
        ("visibility", "vis"),
        ("speed", "spd"),
        ("reliability", "rel"),
        ("robustness", "rob"),
        ("efficiency", "eff"),
        ("token_efficiency", "tok"),
    )

    def _line(name: str, ratings: dict[str, Any]) -> str:
        bits = [
            f"{short}={ratings.get(key) if ratings.get(key) is not None else '—'}"
            for key, short in labels
        ]
        return f"{name:22} " + " ".join(bits)

    lines = []
    for name, payload in per_row.items():
        ratings = (
            payload["ratings"]
            if isinstance(payload, dict) and "ratings" in payload
            else payload
        )
        lines.append(_line(name, ratings))
    lines.append(_line("overall", overall))
    if trust_failures:
        lines.append("trust_failures: " + ", ".join(trust_failures))
    return "\n".join(lines)
