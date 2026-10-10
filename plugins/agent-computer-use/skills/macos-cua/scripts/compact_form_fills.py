"""Native form-plan fills for compact act (compile_cua schema)."""
from __future__ import annotations

from typing import Any


def expectation_supports_value(expect: Any) -> bool:
    if not isinstance(expect, dict):
        return False
    value = expect.get("value")
    if not isinstance(value, dict):
        return False
    return bool(str(value.get("equals") or value.get("text") or "").strip())


def expectation_value_needle(expect: Any) -> str:
    if not isinstance(expect, dict) or not isinstance(expect.get("value"), dict):
        return ""
    return str(expect["value"].get("equals") or expect["value"].get("text") or "")


def resolve_set_value_index(state: dict[str, Any], label: str) -> int | None:
    want = str(label).strip().lower()
    for item in state.get("elements") or []:
        item_label = str(item.get("label") or "").strip().lower()
        role = str(item.get("role") or "")
        if item_label == want and ("Text" in role or role in {"AXTextField", "AXTextArea", "AXComboBox"}):
            return int(item["element_index"])
    return None


def fill_value_from_step(step: dict[str, Any]):
    if step.get("value") is not None:
        return step.get("value")
    return step.get("text")
