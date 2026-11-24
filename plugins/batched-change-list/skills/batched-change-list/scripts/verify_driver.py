#!/usr/bin/env python3
"""Shared, tested execution primitives for BCL verification rounds."""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


@dataclass(frozen=True)
class CheckResult:
    verdict: str
    exit_code: int
    cleanup: str = "not_run"


def judge(exit_code: int) -> str:
    """Convert a check process result into the lane verdict."""
    return "passed" if exit_code == 0 else "failed"


def run_check(command: Sequence[str], *, on_spawn: Callable[[], None] | None = None) -> int:
    """Run one shared or lane check, preserving its exit status."""
    try:
        process = subprocess.Popen(command)
        if on_spawn is not None:
            on_spawn()
        return process.wait()
    except OSError as error:
        print(f"Verification command could not start: {error}", file=sys.stderr)
        return 127


def run_round(*, shared: Sequence[str] | None, lanes: Sequence[Sequence[str]],
              cleanup: Callable[[], None] | None = None,
              on_spawn: Callable[[], None] | None = None) -> tuple[list[CheckResult], int | None]:
    """Run shared gate, then lanes; cleanup status is independent of verdicts."""
    def execute(command: Sequence[str]) -> int:
        if on_spawn is None:
            return run_check(command)
        return run_check(command, on_spawn=on_spawn)

    if shared is not None:
        code = execute(shared)
        if code:
            return [], code
    results = []
    for command in lanes:
        code = execute(command)
        results.append(CheckResult(judge(code), code))
    cleanup_error = None
    if cleanup is not None:
        try:
            cleanup()
            cleanup_status = "passed"
        except Exception as error:  # cleanup must never rewrite a check verdict
            cleanup_status = "failed"
            cleanup_error = str(error)
        results = [CheckResult(item.verdict, item.exit_code, cleanup_status) for item in results]
    return results, (cleanup_error and 1 or None)


def self_test() -> None:
    """Executable invariants checked by `bcl verify` before a round starts."""
    assert judge(0) == "passed"
    assert judge(1) == "failed"

    def fail_cleanup():
        raise RuntimeError("expected self-test cleanup failure")

    work = Path(tempfile.mkdtemp(prefix="bcl-verify-self-test-"))
    marker = work / "shared-command-ran"
    token = "shared-command-ran"
    try:
        results, cleanup_code = run_round(
            shared=[sys.executable, "-c",
                    "from pathlib import Path; Path(%r).write_text(%r)" % (str(marker), token)],
            lanes=[[sys.executable, "-c", "raise SystemExit(0)"]],
            cleanup=fail_cleanup,
        )
        # Evidence must come from the command itself, not from a runner success callback.
        assert marker.is_file() and marker.read_text() == token
        assert len(results) == 1
        assert results[0].verdict == "passed"
        assert results[0].exit_code == 0
        assert results[0].cleanup == "failed"
        assert cleanup_code == 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        print("Verify driver self-test passed")
        return 0
    parser.error("select --self-test; checks are launched by bcl")


if __name__ == "__main__":
    raise SystemExit(main())
