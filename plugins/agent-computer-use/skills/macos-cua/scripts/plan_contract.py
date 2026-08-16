#!/usr/bin/env python3
"""Deterministic plan and action-result contracts for macos-cua."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ActionSpec:
    mutating: bool = False
    needs_state: bool = False
    required: tuple[str, ...] = ()
    target: str | None = None


ACTION_SPECS: dict[str, ActionSpec] = {
    "click": ActionSpec(mutating=True, needs_state=True, target="click"),
    "double_click": ActionSpec(mutating=True, needs_state=True, target="click"),
    "perform_action": ActionSpec(
        mutating=True, needs_state=True, required=("name",), target="element"
    ),
    "drag": ActionSpec(
        mutating=True,
        required=("from_x", "from_y", "to_x", "to_y"),
    ),
    "type": ActionSpec(mutating=True, needs_state=True),
    "set_value": ActionSpec(
        mutating=True, needs_state=True, target="element"
    ),
    "select_text": ActionSpec(
        mutating=True, needs_state=True, required=("text",), target="element"
    ),
    "key": ActionSpec(mutating=True, required=("keys",)),
    "scroll": ActionSpec(mutating=True, needs_state=True),
    "right_click": ActionSpec(mutating=True, needs_state=True, target="element"),
    "wait": ActionSpec(),
    "state": ActionSpec(),
    "snapshot": ActionSpec(),
    "expect": ActionSpec(),
}

# CLI verb aliases → plan action names (agents often paste CLI names into plans).
ACTION_ALIASES: dict[str, str] = {
    "click-label": "click",
    "click_label": "click",
    "click-label-pointer": "click",
    "click_label_pointer": "click",
    "double-click": "double_click",
    "right-click": "right_click",
    "perform-action": "perform_action",
    "set-value": "set_value",
    "select-text": "select_text",
    "type-text": "type",
    "type-label": "type",
    "type_label": "type",
}

MUTATING_ACTIONS = frozenset(
    name for name, spec in ACTION_SPECS.items() if spec.mutating
)


def normalize_action_name(action: Any) -> Any:
    if not isinstance(action, str):
        return action
    return ACTION_ALIASES.get(action, action)


def _has_pair(step: dict[str, Any], first: str, second: str) -> bool:
    return step.get(first) is not None and step.get(second) is not None


def _target_valid(step: dict[str, Any], target: str | None) -> bool:
    if target is None:
        return True
    if target == "click":
        return bool(step.get("label")) or step.get("element") is not None or _has_pair(
            step, "x", "y"
        )
    return bool(step.get("label")) or step.get("element") is not None


def validate_plan(plan: Any) -> list[dict[str, Any]]:
    """Return compact deterministic errors; an empty list means valid."""
    if not isinstance(plan, dict):
        return [{"path": "$", "code": "plan_not_object"}]
    errors: list[dict[str, Any]] = []
    actions = plan.get("actions")
    if not isinstance(actions, list):
        return [{"path": "$.actions", "code": "actions_not_array"}]
    if plan.get("output", "compact") not in {"compact", "full"}:
        errors.append({"path": "$.output", "code": "invalid_output_mode"})
    if plan.get("capture", "failures") not in {"never", "failures", "always"}:
        errors.append({"path": "$.capture", "code": "invalid_capture_mode"})
    for index, step in enumerate(actions):
        path = f"$.actions[{index}]"
        if not isinstance(step, dict):
            errors.append({"path": path, "code": "action_not_object"})
            continue
        action = normalize_action_name(step.get("action"))
        if action != step.get("action") and isinstance(action, str):
            step["action"] = action
        if action == "perform_action" and not step.get("name"):
            step["name"] = "press"
        if action not in ACTION_SPECS:
            errors.append(
                {"path": f"{path}.action", "code": "unknown_action", "value": step.get("action")}
            )
            continue
        spec = ACTION_SPECS[action]
        for field in spec.required:
            if field not in step or step[field] is None:
                errors.append(
                    {"path": f"{path}.{field}", "code": "required_field_missing"}
                )
        if not _target_valid(step, spec.target):
            errors.append({"path": path, "code": "target_missing"})
        if action == "click" and (step.get("x") is None) != (step.get("y") is None):
            errors.append({"path": path, "code": "coordinate_pair_incomplete"})
    return errors


def action_needs_state(action: str) -> bool:
    spec = ACTION_SPECS.get(action)
    return bool(spec and spec.needs_state)


def has_verifiable_expectation(expectations: Any) -> bool:
    if isinstance(expectations, str):
        return bool(expectations.strip())
    if isinstance(expectations, (list, tuple)):
        return bool(expectations) and all(
            has_verifiable_expectation(expected) for expected in expectations
        )
    if not isinstance(expectations, dict) or not expectations:
        return False
    for key in ("text", "not_text", "label"):
        if key in expectations and str(expectations[key]).strip():
            return True
    if str(expectations.get("role") or "").strip():
        return True
    if "element_count_min" in expectations:
        try:
            if int(expectations["element_count_min"]) >= 1:
                return True
        except (TypeError, ValueError):
            pass
    value = expectations.get("value")
    if isinstance(value, dict):
        if "equals" in value:
            return True
        if "contains" in value and str(value["contains"]).strip():
            return True
    return False


def _element_search_text(element: dict[str, Any]) -> str:
    return "\n".join(
        str(element.get(key) or "") for key in ("label", "value", "derived_text")
    ).strip()


def expectation_needles(expectations: Any) -> list[str]:
    if expectations is None:
        return []
    if isinstance(expectations, str):
        return [expectations] if expectations.strip() else []
    if isinstance(expectations, (list, tuple)):
        needles: list[str] = []
        for item in expectations:
            needles.extend(expectation_needles(item))
        return needles
    if not isinstance(expectations, dict):
        return []
    needles = [
        str(expectations[key]).strip()
        for key in ("text", "label")
        if str(expectations.get(key) or "").strip()
    ]
    value = expectations.get("value")
    if isinstance(value, dict):
        for key in ("equals", "contains"):
            if str(value.get(key) or "").strip():
                needles.append(str(value[key]).strip())
    return needles


def expectation_roles(expectations: Any) -> list[str]:
    if isinstance(expectations, dict):
        role = str(expectations.get("role") or "").strip()
        return [role] if role else []
    if isinstance(expectations, (list, tuple)):
        roles: list[str] = []
        for item in expectations:
            roles.extend(expectation_roles(item))
        return roles
    return []


def compact_state_text(text: str, expectations: Any, *, max_lines: int = 12) -> str:
    needles = [item.lower() for item in expectation_needles(expectations)]
    roles = [item.lower() for item in expectation_roles(expectations)]
    lines = [line for line in (text or "").splitlines() if line.strip()]
    if not needles and not roles:
        return ""
    matched = []
    for line in lines:
        lower = line.lower()
        if needles and not any(needle in lower for needle in needles):
            continue
        if roles and not any(role in lower for role in roles):
            continue
        matched.append(line)
    return "\n".join(matched[:max_lines])


def compact_step(row: dict[str, Any]) -> dict[str, Any]:
    """Keep bounded diagnostics on both success and failure."""
    compact = {
        key: row[key]
        for key in (
            "step",
            "action",
            "accepted",
            "duration_ms",
            "label",
            "element",
            "name",
            "keys",
            "state_reused",
        )
        if key in row and row[key] is not None
    }
    result = row.get("result")
    if isinstance(result, dict):
        for key in (
            "method",
            "path",
            "effect",
            "escalation",
            "error",
            "code",
            "reason",
            "selection_type",
            "range",
            "verified_range",
            "seconds",
        ):
            if result.get(key) is not None:
                compact[key] = result[key]
        nested = result.get("result")
        if compact.get("error") is None and isinstance(nested, dict) and nested.get("error"):
            compact["error"] = str(nested["error"])[:300]
        sync = ((result.get("move") or {}).get("sync") or {}).get("duration_ms")
        if sync is not None:
            compact["cursor_sync_ms"] = sync
        move_error = ((result.get("move") or {}).get("sync") or {}).get("error")
        if move_error:
            compact["cursor_sync_error"] = str(move_error)[:300]
    if row.get("verification") is not None:
        compact["verification"] = row["verification"]
    return compact


def evaluate_expectations(
    snapshot_data: dict[str, Any],
    expectations: Any,
    *,
    ignore_element_indices: list[int] | tuple[int, ...] | None = None,
):
    if expectations is None:
        return True, []
    if not has_verifiable_expectation(expectations):
        return False, [{"ok": False, "error": "expectation is empty or unsupported"}]
    if isinstance(expectations, (str, dict)):
        expectations = [expectations]
    ignored = {int(index) for index in (ignore_element_indices or [])}
    tree = snapshot_data.get("tree_markdown", "")
    elements = [
        item
        for item in snapshot_data.get("elements", [])
        if item.get("element_index") not in ignored
    ]
    searchable = "\n".join([tree, *(_element_search_text(item) for item in elements)])
    if ignored:
        searchable = "\n".join(_element_search_text(item) for item in elements)
    details = []
    for expected in expectations:
        if isinstance(expected, str):
            ok = expected.lower() in searchable.lower()
            details.append({"text": expected, "ok": ok})
            continue
        ok = True
        detail = {"expect": expected}
        scoped = elements
        if expected.get("role"):
            role_needle = str(expected["role"]).lower()
            scoped = [
                item
                for item in elements
                if role_needle in str(item.get("role") or "").lower()
            ]
            ok = bool(scoped)
        scoped_text = "\n".join(_element_search_text(item) for item in scoped)
        haystack = scoped_text if expected.get("role") else searchable
        if "text" in expected:
            ok = ok and str(expected["text"]).lower() in haystack.lower()
        if "not_text" in expected:
            ok = ok and str(expected["not_text"]).lower() not in haystack.lower()
        if "label" in expected:
            needle = str(expected["label"]).lower()
            ok = ok and any(
                needle in _element_search_text(element).lower() for element in scoped
            )
        if "element_count_min" in expected:
            ok = ok and snapshot_data.get("element_count", 0) >= int(
                expected["element_count_min"]
            )
        if "value" in expected:
            value_expect = expected["value"]
            label = str(value_expect.get("label", "")).lower()
            matches = [
                element
                for element in scoped
                if label in _element_search_text(element).lower()
            ]
            values = [
                str(element.get("value") or element.get("derived_text") or "")
                for element in matches
            ]
            if "equals" in value_expect:
                ok = ok and str(value_expect["equals"]) in values
            if "contains" in value_expect:
                needle = str(value_expect["contains"]).lower()
                ok = ok and any(needle in value.lower() for value in values)
        detail["ok"] = ok
        details.append(detail)
    return all(item["ok"] for item in details), details


def unasserted_plan_steps(plan: dict[str, Any]) -> list[int]:
    if has_verifiable_expectation(plan.get("expect")):
        return []
    return [
        index
        for index, step in enumerate(plan.get("actions", []), start=1)
        if step.get("action") in MUTATING_ACTIONS
        and not has_verifiable_expectation(step.get("expect"))
    ]


def result_accepted(result: Any) -> bool:
    """Require an explicit success signal; ambiguous payloads fail closed."""
    if not isinstance(result, dict) or result.get("error"):
        return False
    if "accepted" in result:
        return result.get("accepted") is True
    if result.get("effect") == "suspected_noop":
        return False
    if "ok" in result:
        return result.get("ok") is True
    effect = result.get("effect")
    return effect in {"confirmed", "unverifiable"}
