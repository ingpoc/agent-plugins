"""Mirror active Comet Control leases onto the ACU Dynamic Island.

The broker owns the session inventory; the ACU Dynamic Island owns the row
surface. This module polls the broker socket for the live lease list, publishes
one running row per lease (Comet icon plus the agent label), and clears rows the
broker no longer reports. A ``lease_token`` is never stored, posted, or printed.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import signal
import socket
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Callable

ISLAND_SOURCE = "agent"
ROW_PREFIX = "plugin:comet-control:"
OLD_ROW_PREFIX = "comet-lease:"
ISLAND_APP = "Comet"
ROW_TITLE = "Using Comet"
ROW_META = "comet-control"
PLUGIN_NAME = "comet-control"
POLL_SECONDS = 5.0
HEARTBEAT_SECONDS = 20.0
TTL_GRACE_SECONDS = 15.0
MIN_TTL = 30.0
MAX_TTL = 3600.0
MISS_LIMIT = 3

ROW_ID_MAX_CHARS = 80
LABEL_MAX_CHARS = 48
SESSION_TIMEOUT_SECONDS = 5
PUBLISH_PATH = "/api/island/publish"
DISABLED_VALUES = frozenset({"0", "false", "off", "no"})


def island_socket_path() -> Path:
    env = os.environ.get("COMET_CONTROL_ISLAND_SOCKET")
    if env:
        return Path(env).expanduser()
    return Path.home() / "Library/Application Support/CUAService/island.sock"


def enabled() -> bool:
    return os.environ.get("COMET_CONTROL_ISLAND", "").strip().lower() not in DISABLED_VALUES


_ACTIVITY_UNSET = object()
_ACTIVITY: object = _ACTIVITY_UNSET


def activity_module():
    """ACU's shared plugin-activity module; None when it is not installed."""
    global _ACTIVITY
    if _ACTIVITY is not _ACTIVITY_UNSET:
        return _ACTIVITY
    env = os.environ.get("COMET_CONTROL_ISLAND_ACTIVITY")
    path = (
        Path(env).expanduser()
        if env
        else Path.home() / ".agents/plugins/agent-computer-use/bin/island_activity.py"
    )
    try:
        spec = importlib.util.spec_from_file_location("comet_control_island_activity", path)
        if spec is None or spec.loader is None:
            raise RuntimeError("no loader")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _ACTIVITY = module
    except Exception as exc:
        print(f"island activity unavailable: {type(exc).__name__}", file=sys.stderr)
        _ACTIVITY = None
    return _ACTIVITY


def row_id(session_id: str) -> str:
    direct = ROW_PREFIX + session_id
    if len(direct) <= ROW_ID_MAX_CHARS:
        return direct
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]
    return ROW_PREFIX + digest


def _label(lease: dict) -> str:
    for key in ("agent_label", "agent_id"):
        value = lease.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:LABEL_MAX_CHARS]
    return "Agent"


def _ttl_seconds(lease: dict, now_ms: int) -> float:
    expires_at = lease.get("expires_at")
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        return round(MIN_TTL, 1)
    ttl = (float(expires_at) - now_ms) / 1000.0 + TTL_GRACE_SECONDS
    return round(min(MAX_TTL, max(MIN_TTL, ttl)), 1)


def row_for_lease(lease: dict, now_ms: int, activity=None) -> dict | None:
    """Running row body for one lease, or None when it has no usable identity."""
    if not isinstance(lease, dict):
        return None
    session_id = lease.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    ttl = _ttl_seconds(lease, now_ms)
    if activity is not None:
        return activity.row(PLUGIN_NAME, _label(lease), session_id, ttl)
    return {
        "source": ISLAND_SOURCE,
        "id": row_id(session_id),
        "kind": "running",
        "name": _label(lease),
        "app": ISLAND_APP,
        "title": ROW_TITLE,
        "meta": ROW_META,
        "plugin": PLUGIN_NAME,
        "ttl_s": ttl,
    }


def _active_rows(leases: list[dict], now_ms: int, activity=None) -> dict[str, dict]:
    active: dict[str, dict] = {}
    for lease in leases:
        body = row_for_lease(lease, now_ms, activity)
        if body is not None:
            active[body["id"]] = body
    return active


def plan(
    leases: list[dict],
    posted: dict[str, float],
    now_ms: int,
    now_s: float,
    activity=None,
) -> tuple[list[dict], list[str]]:
    """Split one poll into row posts (new or heartbeat-due) and stale ids to clear."""
    active = _active_rows(leases, now_ms, activity)
    posts = [
        body
        for rid, body in active.items()
        if rid not in posted or now_s - posted[rid] >= HEARTBEAT_SECONDS
    ]
    clears = sorted(rid for rid in posted if rid not in active)
    return posts, clears


def _visible(body: dict | None) -> dict | None:
    """Row fields the island shows; ttl_s moves every poll and only rides the heartbeat."""
    return None if body is None else {k: v for k, v in body.items() if k != "ttl_s"}


def _clear_body(rid: str) -> dict:
    return {"source": ISLAND_SOURCE, "id": rid, "kind": "clear"}


