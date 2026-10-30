"""Retire files from the legacy visible-cursor helper without deleting them."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

HOME = Path.home()
TRASH = HOME / ".Trash"
LEGACY_PATHS = (
    HOME / "Library/LaunchAgents/com.macos-cua.operator.plist",
    HOME / "Library/Application Support/macos-cua/macos-cua Operator.app",
    HOME / ".cache/macos-cua/operator-state.json",
    HOME / ".cache/macos-cua/operator-state.json.lock",
    HOME / ".cache/macos-cua/operator",
)


def retire_legacy_operator() -> dict:
    result = {"ok": True, "unloaded": False, "trashed": [], "already_retired": False}
    try:
        bootout = subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}/com.macos-cua.operator"],
            capture_output=True, text=True, check=False, timeout=15,
        )
        if bootout.returncode not in (0, 3, 5, 113):
            result.update(ok=False, reason=f"launchctl bootout failed: {bootout.stderr.strip()}")
            return result
        result["unloaded"] = bootout.returncode == 0
        existing = [path for path in LEGACY_PATHS if path.exists() or path.is_symlink()]
        result["already_retired"] = not existing and not result["unloaded"]
        if not existing:
            return result
        TRASH.mkdir(parents=True, exist_ok=True)
        for path in existing:
            destination = TRASH / path.name
            suffix = 1
            while destination.exists() or destination.is_symlink():
                destination = TRASH / f"{path.name} {suffix}"
                suffix += 1
            shutil.move(str(path), str(destination))
            result["trashed"].append(str(destination))
    except (OSError, shutil.Error, subprocess.TimeoutExpired) as error:
        result.update(ok=False, reason=str(error))
    return result
