#!/usr/bin/env python3
"""Checkout placement rule for agent-plugins (docs/dev-lifecycle.md, Enforcement).

Pushes must come from the canonical checkout (`<checkout_root>/agent-plugins`) or
from a linked worktree of it under `<worktree_root>/agent-plugins/` (or a
harness-owned root in `[exceptions].worktree_roots`). A second clone, or a
worktree anywhere else, is what produced the 2026-11-03 cleanup.

Paths come from `~/.agents/machine.toml` (override with AGENT_PLUGINS_MACHINE_TOML).
No machine policy, or CI set: nothing to check.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

OVERRIDE_ENV = "AGENT_PLUGINS_ALLOW_PLACEMENT"


def _expand(value: str) -> Path:
    return Path(os.path.expanduser(value)).resolve()


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def problems(root: Path, policy_path: Path | None = None) -> list[str]:
    """Placement violations for the checkout at `root`; empty when fine or not checkable."""
    if os.environ.get("CI"):
        return []
    policy_path = policy_path or Path(os.environ.get("AGENT_PLUGINS_MACHINE_TOML", "~/.agents/machine.toml")).expanduser()
    if not policy_path.is_file():
        return []
    policy = tomllib.loads(policy_path.read_text(encoding="utf-8"))
    paths = policy.get("paths", {})
    if "checkout_root" not in paths or "worktree_root" not in paths:
        return []
    canonical = _expand(paths["checkout_root"]) / "agent-plugins"
    allowed = [_expand(paths["worktree_root"]) / "agent-plugins"]
    allowed += [_expand(p) for p in policy.get("exceptions", {}).get("worktree_roots", [])]
    top = Path(_git(root, "rev-parse", "--show-toplevel") or root).resolve()
    common = _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    found: list[str] = []
    if common and Path(common).resolve() != (canonical / ".git").resolve():
        found.append(f"{top} is a second clone; work from {canonical} or a worktree of it "
                     f"under {allowed[0]}/<task>")
    elif top != canonical and not any(_under(top, a) for a in allowed):
        found.append(f"worktree {top} is outside {allowed[0]}; use "
                     f"`git -C {canonical} worktree add {allowed[0]}/<task> -b <harness>/<task> origin-cursor/main`")
    return found


def gate(root: Path, stream=sys.stderr) -> int:
    """1 blocks the push; the override env downgrades a violation to a warning."""
    found = problems(root)
    if not found:
        return 0
    override = os.environ.get(OVERRIDE_ENV) == "1"
    for line in found:
        print(f"pre-push {'WARN' if override else 'FAIL'} placement: {line}", file=stream)
    if override:
        print(f"pre-push note: {OVERRIDE_ENV}=1 set; pushing anyway", file=stream)
        return 0
    print(f"pre-push: set {OVERRIDE_ENV}=1 to push once anyway (say why in the PR)", file=stream)
    return 1


if __name__ == "__main__":
    raise SystemExit(gate(Path.cwd()))
