# Runtime dependencies are injected by the compatibility facade.
# ruff: noqa: F401, F821
"""Batched plan execution, expectations, and compact results."""
from __future__ import annotations

import time

def _plan_snapshot(pid, window_id, max_elements=120):
    state = _native_ax_snapshot(pid, max_elements=max_elements, window_id=window_id)
    if snapshot_content_error(state):
        return snapshot(pid, window_id, max_elements=max_elements)
    return state

def evaluate_expectations(snapshot_data, expectations, *, ignore_element_indices=None):
    """Evaluate deterministic state assertions against a fresh snapshot."""
    return _plan_contract().evaluate_expectations(
        snapshot_data,
        expectations,
        ignore_element_indices=ignore_element_indices,
    )

def wait_for_expectations(
    pid,
    window_id,
    expectations,
    *,
    timeout=5.0,
    poll=0.2,
    max_elements=120,
    ignore_element_indices=None,
):
    """Poll AX until expectations hold. Always snap; never trust a pre-mutation tree."""
    deadline = time.monotonic() + timeout
    last = None
    details = []
    while time.monotonic() <= deadline:
        last = _plan_snapshot(pid, window_id, max_elements=max_elements)
        ok, details = evaluate_expectations(
            last, expectations, ignore_element_indices=ignore_element_indices
        )
        if ok:
            return True, details, last
        time.sleep(poll)
    return False, details, last or {}


def _accepted(result):
    return _plan_contract().result_accepted(result)


def _has_verifiable_expectation(expectations):
    return _plan_contract().has_verifiable_expectation(expectations)


def _unasserted_plan_steps(plan):
    return _plan_contract().unasserted_plan_steps(plan)


