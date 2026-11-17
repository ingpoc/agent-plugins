#!/usr/bin/env python3
"""Report the authoritative BCL rollover gate count."""
import argparse
import json
import sys
from pathlib import Path

import render


def gate_count(data):
    if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
        raise ValueError("tracker must contain a findings array")
    tracking = data.get("tracking", {})
    if not isinstance(tracking, dict):
        raise ValueError("tracker tracking must be an object")
    return render.rollover_gate(data["findings"], tracking)


def merge_gate_count(data):
    if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
        raise ValueError("tracker must contain a findings array")
    tracking = data.get("tracking", {})
    if not isinstance(tracking, dict):
        raise ValueError("tracker tracking must be an object")
    return render.merge_gate(data["findings"], tracking)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True, type=Path, help="live BCL tracker JSON")
    args = parser.parse_args(argv)
    try:
        data = json.loads(args.data.read_text(encoding="utf-8"))
        fixed, threshold, met = gate_count(data)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"FAIL: {error}; fix: provide a valid live tracker and rerun the gate count", file=sys.stderr)
        return 1
    print(f"Gate count: {fixed} of {threshold}; met={str(met).lower()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
