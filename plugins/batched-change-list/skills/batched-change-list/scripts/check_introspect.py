#!/usr/bin/env python3
"""Check Introspect ordering and coverage of the round's performed steps."""
import argparse
import json
import re
import sys
from pathlib import Path
import workflow_definition as WF
import render

SECTION_ORDER = ("delete", "simplify", "optimize", "automate")
SECTION_NOUNS = {"delete": "deletion", "simplify": "simplification",
                 "optimize": "optimization", "automate": "automation"}


def performed_round_steps(tracking):
    if not isinstance(tracking, dict):
        raise ValueError("tracker tracking must be an object")
    reached = tracking.get("stages_reached", [])
    if not isinstance(reached, list) or any(not isinstance(stage, str) for stage in reached):
        raise ValueError("tracking.stages_reached must be a list of stage names")
    steps = [f"Stage: {WF.STAGE_LABELS[stage]}" for stage in WF.ROUND_IDS
             if stage != "introspect" and stage in reached]
    plan = tracking.get("plan")
    current_round = tracking.get("round", plan.get("round", 1) if isinstance(plan, dict) else 1)
    current_cycle = tracking.get("cycle", 1)
    if set(reached) & {"plan_fix", "install", "verify"}:
        if not isinstance(plan, dict) or not isinstance(plan.get("groups"), list) or not plan["groups"]:
            raise ValueError("tracking.plan with current-round groups is required to introspect performed fix work")
        if plan.get("round", current_round) != current_round or plan.get("cycle", current_cycle) != current_cycle:
            raise ValueError("tracking.plan must match the current round and cycle for Introspect coverage")
    if plan is not None:
        if not isinstance(plan, dict) or not isinstance(plan.get("groups"), list):
            raise ValueError("tracking.plan must contain groups for Introspect")
        if plan.get("round", current_round) == current_round and plan.get("cycle", current_cycle) == current_cycle:
            groups = [group for group in plan["groups"] if isinstance(group, dict)
                      and group.get("status") not in WF.LANE["groups"]["parked"]
                      and group.get("phase") not in WF.LANE["groups"]["parked"]]
            if groups:
                steps.append(f"Parallel group count: {len(groups)}")
                for group in groups:
                    group_id = group.get("id")
                    if not isinstance(group_id, str) or not group_id.strip():
                        raise ValueError("current plan groups need IDs for Introspect coverage")
                    codex = group.get("codex", {})
                    one_file = isinstance(codex, dict) and codex.get("one_file") is True
                    steps.append(f"Codex write: {group_id}")
                    steps.append(f"One-file review exemption: {group_id}" if one_file
                                 else f"Codex review: {group_id}")
                    steps.append(f"Polling and rework review: {group_id}")
                    if "verify" in reached:
                        verification = group.get("verify")
                        command = verification.get("command") if isinstance(verification, dict) else None
                        if isinstance(command, str) and command.strip():
                            steps.append(f"Test command: {group_id}: {command.strip()}")
    recorded = render.introspect_record_for_round(tracking)
    if recorded is not None:
        prior_steps = recorded.get("steps")
        if not isinstance(prior_steps, list) or any(not isinstance(step, str) or not step.strip() for step in prior_steps):
            raise ValueError("current-round Introspect record needs a steps array")
        steps.extend(step for step in prior_steps if step not in steps)
    return steps


def draft_introspect(tracking):
    """Build an editable, deliberately incomplete draft from recorded work."""
    return {
        "steps": performed_round_steps(tracking),
        "none": [],
        "sections": {
            name: {"none": f"Review performed steps; no {SECTION_NOUNS[name]} can be inferred from the tracker alone."}
            for name in SECTION_ORDER
        },
    }


def validate_introspect(sections, steps, none_steps, tracking):
    if not isinstance(sections, dict) or list(sections) != list(SECTION_ORDER):
        raise ValueError("sections must appear in order: delete, simplify, optimize, automate")
    if not isinstance(steps, list) or not steps or any(not isinstance(step, str) or not step.strip() for step in steps):
        raise ValueError("steps must list every nonempty step performed this round")
    if len(steps) != len(set(steps)):
        raise ValueError("steps must be unique")
    recorded = performed_round_steps(tracking)
    omitted = set(recorded) - set(steps)
    if omitted:
        raise ValueError("performed-step list omits tracker-recorded work: " + ", ".join(sorted(omitted)))
    if not isinstance(none_steps, list) or any(not isinstance(step, str) or not step.strip() for step in none_steps):
        raise ValueError("none must be a list of reviewed steps with no change")
    if len(none_steps) != len(set(none_steps)):
        raise ValueError("none step entries must be unique")

    covered = set(none_steps)
    unknown = covered - set(steps)
    for name in SECTION_ORDER:
        section = sections[name]
        if not isinstance(section, dict):
            raise ValueError(f"section {name} must be an object")
        if set(section) not in ({"none"}, {"items"}):
            raise ValueError(f"section {name} must contain only items or none")
        items = section.get("items")
        if items is not None:
            if not isinstance(items, list) or not items:
                raise ValueError(f"section {name} items must be a nonempty list")
            for item in items:
                step = item.get("item") or item.get("step") if isinstance(item, dict) else None
                if not isinstance(step, str) or not step.strip():
                    raise ValueError(f"section {name} items must name a performed step")
                if item.get("outcome") == "fixed inline":
                    proof = item.get("proof")
                    if not isinstance(proof, str) or not re.search(
                            r"\btest_[A-Za-z0-9_]+\b|(?:^|\s)(?:python3?|pytest|bcl|make|npm|pnpm|yarn|swift|xcodebuild|cargo|go)\s+",
                            proof):
                        raise ValueError(f"{step}: fixed inline requires a named command or test in proof")
                covered.add(step.strip())
        reason = section.get("none")
        if reason is not None and (not isinstance(reason, str) or not reason.strip()):
            raise ValueError(f"section {name} none requires a reason")
    unknown |= covered - set(steps)
    missing = set(steps) - covered
    if unknown:
        raise ValueError("sections refer to steps absent from the performed-step list: " + ", ".join(sorted(unknown)))
    if missing:
        raise ValueError("performed steps lack section or none coverage: " + ", ".join(sorted(missing)))
    return {"steps": list(steps), "none": list(none_steps)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True, type=Path, help="live BCL tracker JSON")
    parser.add_argument("--sections", required=True, type=Path, help="Introspect JSON containing steps, none, and ordered sections")
    args = parser.parse_args(argv)
    try:
        tracker = json.loads(args.data.read_text(encoding="utf-8"))
        payload = json.loads(args.sections.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or set(payload) != {"steps", "none", "sections"}:
            raise ValueError("input must contain exactly steps, none, and sections")
        validate_introspect(payload["sections"], payload["steps"], payload["none"], tracker.get("tracking"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"FAIL: {error}; fix: list every performed step under a section or none, then rerun Introspect", file=sys.stderr)
        return 1
    print(f"Introspect coverage valid: {len(payload['steps'])} steps")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
