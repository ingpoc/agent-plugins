#!/usr/bin/env python3
"""Compare deterministic skill shape signals against a frozen skill."""
import argparse
import json
from pathlib import Path


def score(path):
    root = Path(path)
    text = (root / "SKILL.md").read_text()
    refs = {p.name for p in (root / "references").glob("*.md")}
    eval_path = root / "evals/evals.json"
    eval_data = json.loads(eval_path.read_text()) if eval_path.exists() else []
    evals = eval_data.get("evals", []) if isinstance(eval_data, dict) else eval_data
    if not isinstance(evals, list):
        evals = []
    return sum(("| Step | When | Load | Run |" in text,
                all(x in refs for x in ("collect.md", "present.md", "gate.md", "tracker.md", "viewer.md", "principles.md")),
                len(evals) >= 3,
                "python3 scripts/statechart.py" in text))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("current")
    parser.add_argument("frozen")
    args = parser.parse_args()
    new, old = score(args.current), score(args.frozen)
    print(f"current={new}/4 frozen={old}/4")
    return 1 if new <= old else 0


if __name__ == "__main__":
    raise SystemExit(main())
