#!/usr/bin/env python3
"""Window enumeration: Quartz first, cua-driver fallback.

`list_windows` via cua-driver can hang or return empty while the daemon socket
is wedged. Quartz CGWindowListCopyWindowInfo is fast and sufficient for pid/window_id.
"""
from __future__ import annotations

import json
import os
import subprocess
import time

CUA_DRIVER = os.environ.get("CUA_DRIVER", os.path.expanduser("~/.local/bin/cua-driver"))


def hard_reset_daemon() -> None:
    subprocess.run(["pkill", "-f", "cua-driver"], capture_output=True)
    sock = os.path.expanduser("~/Library/Caches/cua-driver/cua-driver.sock")
    try:
        os.unlink(sock)
    except OSError:
        pass
    time.sleep(1)
    restart_daemon()


def restart_daemon() -> None:
    uid = os.getuid()
    subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{uid}/com.trycua.driver"],
        capture_output=True,
        timeout=15,
    )
    time.sleep(2)


def daemon_running() -> bool:
    try:
        r = subprocess.run(
            [CUA_DRIVER, "status"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        text = f"{r.stdout or ''}\n{r.stderr or ''}".lower()
        return r.returncode == 0 and "daemon is running" in text
    except subprocess.TimeoutExpired:
        return False


def call_driver(tool_name: str, params=None, timeout: int = 8):
    # ALWAYS pass a params argv (even "{}") and close stdin: without an argv
    # params the CLI reads params from stdin and blocks forever on inherited
    # agent-shell pipes (looks exactly like a wedged daemon).
    cmd = [CUA_DRIVER, "call", tool_name, json.dumps(params or {})]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return {"error": f"timeout after {timeout}s", "tool": tool_name}
    if result.returncode != 0:
        return {"error": (result.stderr or result.stdout).strip(), "tool": tool_name}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"raw": result.stdout.strip()}


def call_driver_recover(tool_name: str, params=None, timeout: int = 8):
    res = call_driver(tool_name, params, timeout=timeout)
    if "timeout" in str(res.get("error", "")):
        hard_reset_daemon()
        res = call_driver(tool_name, params, timeout=timeout)
    return res


def list_windows_quartz() -> list[dict]:
    try:
        from Quartz import CGWindowListCopyWindowInfo, kCGNullWindowID, kCGWindowListOptionOnScreenOnly
    except ImportError:
        return []
    out = []
    for w in CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID):
        owner = w.get("kCGWindowOwnerName") or ""
        bounds = w.get("kCGWindowBounds") or {}
        width = int(bounds.get("Width", 0) or 0)
        height = int(bounds.get("Height", 0) or 0)
        if width < 50 or height < 50:
            continue
        pid = w.get("kCGWindowOwnerPID")
        wid = w.get("kCGWindowNumber")
        if not pid or not wid:
            continue
        out.append(
            {
                "pid": int(pid),
                "window_id": int(wid),
                "app_name": owner,
                "owner": owner,
                "title": w.get("kCGWindowName") or "",
                "bounds": {
                    "width": width,
                    "height": height,
                    "x": int(bounds.get("X", 0) or 0),
                    "y": int(bounds.get("Y", 0) or 0),
                },
                "source": "quartz",
            }
        )
    return out


def list_windows_driver(timeout: int = 8) -> list[dict]:
    data = call_driver_recover("list_windows", timeout=timeout)
    if "error" in data:
        return []
    windows = []
    for w in data.get("windows", []):
        windows.append({**w, "source": "driver"})
    return windows


def list_windows(*, prefer: str = "quartz") -> dict:
    """Return {windows, method}. With default prefer=quartz, never blocks on driver."""
    quartz = list_windows_quartz()
    if quartz and prefer in ("quartz", "quartz-only"):
        return {"windows": quartz, "method": "quartz"}
    if prefer != "quartz-only":
        driver = list_windows_driver(timeout=6)
        if driver:
            return {"windows": driver, "method": "driver"}
    if quartz:
        return {"windows": quartz, "method": "quartz"}
    return {"windows": [], "method": "none"}
