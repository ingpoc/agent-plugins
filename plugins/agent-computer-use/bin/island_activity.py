#!/usr/bin/env python3
"""One shared "plugin activity" signal for the ACU Dynamic Island.

Any plugin an agent is using publishes a running row carrying its plugin mark
("acu", "comet-control", "context-ledger"); the island shows it as a circle on
the right of the compact pill, tied to the calling agent's name. The hub drops a
plugin row when its ttl_s lapses — it never goes stale — so the ttl *is* the
hold: ``call_started`` holds the row while a call runs and ``call_finished``
leaves it CLEAR_AFTER_S (2 s) after the last call, success or failure.

Agent names go through ``normalize_agent`` (the one shared normaliser), so
"grok-bot" and "Grok Bot" are one name across every plugin.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import stat
import time
from pathlib import Path
from typing import Any

PUBLISH_PATH = "/api/island/publish"
ISLAND_SOURCE = "agent"
ROW_ID_MAX_CHARS = 80
NAME_MAX_CHARS = 48
MIN_TTL = 2.0
CLEAR_AFTER_S = 2.0
IN_FLIGHT_S = 120.0
MAX_TTL = 3600.0
THROTTLE_SECONDS = 5.0
DISABLED_VALUES = frozenset({"0", "false", "off", "no"})

# plugin -> (island app name = icon, short label)
MARKS: dict[str, tuple[str, str]] = {
    "acu": ("Agent Computer Use", "Agent Computer Use"),
    "comet-control": ("comet-control", "Comet Control"),
    "context-ledger": ("context-ledger", "Context Ledger"),
    "developer-observability": ("developer-observability", "Developer Observability"),
}

# Row id -> (posted_at, agent) for this process only; keeps a chatty caller to
# one island post per throttle window.
_RECENT: dict[str, tuple[float, str]] = {}


def socket_path() -> Path:
    env = os.environ.get("ACU_ISLAND_SOCKET")
    if env:
        return Path(env).expanduser()
    return Path.home() / "Library/Application Support/CUAService/island.sock"


def enabled() -> bool:
    return os.environ.get("ACU_ISLAND_ACTIVITY", "").strip().lower() not in DISABLED_VALUES


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


_NAME_SPLIT = re.compile(r"[\s._-]+")


def normalize_agent(name: Any) -> str:
    """One display name per agent across plugins: "grok-bot", "grok_bot", "Grok Bot" -> "Grok Bot".

    Split on whitespace, "-", "_" and "."; an all-lowercase word gets a capital first letter, a word
    with any capital (or no letters) is kept; joined with single spaces. Blank -> "Agent".
    """
    if not isinstance(name, str):
        return "Agent"
    words = [w for w in _NAME_SPLIT.split(name.strip()) if w]
    shown = " ".join(w[:1].upper() + w[1:] if w.islower() else w for w in words)
    return shown[:NAME_MAX_CHARS].rstrip() or "Agent"


def _ttl(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = MIN_TTL
    return float(min(MAX_TTL, max(MIN_TTL, number)))


def row_id(plugin: str, key: str) -> str:
    direct = f"plugin:{plugin}:{key}"
    if len(direct) <= ROW_ID_MAX_CHARS:
        return direct
    digest = hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:24]
    return f"plugin:{plugin}:{digest}"


TARGET_APP_MAX_CHARS = 128


def row(plugin: str, agent: str, key: str, ttl_s: float, target_app: str | None = None) -> dict:
    """Running row body for one plugin use. ValueError for an unknown plugin.

    target_app: the Mac app an ACU call operated on (name or bundle id); the island draws its
    real icon inside the pill. Other plugins never send one.
    """
    mark = MARKS.get(plugin)
    if mark is None:
        raise ValueError(f"unknown plugin: {plugin}")
    name = normalize_agent(agent)
    return {
        "source": ISLAND_SOURCE,
        "id": row_id(plugin, key),
        "kind": "running",
        "name": name,
        "app": mark[0],
        "title": f"Using {mark[1]}",
        "meta": plugin,
        "plugin": plugin,
        "ttl_s": _ttl(ttl_s),
        **({"target_app": app} if (app := _text(target_app)[:TARGET_APP_MAX_CHARS]) and plugin == "acu" else {}),
    }


def clear_body(plugin: str, key: str) -> dict:
    return {"source": ISLAND_SOURCE, "id": row_id(plugin, key), "kind": "clear"}


def publish(body: dict, path: Path | None = None, timeout: float = 3.0) -> bool:
    """Post one row body to the CUAService island socket. Never raises."""
    if not enabled():
        return False
    try:
        target = Path(path) if path is not None else socket_path()
        info = os.lstat(target)
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            return False
        request = {"method": "POST", "path": PUBLISH_PATH, "query": {}, "body": body}
        buffer = b""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(target))
            client.sendall(json.dumps(request).encode("utf-8") + b"\n")
            while not buffer.endswith(b"\n"):
                chunk = client.recv(65536)
                if not chunk:
                    break
                buffer += chunk
        reply = json.loads(buffer or b"{}")
    except Exception:
        return False
    return isinstance(reply, dict) and reply.get("status") == 200


def start(plugin: str, agent: str, key: str, ttl_s: float = 600.0, target_app: str | None = None) -> bool:
    try:
        return publish(row(plugin, agent, key, ttl_s, target_app=target_app))
    except Exception:
        return False


def end(plugin: str, key: str) -> bool:
    try:
        rid = row_id(plugin, key)
    except Exception:
        return False
    try:
        return publish({"source": ISLAND_SOURCE, "id": rid, "kind": "clear"})
    except Exception:
        return False


def pulse(plugin: str, agent: str, key: str, hold_s: float = 10.0) -> bool:
    """Heartbeat one in-flight use: post at most once per throttle window, ttl = hold_s.

    The hub drops the row at ttl_s (plugin rows never go stale), so a caller that
    stops pulsing simply disappears after hold_s. True when the signal is live.
    """
    if plugin not in MARKS:
        return False
    try:
        rid = row_id(plugin, key)
        now = time.time()
        window = min(THROTTLE_SECONDS, hold_s / 2)
        recent = _RECENT.get(rid)
        if recent is not None and now - recent[0] < window and recent[1] == normalize_agent(agent):
            return True
        posted = start(plugin, agent, key, ttl_s=hold_s)
        if posted:
            _RECENT[rid] = (now, normalize_agent(agent))
        return posted
    except Exception:
        return False


def _post_now(plugin: str, agent: str, key: str, ttl_s: float, target_app: str | None = None) -> bool:
    if plugin not in MARKS:
        return False
    try:
        posted = start(plugin, agent, key, ttl_s=ttl_s, target_app=target_app)
        if posted:
            _RECENT[row_id(plugin, key)] = (time.time(), normalize_agent(agent))
        return posted
    except Exception:
        return False


def call_started(plugin: str, agent: str, key: str, in_flight_s: float = IN_FLIGHT_S,
                 target_app: str | None = None) -> bool:
    """Hold the row while one call runs (unthrottled). Never raises."""
    return _post_now(plugin, agent, key, in_flight_s, target_app=target_app)


def call_finished(plugin: str, agent: str, key: str, target_app: str | None = None) -> bool:
    """The call ended (ok or failed): the hub drops the row CLEAR_AFTER_S later. Never raises."""
    return _post_now(plugin, agent, key, CLEAR_AFTER_S, target_app=target_app)


def app_key(key: str, target_app: str | None) -> str:
    """One row per operated app, so each app clears 2 s after its own last call."""
    app = _text(target_app)[:TARGET_APP_MAX_CHARS]
    return f"{key}:app:{app}" if app else key


def linger(body: dict) -> dict:
    """Copy of a running row body that the hub drops CLEAR_AFTER_S from now."""
    out = dict(body)
    out["ttl_s"] = CLEAR_AFTER_S
    return out


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="plugin activity for the ACU island")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "end", "pulse", "started", "finished"):
        child = sub.add_parser(name)
        child.add_argument("--plugin", required=True)
        child.add_argument("--agent", default="")
        child.add_argument("--key", required=True)
        child.add_argument("--ttl", type=float, default=600.0)
        child.add_argument("--hold", type=float, default=10.0)
        child.add_argument("--target-app", default="")
    args = parser.parse_args(argv)

    if args.command == "start":
        result = start(args.plugin, args.agent, args.key, ttl_s=args.ttl)
    elif args.command == "end":
        result = end(args.plugin, args.key)
    elif args.command == "started":
        result = call_started(args.plugin, args.agent, args.key, target_app=args.target_app)
    elif args.command == "finished":
        result = call_finished(args.plugin, args.agent, args.key, target_app=args.target_app)
    else:
        result = pulse(args.plugin, args.agent, args.key, hold_s=args.hold)
    print(json.dumps({"ok": bool(result)}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
