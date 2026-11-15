#!/usr/bin/env python3
"""Recover a committed PR change-list page from the current repository's Git history."""
import argparse
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pr", type=int, help="pull request number")
    parser.add_argument("output", type=Path, help="new output HTML file (never overwritten)")
    args = parser.parse_args()
    if args.pr < 1:
        parser.error("PR number must be positive")
    path = f"docs/triage/pr-{args.pr}.html"
    try:
        commit = subprocess.check_output(
            ["git", "log", "--all", "--diff-filter=AM", "-1", "--format=%H", "--", path],
            stderr=subprocess.PIPE, text=True,
        ).strip()
        if not commit:
            raise ValueError(f"no committed report found for PR {args.pr}; fetch its refs or check the PR number")
        data = subprocess.check_output(["git", "show", f"{commit}:{path}"], stderr=subprocess.PIPE)
        if not data:
            raise ValueError(f"report is empty at {commit}; inspect the source commit")
        with args.output.open("xb") as output:
            output.write(data)
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode(errors="replace") if isinstance(error.stderr, bytes) else error.stderr
        print(f"previous-report: git failed: {(detail or str(error)).strip()}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as error:
        print(f"previous-report: {error}", file=sys.stderr)
        return 1
    print(f"Saved {path} from {commit} to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
