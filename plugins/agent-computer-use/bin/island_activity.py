#!/usr/bin/env python3
"""One shared "plugin activity" signal for the ACU Dynamic Island.

Any plugin an agent is using publishes a running row (the plugin's island icon
plus the calling agent's name) and clears it when the use ends. The hub keeps a
running row whose ttl lapses as "updates stopped", so every activity is ended by
an explicit clear; ttl_s is only the fallback. Hold files let a reaper clear
rows that a killed harness never ended.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PUBLISH_PATH = "/api/island/publish"
ISLAND_SOURCE = "agent"
ROW_ID_MAX_CHARS = 80
NAME_MAX_CHARS = 48
MIN_TTL = 5.0
MAX_TTL = 3600.0
THROTTLE_SECONDS = 5.0
HOLD_GRACE_SECONDS = 60.0
DISABLED_VALUES = frozenset({"0", "false", "off", "no"})

# plugin -> (island app name = icon, short label)
MARKS: dict[str, tuple[str, str]] = {
    "acu": ("Agent Computer Use", "ACU"),
    "comet-control": ("Comet", "Comet"),
    "context-ledger": ("Context Ledger", "Context Ledger"),
}

# Row id -> (posted_at, agent) for this process only; keeps a chatty caller to
# one island post per five seconds.
_RECENT: dict[str, tuple[float, str]] = {}


def socket_path() -> Path:
    env = os.environ.get("ACU_ISLAND_SOCKET")
    if env:
        return Path(env).expanduser()
    return Path.home() / "Library/Application Support/CUAService/island.sock"


def state_dir() -> Path:
    env = os.environ.get("ACU_ISLAND_ACTIVITY_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".cache/acu-island-activity"


def enabled() -> bool:
    return os.environ.get("ACU_ISLAND_ACTIVITY", "").strip().lower() not in DISABLED_VALUES


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


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


def row(plugin: str, agent: str, key: str, ttl_s: float) -> dict:
    """Running row body for one plugin use. ValueError for an unknown plugin."""
    mark = MARKS.get(plugin)
    if mark is None:
        raise ValueError(f"unknown plugin: {plugin}")
    name = (_text(agent) or "Agent")[:NAME_MAX_CHARS]
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
    }


def clear_body(plugin: str, key: str) -> dict:
    return {"source": ISLAND_SOURCE, "id": row_id(plugin, key), "kind": "clear"}


def _test_runner_without_fixture() -> bool:
    """A unit-test process that pointed nowhere must not touch the live island."""
    if os.environ.get("ACU_ISLAND_SOCKET"):
        return False
    entry = os.path.basename(sys.argv[0] or "")
    return entry == "run_strict.py" or (entry == "__main__.py" and "unittest" in sys.modules)


def publish(body: dict, path: Path | None = None, timeout: float = 3.0) -> bool:
    """Post one row body to the CUAService island socket. Never raises."""
    if not enabled() or _test_runner_without_fixture():
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


def start(plugin: str, agent: str, key: str, ttl_s: float = 600.0) -> bool:
    try:
        return publish(row(plugin, agent, key, ttl_s))
    except Exception:
        return False


def _hold_path(rid: str) -> Path:
    return state_dir() / (hashlib.sha256(rid.encode("utf-8")).hexdigest()[:32] + ".json")


def _write_hold(rid: str, until: float) -> None:
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    target = _hold_path(rid)
    tmp = directory / (target.name + f".{os.getpid()}.tmp")
    payload = json.dumps({"id": rid, "until": until}, sort_keys=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, target)


def _delete_hold(rid: str) -> None:
    try:
        _hold_path(rid).unlink()
    except OSError:
        pass


def end(plugin: str, key: str) -> bool:
    try:
        rid = row_id(plugin, key)
    except Exception:
        return False
    try:
        ok = publish({"source": ISLAND_SOURCE, "id": rid, "kind": "clear"})
    except Exception:
        ok = False
    _delete_hold(rid)
    return ok


def pulse(plugin: str, agent: str, key: str, hold_s: float = 10.0) -> bool:
    """Heartbeat one in-flight use: post at most every 5 s, always hold the row."""
    if plugin not in MARKS:
        return False
    try:
        rid = row_id(plugin, key)
        now = time.time()
        recent = _RECENT.get(rid)
        if recent is not None and now - recent[0] < THROTTLE_SECONDS and recent[1] == agent:
            posted = True
        else:
            posted = start(plugin, agent, key, ttl_s=hold_s + 20)
            if posted:
                _RECENT[rid] = (now, agent)
        _write_hold(rid, now + hold_s)
        ensure_reaper()
        return posted
    except Exception:
        return False


def _acquire_lock(path: Path) -> int | None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _release_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def ensure_reaper() -> None:
    """Spawn one detached reaper when no other process owns the lock."""
    try:
        lock = state_dir() / "reaper.lock"
        fd = _acquire_lock(lock)
        if fd is None:
            return
        _release_lock(fd)
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "reap"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except Exception:
        return


def _hold_files() -> list[Path]:
    try:
        return sorted(state_dir().glob("*.json"))
    except OSError:
        return []


def _read_hold(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def reap(poll: float = 1.0, idle_exit: float = 30.0) -> int:
    """Clear hold files whose use ended; exit after the list stays empty."""
    lock = state_dir() / "reaper.lock"
    fd = _acquire_lock(lock)
    if fd is None:
        return 0
    try:
        idle_since: float | None = None
        while True:
            now = time.time()
            holds = _hold_files()
            if not holds:
                if idle_since is None:
                    idle_since = now
                if now - idle_since >= idle_exit:
                    _release_lock(fd)
                    fd = None
                    if not _hold_files():
                        return 0
                    fd = _acquire_lock(lock)
                    if fd is None:
                        return 0
                    idle_since = None
            else:
                idle_since = None
                for path in holds:
                    record = _read_hold(path)
                    until = record.get("until") if record is not None else None
                    if record is None or not isinstance(until, (int, float)) or isinstance(until, bool):
                        try:
                            path.unlink()
                        except OSError:
                            pass
                        continue
                    if float(until) > now:
                        continue
                    rid = record.get("id")
                    cleared = False
                    if isinstance(rid, str) and rid:
                        cleared = publish({"source": ISLAND_SOURCE, "id": rid, "kind": "clear"})
                    if cleared or now > float(until) + HOLD_GRACE_SECONDS:
                        try:
                            path.unlink()
                        except OSError:
                            pass
            time.sleep(poll)
    finally:
        if fd is not None:
            _release_lock(fd)


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="plugin activity for the ACU island")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "end", "pulse"):
        child = sub.add_parser(name)
        child.add_argument("--plugin", required=True)
        child.add_argument("--agent", default="")
        child.add_argument("--key", required=True)
        child.add_argument("--ttl", type=float, default=600.0)
        child.add_argument("--hold", type=float, default=10.0)
    sub.add_parser("reap")
    args = parser.parse_args(argv)

    if args.command == "start":
        result = start(args.plugin, args.agent, args.key, ttl_s=args.ttl)
    elif args.command == "end":
        result = end(args.plugin, args.key)
    elif args.command == "pulse":
        result = pulse(args.plugin, args.agent, args.key, hold_s=args.hold)
    else:
        reap()
        result = True
    print(json.dumps({"ok": bool(result)}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
