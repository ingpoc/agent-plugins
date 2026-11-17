"""Shared BCL workflow definition and its skill-line generator."""
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


def skill_loop_line():
    entry = " and ".join(stage["label"] for stage in ENTRY)
    loop = " → ".join(stage["label"] for stage in LOOP)
    exit_path = " → ".join(stage["label"] for stage in EXIT)
    return f"Workflow: {entry} once; then loop {loop}. At {DEFAULT_ROLLOVER_THRESHOLD}+ fixes, {exit_path}."


def write_skill_loop(path=None):
    path = Path(path) if path else Path(__file__).resolve().parents[1] / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    marker = "Workflow: "
    lines = text.splitlines()
    matches = [i for i, line in enumerate(lines) if line.startswith(marker)]
    if len(matches) != 1:
        raise ValueError("SKILL.md needs exactly one generated Workflow line")
    lines[matches[0]] = skill_loop_line()
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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
    parser.add_argument("--write-skill", action="store_true", help="generate the Workflow line in SKILL.md")
    args = parser.parse_args(argv)
    if not args.write_skill:
        parser.error("pass --write-skill")
    try:
        write_skill_loop()
    except (OSError, ValueError) as error:
        print(f"FAIL: {error}; fix: restore one generated Workflow line in SKILL.md", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
