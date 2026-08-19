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
    vision = (scripts / "runtime_vision.py").read_text()
    add(
        "current-tree label aliases are app-agnostic",
        "LABEL_ALIASES" in vision
        and "calculator" not in vision.lower()
        and "whatsapp" not in vision.lower(),
        "in-place retitled controls resolve without a named-app helper",
    )
    skill_md = (root / "SKILL.md").read_text()
    mcp = (scripts / "compact_mcp.py").read_text()
    workflow = (scripts / "workflow.py").read_text()
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
        and "Preflight once at start" in mcp
        and "closeout once at end" in mcp
        and "one compact `state`" not in skill_md
        and "start_session → `state` / `act` / `verify`" not in skill_md,
        "every friction must be encoded for any Mac app, not left in chat",
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
    start_schema = mcp.split('"name": "start_session"', 1)[1].split('"name": "state"', 1)[0]
    add(
        "start_session does not own workflow preflight",
        '"preflight": {"type": "boolean"' not in start_schema
        and 'arguments.get("preflight")' not in mcp
        and "preflight:true" not in skill_md,
        "preflight is workflow.py once at start, not an MCP argument",
    )
    add(
        "workflow has no named-app cursor-demo",
        "cursor-demo" not in workflow and "cmd_cursor_demo" not in workflow,
        "Calculator demo is out of scope; smoke stays generic",
    )
    operator_swift = "\n".join(
        path.read_text() for path in sorted((root / "operator").glob("*.swift"))
    )
    add(
        "operator cursor stays above controlled window only",
        "cursorOverlayPanel.level = .popUpMenu" in operator_swift
        and "cursorOverlayPanel.level = .screenSaver" not in operator_swift
        and "order(.above, relativeTo:" in operator_swift
        and "isCursorPointVisible" in operator_swift
        and "macos-cua · \\(agent)" in operator_swift
        and "Using your computer" not in operator_swift,
        "desktop cursor badge shows harness, not Esc copy",
    )
    add(
        "skill has no target-app recipe file",
        "likeminded.md" not in skill_md,
        "app recipes stay in the target repo",
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
