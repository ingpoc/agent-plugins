"""Shared BCL workflow definition and stage checks."""
import importlib
import argparse
import json
import sys
from pathlib import Path

DEFINITION = json.loads(Path(__file__).with_name("workflow_definition.json").read_text(encoding="utf-8"))
ENTRY = DEFINITION["entry"]
LOOP = DEFINITION["loop"]
EXIT = DEFINITION["exit"]
STAGES = ENTRY + LOOP + EXIT
STAGE_LABELS = {stage["id"]: stage["label"] for stage in STAGES}
ROUND_IDS = [stage["id"] for stage in LOOP]
TRANSITIONS = DEFINITION["transitions"]
REQUIRED_CHECKS = DEFINITION["checks"]
DEFAULT_ROLLOVER_THRESHOLD = DEFINITION["gate"]["default_threshold"]
MERGE_PREVIOUS_PR_THRESHOLD = DEFINITION["gate"]["merge_previous_pr_threshold"]


def _lane_contract(lane):
    """One ordered lane table: statuses, phases, groups and protected runtime paths."""
    statuses = [row["id"] for row in lane["statuses"]]
    phases = [row["id"] for row in lane["phases"]]
    aliases, phase_aliases = lane["status_aliases"], lane["phase_aliases"]
    known = set(statuses) | set(aliases) | set(lane["raw_statuses"]) | {"in_progress"}
    for name, members in lane["groups"].items():
        unknown = [member for member in members if member not in known and member not in phases]
        if unknown:
            raise ValueError(f"lane group {name} names unknown members: {', '.join(unknown)}")
    if any(target not in statuses for target in aliases.values()) or any(target not in phases for target in phase_aliases.values()):
        raise ValueError("lane aliases must resolve to declared statuses and phases")
    if not set(lane["pending_defaults"]) <= set(lane["protected_paths"]):
        raise ValueError("lane pending defaults must be protected paths")
    return {
        "statuses": tuple(statuses),
        "status_labels": {row["id"]: row["label"] for row in lane["statuses"]},
        "status_aliases": dict(aliases),
        "raw_statuses": frozenset(lane["raw_statuses"]),
        "phases": tuple(phases),
        "phase_labels": {row["id"]: row["label"] for row in lane["phases"]},
        "phase_rank": {row["id"]: row["rank"] for row in lane["phases"]},
        "phase_aliases": dict(phase_aliases),
        "special_phases": frozenset(lane["special_phases"]),
        "job_phases": tuple(lane["job_phases"]),
        "review_phases": frozenset(lane["review_phases"]),
        "fix_states": tuple(lane["fix_states"]),
        "review_states": tuple(lane["review_states"]),
        "presuite_states": tuple(lane["presuite_states"]),
        "groups": {name: frozenset(members) for name, members in lane["groups"].items()},
        "protected_paths": tuple(lane["protected_paths"]),
        "pending_defaults": frozenset(lane["pending_defaults"]),
    }


LANE = _lane_contract(DEFINITION["lane"])
# Consumers still reading the old top-level rank move to LANE["phase_rank"] in later sub-lanes.
DEFINITION.setdefault("lane_phase_rank", LANE["phase_rank"])


def run_check(name, *args, **kwargs):
    spec = REQUIRED_CHECKS[name]
    function = getattr(importlib.import_module(spec["module"]), spec["function"])
    return function(*args, **kwargs)


def run_stage_check(stage, *args, **kwargs):
    spec = next((row for row in STAGES if row["id"] == stage), None)
    if spec is None or "check" not in spec:
        raise ValueError(f"workflow stage has no declared check: {stage}")
    return run_check(spec["check"], *args, **kwargs)


def require_transition(stage, reached):
    transition = TRANSITIONS.get(stage, [])
    if isinstance(transition, dict):
        if transition.get("when_reached") not in reached:
            return
        transition = transition.get("requires", [])
    missing = [required for required in transition if required not in reached]
    if missing:
        raise ValueError(f"{stage} requires {', '.join(missing)} to be reached first")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
