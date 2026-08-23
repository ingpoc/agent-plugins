#!/usr/bin/env python3
"""Deterministic contract validation plus opt-in live macOS acceptance tests."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys


HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
MAIN = HERE / "macos-cua.py"
OPERATOR = HERE / "operator_ui.py"
HERMES_CURSOR = SKILL / "assets" / "pointer-shape-animated.svg"


def load_main():
    spec = importlib.util.spec_from_file_location("macos_cua", MAIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def static_checks():
    checks = []

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    client_source = (SKILL / "service" / "cua_client.py").read_text()
    router_source = (
        SKILL / "service" / "Sources" / "CUAService" / "MethodRouter.swift"
    ).read_text()
    rpc_methods = ("list_apps", "get_app_state", "execute_plan", "open_item")
    add(
        "CUAService RPC contracts",
        all(f"def {method}" in client_source for method in rpc_methods)
        and all(f'case "{method}"' in router_source for method in rpc_methods),
        "Python client and Swift router must expose the same native owner",
    )

    help_result = subprocess.run(
        [sys.executable, str(MAIN), "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        stdin=subprocess.DEVNULL,
    )
    expected_commands = {
        "apps",
        "state",
        "click-point",
        "click-desktop",
        "double-click",
        "drag",
        "type-text",
        "set-value",
        "select-text",
        "perform-action",
        "run",
        "operator",
    }
    missing_commands = sorted(
        command for command in expected_commands if command not in help_result.stdout
    )
    add(
        "CLI parity commands",
        help_result.returncode == 0 and not missing_commands,
        f"missing={missing_commands}" if missing_commands else "",
    )
    add(
        "shared Hermes cursor asset",
        HERMES_CURSOR.is_file() and "pointer-shape-animated.svg" in MAIN.read_text(),
        str(HERMES_CURSOR),
    )
    fast_path = HERE / "fast_path.py"
    spec = importlib.util.spec_from_file_location("macos_cua_fast_path", fast_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    checks.extend(module.lint_source(SKILL))
    return checks


def live_checks(progress=False):
    """Load the opt-in live gate only when the caller requests it."""
    path = HERE / "validator_live.py"
    spec = importlib.util.spec_from_file_location("macos_cua_validator_live", path)
    module = importlib.util.module_from_spec(spec)
    module.__dict__.update(
        {name: value for name, value in globals().items() if not name.startswith("__")}
    )
    spec.loader.exec_module(module)
    return module.live_checks(progress=progress)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--live",
        action="store_true",
        help="Operate safe native-app acceptance scenarios",
    )
    parser.add_argument(
        "--progress", action="store_true", help="Print live gate progress to stderr"
    )
    args = parser.parse_args()
    checks = static_checks()
    if args.live:
        checks.extend(live_checks(progress=args.progress))
    result = {
        "ok": all(check["ok"] for check in checks),
        "mode": "live" if args.live else "static",
        "checks": checks,
    }
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
