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
