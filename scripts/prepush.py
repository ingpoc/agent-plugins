#!/usr/bin/env python3
"""Fast local gate for git pre-push: changed files only, target under 15 s.

Reads pre-push stdin (`<local-ref> <local-sha> <remote-ref> <remote-sha>`) and
checks the files each pushed range changes:

- format: `git diff --check` (whitespace errors, conflict markers); JSON parses
- lint: `ruff check --select E9,F63,F7,F82` (syntax errors, undefined names) on
  changed .py; `bash -n` and `shellcheck -S error` on changed .sh
- typecheck: `python -m py_compile` on changed .py (no type checker is configured)
- contract: `scripts/check.py --validate-only` (manifests and catalogs)

The test suites stay in CI (the Origin check `scripts/check.py`). A missing
optional tool (ruff, shellcheck) is reported and skipped, never a silent pass of
the whole gate. `prepush.py --files A B` checks named files without git input.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ZERO = "0" * 40
BUDGET_SECONDS = 15.0
RUFF_RULES = "E9,F63,F7,F82"


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)


def pushed_ranges(stdin: str) -> list[tuple[str, str]]:
    """(base, head) per pushed ref; a new branch is measured from its merge base with main."""
    ranges: list[tuple[str, str]] = []
    for line in stdin.splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        _, local_sha, _, remote_sha = parts
        if local_sha == ZERO:  # branch deletion
            continue
        base = remote_sha
        if remote_sha == ZERO or git("cat-file", "-e", f"{remote_sha}^{{commit}}").returncode != 0:
            base = ""
            for main in ("origin-cursor/main", "origin/main", "main"):
                found = git("merge-base", local_sha, main)
                if found.returncode == 0 and found.stdout.strip():
                    base = found.stdout.strip()
                    break
            if not base:
                base = f"{local_sha}^"
        ranges.append((base, local_sha))
    return ranges


def changed_files(ranges: list[tuple[str, str]]) -> list[str]:
    seen: dict[str, None] = {}
    for base, head in ranges:
        proc = git("diff", "--name-only", "--diff-filter=ACMR", "-z", base, head)
        for name in proc.stdout.split("\0"):
            if name:
                seen.setdefault(name, None)
    return [name for name in seen if (ROOT / name).is_file()]


def step(name: str, cmd: list[str]) -> tuple[str, int, str]:
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)
    return name, proc.returncode, (proc.stdout + proc.stderr).strip()


def run(files: list[str], ranges: list[tuple[str, str]]) -> int:
    started = time.monotonic()
    results: list[tuple[str, int, str]] = []
    notes: list[str] = []
    for base, head in ranges:
        results.append(step(f"format: git diff --check {base[:8]}..{head[:8]}",
                            ["git", "diff", "--check", base, head]))
    py = [f for f in files if f.endswith(".py")]
    sh = [f for f in files if f.endswith(".sh") or f.startswith(".githooks/")]
    for name in (f for f in files if f.endswith(".json")):
        try:
            json.loads((ROOT / name).read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            results.append((f"format: json {name}", 1, str(exc)))
    if py:
        ruff = shutil.which("ruff")
        if ruff:
            results.append(step(f"lint: ruff {RUFF_RULES} ({len(py)} files)",
                                [ruff, "check", "--no-cache", "--quiet", "--select", RUFF_RULES, *py]))
        else:
            notes.append("ruff not installed: lint skipped (pip install ruff)")
        results.append(step(f"typecheck: py_compile ({len(py)} files)",
                            [sys.executable, "-m", "py_compile", *py]))
    for name in sh:
        results.append(step(f"lint: bash -n {name}", ["bash", "-n", name]))
    if sh:
        shellcheck = shutil.which("shellcheck")
        if shellcheck:
            results.append(step(f"lint: shellcheck -S error ({len(sh)} files)",
                                [shellcheck, "-S", "error", *sh]))
        else:
            notes.append("shellcheck not installed: shell lint skipped")
    results.append(step("contract: check.py --validate-only",
                        [sys.executable, str(ROOT / "scripts" / "check.py"), "--validate-only"]))
    failed = [r for r in results if r[1] != 0]
    for name, code, output in results:
        print(f"pre-push {'ok  ' if code == 0 else 'FAIL'} {name}", file=sys.stderr)
        if code != 0 and output:
            print(output, file=sys.stderr)
    for note in notes:
        print(f"pre-push note: {note}", file=sys.stderr)
    elapsed = time.monotonic() - started
    over = f" (over the {BUDGET_SECONDS:.0f} s budget)" if elapsed > BUDGET_SECONDS else ""
    print(f"pre-push {'failed' if failed else 'passed'}: {len(files)} changed files, "
          f"{elapsed:.1f}s{over}. Tests run in CI.", file=sys.stderr)
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--files", nargs="*", help="Check these files instead of reading pre-push stdin.")
    args = parser.parse_args(argv)
    if args.files is not None:
        return run([f for f in args.files if (ROOT / f).is_file()], [])
    ranges = pushed_ranges(sys.stdin.read())
    if not ranges:
        return 0
    return run(changed_files(ranges), ranges)


if __name__ == "__main__":
    raise SystemExit(main())
