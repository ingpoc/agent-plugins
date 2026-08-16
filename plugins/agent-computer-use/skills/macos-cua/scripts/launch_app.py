#!/usr/bin/env python3
"""
launch_app: robust macOS app launcher for GUI testing.

Solves the common pain point where `open -n <app>` launches a process but
the SwiftUI/AppKit window never comes on-screen (so get_window_state only
sees the menu bar). This script:

  - resolves an app bundle path from a display name OR bundle id
  - launches it (optionally with -n for a fresh instance, optionally with
    `--args` to pass launch arguments for deep-link / validation testing)
  - waits until the main window is actually on screen
  - optionally moves/resizes the window to a fixed validation rect
  - clears the macos-cua cache for that app name so the next snap resolves
    the freshly-launched pid

Usage:
    python3 launch_app.py --app Calculator              # by name
    python3 launch_app.py --bundle com.apple.TextEdit   # by bundle id
    python3 launch_app.py --app Preview --fresh --args /tmp/proof.png
    python3 launch_app.py --app TextEdit --frame 80,80,1200,760
"""

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time

CACHE_DIR = os.environ.get(
    "MACOS_CUA_CACHE_DIR", os.path.expanduser("~/.cache/macos-cua")
)


def run(cmd, timeout=20):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timeout after " + str(timeout) + "s"


def find_bundle(app=None, bundle_id=None):
    """Resolve bundle path via mdfind."""
    query = f"kMDItemCFBundleIdentifier == '{bundle_id}'" if bundle_id else (
        f"kMDItemFSName == '{app}.app'c"
    )
    code, out, _ = run(["mdfind", "-onlyin", "/Applications", query], timeout=15)
    if not out:
        # Fallback: search broader
        code, out, _ = run(["mdfind", query], timeout=15)
    for line in out.splitlines():
        if line.endswith(".app") and os.path.exists(line):
            return line
    # Last-resort common paths
    candidates = []
    if app:
        candidates += [
            f"/Applications/{app}.app",
            os.path.expanduser(f"~/Applications/{app}.app"),
        ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def list_windows():
    wr_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "window_resolve.py")
    spec = importlib.util.spec_from_file_location("window_resolve", wr_path)
    wr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wr)
    data = wr.list_windows(prefer="quartz")
    return {"windows": data["windows"]}


def find_main_window(owner_names, min_width=400, title_hint=None):
    """Pick the largest on-screen window whose owner matches."""
    data = list_windows()
    candidates = []
    for w in data.get("windows", []):
        owner = (w.get("app_name") or w.get("owner") or "").lower()
        title = (w.get("title") or "").lower()
        names = [n.lower() for n in owner_names]
        owner_hit = any(n in owner or owner in n for n in names)
        title_hit = title_hint and title_hint.lower() in title
        if not owner_hit and not title_hit:
            continue
        bounds = w.get("bounds", {}) or {}
        if bounds.get("width", 0) < min_width:
            continue
        candidates.append(w)
    candidates.sort(
        key=lambda w: w.get("bounds", {}).get("width", 0)
        * w.get("bounds", {}).get("height", 0),
        reverse=True,
    )
    return candidates[0] if candidates else None


def activate(bundle_id):
    """Force-activate via NSWorkspace (works after `open -n`)."""
    script = (
        "from AppKit import NSWorkspace\n"
        f"apps = list(NSWorkspace.sharedWorkspace()"
        f".runningApplicationsWithBundleIdentifier_('{bundle_id}'))\n"
        "for a in apps:\n"
        "    a.activateWithOptions_(2)\n"  # ActivateIgnoringOtherApps
        "print(len(apps))\n"
    )
    run(["python3", "-c", script], timeout=10)


def set_window_frame(owner_names, x, y, w, h, attempts=10):
    """Move + resize the main window via AppleScript."""
    for _ in range(attempts):
        script = (
            'tell application "System Events"\n'
            f"  if not (exists process \"{owner_names[0]}\") then return \"NO_PROC\"\n"
            f"  tell process \"{owner_names[0]}\"\n"
            "    if (count of windows) is 0 then return \"NO_WIN\"\n"
            f"    set position of window 1 to {{{x}, {y}}}\n"
            f"    set size of window 1 to {{{w}, {h}}}\n"
            "    return \"OK\"\n"
            "  end tell\n"
            "end tell\n"
        )
        code, out, _ = run(["osascript", "-e", script], timeout=8)
        if "OK" in out:
            return True
        time.sleep(0.5)
    return False


def clear_cache(names):
    os.makedirs(CACHE_DIR, exist_ok=True)
    for f in os.listdir(CACHE_DIR):
        if f.endswith(".json"):
            os.unlink(os.path.join(CACHE_DIR, f))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", help="display name (without .app)")
    ap.add_argument("--bundle", help="bundle id")
    ap.add_argument("--fresh", action="store_true",
                    help="kill existing instances first")
    ap.add_argument("--args", nargs=argparse.REMAINDER, default=[],
                    help="launch args passed after --args")
    ap.add_argument("--frame",
                    help="window rect x,y,w,h (default 80,80,1200,760)")
    ap.add_argument("--wait", type=float, default=8.0,
                    help="max seconds to wait for window on screen")
    args = ap.parse_args()

    if not args.app and not args.bundle:
        print(json.dumps({"error": "need --app or --bundle"}))
        sys.exit(1)

    bundle_path = find_bundle(args.app, args.bundle)
    if not bundle_path:
        print(json.dumps({"error": f"app not found: {args.app or args.bundle}"}))
        sys.exit(1)

    # Resolve common display/process-name variants for window searching.
    names = []
    if args.app:
        names.append(args.app)
    # The process name sometimes strips a trailing "Mac".
    if args.app and args.app.endswith("Mac"):
        names.append(args.app[:-3])
    if args.bundle:
        names.append(args.bundle.split(".")[-1])

    if args.fresh:
        run(["pkill", "-9", "-x", args.app or names[0]], timeout=5)
        run(["pkill", "-9", "-f", bundle_path], timeout=5)
        time.sleep(1.5)

    # Launch
    cmd = ["open"]
    if args.fresh:
        cmd += ["-F", "-n"]
    cmd += [bundle_path]
    if args.args:
        # argparse REMAINDER includes the literal "--args"; drop it if present.
        passed = [a for a in args.args if a != "--args"]
        if passed:
            cmd += ["--args"] + passed
    code, _, err = run(cmd, timeout=15)
    if code != 0:
        print(json.dumps({"error": f"open failed: {err}"}))
        sys.exit(1)

    # Wait for window
    deadline = time.time() + args.wait
    win = None
    while time.time() < deadline:
        win = find_main_window(names)
        if win:
            break
        time.sleep(0.4)

    if not win:
        print(json.dumps({"error": "no main window appeared",
                          "owner_names_tried": names}))
        sys.exit(1)

    # Activate + frame
    if args.bundle:
        activate(args.bundle)
    if args.frame:
        x, y, w, h = [int(v) for v in args.frame.split(",")]
    else:
        x, y, w, h = 80, 80, 1200, 760
    set_window_frame(names, x, y, w, h)
    clear_cache(names)

    print(json.dumps({
        "ok": True,
        "bundle_path": bundle_path,
        "pid": win.get("pid"),
        "window_id": win.get("window_id"),
        "owner": win.get("app_name"),
        "frame": [x, y, w, h],
    }))


if __name__ == "__main__":
    main()
