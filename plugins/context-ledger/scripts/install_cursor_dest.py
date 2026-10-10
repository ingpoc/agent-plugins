#!/usr/bin/env python3
"""Rewrite every Cursor dest mcp.json to an absolute launcher.

Source mcp.json stays portable (`./bin/context-ledger-mcp`, cwd `./`).
Cursor resolves a relative command against the workspace and does not expand
`${PLUGIN_*}`. Discovery includes the local install and every marketplace
cache hash, so a refresh that restored the portable file is rewritten again.
A dest that is still relative after the write fails the process.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

from context_ledger.cursor_dest import candidate_dests, rewrite_dest  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dest",
        type=Path,
        action="append",
        help="Cursor plugin install root (repeatable). Default: discover local+cache.",
    )
    args = parser.parse_args(argv)
    try:
        dests = [path.resolve() for path in args.dest] if args.dest else candidate_dests()
    except OSError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    if not dests:
        print(json.dumps({"ok": False, "error": "no Cursor dest found"}))
        return 1
    results = []
    failed = False
    for dest in dests:
        result = rewrite_dest(dest)
        results.append(result)
        failed = failed or not result.get("ok")
    print(json.dumps({"ok": not failed, "results": results}, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
