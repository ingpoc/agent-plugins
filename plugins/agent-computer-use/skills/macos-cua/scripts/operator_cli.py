# Operator functions are injected by operator_ui.py.
# ruff: noqa: F821
"""Command-line parser for the native macos-cua operator service."""
from __future__ import annotations

import argparse
import json

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build")
    sub.add_parser("ensure")
    sub.add_parser("status")
    sub.add_parser("show-pip")
    sub.add_parser("hide-pip")
    sub.add_parser("stop")
    sub.add_parser("install-service")
    sub.add_parser("uninstall-service")
    sub.add_parser("signing-status")
    update_parser = sub.add_parser("update")
    update_parser.add_argument("--app")
    update_parser.add_argument("--pid", type=int)
    update_parser.add_argument("--window-id", type=int)
    update_parser.add_argument("--window-title")
    update_parser.add_argument("--screenshot", dest="screenshot_path")
    update_parser.add_argument("--raw-screenshot", dest="raw_screenshot_path")
    update_parser.add_argument("--cursor-x", type=float)
    update_parser.add_argument("--cursor-y", type=float)
    update_parser.add_argument("--cursor-image", dest="cursor_image_path")
    update_parser.add_argument("--cursor-visible", action="store_true", default=None)
    update_parser.add_argument("--status", default="active")
    update_parser.add_argument("--message")
    update_parser.add_argument("--harness")
    update_parser.add_argument("--session-id")
    update_parser.add_argument("--inactive", action="store_true")
    args = parser.parse_args()

    if args.command == "build":
        result = build(force=True)
    elif args.command == "ensure":
        result = ensure()
    elif args.command == "status":
        result = status()
    elif args.command == "show-pip":
        result = set_pip_visible(True)
    elif args.command == "hide-pip":
        result = set_pip_visible(False)
    elif args.command == "stop":
        result = stop()
    elif args.command == "install-service":
        result = install_service()
    elif args.command == "uninstall-service":
        result = uninstall_service()
    elif args.command == "signing-status":
        result = signing_status()
    else:
        result = update(
            active=not args.inactive,
            app=args.app,
            pid=args.pid,
            window_id=args.window_id,
            window_title=args.window_title,
            screenshot_path=args.screenshot_path,
            raw_screenshot_path=args.raw_screenshot_path,
            cursor_x=args.cursor_x,
            cursor_y=args.cursor_y,
            cursor_image_path=args.cursor_image_path,
            cursor_visible=args.cursor_visible,
            status=args.status,
            message=args.message,
            harness=args.harness,
            session_id=args.session_id,
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1
