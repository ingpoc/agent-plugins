#!/usr/bin/env python3
"""CLI wrapper for the deterministic Comet Control Bridge diagnostics.

Optional CLI over `diagnostics.run_diagnostics()`. Prints the shared payload.

Usage:
  python3 diagnose.py          # human-readable summary
  python3 diagnose.py --json   # machine-readable (skill preflight parses this)

Exit code: 0 if no blocking checks failed, 1 otherwise.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Import the shared checker from the plugin root (sibling of scripts/).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from diagnostics import run_diagnostics  # noqa: E402  # pyright: ignore[reportMissingImports]


def main() -> int:
    report = run_diagnostics()
    as_json = "--json" in sys.argv[1:]

    if as_json:
        print(json.dumps(report, indent=2))
    else:
        verdict = "READY" if report["preflight_ok"] else "BLOCKED"
        print(f"comet-control diagnose [{report['platform']}] → {verdict}")
        for c in report["checks"]:
            mark = {True: "PASS", False: "FAIL", None: "????"}[c["ok"]]
            sev = "" if c["ok"] else f"  ({c['severity']})"
            print(f"  {mark} {c['name']}{sev}")
            if c["ok"] is not True:
                print(f"       {c['detail']}")
                if c["ok"] is False:
                    print(f"       surface: {c['surface']}")
                    print(f"       fix: {c['fix']}")

    return 0 if report["preflight_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
