#!/usr/bin/env python3
"""Deterministic contract validation plus opt-in live macOS acceptance tests."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys


HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
MAIN = HERE / "macos-cua.py"
CUA_DRIVER = Path(os.environ.get("CUA_DRIVER", "~/.local/bin/cua-driver")).expanduser()
OPERATOR = HERE / "operator_ui.py"
HERMES_CURSOR = SKILL / "assets" / "pointer-shape-animated.svg"


def load_main():
    spec = importlib.util.spec_from_file_location("macos_cua", MAIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_json(command: list[str], timeout: int = 20):
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return json.loads(result.stdout)


def static_checks():
    checks = []
    cua = load_main()

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    add(
        "driver executable",
        CUA_DRIVER.is_file() and os.access(CUA_DRIVER, os.X_OK),
        str(CUA_DRIVER),
    )
    version = cua.driver_version()
    add("driver supported version", version.get("ok"), version)
    docs = run_json([str(CUA_DRIVER), "dump-docs", "--type", "json"])
    schemas = {
        tool["name"]: set(
            tool.get("inputSchema", tool.get("input_schema", {})).get("properties", {})
        )
        for tool in docs["mcp"]["tools"]
    }
    required = {
        "get_window_state": {
            "pid",
            "window_id",
            "include_screenshot",
            "screenshot_out_file",
        },
        "click": {"pid", "window_id", "element_index", "x", "y", "count", "action"},
        "drag": {"pid", "window_id", "from_x", "from_y", "to_x", "to_y"},
        "type_text": {"pid", "window_id", "element_index", "text"},
        "set_value": {"pid", "window_id", "element_index", "value"},
        "press_key": {"pid", "window_id", "key", "delivery_mode"},
        "hotkey": {"pid", "window_id", "keys", "delivery_mode"},
        "scroll": {
            "pid",
            "window_id",
            "direction",
            "element_index",
            "x",
            "y",
            "delivery_mode",
        },
        "right_click": {"pid", "window_id", "element_index"},
    }
    for tool, fields in required.items():
        missing = sorted(fields - schemas.get(tool, set()))
        add(
            f"driver schema: {tool}",
            not missing,
            f"missing={missing}" if missing else "",
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
    operator_build = run_json([sys.executable, str(OPERATOR), "build"], timeout=120)
    add(
        "native operator build",
        operator_build.get("ok")
        and Path(operator_build.get("binary", "")).is_file()
        and operator_build.get("signing", {}).get("mode") == "identity",
        operator_build,
    )
    source = "\n".join(
        path.read_text() for path in sorted((SKILL / "operator").glob("*.swift"))
    )
    add(
        "operator UI contracts",
        all(
            token in source
            for token in (
                "NSStatusItem",
                "NSPanel",
                "PreviewView",
                "cursorImagePath",
                "End Controlled Session",
                "Harness:",
                "cursor_update_id",
                "cursor_rendered_update_id",
                "flock(lockDescriptor, LOCK_EX)",
            )
        ),
        "menu bar + PiP cursor + native controls + controlled app + harness",
    )
    operator_source = OPERATOR.read_text() + (HERE / "operator_cli.py").read_text()
    add(
        "signed launchd service contracts",
        all(
            token in operator_source
            for token in (
                "codesign",
                "Apple Development:",
                "launchctl",
                "KeepAlive",
                "install-service",
                "uninstall-service",
            )
        ),
        "identity signing + packaged app + reversible launchd lifecycle",
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
