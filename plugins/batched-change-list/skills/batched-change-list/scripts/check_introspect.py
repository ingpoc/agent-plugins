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
    for row in rank_costliest_jobs(tracking):
        if row["step"] not in steps:
            steps.append(row["step"])
    return steps


def job_receipts(tracking):
    """Collect finished job receipts from the current plan for cost ranking."""
    plan = tracking.get("plan") if isinstance(tracking, dict) else None
    groups = plan.get("groups") if isinstance(plan, dict) else []
    receipts = []
    for group in groups or []:
        if not isinstance(group, dict):
            continue
        group_id = group.get("id") or "lane"
        codex = group.get("codex") if isinstance(group.get("codex"), dict) else {}
        jobs = []
        if isinstance(codex.get("job"), dict):
            jobs.append(codex["job"])
        if isinstance(codex.get("jobs"), list):
            jobs.extend(job for job in codex["jobs"] if isinstance(job, dict))
        seen = set()
        for job in jobs:
            job_id = job.get("id") or job.get("job_id")
            key = job_id or id(job)
            if key in seen:
                continue
            seen.add(key)
            if "exit_code" not in job and job.get("finished_phase_status") is None:
                continue
            receipts.append({"group_id": group_id, "job_id": job_id, "phase": job.get("phase"),
                             "wall_time_s": job.get("wall_time_s") if isinstance(job.get("wall_time_s"), int) else None,
                             "tokens": job.get("tokens") if isinstance(job.get("tokens"), dict) else None,
                             "model": job.get("model")})
    return receipts


def rank_costliest_jobs(tracking, limit=3):
    """Rank finished jobs by wall time and tokens, and propose a cut for the top ones."""
    receipts = job_receipts(tracking)
    by_time = sorted((row for row in receipts if isinstance(row.get("wall_time_s"), int)),
                     key=lambda row: row["wall_time_s"], reverse=True)
    by_tokens = sorted(
        (row for row in receipts if isinstance((row.get("tokens") or {}).get("total"), int)),
        key=lambda row: row["tokens"]["total"], reverse=True)
    ranked = []
    for source, rows, unit in (("wall time", by_time, "s"), ("tokens", by_tokens, " tokens")):
        for index, row in enumerate(rows[:limit], start=1):
            amount = row["wall_time_s"] if unit == "s" else row["tokens"]["total"]
            step = (f"Job {source}: {row['group_id']}"
                    + (f" {row['job_id']}" if row.get("job_id") else "")
                    + f" {amount}{unit}")
            cut = ("Propose a cut: split or skip this step first; it used the most "
                   f"{source} in the recorded jobs.")
            if index > 1:
                cut = f"Propose a cut: rank {index} by {source}; shorten or defer it after the top step."
            ranked.append({"step": step, "item": step, "change": cut, "metric": f"{amount}{unit}"})
    return ranked


def cross_round_repeats(tracking):
    """Name repeating waste kinds that already appeared more than once."""
    if not isinstance(tracking, dict):
        return []
    repeats = []
    history = tracking.get("verification_history") if isinstance(tracking.get("verification_history"), list) else []
    interrupted = [row for row in history if isinstance(row, dict) and row.get("status") == "interrupted"]
    if len(interrupted) >= 2:
        ids = ", ".join(str(row.get("id") or "run") for row in interrupted[:4])
        repeats.append({
            "kind": "interrupted runs",
            "step": "Repeat: interrupted verification runs",
            "evidence": f"{len(interrupted)} interrupted runs, including {ids}",
            "change": "Require a workflow change so an observer drop cannot leave a run interrupted.",
        })
    plan = tracking.get("plan") if isinstance(tracking.get("plan"), dict) else {}
    for group in plan.get("groups") or []:
        if not isinstance(group, dict):
            continue
        group_id = group.get("id") or "lane"
        failures = group.get("failure_history") if isinstance(group.get("failure_history"), list) else []
        reviews = [row for row in failures if isinstance(row, dict) and row.get("source") == "review"]
        if len(reviews) >= 2:
            repeats.append({
                "kind": "review loops",
                "step": f"Repeat: review failures on {group_id}",
                "evidence": f"{group_id} has {len(reviews)} review failures in failure_history",
                "change": "Require a workflow change; do not keep running plan reviews on the same lane.",
            })
        plans = group.get("plan_review_history") if isinstance(group.get("plan_review_history"), list) else []
        if len(plans) >= 2:
            repeats.append({
                "kind": "plan review loops",
                "step": f"Repeat: plan reviews on {group_id}",
                "evidence": f"{group_id} has {len(plans)} plan reviews in plan_review_history",
                "change": "Require a workflow change when plan review repeats on the same lane.",
            })
    return repeats


