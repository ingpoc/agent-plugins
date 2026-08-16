#!/usr/bin/env python3
"""Argument schema for the macos-cua CLI; execution stays in the owner runtime."""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys


AX_FOREGROUND_COMMANDS = frozenset()
OBSERVING_COMMANDS = frozenset({"state", "snap", "find", "list-buttons", "hold-key"})

# Invented names agents guess → exact advertised command (proven session friction).
COMMAND_REDIRECTS = {
    "right-click-point": (
        "click-point <app> <x> <y> --button right  "
        "(window-local) or click-desktop <x> <y> --button right (global Quartz)"
    ),
    "right_click_point": (
        "click-point <app> <x> <y> --button right  "
        "(window-local) or click-desktop <x> <y> --button right (global Quartz)"
    ),
    "click-point-right": (
        "click-point <app> <x> <y> --button right"
    ),
}


def filter_apps(payload, *, query: str | None = None, running: bool = False) -> dict:
    """Return a token-cheap slice of list_apps output without changing driver shape."""
    apps = list(payload.get("apps") or [])
    if running:
        apps = [app for app in apps if app.get("running")]
    if query:
        needle = query.strip().lower()
        if needle:
            matched = []
            for app in apps:
                haystack = " ".join(
                    str(app.get(key) or "")
                    for key in ("name", "bundle_id", "launch_path", "kind", "pid")
                ).lower()
                if needle in haystack:
                    matched.append(app)
            apps = matched
    result = {
        "apps": apps,
        "match_count": len(apps),
    }
    if query is not None:
        result["query"] = query
    if running:
        result["running_only"] = True
    return result


def suggest_command(invalid: str, choices: list[str]) -> str | None:
    """Return a redirect hint or nearest valid command for an invented name."""
    key = (invalid or "").strip().lower().replace("_", "-")
    if key in COMMAND_REDIRECTS:
        return COMMAND_REDIRECTS[key]
    # Also try raw key for underscore variants already normalized.
    if invalid in COMMAND_REDIRECTS:
        return COMMAND_REDIRECTS[invalid]
    matches = difflib.get_close_matches(invalid, choices, n=3, cutoff=0.6)
    if not matches:
        return None
    return "did you mean: " + ", ".join(matches)


class SuggestingArgumentParser(argparse.ArgumentParser):
    """Argparse that points agents at the nearest real command on typos."""

    def error(self, message: str) -> None:  # pragma: no cover - exercised via CLI
        hint = None
        marker = "invalid choice: '"
        if marker in message and "' (choose from" in message:
            invalid = message.split(marker, 1)[1].split("'", 1)[0]
            action = next(
                (
                    item
                    for item in self._actions
                    if isinstance(item, argparse._SubParsersAction)
                ),
                None,
            )
            choices = sorted(action.choices) if action else []
            hint = suggest_command(invalid, choices)
        sys.stderr.write(f"{self.prog}: error: {message}\n")
        if hint:
            sys.stderr.write(f"{self.prog}: hint: {hint}\n")
        self.exit(2)


def emit_json(
    result,
    *,
    accepted,
    require_ok: bool = False,
    require_accepted: bool = False,
) -> None:
    pretty = os.environ.get("MACOS_CUA_PRETTY") == "1"
    print(
        json.dumps(
            result,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
            default=str,
        )
    )
    failed = isinstance(result, dict) and (
        bool(result.get("error")) or (require_ok and result.get("ok") is not True)
    )
    if require_accepted and not accepted(result):
        failed = True
    if failed:
        raise SystemExit(1)


