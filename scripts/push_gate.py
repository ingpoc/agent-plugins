#!/usr/bin/env python3
"""Deterministic pre-push rules for agent-plugins (docs/dev-lifecycle.md, Enforcement).

Pure rule checks on the pushing checkout and the pre-push stdin; no network.
Each failure names the rule, the fix and its one-push override:

- placement: canonical checkout or a worktree under worktree_root / worktree_roots
  (scripts/placement.py; AGENT_PLUGINS_ALLOW_PLACEMENT=1)
- branch-name: a non-main branch is <owner>/<slug>, owner from
  [push_gate].branch_owners in machine.toml or DEFAULT_OWNERS
  (AGENT_PLUGINS_ALLOW_BRANCH_NAME=1)
- main-push: main is pushed only from the canonical checkout, never from a linked
  worktree; the normal path is an Origin PR and `origin pr merge`
  (AGENT_PLUGINS_ALLOW_MAIN_PUSH=1)

A new branch prints a reminder to open its PR in the same turn (never blocks).
Tags and deletions are not checked. No-op in CI or without ~/.agents/machine.toml.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import placement  # noqa: E402

ZERO = "0" * 40
DEFAULT_OWNERS = ("codex", "acu", "grokbot", "efficient", "t3", "goal", "cursor", "claude", "grok", "opencode")
SLUG_RE = r"[A-Za-z0-9][A-Za-z0-9._-]*"
BRANCH_ENV = "AGENT_PLUGINS_ALLOW_BRANCH_NAME"
MAIN_ENV = "AGENT_PLUGINS_ALLOW_MAIN_PUSH"
HEADS = "refs/heads/"


def load_policy(policy_path: Path | None = None) -> dict | None:
    path = policy_path or Path(os.environ.get("AGENT_PLUGINS_MACHINE_TOML", "~/.agents/machine.toml")).expanduser()
    return tomllib.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def owners(policy: dict) -> tuple[str, ...]:
    listed = policy.get("push_gate", {}).get("branch_owners")
    if isinstance(listed, list) and listed and all(isinstance(o, str) and o for o in listed):
        return tuple(listed)
    return DEFAULT_OWNERS


def parse_updates(stdin: str) -> list[tuple[str, str, str, str]]:
    return [tuple(parts) for parts in (line.split() for line in stdin.splitlines()) if len(parts) == 4]


def _branches(updates) -> list[tuple[str, str]]:
    """(branch, remote_sha) for each pushed (not deleted) branch ref."""
    return [(remote_ref[len(HEADS):], remote_sha) for _, local_sha, remote_ref, remote_sha in updates
            if local_sha != ZERO and remote_ref.startswith(HEADS)]


def problems(root: Path, updates, policy_path: Path | None = None) -> list[tuple[str, str, str, str]]:
    """(rule, message, fix, override_env) per violation; empty when fine or not checkable."""
    if os.environ.get("CI"):
        return []
    policy = load_policy(policy_path)
    paths = (policy or {}).get("paths", {})
    if "checkout_root" not in paths or "worktree_root" not in paths:
        return []
    canonical = Path(os.path.expanduser(paths["checkout_root"])).resolve() / "agent-plugins"
    proc = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True, check=False)
    top = Path(proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else root).resolve()
    found = [("placement", msg, "work from the canonical checkout or a worktree under worktree_root",
              placement.OVERRIDE_ENV) for msg in placement.problems(root, policy_path)]
    allowed = owners(policy)
    pattern = re.compile(rf"({'|'.join(map(re.escape, allowed))})/{SLUG_RE}")
    for branch, _ in _branches(updates):
        if branch == "main":
            if top != canonical:
                found.append(("main-push", f"{top} is a linked worktree pushing main",
                              f"open an Origin PR and merge with `origin pr merge` (docs/dev-lifecycle.md "
                              f"section 3); main moves only on Origin or from {canonical}", MAIN_ENV))
        elif not pattern.fullmatch(branch):
            found.append(("branch-name", f"branch {branch!r} is not <owner>/<slug> with owner in "
                          f"{', '.join(allowed)}", "rename: `git branch -m <owner>/<slug>`, or add the owner "
                          "to [push_gate].branch_owners in machine.toml", BRANCH_ENV))
    return found


def reminders(updates) -> list[str]:
    return [f"new branch {b}: open an Origin PR into main in this same turn "
            f"(origin pr create --base main --head {b})"
            for b, remote_sha in _branches(updates) if b != "main" and remote_sha == ZERO]


def gate(root: Path, stdin: str, stream=sys.stderr, policy_path: Path | None = None) -> int:
    """1 blocks the push; a rule's override env downgrades its failure to a warning."""
    updates = parse_updates(stdin)
    blocked = False
    for rule, msg, fix, env in problems(root, updates, policy_path):
        overridden = os.environ.get(env) == "1"
        blocked |= not overridden
        print(f"pre-push {'WARN' if overridden else 'FAIL'} {rule}: {msg}", file=stream)
        print(f"  fix: {fix}", file=stream)
        print(f"  override once: {env}=1 (say why in the PR)", file=stream)
    if blocked:
        return 1
    if not os.environ.get("CI"):
        for note in reminders(updates):
            print(f"pre-push note: {note}", file=stream)
    return 0


if __name__ == "__main__":
    raise SystemExit(gate(Path.cwd(), sys.stdin.read()))