def run_actions(
    pid, window_id, plan, *, app_name="app", foreground_prepared=False
):
    """Run a state-aware action plan with fresh indices and assertions."""
    started = time.monotonic()
    plan_errors = _plan_contract().validate_plan(plan)
    if plan_errors:
        return {
            "ok": False,
            "accepted": False,
            "verified": False,
            "code": "invalid_plan",
            "errors": plan_errors,
            "duration_ms": round((time.monotonic() - started) * 1000),
        }
    unasserted = _unasserted_plan_steps(plan)
    allow_unverified = bool(plan.get("allow_unverified", False))
    if unasserted and not allow_unverified:
        return {
            "ok": False,
            "code": "assertion_required",
            "error": "mutating plans require a final or per-step expectation",
            "unasserted_steps": unasserted,
            "duration_ms": 0,
        }
    output_mode = str(plan.get("output", "compact")).lower()
    operator_update(
        app_name,
        pid,
        window_id,
        status="acting",
        active=True,
        message=f"Running {len(plan.get('actions', []))} asserted actions",
    )
    results = []
    max_elements = int(plan.get("max_elements", 120))
    # Keep the user's system pointer untouched, while making agent actions
    # human-legible through the signed software cursor by default.
    pointer = bool(plan.get("pointer", True))
    settle = max(0.0, float(plan.get("settle_ms", 0)) / 1000.0)
    dispatches_accepted = True
    assertions_passed = True
    acted_indices: list[int] = []
    latest_snapshot = None
    latest_snapshot_fresh = False
    seed = plan.get("seed_snapshot")
    if isinstance(seed, dict) and (
        seed.get("elements") is not None or seed.get("tree_markdown")
    ):
        latest_snapshot = seed
        latest_snapshot_fresh = True
    selected_element = None
    state_reuses = 0
    cursor_cleanup = None
    if pointer and any(
        step.get("action")
        in {"click", "type", "set_value", "perform_action", "right_click"}
        and (step.get("label") or step.get("element") is not None)
        for step in plan.get("actions", [])
    ):
        cursor_cleanup = _cleanup_driver_cursors()
    for i, step in enumerate(plan.get("actions", []), start=1):
        step_started = time.monotonic()
        act = step.get("action")
        row = {"step": i, "action": act}
        current = None
        result = None
        element = step.get("element")
        pointer_label_click = bool(act == "click" and step.get("label") and pointer)
        needs_state = _plan_contract().action_needs_state(act)
        if pointer_label_click and latest_snapshot_fresh and latest_snapshot is not None:
            current = latest_snapshot
            row["state_reused"] = True
            state_reuses += 1
        if needs_state and current is None:
            if latest_snapshot_fresh and latest_snapshot is not None:
                current = latest_snapshot
                row["state_reused"] = True
                state_reuses += 1
            else:
                current = _plan_snapshot(
                    pid,
                    window_id,
                    max_elements=int(step.get("max_elements", max_elements)),
                )

        if act == "click":
            label = step.get("label")
            if element is None and label and current is not None:
                element, label_err = resolve_clickable_index(current, label)
                if label_err is not None and row.get("state_reused"):
                    current = _plan_snapshot(
                        pid,
                        window_id,
                        max_elements=int(step.get("max_elements", max_elements)),
                    )
                    row["state_reused"] = False
                    element, label_err = resolve_clickable_index(current, label)
                if label_err is not None:
                    result = label_err
                    row.update(label=label, element=None)
                else:
                    label_err = None
            else:
                label_err = None
            if result is None and label and pointer:
                try:
                    result = click_label_pointer(
                        pid,
                        window_id,
                        label,
                        int(step.get("max_elements", max_elements)),
                        snapshot_data=current,
                        app_name=app_name,
                        prepare_cursor=False,
                    )
                    element = result.get("element")
                except Exception as exc:
                    if getattr(exc, "error_code", None) == "ambiguous_label" or exc.__class__.__name__ == "AmbiguousLabelError":
                        result = {
                            "ok": False,
                            "error": str(exc),
                            "error_code": "ambiguous_label",
                            "matches": getattr(exc, "matches", []),
                        }
                    else:
                        raise
            elif result is None and element is not None:
                result = click_with_retry(
                    pid, window_id, element, max_elements,
                    app_name=app_name if pointer else None,
                )
            elif result is None and step.get("x") is not None and step.get("y") is not None:
                point_proof = None
                if pointer:
                    point_proof = app_state(
                        app_name,
                        pid,
                        window_id,
                        max_elements=int(step.get("max_elements", max_elements)),
                        include_screenshot=True,
                        foreground_prepared=foreground_prepared,
                    )
                    if not point_proof.get("ok"):
                        result = {
                            "ok": False,
                            "error": "fresh point-input observation failed",
                            "detail": point_proof.get("error"),
                        }
                if result is None:
                    result = click_point(
                        pid,
                        window_id,
                        step["x"],
                        step["y"],
                        button=step.get("button", "left"),
                        click_count=int(step.get("count", 1)),
                        delivery_mode=step.get("delivery_mode", "background"),
                        debug_image_out=step.get("debug_image_out"),
                        preserve_pointer=bool(step.get("preserve_pointer", False)),
                        app_name=app_name if pointer else None,
                    )
            elif result is None:
                result = {"error": "click requires label, element, or x/y"}
            row.update(label=label, element=element)
        elif act == "double_click":
            label = step.get("label")
            if element is None and label:
                element, label_err = resolve_clickable_index(current, label)
                if label_err is not None:
                    result = label_err
            has_point = step.get("x") is not None and step.get("y") is not None
            if result is None and (element is not None or has_point):
                result = double_click(
                    pid,
                    window_id,
                    element_index=element,
                    x=step.get("x"),
                    y=step.get("y"),
                    delivery_mode=step.get("delivery_mode", "background"),
                    snapshot_data=current if element is not None else None,
                    app_name=app_name if pointer else None,
                )
            elif result is None:
                result = {"error": "double_click requires label, element, or x/y"}
            row.update(label=label, element=element)
        elif act == "perform_action":
            label = step.get("label")
            if element is None and label:
                element, label_err = resolve_clickable_index(current, label)
                if label_err is not None:
                    result = label_err
            pre = pointer_preflight(
                pointer,
                app_name,
                pid,
                window_id,
                current,
                element,
                f"Moving to {label or element}",
            )
            if pre and not pre.get("ok"):
                result = pre
            elif result is None:
                result = (
                    perform_action(
                        pid, window_id, element, step["name"], snapshot_data=current
                    )
                    if element is not None
                    else {"error": "perform_action: element not found"}
                )
                result = merge_pointer_proof(result, pre)
            row.update(label=label, element=element, name=step.get("name"))
        elif act == "drag":
            result = drag(
                pid,
                window_id,
                step["from_x"],
                step["from_y"],
                step["to_x"],
                step["to_y"],
                delivery_mode=step.get("delivery_mode", "background"),
                duration_ms=int(step.get("duration_ms", 500)),
                steps=int(step.get("steps", 20)),
                app_name=app_name,
            )
        elif act in ("type", "set_value"):
            label = step.get("label")
            if element is None and label:
                element = find_field_index(current, label)
            focus = None
            if label and pointer and element is not None:
                focus = click_label_pointer(
                    pid,
                    window_id,
                    label,
                    int(step.get("max_elements", max_elements)),
                    snapshot_data=current,
                    app_name=app_name,
                    prepare_cursor=False,
                    element_index=element,
                )
                if not focus.get("ok"):
                    result = {
                        "ok": False,
                        "error": "visible agent cursor could not focus the field",
                        "focus": focus,
                    }
            if result is not None:
                pass
            elif element is None and act == "set_value":
                result = {"error": f"{act}: field not found"}
            elif act == "set_value":
                result = set_value(pid, window_id, element, step.get("value", ""))
            else:
                element = element if element is not None else selected_element
                result = type_text(
                    pid,
                    window_id,
                    element,
                    step.get("text", ""),
                    x=step.get("x"),
                    y=step.get("y"),
                    delivery_mode=step.get("delivery_mode", "background"),
                    allow_newline=bool(step.get("allow_newline", False)),
                )
                selected_element = None
            row.update(label=label, element=element, focus=focus)
        elif act == "select_text":
            label = step.get("label")
            if element is None and label:
                element = find_field_index(current, label)
            result = (
                select_text_action(
                    pid,
                    current,
                    element,
                    step["text"],
                    prefix=step.get("prefix"),
                    suffix=step.get("suffix"),
                    selection_type=step.get("selection_type", "text"),
                )
                if element is not None
                else {"error": "select_text: field not found"}
            )
            if _accepted(result):
                selected_element = element
            row.update(label=label, element=element)
        elif act == "key":
            result = press_key(
                pid,
                window_id,
                step["keys"],
                step.get("delivery_mode", "background"),
            )
            row["keys"] = step["keys"]
            shortcut = "+".join(step["keys"]) if isinstance(step["keys"], list) else str(step["keys"])
            if _accepted(result) and shortcut.lower().replace("command", "cmd") == "cmd+n":
                window_id = _new_ax_window_id(pid, window_id) or window_id
                _write_cache(app_name, pid, window_id)
                row["window_id"] = window_id
        elif act == "scroll":
            if element is None and step.get("label"):
                element, label_err = resolve_clickable_index(current, step["label"])
                if label_err is not None:
                    result = label_err
            if result is None:
                result = scroll(
                    pid,
                    window_id,
                    step.get("direction", "down"),
                    int(step.get("amount", 3)),
                    by=step.get("by", "line"),
                    element_index=element,
                    x=step.get("x"),
                    y=step.get("y"),
                    delivery_mode=step.get("delivery_mode", "background"),
                )
            row["element"] = element
        elif act == "right_click":
            label = step.get("label")
            if element is None and label:
                element, label_err = resolve_clickable_index(current, label)
                if label_err is not None:
                    result = label_err
            if result is None:
                result = (
                    right_click(pid, window_id, element, app_name=app_name)
                    if element is not None
                    else {"error": "right_click: element not found"}
                )
            row.update(label=label, element=element)
        elif act == "wait":
            seconds = float(step.get("seconds", 0.5))
            time.sleep(seconds)
            result = {"ok": True, "seconds": seconds}
        elif act in ("state", "snapshot"):
            current = _plan_snapshot(
                pid,
                window_id,
                max_elements=int(step.get("max_elements", max_elements)),
            )
            result = {
                "ok": bool(current.get("tree_markdown")),
                "element_count": current.get("element_count", 0),
            }
        elif act == "expect":
            ok, details, current = wait_for_expectations(
                pid,
                window_id,
                step.get("expect"),
                timeout=float(step.get("timeout", 5)),
                max_elements=int(step.get("max_elements", max_elements)),
                ignore_element_indices=acted_indices,
            )
            result = {"ok": ok, "assertions": details}
        else:
            result = {"error": f"unknown action: {act}"}

        accepted = True if act == "expect" else _accepted(result)
        acted = row.get("element")
        if accepted and acted is not None and act not in ("wait", "expect", "state", "snapshot"):
            acted_indices.append(int(acted))
        if (
            accepted
            and settle
            and step.get("expect") is None
            and act not in ("wait", "expect", "state", "snapshot")
        ):
            time.sleep(settle)
        verification = None
        if act == "expect":
            verification = {
                "ok": bool(result.get("ok")),
                "assertions": result.get("assertions", []),
            }
        elif step.get("expect") is not None:
            if accepted:
                verified, details, current = wait_for_expectations(
                    pid,
                    window_id,
                    step["expect"],
                    timeout=float(step.get("timeout", 5)),
                    max_elements=int(step.get("max_elements", max_elements)),
                    ignore_element_indices=acted_indices,
                )
                verification = {"ok": verified, "assertions": details}
            else:
                verification = {
                    "ok": False,
                    "assertions": [],
                    "code": "dispatch_not_accepted",
                }
        if verification is not None:
            assertions_passed = assertions_passed and verification["ok"]
        if verification is not None:
            latest_snapshot = current
            latest_snapshot_fresh = True
        elif act in ("expect", "state", "snapshot"):
            latest_snapshot = current
            latest_snapshot_fresh = True
        else:
            latest_snapshot = current if accepted else latest_snapshot
            latest_snapshot_fresh = bool(accepted and current is not None)
        row.update(
            accepted=accepted,
            result=result,
            verification=verification,
            duration_ms=round((time.monotonic() - step_started) * 1000),
        )
        results.append(row)
        dispatches_accepted = dispatches_accepted and accepted
        step_ok = accepted and (
            verification is None or verification.get("ok") is True
        )
        if not step_ok:
            if not plan.get("continue_on_error", False):
                break

    skipped_steps = plan.get("actions", [])[len(results) :]
    if any(_has_verifiable_expectation(step.get("expect")) for step in skipped_steps):
        assertions_passed = False
    plan_completed = len(results) == len(plan.get("actions", []))
    accepted_plan = dispatches_accepted and plan_completed

    plan_expect = plan.get("expect")
    final_snapshot = latest_snapshot if latest_snapshot_fresh else None
    if plan_expect is not None and final_snapshot is not None:
        final_ok, final_assertions = evaluate_expectations(
            final_snapshot, plan_expect, ignore_element_indices=acted_indices
        )
    elif plan_expect is None:
        final_ok, final_assertions = True, []
    else:
        final_ok, final_assertions = False, []
    if plan_expect is not None and not final_ok:
        final_ok, final_assertions, final_snapshot = wait_for_expectations(
            pid,
            window_id,
            plan_expect,
            timeout=float(plan.get("timeout", 5)),
            max_elements=max_elements,
            ignore_element_indices=acted_indices,
        )
    if final_snapshot is None:
        final_snapshot = _plan_snapshot(pid, window_id, max_elements=max_elements)
    if plan_expect is not None:
        assertions_passed = assertions_passed and final_ok
    verified_plan = not unasserted and assertions_passed
    proof_ok = accepted_plan and verified_plan
    capture = plan.get("capture", "failures")
    screenshot = None
    capture_error = None
    capture_geometry = None
    capture_recovery = None
    capture_attempted = capture == "always" or (
        capture == "failures" and not proof_ok
    )
    if capture_attempted:
        final_state = app_state(
            app_name,
            pid,
            window_id,
            max_elements=max_elements,
            include_screenshot=True,
            foreground_prepared=foreground_prepared,
        )
        screenshot = final_state.get("screenshot")
        capture_geometry = final_state.get("capture_geometry")
        capture_recovery = final_state.get("capture_recovery")
        if screenshot is None or not final_state.get("ok"):
            capture_error = final_state.get("error") or "required screenshot was not captured"
    final_elements = _state_elements(final_snapshot.get("elements", []))
    result_ok = proof_ok and capture_error is None
    full_output = output_mode == "full"
    visible = final_elements
    if not full_output and acted_indices:
        ignored = set(acted_indices)
        visible = [item for item in final_elements if item.get("element_index") not in ignored]
    final_text = _state_text(app_name, visible)
    if not full_output:
        final_text = _plan_contract().compact_state_text(final_text, plan_expect)
    final = {
        "text": final_text,
        "element_count": len(final_elements),
        "hidden_element_count": max(
            0, len(final_snapshot.get("elements", [])) - len(final_elements)
        ),
        "screenshot": screenshot,
        "capture_error": capture_error,
    }
    if capture_attempted:
        final["capture_geometry"] = capture_geometry
        final["capture_recovery"] = capture_recovery
    if full_output:
        final["elements"] = final_elements
    result = {
        "ok": result_ok,
        "accepted": accepted_plan,
        "verified": verified_plan,
        "app": app_name,
        "pid": pid,
        "window_id": window_id,
        "steps": results if full_output else [_plan_contract().compact_step(row) for row in results],
        "assertions": final_assertions,
        "final": final,
        "metrics": {
            "state_reuses": state_reuses,
            "cursor_cleanup_ended": len((cursor_cleanup or {}).get("ended", [])),
        },
        "duration_ms": round((time.monotonic() - started) * 1000),
    }
    if accepted_plan and not verified_plan:
        result["code"] = "unverified_run"
    operator_update(
        app_name,
        pid,
        window_id,
        status="complete" if result_ok else "error",
        active=True,
        screenshot_path=(screenshot or {}).get("raw_path")
        or (screenshot or {}).get("path")
        if screenshot
        else None,
        message=(
            "Assertions passed"
            if result_ok
            else "Required capture failed"
            if capture_error
            else "Actions dispatched without complete outcome proof"
            if accepted_plan and not verified_plan
            else "One or more actions were not accepted"
            if not accepted_plan
            else "One or more assertions failed"
        ),
    )
    return result
