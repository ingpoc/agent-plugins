#!/usr/bin/env python3
"""Deterministic linters/graders for the macos-cua MCP fast path.

App-agnostic. Mined from Cursor Computer Use sessions: repeated observe,
unpaired start_session, one-act batches, stale PID after quit/relaunch,
desktop-click false-greens, README refresh from a failed 1-repeat, dropped
driver sessions after native AX, container-row AXPress, stale-label
reobserve. Source JSONL is immutable; this module grades normalized
session-parse JSON and production source.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


SKILL = Path(__file__).resolve().parents[1]
WARM_README_REPEAT = 5
FORBIDDEN_DISPATCH_SUBSTRINGS = (
    "operator-proof-screen-coordinate",
    "global_input",
)
FORBIDDEN_CLICK_POINT_MARKERS = (
    "click_at_desktop",
    '"scope": "desktop"',
    "'scope': 'desktop'",
)
AX_PRESS_FAIL_MARKERS = ("-25204", "-25206")
CONTAINER_PRESS_ROLES = frozenset(
    {"AXRow", "AXCell", "AXOutline", "AXList", "AXGroup"}
)


def dispatch_path(result: Any) -> str:
    parts: list[str] = []
    current = result
    for _ in range(4):
        if not isinstance(current, dict):
            break
        for key in ("path", "method", "route"):
            value = current.get(key)
            if value:
                parts.append(str(value))
        current = current.get("result")
    return " ".join(parts).lower()


def forbidden_dispatch(result: Any) -> bool:
    path = dispatch_path(result)
    return any(marker in path for marker in FORBIDDEN_DISPATCH_SUBSTRINGS)


def driver_session_ended(result: Any) -> bool:
    """True when cua-driver refused because its named session is gone."""
    if not isinstance(result, dict):
        return False
    refusal = result.get("refusal") if isinstance(result.get("refusal"), dict) else {}
    code = str(result.get("code") or refusal.get("code") or "")
    message = str(
        result.get("message")
        or refusal.get("message")
        or result.get("error")
        or ""
    ).lower()
    return code == "session_ended" or "this session has ended" in message


def _nested_result(result: Any) -> dict[str, Any]:
    nested = result.get("result") if isinstance(result, dict) else None
    return nested if isinstance(nested, dict) else (
        result if isinstance(result, dict) else {}
    )


def grade_click_result(
    result: Any, *, text_target: bool = False, role: str | None = None
) -> list[dict[str, Any]]:
    """Dispatch ok is never outcome. Text points need AX selection readback."""
    errors: list[dict[str, Any]] = []
    if not isinstance(result, dict):
        return [{"code": "click_result_not_object"}]
    if forbidden_dispatch(result):
        errors.append(
            {
                "code": "forbidden_click_path",
                "path": dispatch_path(result),
            }
        )
    if result.get("effect") == "unverifiable" and not result.get("verified"):
        errors.append({"code": "dispatch_is_not_outcome"})
    if driver_session_ended(result) and not result.get("session_recovered"):
        errors.append({"code": "driver_session_dropped"})
    nested = _nested_result(result)
    err = str(nested.get("error") or result.get("error") or "")
    hid_ok = bool(result.get("ok")) and "ax-frame-hid" in dispatch_path(result)
    if (
        role in CONTAINER_PRESS_ROLES
        and any(marker in err for marker in AX_PRESS_FAIL_MARKERS)
        and not hid_ok
    ):
        if not nested.get("pressed_descendant") and not result.get(
            "pressed_descendant"
        ):
            errors.append(
                {
                    "code": "container_press_without_descendant",
                    "role": role,
                }
            )
        else:
            errors.append(
                {
                    "code": "container_press_without_ax_frame_hid",
                    "role": role,
                }
            )
    if text_target:
        path = dispatch_path(result)
        verified = bool(result.get("verified")) or (
            isinstance(result.get("result"), dict)
            and result["result"].get("verified") is True
        )
        if "native-ax-text" not in path and "native_ax_range_for_position" not in path:
            errors.append({"code": "text_click_missing_ax_range"})
        elif not verified:
            errors.append({"code": "text_click_unverified_selection"})
    point = nested.get("point") if isinstance(nested.get("point"), dict) else result.get("point")
    method = str(result.get("method") or nested.get("method") or "")
    if method == "cgevent-click":
        if not isinstance(point, dict) or point.get("x") is None or point.get("y") is None:
            errors.append({"code": "nonfinite_click_point"})
    return errors


def can_publish_readme(payload: Any) -> list[dict[str, Any]]:
    """README scores may only come from a passing warm 5-repeat --rate run."""
    if not isinstance(payload, dict):
        return [{"code": "readme_payload_not_object"}]
    errors: list[dict[str, Any]] = []
    repeat = payload.get("repeat")
    try:
        repeat_n = int(repeat)
    except (TypeError, ValueError):
        repeat_n = 0
    if repeat_n < WARM_README_REPEAT:
        errors.append(
            {
                "code": "readme_repeat_below_warm",
                "repeat": repeat,
                "required": WARM_README_REPEAT,
            }
        )
    if payload.get("ok") is not True:
        errors.append({"code": "readme_suite_not_ok"})
    if not payload.get("ratings"):
        errors.append({"code": "readme_ratings_missing"})
    return errors


def grade_tool_trace(calls: Any) -> list[dict[str, Any]]:
    """Fail the inefficient MCP hop sequence. App-agnostic. Independent of metrics."""
    if not isinstance(calls, list):
        return [{"code": "trace_not_list"}]
    errors: list[dict[str, Any]] = []
    prev: dict[str, Any] | None = None
    last_verified_app = ""
    last_act_app = ""
    singles_same_app = 0
    singles_app = ""

    def name_of(item: dict[str, Any]) -> str:
        return str(item.get("name") or item.get("tool") or "").strip()

    def app_of(item: dict[str, Any]) -> str:
        args = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
        return str(args.get("app") or item.get("app") or "").strip()

    def verified_of(item: dict[str, Any]) -> bool | None:
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        if "verified" in result:
            return bool(result["verified"])
        if "verified" in item:
            return bool(item["verified"])
        return None

    def action_count(item: dict[str, Any]) -> int:
        args = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
        plan = args.get("plan") if isinstance(args.get("plan"), dict) else {}
        actions = plan.get("actions")
        if isinstance(actions, list) and actions:
            return len(actions)
        if args.get("label") or args.get("element") is not None:
            return 1
        return 0

    for item in calls:
        if not isinstance(item, dict):
            continue
        name = name_of(item)
        app = app_of(item)
        if (
            name == "state"
            and last_act_app
            and app
            and app != last_act_app
        ):
            errors.append({"code": "cross_app_observe_between_acts", "app": app})
        if (
            name == "act"
            and prev is not None
            and name_of(prev) == "state"
            and app
            and app == app_of(prev)
        ):
            errors.append({"code": "pre_act_state_before_act", "app": app})
        if name == "act":
            verified = verified_of(item)
            last_verified_app = app if verified is True else ""
            last_act_app = app
            count = action_count(item)
            if app and count <= 1:
                singles_same_app = singles_same_app + 1 if app == singles_app else 1
                singles_app = app
            else:
                singles_same_app = 0
                singles_app = ""
            if singles_same_app >= 3:
                errors.append(
                    {
                        "code": "granular_unbatched_acts",
                        "app": app,
                        "consecutive_single_acts": singles_same_app,
                    }
                )
        if (
            name == "verify"
            and last_verified_app
            and (not app or app == last_verified_app)
        ):
            errors.append(
                {
                    "code": "redundant_verify_after_verified_act",
                    "app": app or last_verified_app,
                }
            )
        # Detect cursor-glide failure that blocked an action despite AX availability.
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        err = str(result.get("error") or "")
        if (
            name == "act"
            and not result.get("ok")
            and ("cursor" in err.lower() or "operator" in err.lower() or "glide" in err.lower())
            and result.get("label")
        ):
            errors.append(
                {
                    "code": "cursor_glide_blocked_ax_action",
                    "app": app,
                    "label": result.get("label"),
                    "error": err[:200],
                }
            )
        prev = item
    return errors


def grade_acu_session(parsed: Any) -> list[dict[str, Any]]:
    """Grade session-parsing compact JSON. Heuristics stay in inferences."""
    if not isinstance(parsed, dict):
        return [{"code": "session_not_object"}]
    metrics = parsed.get("metrics") if isinstance(parsed.get("metrics"), dict) else {}
    tools = metrics.get("tools") if isinstance(metrics.get("tools"), dict) else {}
    errors: list[dict[str, Any]] = []
    starts = int(tools.get("start_session") or 0)
    ends = int(tools.get("end_session") or 0)
    if starts and ends < starts:
        errors.append(
            {
                "code": "unpaired_start_session",
                "start_session": starts,
                "end_session": ends,
            }
        )
    repeated = int(metrics.get("repeated_observations") or 0)
    if repeated:
        errors.append({"code": "repeated_observe", "count": repeated})
    actions = int(metrics.get("actions") or 0)
    max_batch = int(metrics.get("max_action_batch") or 0)
    batches = int(metrics.get("action_batches") or 0)
    if actions >= 6 and max_batch <= 1 and batches == 0:
        errors.append(
            {
                "code": "granular_unbatched_acts",
                "actions": actions,
                "max_action_batch": max_batch,
            }
        )
    verified_acts = int(metrics.get("verified_acts") or 0)
    verify_calls = int(tools.get("verify") or 0)
    if verified_acts and verify_calls >= verified_acts:
        errors.append(
            {
                "code": "redundant_verify_after_verified_act",
                "verified_acts": verified_acts,
                "verify": verify_calls,
            }
        )
    pre_act_states = int(metrics.get("pre_act_observe_same_app") or 0)
    if pre_act_states:
        errors.append(
            {"code": "pre_act_state_before_act", "count": pre_act_states}
        )
    cross_observe = int(metrics.get("cross_app_observe_between_acts") or 0)
    if cross_observe:
        errors.append(
            {"code": "cross_app_observe_between_acts", "count": cross_observe}
        )
    blob = json.dumps(parsed).lower()
    if "session_ended" in blob or "this session has ended" in blob:
        errors.append({"code": "driver_session_dropped"})
    trace = parsed.get("calls") or parsed.get("trace")
    if isinstance(trace, list) and trace:
        for item in grade_tool_trace(trace):
            if item not in errors and not any(
                existing.get("code") == item.get("code") for existing in errors
            ):
                errors.append(item)
    return errors


def _function_source(text: str, name: str) -> str:
    marker = f"def {name}"
    start = text.find(marker)
    if start < 0:
        return ""
    rest = text[start + len(marker) :]
    next_def = rest.find("\ndef ")
    return text[start : start + len(marker) + (len(rest) if next_def < 0 else next_def)]


def post_hid_helper_body(actions_src: str) -> str:
    """Body of `private func postHid` only — excludes `postHidGlobal`."""
    marker = "private func postHid"
    start = actions_src.find(marker)
    if start < 0:
        return ""
    rest = actions_src[start + len(marker) :]
    # Stop before sibling helpers / MARK so cghidEventTap in postHidGlobal is ignored.
    for sep in ("\n    private func ", "\n    // MARK:"):
        idx = rest.find(sep)
        if idx >= 0:
            rest = rest[:idx]
            break
    return rest


def hid_dual_posts_same_helper(actions_src: str) -> bool:
    """True when one helper both postToPid and cghidEventTap (doubled glyphs)."""
    body = post_hid_helper_body(actions_src)
    return "postToPid" in body and "cghidEventTap" in body


def lint_source(skill: Path | None = None) -> list[dict[str, Any]]:
    root = Path(skill or SKILL)
    scripts = root / "scripts"
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    fixture = (scripts / "bench_mcp_runtime.py").read_text()
    reset = _function_source(fixture, "reset_app_fixture")
    add(
        "app fixture reset is name-parameterized and clears cache",
        "def reset_app_fixture" in fixture
        and "app_name" in reset
        and "clear_resolution_cache" in reset
        and "json.dumps(app)" in reset
        and "resolve_app(app)" in reset,
        "reset_app_fixture(app_name) must quit then reuse Computer Use resolve",
    )
    launch = _function_source(
        (scripts / "runtime_apps.py").read_text(), "launch_or_activate"
    )
    add(
        "cold launch forces a new instance",
        '"-n"' in launch and '"-b"' in launch and '"-a"' in launch,
        "open -a after quit is a LaunchServices no-op; cold launch must use -n",
    )
    resolve = _function_source(
        (scripts / "runtime_apps.py").read_text(), "resolve_app"
    )
    add(
        "resolve polls until a window exists after launch",
        "range(20)" in resolve and "launch_if_missing=False" in resolve,
        "PID-live with no window is not an outcome; poll, do not fail closed after one sleep",
    )
    runtime = (scripts / "mcp_runtime.py").read_text()
    add(
        "Computer Use resolve is one owner",
        "resolve_app" in _function_source(runtime, "_resolve")
        and "launch_or_activate" not in _function_source(runtime, "_resolve"),
        "state/act must not launch then hope; resolve_app owns window readback",
    )
    click_point = _function_source(
        (scripts / "runtime_pointer_actions.py").read_text(), "click_point"
    )
    banned = [marker for marker in FORBIDDEN_CLICK_POINT_MARKERS if marker in click_point]
    add(
        "click_point is not a desktop-global click",
        bool(click_point) and not banned,
        ",".join(banned) or "click_point uses PID-scoped AX/mouse paths",
    )
    add(
        "MCP state/act stay in one process",
        "class SessionRuntime" in runtime
        and "macos-cua.py" not in _function_source(runtime, "_with_target"),
        "mcp_runtime must not spawn macos-cua.py per state/act",
    )
    add(
        "start_session clears resolution cache",
        "clear_resolution_cache" in _function_source(runtime, "start_driver"),
        "start_driver must drop stale app PIDs before resolve",
    )
    walk = (scripts / "runtime_accessibility.py").read_text()
    add(
        "AXRow/AXCell children are BFS-priority",
        'role in {"AXRow", "AXCell"}' in walk and "extendleft" in walk,
        "table/outline rows must resolve in-budget for any app",
    )
    driver = (scripts / "runtime_driver.py").read_text()
    call = _function_source(driver, "call_driver")
    add(
        "driver calls revive a dropped session once",
        "driver_session_ended" in driver
        and "start_session" in call
        and "end_session" in call
        and "retry_params" in call
        and 'retry_params["session"]' in call,
        "call_driver must start_session once on session_ended, not fail the key",
    )
    press = _function_source(walk, "press_key")
    add(
        "keys survive a dropped driver session via native PID delivery",
        "driver_session_ended" in press
        and "press_key_after_dropped_session" in press
        and "global_input" in press,
        "Escape/hotkey must not die when cua-driver session_ended after AX",
    )
    add(
        "page scroll survives global_input after a dropped session",
        "global_input" in _function_source(walk, "scroll")
        and "page_key_scroll" in _function_source(walk, "scroll"),
        "scroll must not accept unverifiable global_input as the outcome",
    )
    labels = (scripts / "runtime_labels.py").read_text()
    add(
        "container AXPress uses a pressable descendant",
        "pressable_descendant_index" in labels and "pressed_descendant" in labels,
        "row/cell/group press must not stop at NotificationUnsupported",
    )
    retry = _function_source(labels, "_native_ax_press_label_with_retry")
    add(
        "stale AX identity rematch does not require a second observe",
        "_ax_press_unsupported" in retry and "find_clickable_index" in retry,
        "retitled controls rematch on the current tree via aliases/frame",
    )
    click_label = _function_source(labels, "click_label_pointer")
    add(
        "unsupported container press after glide uses AX-frame HID",
        "ax-frame-hid" in click_label and "delivery_mode=\"foreground\"" in click_label,
        "row/cell AXPress miss after glide must click the AX frame, not desktop-global search",
    )
    add(
        "cursor glide failure does not block native AX press fallback",
        "native_fallback or idx is not None" in click_label
        and click_label.index("native_fallback or idx") < click_label.index("native_fallback:"),
        "glide is UX; when the AX element exists, action dispatch must proceed via AX press",
    )
    pointer = (scripts / "runtime_pointer.py").read_text()
    preflight_fn = _function_source(pointer, "pointer_preflight")
    add(
        "pointer_preflight soft-fails on cursor glide timeout",
        preflight_fn.count("return None") >= 2
        and "return glide" not in preflight_fn,
        "cursor glide failure in preflight must return None (skip), not propagate error",
    )
    glide_fn = _function_source(pointer, "glide_operator_to_element")
    add(
        "legacy operator glide stays non-blocking",
        "_wait_for_operator_cursor" not in glide_fn
        and "time.sleep" not in glide_fn,
        "operator twin is not the CUAService tip; do not resurrect wait-ack there",
    )
    settle = (root / "service" / "Sources" / "CUAService" / "MethodRouter.swift").read_text()
    overlay = (root / "service" / "Sources" / "CUAService" / "CursorOverlay.swift").read_text()
    add(
        "CUAService click waits for overlay tip before press",
        "tip.wait" in settle
        and "Task.sleep" in settle
        and "Tip lands first" in settle
        and "return (quartz, animDuration)" in overlay
        and "wait: TimeInterval)" in overlay,
        "fire-and-forget glide left the badge mid-air while AX already pressed",
    )
    vision = (scripts / "runtime_vision.py").read_text()
    add(
        "current-tree label aliases are app-agnostic",
        "LABEL_ALIASES" in vision
        and "calculator" not in vision.lower()
        and "whatsapp" not in vision.lower(),
        "in-place retitled controls resolve without a named-app helper",
    )
    skill_md = (root / "SKILL.md").read_text()
    mcp = "\n".join(
        (scripts / name).read_text()
        for name in ("compact_mcp.py", "compact_backend.py")
    )
    method_router = (
        root / "service" / "Sources" / "CUAService" / "MethodRouter.swift"
    ).read_text()
    cua_client = (root / "service" / "cua_client.py").read_text()
    installer = (root / "service" / "install_service.py").read_text()
    workflow = (scripts / "workflow.py").read_text()
    add(
        "cold app launch waits for its first window",
        "def _get_app_state" in mcp
        and '"No window for app:"' in mcp
        and "range(3)" in mcp,
        "a launched app may not expose a window on the first AX query",
    )
    add(
        "service signing selects the certificate by Team ID",
        "resolve_codesign_identity" in installer
        and "OU=" in installer
        and "Apple Development: Team" not in installer
        and '"--sign", "-"' not in installer,
        "invented certificate labels and silent ad-hoc fallback invalidate stable TCC trust",
    )
    add(
        "skill and MCP require app-agnostic friction graders",
        "app-agnostic" in skill_md.lower()
        and "fast_path" in skill_md
        and "linter/grader" in skill_md
        and "Two wall clocks" in skill_md
        and "Encode friction" in skill_md
        and "fails the old trace" in skill_md
        and "Act-first" in skill_md
        and "app-agnostic fast_path grader" in mcp
        and "Two wall clocks" in mcp
        and "Act-first" in mcp
        and "fails the old trace" in mcp
        and "Do not verify when act.verified" in mcp
        and "CUAService" in mcp
        and '"name": "start_session"' not in mcp
        and '"name": "verify"' not in mcp
        and "one compact `state`" not in skill_md
        and "start_session → `state` / `act` / `verify`" not in skill_md
        and "Input delivery (any app)" in skill_md,
        "every friction must be encoded for any Mac app, not left in chat",
    )
    add(
        "skill preserves MCP and native-engine ownership",
        "MCP follows stable `2026-07-28`" in skill_md
        and "Keep the model surface at `state` + `act`" in skill_md
        and "one native `execute_plan` RPC" in skill_md
        and "only completion gate" in skill_md
        and "`NSWorkspace` plus `FileManager` validation" in skill_md
        and "`compact_mcp.py` owns the canonical MCP input/output schema" in skill_md,
        "protocol guidance must not invent tools or describe an unshipped native plan RPC as live",
    )
    add(
        "exact paths and same-app steps use one native plan",
        'case "execute_plan"' in method_router
        and 'case "open_item"' in method_router
        and "NSWorkspace.shared.open" in method_router
        and "FileManager.default.fileExists" in method_router
        and "requestRunning" in method_router
        and "client.execute_plan(app, native_steps)" in mcp
        and '"method": "open_item"' in mcp
        and '"method": "scroll"' in mcp
        and '"method": "drag"' in mcp
        and '"method": "select_text"' in mcp
        and '"execute_plan", {"app": app, "steps": steps}, retry=False' in cua_client,
        "known filesystem targets must not become Finder searches or replayable multi-RPC action chains",
    )
    add(
        "session-shape benchmark encodes two wall clocks",
        (scripts / "bench_session_shape.py").is_file()
        and "within_app_act_first" in (scripts / "bench_session_shape.py").read_text()
        and "cross_app_act_only" in (scripts / "bench_session_shape.py").read_text(),
        "act-first vs probe-state must stay measurable app-agnostically",
    )
    add(
        "tool-trace grader fails probe-state and redundant verify",
        "def grade_tool_trace" in (scripts / "fast_path.py").read_text()
        and "pre_act_state_before_act" in (scripts / "fast_path.py").read_text()
        and "cross_app_observe_between_acts" in (scripts / "fast_path.py").read_text()
        and "redundant_verify_after_verified_act" in (scripts / "fast_path.py").read_text(),
        "session hop waste must fail without named-app helpers",
    )
    add(
        "MCP catalog is state and act only",
        '"name": "state"' in mcp
        and '"name": "act"' in mcp
        and '"name": "start_session"' not in mcp
        and "from cua_client import CUAClient" in mcp,
        "cua-driver lifecycle tools are not the agent path",
    )
    add(
        "workflow has no named-app cursor-demo",
        "cursor-demo" not in workflow and "cmd_cursor_demo" not in workflow,
        "Calculator demo is out of scope; smoke stays generic",
    )
    overlay = (root / "service" / "Sources" / "CUAService" / "CursorOverlay.swift").read_text()
    add(
        "service cursor is window-local on the target CGWindow",
        "canJoinAllSpaces" in overlay
        and ".stationary" not in overlay
        and "orderFrontRegardless" in overlay
        and "order(.above, relativeTo:" in overlay
        and "quartzBounds(of:" in overlay
        and "axBounds" in overlay
        and "preferredWindowBounds" in overlay
        and "clamp(" in overlay,
        "overlay tip stays inside the AX window of the app being driven, not a Stage Manager proxy",
    )
    operator_ui = (scripts / "operator_ui.py").read_text()
    add(
        "operator overlay is not started when CUAService exists",
        "CUA_SERVICE_APP" in operator_ui
        and "skipped" in operator_ui
        and "CUAService overlay" in operator_ui,
        "two labeled pointers (Cursor + Agent) means the old operator twin is still running",
    )
    add(
        "expect matches text values not button titles",
        "def expect_verified" in mcp
        and "AXStaticText" in mcp
        and "AXTextArea" in mcp
        and "AXTextField" in mcp
        and "AXCell" in mcp
        and 'AXCell "' in mcp
        and "expect in text" not in mcp,
        "keypad titles must not false-green expect (e.g. 0 matching button 0)",
    )
    add(
        "mutating compact acts fail closed until the settled state verifies",
        "def _has_ui_effect" in mcp
        and "verification_required: mutating act needs expect" in mcp
        and '"dispatched": dispatched' in mcp
        and '"completion": "verified" if verified else "unverified"' in mcp,
        "dispatch acceptance must not become a Samantha completion claim",
    )
    activity = (root / "service" / "Sources" / "CUAService" / "SamanthaActivityLog.swift").read_text()
    add(
        "Samantha activity JSONL uses cross-process atomic append",
        "O_APPEND" in activity and "Darwin.write" in activity,
        "Swift seek-to-end raced Python append and produced malformed JSONL",
    )
    client = (root / "service" / "cua_client.py").read_text()
    add(
        "timed-out mutations are bounded and never replayed",
        "sock.settimeout(15.0)" in client
        and "retry=False" in client
        and "if not retry:" in mcp,
        "45s client retry plus whole-batch retry caused 120s hangs and duplicate input",
    )
    add(
        "batched act captures landing shots before and after without a screenshot tool",
        "screenshot_before" in mcp
        and "screenshot_after" in mcp
        and '"name": "screenshot"' not in mcp
        and "Stage Manager thumb" in mcp,
        "pixels are a correction check inside act, not a third catalog tool",
    )
    settle = (root / "service" / "Sources" / "CUAService" / "MethodRouter.swift").read_text()
    add(
        "get_app_state retries window capture once",
        "120_000_000" in settle
        and "screenshotPath == nil" in settle,
        "first resolve after raise can miss CG image; start shot must not be empty",
    )
    add(
        "click settle is bounded so batched act stays under the RPC timeout",
        "timeout: 0.6" in settle and "minQuiet: 0.08" in settle,
        "Codex waits on next state, not a 5s AX quiescence per click",
    )
    add(
        "successful AX press skips the 0.6s settle wait",
        '"reason": "ax-action"' in settle
        and "cgevent-click" in settle,
        "AX already applied; quiescence wait was the click floor",
    )
    actions_src = (root / "service" / "Sources" / "CUAService" / "InputActions.swift").read_text()
    add(
        "type_text inserts via focused AX value before HID",
        "kAXSelectedTextAttribute" in actions_src
        and "func axInsertText" in actions_src
        and "func hidTypeUnicode" in actions_src
        and "func axInsertLanded" in actions_src
        and "NSAttributedString" in actions_src
        and "liveTextAreaTargets" in actions_src
        and "focusedIsArea" in actions_src,
        "AX SelectedText success with unreadable value was treated as landing; search-role focus stole keys from the text area",
    )
    resolver = (root / "service" / "Sources" / "CUAService" / "AppResolver.swift").read_text()
    add(
        "AX messaging timeout is set; raise sleep only for Stage Manager stubs",
        "AXUIElementSetMessagingTimeout" in resolver
        and "if tiny" in resolver
        and "Thread.sleep(forTimeInterval: 0.08)" in resolver,
        "every resolve slept 80ms when Cursor was front and the target was already full-size",
    )
    shot = (root / "service" / "Sources" / "CUAService" / "ScreenshotCapture.swift").read_text()
    add(
        "window capture is window-backed and SCK-bounded",
        "sckDeadlineNs" in shot
        and "photographs wallpaper" in shot
        and "SCScreenshotManager" in shot,
        "AX-rect CG of a Stage Manager stub photographed wallpaper; SCK-first hung act",
    )
    add(
        "SCK uses pointPixelScale, ignoreShadows, and short-TTL shareable cache",
        "pointPixelScale" in shot
        and "ignoreShadowsSingleWindow" in shot
        and "shareableTTL" in shot
        and "loadShareableContent" in shot,
        "every SCK shot re-enumerated SCShareableContent and used NSScreen max scale",
    )
    add(
        "window PNG is reused under 200ms unless input invalidates it",
        "func invalidate" in shot
        and "cachedAt < 0.2" in shot
        and "capture_cached" in settle
        and "screenshotCapture.invalidate" in settle,
        "act before/after and repeated get_app_state recaptured the same window; capture p95 517ms",
    )
    resolver = (root / "service" / "Sources" / "CUAService" / "AppResolver.swift").read_text()
    add(
        "main window pick includes off-screen layer-0, then raise",
        "[.optionAll, .excludeDesktopElements]" in resolver
        and (
            "activateIgnoringOtherApps" in resolver
            or ("yieldActivation" in resolver and "activate(from:" in resolver)
        )
        and "kAXRaiseAction" in resolver
        and "0.08" in resolver
        and "quartzDisplays" in resolver
        and "focusedWindowID" in resolver,
        "on-screen-only selected the Stage Manager thumb; start/end shots were wallpaper",
    )
    add(
        "raise uses cooperative activation on macOS 14+",
        "yieldActivation" in resolver
        and "activate(from:" in resolver
        and "activateForInput" in resolver,
        "activateIgnoringOtherApps no-ops on 14+; raise never made the target front",
    )
    add(
        "main window id is cached for a full-size window",
        "windowCache" in resolver and "now - cache.at < 2.0" in resolver,
        "every resolve scanned CGWindowList for all windows; resolve p95 ~120ms",
    )
    settle = (root / "service" / "Sources" / "CUAService" / "MethodRouter.swift").read_text()
    add(
        "after New, type walks the focused window not the cached largest",
        "func invalidateWindowCache" in resolver
        and "preferFocusedWindow" in resolver
        and "preferFocusedWindow: afterNew" in settle
        and '["cmd+n", "command+n", "cmd+t", "command+t"]' in settle,
        "2s window-id cache kept cmd+n type on the previous document; stale-document refuse was correct but the new untitled was never walked",
    )
    walker = (root / "service" / "Sources" / "CUAService" / "AXTreeWalker.swift").read_text()
    add(
        "AX labels strip bidi marks and fold unicode dashes",
        "0x200B" in walker and "func axNorm" in walker and "0x2010" in walker,
        "U+200E prefixed chrome labels; Wi-Fi missed Wi‑Fi",
    )
    add(
        "AX walk root is a window, never the application menu bar",
        "return [axApp]" not in walker
        and "_CGSGetWindowID(win" in walker
        and "return []" in walker,
        "TextEdit with no focused AX window walked Apple menu; 80 nodes, null screenshot, HID type",
    )
    add(
        "empty outline rows do not consume the AX node budget",
        "func axEmitNode" in walker and "visited < 400" in walker
        and "skipChrome.contains" in walker,
        "System Settings 80 unlabeled AXRows; Wi-Fi/Bluetooth clicks all missed",
    )
    add(
        "AX window frame wins over a tiny CGWindowList proxy",
        "cgArea >= axArea * 0.5" in walker,
        "Stage Manager 30×79 thumbs must not own screenshot or overlay bounds",
    )
    add(
        "markdown keeps full text values for expect",
        "prefix(57)" not in walker and 'value=\\"\\(value)\\"' in walker,
        "60-char ellipsis made long TextEdit expect false-red",
    )
    add(
        "AX walk packs attributes in one IPC",
        "AXUIElementCopyMultipleAttributeValues" in walker
        and "func axPacked" in walker
        and "func cachedSnapshot" in walker
        and "func axKeepNode" in walker
        and "liveElements" in walker
        and "func liveTextElements" in walker
        and "func liveTextAreaTargets" in walker
        and "areas + others" in walker,
        "per-attribute CopyAttributeValue and a second BFS resolve dominated get_app_state/click",
    )
    add(
        "type after New refuses a still-full text field",
        "type_refused_stale_document" in actions_src
        and "afterNewDocument" in actions_src
        and "func longestTextValue" in walker,
        "cmd+n HID hit Cursor then type_text prepended into the open Notes document",
    )
    add(
        "type_text does not HID when no text field is in the walk",
        "type_no_text_target" in actions_src and "func isTextRole" in actions_src,
        "TextEdit empty walk still posted unicode into the front app (Cursor)",
    )
    add(
        "HID keys raise the target app so Cursor does not eat cmd+n",
        "raiseForInput: true" in settle
        and "raiseForInput" in resolver
        and "waitUntilFrontmost" in resolver
        and "key_target_not_front" in actions_src
        and "kAXFocusedApplicationAttribute" in resolver
        and "timeout: TimeInterval =" in resolver
        and "Date().addingTimeInterval(timeout)" in resolver
        and "if !tiny && runningApp.isActive { return }" not in resolver,
        "isActive skip left the host IDE key window; cmd+n HID created a tab there",
    )
    add(
        "batched act stops on first failed step",
        "if item.get(\"ok\") is not True:" in mcp
        and "break" in mcp
        and "after_new_document" in mcp
        and "cmd+n" in mcp,
        "label miss then Equals still mutated the display; cmd+n then type wrote the old note",
    )
    add(
        "act schema keeps coordinate and wait steps",
        '"x": {"type": "number"}' in mcp
        and '"y": {"type": "number"}' in mcp
        and '"wait": {"type": "number"' in mcp
        and "time.sleep" in mcp
        and "client.set_value" in mcp,
        "Cursor strips nested x/y when they are not in the schema; unlabeled fields cannot type",
    )
    add(
        "coordinate click fails closed on a nonfinite point",
        "nonfinite click point" in mcp
        and "def _normalize_step_result" in mcp
        and "nonfinite_click_point" in (scripts / "fast_path.py").read_text(),
        "NaN CGFloat serialized as null and still counted as a hit",
    )
    # Ban is dual-post (pid AND global for the same event → doubled glyphs), not
    # pid-only postToPid. postHid may postToPid; postHidGlobal may cghid separately.
    post_hid_body = post_hid_helper_body(actions_src)
    add(
        "AXPress only when the control advertises it; CG posts HID at the AX frame",
        'element.actions.contains("AXPress")' in actions_src
        and "func postHid" in actions_src
        and "cghidEventTap" in actions_src
        and "postToPid" in post_hid_body
        and not hid_dual_posts_same_helper(actions_src)
        and "doubled glyphs" in actions_src
        and "func paramDouble" in (root / "service" / "Sources" / "CUAService" / "JSONRPCCodec.swift").read_text()
        and "paramDouble(\"x\")" in (root / "service" / "Sources" / "CUAService" / "MethodRouter.swift").read_text()
        and "case let f as CGFloat" in (root / "service" / "Sources" / "CUAService" / "JSONRPCCodec.swift").read_text(),
        "unadvertised AXPress returned success as a no-op; integer x/y never clicked; PID+HID doubled glyphs",
    )
    add(
        "HID uses shared CGEventSource with suppression interval 0",
        "localEventsSuppressionInterval" in actions_src
        and "CGEventSource" in actions_src
        and "permitLocalMouseEvents" in actions_src
        and "hidSource" in actions_src,
        "default 0.25s local-events suppression stalled multi-step HID bursts",
    )
    add(
        "PostEvent TCC is preflighted fail-closed",
        "CGPreflightPostEventAccess" in actions_src
        and "ensurePostEventAccess" in actions_src,
        "Accessibility grant alone still silent-no-op PostEvent HID",
    )
    add(
        "expect must be new versus the before-tree",
        "def expect_is_new" in mcp
        and "expectation_is_new(expect, before_text, text, results)" in mcp
        and "already in the body" in skill_md,
        "needle already in a TextArea false-greened a later cell/table write",
    )
    add(
        "act retries a missing window screenshot once",
        "not before.get(\"screenshot\")" in mcp
        and "not after.get(\"screenshot\")" in mcp,
        "service restart left screenshot_before null; landing check needs pixels",
    )
    router = (root / "service" / "Sources" / "CUAService" / "MethodRouter.swift").read_text()
    delegate = (root / "service" / "Sources" / "CUAService" / "ServiceDelegate.swift").read_text()
    plist = (root / "service" / "Resources" / "Info.plist").read_text()
    package = (root / "service" / "Package.swift").read_text()
    entitlements = (root / "service" / "Resources" / "CUAService.entitlements").read_text()
    add(
        "service reports and prompts TCC instead of silent AXUnknown",
        "axTrusted" in router
        and "promptTCC" in delegate
        and "NSScreenCaptureUsageDescription" in plist,
        "ad-hoc re-sign drops Accessibility; AXUnknown must be diagnosable",
    )
    status = (root / "service" / "Sources" / "CUAService" / "StatusBarController.swift").read_text()
    voice = (root / "service" / "Sources" / "CUAService" / "VoiceSupervisor.swift").read_text()
    island = (root / "service" / "Sources" / "CUAService" / "IslandController.swift").read_text()
    panel_path = root / "service" / "Sources" / "CUAService" / "SamanthaMenuBarIslandPanel.swift"
    panel = panel_path.read_text() if panel_path.is_file() else ""
    anchor_path = root / "service" / "Sources" / "CUAService" / "MenuBarWindowAnchor.swift"
    anchor_src = anchor_path.read_text() if anchor_path.is_file() else ""
    settings_store = (root / "service" / "Sources" / "CUAService" / "VoiceSettingsStore.swift").read_text()
    add(
        "CUAService menu supervises voice without stopping socket",
        "Samantha" in status
        and "NSSwitch" in status
        and "openSettings" in status
        and "VoiceSettingsStore" in settings_store
        and "voice-cua.app" in voice
        and "voice.log" in voice
        and "Task { @MainActor [weak self]" in voice
        and "VoiceSupervisor" in delegate
        and "SamanthaMenuBarIslandPanel" in island
        and "MenuBarWindowAnchor" in panel
        and "Menubar" in anchor_src
        and "isFloatingPanel = false" in panel
        and "DynamicNotchKit" not in island,
        "Phase 4: Samantha toggle + Menubar-window-anchored island",
    )
    add(
        "voice supervisor recovers bounded unexpected exits",
        "scheduleRestart" in voice
        and "restart_suppressed" in voice
        and "current !== terminated" in voice
        and "stopping" in voice,
        "Realtime sessions expire and crashes must recover without reviving intentional stops",
    )
    add(
        "Samantha requests microphone permission before launching voice",
        "ensureMicrophoneAccess" in voice
        and "AVCaptureDevice.requestAccess" in voice
        and "NSMicrophoneUsageDescription" in plist
        and "com.apple.security.device.audio-input" in entitlements,
        "the CUAService owner must request TCC so ON means microphone-ready",
    )
    add(
        "voice bundles are signed inside-out with audio input entitlement",
        '"--deep"' not in installer
        and "CUAService.entitlements" in installer
        and "codesign(helper" in installer
        and 'frameworks.rglob("*")' in installer
        and "MACHO_MAGICS" in installer,
        "hardened runtime denies microphone without entitlement on each responsible executable",
    )
    add(
        "microphone alert opens the current macOS privacy extension",
        "com.apple.settings.PrivacySecurity.extension?Privacy_Microphone" in status,
        "the legacy preference-pane URL opened Screen & System Audio Recording on macOS 26",
    )
    add(
        "SwiftPM excludes only existing test fixtures",
        '"__pycache__"' not in package,
        "stale excludes make every build warn and hide useful diagnostics",
    )
    add(
        "single menubar owner for Voice CUA",
        "NSStatusBar.system.statusItem" in status
        and "Samantha" in status
        and "NSStatusItem" not in island
        and "NSStatusBar.system.statusItem" not in island,
        "Phase 4: only CUAService monitor icon in menu bar; IslandApp is notch-only",
    )
    add(
        "skill has no target-app recipe file",
        "likeminded.md" not in skill_md,
        "app recipes stay in the target repo",
    )
    harness = (scripts / "install_harness.py").read_text()
    plugin_root = root.parents[1]
    mcp_config = (plugin_root / "mcp.json").read_text()
    installer = (root / "service" / "install_service.py").read_text()
    voice_runtime = plugin_root / "runtime" / "voice-cua"
    voice_builder = (voice_runtime / "scripts" / "build_voice_helper.py").read_text()
    voice_bridge = (voice_runtime / "python" / "voice_cua" / "cua_bridge.py").read_text()
    add(
        "Samantha runtime is self-contained in the portable plugin",
        (voice_runtime / "python" / "voice_cua" / "voice_stack.py").is_file()
        and (voice_runtime / "config" / ".secret" / "openai-api.json").is_file()
        and 'VOICE_RUNTIME_ROOT = PLUGIN_ROOT / "runtime" / "voice-cua"' in installer
        and "VOICE_CUA_AGENT_ROOT" not in installer
        and "voice-cua-agent repo" not in installer
        and "devVoiceRoots" not in voice
        and "PYTHONPATH" not in voice
        and 'PLUGIN_ROOT / "skills" / "macos-cua"' in voice_builder
        and '"--scratch-path"' in installer
        and '"--workpath"' in voice_builder
        and '"--distpath"' in voice_builder
        and "_PACKAGED_PLUGIN_SCRIPTS" in voice_bridge
        and "remote-claude" not in voice_bridge,
        "a clean plugin install must not depend on a sibling checkout or machine path",
    )
    add(
        "installer cannot recreate unprefixed macos-cua aliases",
        "HARNESS_SKILL_DIRS" not in harness
        and "install_link(" not in harness
        and "CUA_DRIVER_" not in mcp_config
        and "--delete-excluded" in harness
        and '".build"' in harness,
        "install only the plugin and exclude generated build caches",
    )
    agents = (root.parents[1] / "AGENTS.md").read_text()
    add(
        "README refresh requires warm 5-repeat",
        "--repeat 5 --rate" in agents,
        str(root.parents[1] / "AGENTS.md"),
    )
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lint", action="store_true")
    parser.add_argument("--session-json", type=Path)
    parser.add_argument("--readme-json", type=Path)
    args = parser.parse_args(argv)
    payload: dict[str, Any] = {"ok": True}
    if args.lint or not (args.session_json or args.readme_json):
        checks = lint_source()
        payload["lint"] = checks
        payload["ok"] = payload["ok"] and all(item["ok"] for item in checks)
    if args.session_json:
        errors = grade_acu_session(json.loads(args.session_json.read_text()))
        payload["session"] = errors
        payload["ok"] = payload["ok"] and not errors
    if args.readme_json:
        errors = can_publish_readme(json.loads(args.readme_json.read_text()))
        payload["readme"] = errors
        payload["ok"] = payload["ok"] and not errors
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