class IslandMirror:
    """Polls the broker lease list and keeps one island row per active lease."""

    def __init__(
        self,
        fetch_sessions: Callable[[], list[dict]],
        publish: Callable[[dict], bool],
        state_path: Path,
        clock: Callable[[], float] = time.time,
        activity=None,
    ) -> None:
        self.fetch_sessions = fetch_sessions
        self.publish = publish
        self.state_path = Path(state_path)
        self.clock = clock
        self.activity = activity
        self.posted: dict[str, float] = {}
        self.last_body: dict[str, dict] = {}
        self.misses = 0
        self._load_state()

    def _load_state(self) -> None:
        try:
            if not self.state_path.exists():
                return
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"island mirror state ignored: {type(exc).__name__}", file=sys.stderr)
            return
        rows = payload.get("rows") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return
        for rid in rows:
            if isinstance(rid, str) and (
                rid.startswith(ROW_PREFIX) or rid.startswith(OLD_ROW_PREFIX)
            ):
                self.posted[rid] = 0.0

    def save_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_name(self.state_path.name + ".tmp")
            payload = json.dumps({"rows": sorted(self.posted)}, sort_keys=True)
            tmp.write_text(payload, encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.state_path)
        except OSError as exc:
            print(f"island mirror state unavailable: {type(exc).__name__}", file=sys.stderr)

    def tick(self) -> dict:
        try:
            leases = self.fetch_sessions()
            self.misses = 0
        except Exception:
            self.misses += 1
            if self.misses < MISS_LIMIT:
                return {"active": len(self.posted), "posted": 0, "cleared": 0, "error": True}
            leases = []
        if not isinstance(leases, list):
            leases = []
        now_s = self.clock()
        now_ms = int(now_s * 1000)
        active = _active_rows(leases, now_ms, self.activity)
        posts, clears = plan(leases, self.posted, now_ms, now_s, self.activity)
        planned = {body["id"] for body in posts}
        for rid, body in active.items():
            if rid in planned:
                continue
            if _visible(self.last_body.get(rid)) != _visible(body):
                posts.append(body)
        before = set(self.posted)
        posted_ok = 0
        for body in posts:
            if self.publish(body):
                self.posted[body["id"]] = now_s
                self.last_body[body["id"]] = body
                posted_ok += 1
        cleared_ok = 0
        for rid in clears:
            if self.publish(_clear_body(rid)):
                self.posted.pop(rid, None)
                self.last_body.pop(rid, None)
                cleared_ok += 1
        if set(self.posted) != before:
            self.save_state()
        return {"active": len(active), "posted": posted_ok, "cleared": cleared_ok, "error": False}

    def shutdown(self) -> None:
        for rid in sorted(self.posted):
            if self.publish(_clear_body(rid)):
                self.last_body.pop(rid, None)
                self.posted.pop(rid, None)
        self.save_state()

    def run(self, stop: threading.Event, interval: float = POLL_SECONDS) -> None:
        while not stop.is_set():
            try:
                self.tick()
            except Exception as exc:
                print(f"island mirror tick failed: {type(exc).__name__}", file=sys.stderr)
            stop.wait(interval)


def broker_sessions(socket_path: Path, timeout: float = 8.0) -> list[dict]:
    """Live lease inventory from the broker socket; lease tokens are dropped."""
    request = json.dumps({"type": "sessions", "timeoutSeconds": SESSION_TIMEOUT_SECONDS})
    chunks: list[bytes] = []
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(timeout)
        client.connect(str(socket_path))
        client.sendall(request.encode("utf-8"))
        client.shutdown(socket.SHUT_WR)
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    except OSError as exc:
        raise RuntimeError("session inventory unavailable") from exc
    finally:
        client.close()
    try:
        reply = json.loads(b"".join(chunks) or b"{}")
    except ValueError as exc:
        raise RuntimeError("session inventory unavailable") from exc
    if not isinstance(reply, dict) or reply.get("success") is not True:
        raise RuntimeError("session inventory unavailable")
    sessions = reply.get("sessions")
    if not isinstance(sessions, list):
        raise RuntimeError("session inventory unavailable")
    return [
        {key: value for key, value in session.items() if key != "lease_token"}
        for session in sessions
        if isinstance(session, dict)
    ]


def island_publish(body: dict, path: Path | None = None, timeout: float = 3.0) -> bool:
    """Post one row body to the CUAService island socket. Never raises."""
    try:
        target = Path(path) if path is not None else island_socket_path()
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


def start_in_thread(
    broker_socket: Path, state_path: Path
) -> tuple[IslandMirror, threading.Event] | None:
    if not enabled():
        return None
    activity = activity_module()
    if activity is None:
        return None
    mirror = IslandMirror(
        lambda: broker_sessions(broker_socket),
        activity.publish,
        state_path,
        activity=activity,
    )
    stop = threading.Event()
    threading.Thread(
        target=mirror.run, args=(stop,), name="comet-island-mirror", daemon=True
    ).start()
    return mirror, stop


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--socket", required=True, help="broker AF_UNIX socket")
    parser.add_argument("--state", required=True, help="persisted row-id state file")
    parser.add_argument("--once", action="store_true", help="run one tick and print it")
    parser.add_argument("--interval", type=float, default=POLL_SECONDS)
    args = parser.parse_args(argv)

    broker_socket = Path(args.socket).expanduser()
    activity = activity_module()
    if activity is None:
        print(json.dumps({"error": "acu island_activity missing"}), flush=True)
        return 0
    mirror = IslandMirror(
        lambda: broker_sessions(broker_socket),
        activity.publish,
        Path(args.state).expanduser(),
        activity=activity,
    )
    if args.once:
        print(json.dumps(mirror.tick(), sort_keys=True), flush=True)
        return 0

    stop = threading.Event()

    def _request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)
    previous: dict | None = None
    while not stop.is_set():
        try:
            result = mirror.tick()
        except Exception as exc:
            print(f"island mirror tick failed: {type(exc).__name__}", file=sys.stderr)
            result = None
        if result is not None and result != previous:
            print(json.dumps(result, sort_keys=True), flush=True)
            previous = result
        stop.wait(args.interval)
    mirror.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
