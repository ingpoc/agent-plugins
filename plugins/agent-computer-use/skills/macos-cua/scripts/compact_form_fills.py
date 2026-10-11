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


def match_set_value_indices(state: dict[str, Any], label: str) -> list[int]:
    """All field element_index values whose label matches exactly (case-insensitive)."""
    want = str(label).strip().lower()
    matches: list[int] = []
    for item in state.get("elements") or []:
        item_label = str(item.get("label") or "").strip().lower()
        role = str(item.get("role") or "")
        if item_label == want and (role in _FIELD_ROLES or "Text" in role):
            matches.append(int(item["element_index"]))
    return matches


def resolve_set_value_index(state: dict[str, Any], label: str) -> int | None:
    """Unique label → index. Ambiguous duplicates raise ValueError (Codex r9 #4)."""
    matches = match_set_value_indices(state, label)
    if len(matches) > 1:
        raise ValueError(
            f"ambiguous label {label!r}: {len(matches)} matching fields "
            f"(indices {matches}); refuse silent first-match"
        )
    if len(matches) == 1:
        return matches[0]
    return None


def fill_value_from_step(step: dict[str, Any]):
    if step.get("value") is not None:
        return step.get("value")
    return step.get("text")



def compile_set_value_step(
    step: dict[str, Any],
    *,
    elements_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile set_value for native plans. Always emits element_index or compile_error.

    CUAService rejects label-only set_value; resolve label→index before execute_plan.
    """
    fill_value = fill_value_from_step(step)
    if fill_value is None:
        raise ValueError("compile_set_value_step requires a fill value")
    element = step.get("element")
    idx = int(element) if element is not None else None
    if idx is None and step.get("label"):
        try:
            idx = resolve_set_value_index(elements_state or {}, str(step["label"]))
        except ValueError as exc:
            return {
                "method": "set_value",
                "params": {"value": str(fill_value)},
                "compile_error": f"set_value: {exc}",
            }
    if idx is None:
        err = (
            f"set_value: field not found: {step.get('label')}"
            if step.get("label")
            else "set_value needs label or element"
        )
        return {
            "method": "set_value",
            "params": {"value": str(fill_value)},
            "compile_error": err,
        }
    return {
        "method": "set_value",
        "params": {"element_index": int(idx), "value": str(fill_value)},
    }


def steps_need_label_state(steps: list) -> bool:
    return any(
        isinstance(s, dict)
        and str(s.get("action") or "").lower() == "set_value"
        and fill_value_from_step(s) is not None
        and s.get("element") is None
        and s.get("label")
        for s in steps
    )


def is_label_fill_step(step: dict[str, Any]) -> bool:
    return (
        str(step.get("action") or "").lower() == "set_value"
        and fill_value_from_step(step) is not None
        and step.get("element") is None
        and bool(step.get("label"))
    )


def step_mutates_layout(step: dict[str, Any]) -> bool:
    """True when the step can retarget later label→index mappings."""
    if step.get("wait") is not None:
        return False
    action = str(step.get("action") or step.get("op") or "").lower()
    if action in {"focus", "raise", "activate", "set_value"}:
        return False
    if step.get("key") is not None:
        return True
    if step.get("label") is not None or step.get("element") is not None:
        return True
    if step.get("text") is not None:  # type_text / free typing
        return True
    if action in {"open", "reveal", "scroll", "drag", "select_text"}:
        return True
    if step.get("x") is not None and step.get("y") is not None:
        return True
    if step.get("path") is not None or step.get("url") is not None:
        return True
    return False


def label_fill_follows_mutation(steps: list) -> bool:
    """True when a label fill is preceded by a layout-mutating step (Codex r9 #3)."""
    seen_mut = False
    for step in steps:
        if not isinstance(step, dict):
            continue
        if is_label_fill_step(step) and seen_mut:
            return True
        if step_mutates_layout(step):
            seen_mut = True
    return False



def compile_error_payload(err: str, expect: Any) -> dict[str, Any]:
    return {
        "ok": False, "verified": False, "dispatched": False,
        "completion": "unverified", "error": err, "method": "set_value",
        "results": [{"ok": False, "error": err, "method": "set_value"}],
        "expect": expect,
    }


def has_ui_effect(arguments: dict[str, Any]) -> bool:
    keys = (
        "label", "element", "action", "op", "text", "key", "x", "y",
        "path", "url", "direction", "from_x", "from_y", "to_x", "to_y",
    )
    steps = arguments.get("steps")
    candidates = steps if isinstance(steps, list) and steps else [arguments]
    return any(
        isinstance(step, dict)
        and any(step.get(key) is not None for key in keys)
        and str(step.get("op") or step.get("action") or "").lower()
        not in {"focus", "raise", "activate"}
        for step in candidates
    )
