"""Native form-plan fills for compact act (compile_cua schema)."""
from __future__ import annotations

from typing import Any

_FIELD_ROLES = {"AXTextField", "AXTextArea", "AXComboBox"}


def expectation_supports_value(expect: Any) -> bool:
    if not isinstance(expect, dict):
        return False
    value = expect.get("value")
    if not isinstance(value, dict):
        return False
    return bool(str(value.get("equals") or value.get("text") or "").strip())


def expectation_value_needle(expect: Any) -> str:
    """Legacy: plain equals string for callers that still scan tree text."""
    if not isinstance(expect, dict) or not isinstance(expect.get("value"), dict):
        return ""
    return str(expect["value"].get("equals") or expect["value"].get("text") or "")


def expectation_value_field_matches(expect: Any, after_state: Any) -> bool:
    """Resolve expect.value.label uniquely in after_state and compare value exactly."""
    if not isinstance(expect, dict) or not isinstance(expect.get("value"), dict):
        return False
    spec = expect["value"]
    label = str(spec.get("label") or "").strip()
    equals = str(spec.get("equals") if spec.get("equals") is not None else spec.get("text") or "")
    if not label:
        return False
    want = label.lower()
    matches = []
    for item in (after_state or {}).get("elements") or []:
        item_label = str(item.get("label") or "").strip().lower()
        role = str(item.get("role") or "")
        if item_label == want and (role in _FIELD_ROLES or "Text" in role):
            matches.append(item)
    if len(matches) != 1:
        return False
    return str(matches[0].get("value") if matches[0].get("value") is not None else "") == equals


def resolve_set_value_index(state: dict[str, Any], label: str) -> int | None:
    want = str(label).strip().lower()
    for item in state.get("elements") or []:
        item_label = str(item.get("label") or "").strip().lower()
        role = str(item.get("role") or "")
        if item_label == want and (role in _FIELD_ROLES or "Text" in role):
            return int(item["element_index"])
    return None


def fill_value_from_step(step: dict[str, Any]):
    if step.get("value") is not None:
        return step.get("value")
    return step.get("text")