def build_parser(*, cua_session: str, key_codes) -> argparse.ArgumentParser:
    parser = SuggestingArgumentParser(
        prog="macos-cua",
        description="Reliable macOS Computer Use through cua-driver.",
    )
    parser.add_argument(
        "--browser-intent",
        choices=("native-app", "native-dialog", "chrome-admin"),
        default=os.environ.get("MACOS_CUA_BROWSER_INTENT", "native-app"),
        help="Explicit handoff intent when the target is Hermes-managed Chrome",
    )
    parser.add_argument(
        "--browser-session-id",
        default=os.environ.get("MACOS_CUA_BROWSER_SESSION_ID"),
        help="Owning Hermes session for native-dialog handoff",
    )
    parser.add_argument(
        "--browser-claim-token",
        default=os.environ.get("MACOS_CUA_BROWSER_CLAIM_TOKEN"),
        help="Short-lived claim issued by the token-private Hermes driver",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Driver health + permissions")
    sub.add_parser("reset", help="Clear the app-resolution cache")
    p = sub.add_parser("apps", help="List installed and running macOS apps")
    p.add_argument(
        "--query",
        help="Substring filter on name/bundle_id/launch_path/kind/pid (token-cheap)",
    )
    p.add_argument(
        "--running",
        action="store_true",
        help="Only apps with running=true",
    )
    sub.add_parser("displays", help="List monitors (name, frame, main)")

    p = sub.add_parser(
        "ensure-display", help="Move app window onto the configured/secondary display"
    )
    p.add_argument("app")
    p.add_argument("--display", default=os.environ.get("MACOS_CUA_DISPLAY"))
    p.add_argument("--width", type=int)
    p.add_argument("--height", type=int)
    p.add_argument("--margin", type=int, default=120)

    p = sub.add_parser("focus", help="Activate app + window")
    p.add_argument("app")

    p = sub.add_parser("snap", help="Snapshot app UI tree")
    p.add_argument("app")
    p.add_argument("--max", type=int, default=30, dest="max_elements")
    p.add_argument("--mode", default="som", choices=["som", "ax", "vision"])

    p = sub.add_parser("state", help="Get AX state plus a grounding screenshot as JSON")
    p.add_argument("app")
    p.add_argument("--max", type=int, default=120, dest="max_elements")
    p.add_argument("--query", help="Filter rendered AX text without changing indices")
    p.add_argument("--screenshot", help="PNG output path (defaults under the skill cache)")
    p.add_argument("--no-screenshot", action="store_true", help="Cheap AX-only observation")
    state_delivery = p.add_mutually_exclusive_group()
    state_delivery.add_argument(
        "--foreground",
        action="store_true",
        help="Explicitly front the app for capture; interrupts the current user flow",
    )
    state_delivery.add_argument(
        "--background",
        action="store_true",
        help="Deprecated compatibility flag; background capture is now the default",
    )
    p.add_argument(
        "--compact",
        action="store_true",
        help="Omit the structured element array; AX text retains fresh indices",
    )
    p.add_argument(
        "--diff",
        action="store_true",
        help="After a prior state for this app/query, emit only added/removed/changed AX lines",
    )

    p = sub.add_parser("click", help="Click element index")
    p.add_argument("app")
    p.add_argument("element", type=int)

    p = sub.add_parser("click-point", help="Click window-local screenshot coordinates")
    p.add_argument("app")
    p.add_argument("x", type=float)
    p.add_argument("y", type=float)
    p.add_argument("--button", choices=["left", "right", "middle"], default="left")
    p.add_argument("--count", type=int, default=1)
    p.add_argument("--foreground", action="store_true")
    p.add_argument(
        "--preserve-pointer",
        action="store_true",
        help="Restore the system pointer after the click (mitigation, not isolation)",
    )
    p.add_argument("--debug-image")
    p.add_argument(
        "--window-id",
        type=int,
        help="Target a proven same-process modal/window instead of the main window",
    )

    p = sub.add_parser(
        "click-desktop",
        help="Click global Quartz desktop coordinates (non-AX surfaces / widgets)",
    )
    p.add_argument("x", type=float)
    p.add_argument("y", type=float)
    p.add_argument("--button", choices=["left", "right", "middle"], default="left")
    p.add_argument("--count", type=int, default=1)
    p.add_argument(
        "--preserve-pointer",
        action="store_true",
        help="Restore the system pointer after the click (mitigation, not isolation)",
    )

    p = sub.add_parser("double-click", help="Double-click an element or window-local point")
    p.add_argument("app")
    p.add_argument("--element", type=int)
    p.add_argument("--x", type=float)
    p.add_argument("--y", type=float)
    p.add_argument(
        "--foreground",
        action="store_true",
        help="Briefly front the app when background double-click delivery does not land",
    )

    p = sub.add_parser("perform-action", help="Perform an AX action exposed by fresh state")
    p.add_argument("app")
    p.add_argument("action", help="e.g. open, show_menu, confirm, cancel, pick, press")
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--element", type=int)
    target.add_argument("--label")

    p = sub.add_parser("drag", help="Drag between window-local screenshot coordinates")
    p.add_argument("app")
    p.add_argument("from_x", type=float)
    p.add_argument("from_y", type=float)
    p.add_argument("to_x", type=float)
    p.add_argument("to_y", type=float)
    p.add_argument("--foreground", action="store_true")
    p.add_argument("--duration-ms", type=int, default=500)
    p.add_argument("--steps", type=int, default=20)

    p = sub.add_parser("type-text", help="Type into the focused UI or a window-local point")
    p.add_argument("app")
    p.add_argument("text")
    p.add_argument("--element", type=int)
    p.add_argument("--x", type=float)
    p.add_argument("--y", type=float)
    p.add_argument("--foreground", action="store_true")

    p = sub.add_parser("key", help="Press key/combo (e.g. Return, cmd+s)")
    p.add_argument("app")
    p.add_argument("keys")
    p.add_argument(
        "--foreground",
        action="store_true",
        help="Briefly front the app for native shortcuts that ignore background delivery",
    )
    p.add_argument(
        "--system-events",
        action="store_true",
        help="Exact-PID native modal fallback; requires fresh state to prove effect",
    )

    p = sub.add_parser("hold-key", help="Hold a movement/navigation key for a duration")
    p.add_argument("app")
    p.add_argument("key", choices=sorted(key_codes))
    p.add_argument("--duration", type=float, default=0.5)
    p.add_argument(
        "--foreground",
        action="store_true",
        help="Front the app and deliver through the system input tap for raw-input surfaces",
    )

    p = sub.add_parser("scroll", help="Scroll up/down")
    p.add_argument("app")
    p.add_argument("direction", choices=["up", "down", "left", "right"])
    p.add_argument("--amount", type=int, default=3)
    p.add_argument("--by", choices=["line", "page"], default="line")
    p.add_argument("--element", type=int)
    p.add_argument("--x", type=float)
    p.add_argument("--y", type=float)
    p.add_argument(
        "--foreground",
        action="store_true",
        help="Briefly front the app when background scroll delivery does not land",
    )

    p = sub.add_parser("right-click", help="Right-click an element index")
    p.add_argument("app")
    p.add_argument("element", type=int)

    p = sub.add_parser("set-value", help="Set a value on an AX element")
    p.add_argument("app")
    p.add_argument("element", type=int)
    p.add_argument("value")

    p = sub.add_parser("select-text", help="Select matching text in an editable AX element")
    p.add_argument("app")
    p.add_argument("text")
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--element", type=int)
    target.add_argument("--label")
    p.add_argument("--prefix")
    p.add_argument("--suffix")
    p.add_argument(
        "--selection-type",
        choices=["text", "cursor_before", "cursor_after"],
        default="text",
    )

    p = sub.add_parser("find", help="Find element index by label substring")
    p.add_argument("app")
    p.add_argument("text")
    p.add_argument("--max", type=int, default=50, dest="max_elements")

    p = sub.add_parser("click-label", help="Click by accessibility label substring")
    p.add_argument("app")
    p.add_argument("label")
    p.add_argument("--max", type=int, default=50, dest="max_elements")

    p = sub.add_parser(
        "click-label-pointer",
        help="Agent cursor glide + driver click (does not move user mouse)",
    )
    p.add_argument("app")
    p.add_argument("label")
    p.add_argument("--max", type=int, default=50, dest="max_elements")
    p.add_argument("--frame", help="(legacy) ignored — use ensure-display; moves to MACOS_CUA_DISPLAY")

    p = sub.add_parser("type-label", help="Fill a text field by label")
    p.add_argument("app")
    p.add_argument("label")
    p.add_argument("text")
    p.add_argument("--max", type=int, default=50, dest="max_elements")

    p = sub.add_parser("list-buttons", help="Print interactive controls (JSON lines)")
    p.add_argument("app")
    p.add_argument("--max", type=int, default=120, dest="max_elements")

    p = sub.add_parser("run", help="Run batched actions (@file.json or JSON string)")
    p.add_argument("app")
    p.add_argument("json")

    p = sub.add_parser("cursor", help="Cursor overlay control")
    p.add_argument("action", choices=["status", "show", "hide", "move", "configure"])
    p.add_argument("--icon", help="Custom cursor image path (SVG/PNG)")
    p.add_argument("--x", type=float)
    p.add_argument("--y", type=float)
    p.add_argument("--session", default=cua_session)

    p = sub.add_parser("operator", help="Native menu-bar and Picture-in-Picture UI")
    p.add_argument(
        "action",
        choices=[
            "start",
            "status",
            "show-pip",
            "hide-pip",
            "stop",
            "install-service",
            "uninstall-service",
            "signing-status",
        ],
    )
    return parser
