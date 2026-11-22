#!/usr/bin/env python3
"""Commit hook: regenerate the batched-change-list state chart when its sources change.

The chart (statechart.html) is derived from SKILL.md, references/*.md and
scripts/workflow_definition.json. When a commit touches the skill and the chart is
stale, the pre-commit hook regenerates it from the working tree and stages it, so
the chart lands in the same commit. Git cannot add files to a clean merge commit,
so after such a merge the post-merge hook commits the regenerated chart on top.
Either way the pre-push suite no longer fails on a stale chart (BCL-155).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = "plugins/batched-change-list/skills/batched-change-list"
CHART = f"{SKILL}/statechart.html"


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)


def commit_on_head(message: str) -> subprocess.CompletedProcess:
    # Plumbing, so leftover merge state cannot add a parent: one commit on HEAD with the staged chart.
    tree = git("write-tree")
    if tree.returncode != 0:
        return tree
    commit = git("commit-tree", tree.stdout.strip(), "-p", "HEAD", "-m", message)
    if commit.returncode != 0:
        return commit
    return git("update-ref", "-m", "precommit_chart: " + message, "HEAD", commit.stdout.strip())


def touched(after_merge: bool) -> list[str]:
    if after_merge:
        changed = git("diff", "--name-only", "ORIG_HEAD", "HEAD").stdout.split()
    else:
        changed = git("diff", "--cached", "--name-only").stdout.split()
    return [path for path in changed if path.startswith(SKILL + "/")]


def main(argv: list[str]) -> int:
    after_merge = argv[1:] == ["post-merge"]
    if not touched(after_merge) or not (ROOT / SKILL / "scripts/statechart.py").is_file():
        return 0
    script = [sys.executable, str(ROOT / SKILL / "scripts/statechart.py")]
    if subprocess.run([*script, "--check"], cwd=ROOT, capture_output=True).returncode == 0:
        return 0
    built = subprocess.run(script, cwd=ROOT, capture_output=True, text=True)
    if built.returncode != 0:
        sys.stderr.write(f"precommit_chart: regenerating {CHART} failed:\n{built.stderr}")
        return 1
    step = git("add", CHART)
    if step.returncode == 0 and after_merge:
        step = commit_on_head("Regenerate the batched-change-list state chart after merge")
    if step.returncode != 0:
        sys.stderr.write(f"precommit_chart: recording {CHART} failed: {step.stderr}")
        return 1
    print(f"precommit_chart: regenerated {CHART}" + (" and committed it after the merge" if after_merge else " and staged it"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
