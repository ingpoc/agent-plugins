#!/usr/bin/env python3
"""Check Introspect ordering and coverage of the round's performed steps."""
import argparse
import json
import sys
from pathlib import Path

SECTION_ORDER = ("delete", "simplify", "optimize", "automate")


def validate_introspect(sections, steps, none_steps):
    if not isinstance(sections, dict) or list(sections) != list(SECTION_ORDER):
        raise ValueError("sections must appear in order: delete, simplify, optimize, automate")
    if not isinstance(steps, list) or not steps or any(not isinstance(step, str) or not step.strip() for step in steps):
        raise ValueError("steps must list every nonempty step performed this round")
    if len(steps) != len(set(steps)):
        raise ValueError("steps must be unique")
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
    parser.add_argument("--sections", required=True, type=Path, help="Introspect JSON containing steps, none, and ordered sections")
    args = parser.parse_args(argv)
    try:
        payload = json.loads(args.sections.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or set(payload) != {"steps", "none", "sections"}:
            raise ValueError("input must contain exactly steps, none, and sections")
        validate_introspect(payload["sections"], payload["steps"], payload["none"])
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"FAIL: {error}; fix: list every performed step under a section or none, then rerun Introspect", file=sys.stderr)
        return 1
    print(f"Introspect coverage valid: {len(payload['steps'])} steps")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
