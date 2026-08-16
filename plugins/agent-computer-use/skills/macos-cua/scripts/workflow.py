#!/usr/bin/env python3
"""macos-cua workflow entry point — preflight → smoke → closeout.

Usage:
  python3 workflow.py preflight
  python3 workflow.py smoke [--app Calculator]
  python3 workflow.py cursor-demo [--app Calculator]
  python3 workflow.py closeout
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ACTIONS = os.path.join(HERE, "macos-cua.py")
CUA_DRIVER = os.environ.get("CUA_DRIVER", os.path.expanduser("~/.local/bin/cua-driver"))


def load_window_resolve():
    spec = importlib.util.spec_from_file_location("window_resolve", os.path.join(HERE, "window_resolve.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_macos_cua():
    spec = importlib.util.spec_from_file_location("macos_cua", ACTIONS)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_proof_cache():
    spec = importlib.util.spec_from_file_location(
        "proof_cache", os.path.join(HERE, "proof_cache.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(cmd: list[str], timeout: int = 20) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"


def permissions_ready(perms: object) -> bool:
    """Trust daemon-owned grants; fail only when capture is known unavailable."""
    return bool(
        isinstance(perms, dict)
        and perms.get("accessibility") is True
        and perms.get("screen_recording") is True
        and perms.get("screen_recording_capturable") is not False
    )


def _print_result(result: dict, *, verbose: bool = False) -> None:
    print(
        json.dumps(
            result,
            indent=2 if verbose else None,
            separators=None if verbose else (",", ":"),
        )
    )


def cmd_preflight(*, quiet: bool = False, verbose: bool = False) -> int:
    started = time.monotonic()
    wr = load_window_resolve()
    cua = load_macos_cua()
    out: dict = {"checks": [], "ready": False}

    def add(label: str, ok: bool, detail: str = ""):
        out["checks"].append({"label": label, "ok": ok, "detail": detail})
        return ok

    binary = os.path.exists(CUA_DRIVER) and os.access(CUA_DRIVER, os.X_OK)
    add("cua-driver binary", binary, CUA_DRIVER)
    version = cua.driver_version() if binary else {"ok": False, "error": "binary missing"}
    add(
        "cua-driver supported version",
        bool(version.get("ok")),
        str(version),
    )

    healthy = wr.daemon_running()
    if not healthy:
        wr.restart_daemon()
        healthy = wr.daemon_running()
    add("daemon reachable", healthy)

    perms = wr.call_driver(
        "check_permissions",
        {"prompt": False},
        timeout=float(os.environ.get("MACOS_CUA_PERMISSION_TIMEOUT", "12")),
    )
    perms_ok = permissions_ready(perms)
    if not perms_ok:
        add(
            "permissions (AX + Screen grant)",
            False,
            str(perms)[:240]
            + " (run `cua-driver permissions grant`, then re-run preflight)",
        )
    else:
        add("permissions (AX + Screen grant)", True, str(perms)[:240])

    plist = os.path.expanduser("~/Library/LaunchAgents/com.trycua.driver.plist")
    add("LaunchAgent installed", os.path.exists(plist), plist)

    listed = wr.list_windows(prefer="quartz")
    win_count = len(listed["windows"])
    add("window enumeration", win_count > 0, f"{win_count} via {listed['method']}")

    controller = cua._operator_ui()
    service = controller._service_status()
    operator = (
        controller.install_service()
        if not service.get("installed")
        and os.environ.get("MACOS_CUA_INSTALL_SERVICE", "1") != "0"
        else controller.ensure()
    )
    service = controller._service_status()
    signing = controller.signing_status()
    add("operator menu + PiP", bool(operator.get("ok")), str(operator)[:180])
    add("operator launchd lifecycle", bool(service.get("running")), str(service)[:180])
    add(
        "operator signed app bundle",
        bool(signing.get("ok") and signing.get("mode") == "identity"),
        str(signing)[:180],
    )

    packet = cua._displays().display_packet()
    add("pinned display active", not packet.get("pin_error"), packet.get("pin_error") or "")
    out["ready"] = all(check["ok"] for check in out["checks"])
    out["method"] = listed.get("method")
    out["duration_ms"] = round((time.monotonic() - started) * 1000)
    out["display"] = {
        "active": packet.get("display_count_active"),
        "configured": packet.get("display_count_configured"),
    }
    if not quiet:
        if verbose or not out["ready"]:
            _print_result(out, verbose=True)
        else:
            _print_result(
                {
                    "ready": True,
                    "checks_passed": len(out["checks"]),
                    "method": out["method"],
                    "duration_ms": out["duration_ms"],
                    "display": out["display"],
                }
            )
    return 0 if out["ready"] else 1


def cmd_smoke(app: str) -> int:
    if cmd_preflight(quiet=True) != 0:
        print(json.dumps({"success": False, "error": "preflight failed"}, indent=2))
        return 1
    cua = load_macos_cua()
    cua.launch_or_activate(app)
    time.sleep(0.8)
    pid, wid, name, err = cua.resolve_app(app, launch_if_missing=False)
    if err:
        pid, wid, name, err = cua.resolve_app(app)
    if err:
        print(json.dumps({"success": False, "error": err}))
        return 1
    snap = cua.snapshot(pid, wid, max_elements=25, mode="ax", retries=3)
    tree = snap.get("tree_markdown", "") if isinstance(snap, dict) else ""
    count = snap.get("element_count", 0) if isinstance(snap, dict) else 0
    ok = len(tree) > 80 or count > 0
    out = {
        "success": ok,
        "app": app,
        "pid": pid,
        "window_id": wid,
        "name": name,
        "element_count": count,
        "tree_chars": len(tree),
        "method": "quartz-resolve",
    }
    if "error" in snap:
        out["snap_error"] = snap["error"]
    print(json.dumps(out, indent=2))
    return 0 if ok else 1


def _calc_display(snap: dict) -> str:
    tree = snap.get("tree_markdown", "") if isinstance(snap, dict) else ""
    m = re.search(r'AXStaticText\s*=\s*"([^"]*)"', tree)
    return (m.group(1) if m else "").strip()


def cmd_cursor_demo(app: str) -> int:
    """Visible agent cursor + label clicks; Calculator shows 11 after two 1 taps."""
    if app.lower() != "calculator":
        print(json.dumps({"success": False, "error": "cursor-demo only supports Calculator"}))
        return 2
    if cmd_preflight(quiet=True) != 0:
        print(json.dumps({"success": False, "error": "preflight failed"}, indent=2))
        return 1

    cua = load_macos_cua()
    cua.launch_or_activate(app)
    time.sleep(0.4)
    cua.clear_resolution_cache()
    pid, wid, name, err = cua.resolve_app(app)
    if err:
        print(json.dumps({"success": False, "error": err}, indent=2))
        return 1

    steps = []
    for label in ("Clear", "1", "1"):
        res = cua.click_label_pointer(
            pid, wid, label, max_elements=40, app_name=name or app
        )
        steps.append({
            "label": label,
            "ok": res.get("ok"),
            "coords": res.get("coords"),
            "method": res.get("method"),
        })
        time.sleep(0.35)

    snap = cua.snapshot(pid, wid, max_elements=40, mode="ax", retries=2)
    display = _calc_display(snap)
    steps_ok = all(step.get("ok") for step in steps)
    ok = (display == "11" or display.endswith("11")) and steps_ok
    out = {
        "success": ok,
        "app": app,
        "display": display,
        "steps": steps,
        "method": "signed-operator-glide+ax-click",
    }
    print(json.dumps(out, indent=2))
    return 0 if ok else 1


def cmd_closeout(*, verbose: bool = False) -> int:
    code, stdout, _ = run([sys.executable, ACTIONS, "reset"], timeout=10)
    out = {"success": code == 0, "cleared_cache": code == 0}
    if stdout:
        try:
            out["reset"] = json.loads(stdout)
        except json.JSONDecodeError:
            out["reset_raw"] = stdout[:200]
    wr = load_window_resolve()
    cua = load_macos_cua()
    # Wipe every cua-driver agent cursor (cyan auto-* + named). The signed
    # Hermes operator overlay is the only pointer that should remain.
    cleanup = cua._cleanup_driver_cursors(include_named=True)
    ended = list(cleanup.get("ended") or [])
    out["operator"] = cua.operator_update(
        status="idle",
        active=False,
        message="No controlled app",
    )
    out["ended_cursor_sessions"] = ended
    proof_cache = load_proof_cache()
    out["proof_cache"] = proof_cache.prune(
        cua.CACHE_DIR,
        max_bytes=int(
            os.environ.get("MACOS_CUA_PROOF_MAX_BYTES", str(256 * 1024 * 1024))
        ),
        max_age_seconds=int(
            os.environ.get("MACOS_CUA_PROOF_MAX_AGE_SECONDS", str(30 * 24 * 60 * 60))
        ),
    )
    out["daemon_ready"] = wr.daemon_running()
    if verbose or not (out["success"] and out["daemon_ready"]):
        _print_result(out, verbose=True)
    else:
        _print_result(
            {
                "success": True,
                "cleared_cache": out["cleared_cache"],
                "ended_cursor_sessions": len(ended),
                "proof_cache_removed": out["proof_cache"]["removed"],
                "daemon_ready": True,
            }
        )
    return 0 if out["success"] and out["daemon_ready"] else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["preflight", "smoke", "cursor-demo", "closeout"])
    ap.add_argument("--app")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    if args.command == "preflight":
        if args.app:
            ap.error("--app is only valid for smoke and cursor-demo")
        return cmd_preflight(verbose=args.verbose)
    if args.command == "smoke":
        return cmd_smoke(args.app or "Calculator")
    if args.command == "cursor-demo":
        return cmd_cursor_demo(args.app or "Calculator")
    return cmd_closeout(verbose=args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
