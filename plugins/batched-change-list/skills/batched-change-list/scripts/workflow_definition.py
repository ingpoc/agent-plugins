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
        unknown = [member for member in members if member not in known and member not in phases and member not in lane["job_phases"]]
        if unknown:
            raise ValueError(f"lane group {name} names unknown members: {', '.join(unknown)}")
    if any(target not in statuses for target in aliases.values()) or any(target not in phases for target in phase_aliases.values()):
        raise ValueError("lane aliases must resolve to declared statuses and phases")
    if not set(lane["pending_defaults"]) <= set(lane["protected_paths"]):
        raise ValueError("lane pending defaults must be protected paths")
    if set(lane["plan_fields"]) & set(lane["protected_paths"]) or {f"codex.{key}" for key in lane["codex_fields"]} & set(lane["protected_paths"]):
        raise ValueError("lane plan fields and protected paths must not overlap")
    if not set(lane["gate_paths"]) <= set(lane["protected_paths"]):
        raise ValueError("lane gate paths must be protected paths")
    if set(lane["moves"]) != set(statuses) or any(t not in statuses for targets in lane["moves"].values() for t in targets):
        raise ValueError("lane moves must list every declared status and only declared targets")
    shown = set(statuses) | set(lane["display_states"])
    if any(row[0] not in shown for row in lane["tally"]) or any(state not in statuses for state in lane["step_chips"]):
        raise ValueError("lane tally and step chips must name declared states")
    if not set(lane["review_cleared"]) <= set(lane["review_states"]) or not set(lane["presuite_blocking"]) <= set(lane["presuite_states"]):
        raise ValueError("lane review_cleared and presuite_blocking must name declared review and presuite states")
    if set(lane["job_phase_labels"]) != set(lane["job_phases"]):
        raise ValueError("lane job phase labels must match job phases")
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
        "gate_paths": tuple(lane["gate_paths"]),
        "plan_fields": frozenset(lane["plan_fields"]),
        "codex_fields": frozenset(lane["codex_fields"]),
        "review_cleared": frozenset(lane["review_cleared"]),
        "record_states": {name: frozenset(states) for name, states in lane["record_states"].items()},
        "presuite_blocking": frozenset(lane["presuite_blocking"]),
        "hash_excluded": tuple(lane["hash_excluded"]),
        "status_phase": {row["id"]: row["phase"] for row in lane["statuses"]},
        "status_copy": {row["id"]: row["copy"] for row in lane["statuses"]},
        "status_kind": {row["id"]: row["kind"] for row in lane["statuses"]},
        "job_phase_labels": dict(lane["job_phase_labels"]),
        "display_states": tuple(lane["display_states"]),
        "tally": tuple(tuple(row) for row in lane["tally"]),
        "step_chips": tuple(lane["step_chips"]),
        "moves": {status: frozenset(targets) for status, targets in lane["moves"].items()},
    }


LANE = _lane_contract(DEFINITION["lane"])


def lane_status_name(status):
    """Normalize a stored lane status through the declared aliases; raw statuses stay as stored."""
    return LANE["status_aliases"].get(status, status)


def refuse_lane_move(identifier, before, after):
    """Refuse a status write the lane table does not allow from the current status."""
    old, new = lane_status_name(before), lane_status_name(after)
    if old == new or old not in LANE["moves"] or new not in LANE["moves"]:
        return
    if new not in LANE["moves"][old]:
        allowed = ", ".join(sorted(LANE["moves"][old])) or "no move"
        raise ValueError(f"lane {identifier} cannot move from {old} to {new}; the lane table allows {allowed}")


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