def token_comparison(tracking):
    """Compare current-round token totals with the previous round, naming missing sources."""
    receipts = job_receipts(tracking)
    current_total = 0
    missing = []
    for row in receipts:
        tokens = row.get("tokens") or {}
        if isinstance(tokens.get("total"), int):
            current_total += tokens["total"]
        else:
            missing.append(row.get("job_id") or row.get("group_id") or "job")
    previous = None
    history = tracking.get("introspect_history") if isinstance(tracking.get("introspect_history"), list) else []
    current_round = tracking.get("round", 1)
    for row in reversed(history):
        if not isinstance(row, dict):
            continue
        if type(row.get("round")) is int and row["round"] < current_round and type(row.get("tokens_total")) is int:
            previous = row["tokens_total"]
            break
    parts = [f"This round's recorded job tokens total {current_total}"]
    if previous is None:
        parts.append("the previous round has no comparable token total")
    else:
        parts.append(f"the previous round totaled {previous}")
    if missing:
        parts.append("missing token source for " + ", ".join(missing))
    return current_total, previous, missing, "; ".join(parts) + "."


def draft_introspect(tracking):
    """Build an editable, deliberately incomplete draft from recorded work."""
    steps = performed_round_steps(tracking)
    sections = {
        name: {"none": f"Review performed steps; no {SECTION_NOUNS[name]} can be inferred from the tracker alone."}
        for name in SECTION_ORDER
    }
    ranked = rank_costliest_jobs(tracking)
    _, _, _, token_line = token_comparison(tracking)
    if ranked:
        lines = [f"{row['item']}. {row['change']}" for row in ranked]
        sections["optimize"] = {"none": " ".join(lines) + " " + token_line}
    else:
        sections["optimize"] = {"none": token_line}
    repeats = cross_round_repeats(tracking)
    if repeats:
        items = []
        for row in repeats:
            if row["step"] not in steps:
                steps.append(row["step"])
            items.append({"item": row["step"], "evidence": row["evidence"],
                          "outcome": "issue", "change": row["change"]})
        sections["simplify"] = {"items": items}
    sections["automate"] = {"none": (
        "Evidence quality: judge whether observability evidence was complete and useful; "
        "file or append DO issues for gaps. Items without a finding key or evidence path must be flagged or dropped."
    )}
    return {"steps": steps, "none": [], "sections": sections}


def draft_post_merge_introspect(tracking):
    """Draft a post-merge Introspect over the PR's measured job data."""
    draft = draft_introspect(tracking)
    draft["scope"] = "post_merge"
    repeats = cross_round_repeats(tracking)
    if repeats:
        draft["required_changes"] = [row["change"] for row in repeats]
    return draft


def source_list_path(live_path, tracking):
    """Return the sibling live tracker for rollover.source_pr, if it exists."""
    if not live_path or not isinstance(tracking, dict):
        return None
    rollover = tracking.get("rollover") if isinstance(tracking.get("rollover"), dict) else {}
    source_pr = rollover.get("source_pr")
    if source_pr is None:
        return None
    candidate = Path(live_path).expanduser().resolve().parent / f"{source_pr}.json"
    return candidate if candidate.is_file() else None


def coverage_tracking(live_path, tracking, scope=None):
    """For a post-merge Introspect, use the merged list's job data when this list has not reached Verify."""
    if not isinstance(tracking, dict):
        raise ValueError("tracker tracking must be an object")
    if scope != "post_merge":
        return tracking
    reached = tracking.get("stages_reached") or []
    if isinstance(reached, list) and "verify" in reached:
        return tracking
    source = source_list_path(live_path, tracking)
    if source is None:
        return tracking
    document = json.loads(source.read_text(encoding="utf-8"))
    source_tracking = document.get("tracking") if isinstance(document, dict) else None
    if not isinstance(source_tracking, dict):
        raise ValueError(f"source list {source} has no tracking object")
    return source_tracking


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
    parser.add_argument("--scope", choices=("post_merge",), help="validate a post-merge Introspect against the merged list when needed")
    args = parser.parse_args(argv)
    try:
        tracker = json.loads(args.data.read_text(encoding="utf-8"))
        payload = json.loads(args.sections.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or set(payload) != {"steps", "none", "sections"}:
            raise ValueError("input must contain exactly steps, none, and sections")
        tracking = coverage_tracking(args.data, tracker.get("tracking"), args.scope)
        validate_introspect(payload["sections"], payload["steps"], payload["none"], tracking)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"FAIL: {error}; fix: list every performed step under a section or none, then rerun Introspect", file=sys.stderr)
        return 1
    print(f"Introspect coverage valid: {len(payload['steps'])} steps")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
