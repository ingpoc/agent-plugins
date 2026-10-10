#!/usr/bin/env python3
"""Thin JSON-RPC client for CUAService over Unix domain socket.

Translates the existing MCP act/state/verify interface into JSON-RPC calls
to the unified Swift service. Auto-spawns the service if the socket is missing.
"""
from __future__ import annotations

import fcntl
import json
import os
import signal
import socket
import struct
import subprocess
import time
from pathlib import Path
from typing import Any

DEFAULT_SOCKET = Path("~/.cache/macos-cua/cua-service.sock").expanduser()
SERVICE_APP = Path("~/.cache/macos-cua/CUAService.app").expanduser()
SERVICE_BIN = SERVICE_APP / "Contents" / "MacOS" / "CUAService"
RELAUNCH_LOCK = DEFAULT_SOCKET.parent / "cua-service.relaunch.lock"
RELAUNCH_STAMP = DEFAULT_SOCKET.parent / "cua-service.relaunch.stamp"
RELAUNCH_COOLDOWN_S = 30.0
RECOVERY_COMMAND = "pkill -x CUAService; open ~/.cache/macos-cua/CUAService.app"


class CUAServiceUnavailable(ConnectionError):
    """CUAService socket is unusable; message carries the exact recovery."""

_counter = 0


def _next_id() -> int:
    global _counter
    _counter += 1
    return _counter


class CUAClient:
    """JSON-RPC 2.0 client with length-prefixed framing over Unix socket."""

    def __init__(self, socket_path: str | Path | None = None):
        self.socket_path = str(socket_path or DEFAULT_SOCKET)
        self._sock: socket.socket | None = None
        self.relaunch: dict[str, Any] | None = None

    def connect(self, timeout: float = 5.0) -> None:
        """Connect, healing a dead socket through one serialized recovery.

        Missing socket or refusing listener: _recover() under a cross-agent
        flock re-probes, then spawns (no service) or does one guarded relaunch
        (wedged default-socket service: kill + open the app; 30s cooldown;
        MACOS_CUA_NO_RELAUNCH=1 opts out). Still unusable: raise
        CUAServiceUnavailable naming the exact recovery command.
        """
        deadline = time.monotonic() + timeout
        recovery: dict[str, Any] | None = None
        refused_since: float | None = None
        last: Exception | None = None
        while True:
            try:
                self._sock = self._probe()
                self.relaunch = recovery
                return
            except OSError as exc:
                last = exc
            now = time.monotonic()
            if isinstance(last, ConnectionRefusedError):
                # A starting service can refuse briefly; act after 1s.
                refused_since = refused_since if refused_since is not None else now
                due = now - refused_since >= 1.0
            else:
                due = True
            if recovery is None and due:
                recovery = self._recover()
                if recovery.get("relaunched") or recovery.get("spawned"):
                    deadline = max(deadline, time.monotonic() + 8.0)
                    refused_since = None
            if time.monotonic() >= deadline:
                break
            time.sleep(0.2)
        state = "refusing connections" if isinstance(
            last, ConnectionRefusedError) else f"unreachable ({last})"
        raise CUAServiceUnavailable(
            f"CUAService {state} at {self.socket_path} after {timeout}s "
            f"(pids={owner_pids()}, recovery={recovery}). "
            f"Recover with: {RECOVERY_COMMAND}"
        )

    def _probe(self) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(15.0)
            sock.connect(self.socket_path)
        except OSError:
            sock.close()
            raise
        return sock

    def _recover(self) -> dict[str, Any]:
        """All spawn/relaunch decisions happen under one cross-agent flock."""
        RELAUNCH_LOCK.parent.mkdir(parents=True, exist_ok=True)
        with open(RELAUNCH_LOCK, "w") as lock:
            end = time.monotonic() + 15.0
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= end:
                        return {"recovered": False, "reason": "recovery lock busy for 15s"}
                    time.sleep(0.1)
            try:  # a peer may have healed it while we waited
                self._probe().close()
                return {"recovered": True, "by": "peer"}
            except OSError as exc:
                failure = exc
            default = Path(self.socket_path) == DEFAULT_SOCKET
            pids = owner_pids() if default else []
            if not pids:
                self._ensure_service()
                return {"spawned": True, "probe": type(failure).__name__}
            if not isinstance(failure, ConnectionRefusedError):
                return {"recovered": False, "reason": f"service running, socket {failure}"}
            if os.environ.get("MACOS_CUA_NO_RELAUNCH") == "1":
                return {"relaunched": False, "reason": "MACOS_CUA_NO_RELAUNCH=1"}
            try:
                age = time.time() - RELAUNCH_STAMP.stat().st_mtime
            except FileNotFoundError:
                age = None
            if age is not None and age < RELAUNCH_COOLDOWN_S:
                return {"relaunched": False, "reason": f"relaunched {age:.0f}s ago"}
            RELAUNCH_STAMP.touch()
            for pid in pids:
                _signal(pid, signal.SIGTERM)
            stop = time.monotonic() + 2.0
            while time.monotonic() < stop and set(pids) & set(owner_pids()):
                time.sleep(0.1)
            for pid in set(pids) & set(owner_pids()):
                _signal(pid, signal.SIGKILL)
            subprocess.run(["open", str(SERVICE_APP)], check=True,
                           stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=15)
            return {"relaunched": True, "killed": pids}

    def close(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        retry: bool = True,
    ) -> Any:
        last: Exception | None = None
        for attempt in range(2):
            try:
                return self._call_once(method, params)
            except (ConnectionError, TimeoutError, OSError) as exc:
                last = exc
                self.close()
                if retry and attempt == 0:
                    self.connect()
                    continue
                raise
        raise last or ConnectionError("RPC failed")

    def _call_once(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if not self._sock:
            self.connect()
        req_id = _next_id()
        request = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": req_id,
        }
        body = json.dumps(request).encode("utf-8")
        frame = struct.pack("<I", len(body)) + body
        assert self._sock is not None
        self._sock.sendall(frame)

        for _ in range(8):
            header = self._recv_exact(4)
            length = struct.unpack("<I", header)[0]
            if length > 16_000_000:
                raise ConnectionError(f"implausible frame length {length}")
            raw = self._recv_exact(length)
            response = json.loads(raw)
            if response.get("id") != req_id:
                continue
            if "error" in response and response["error"]:
                err = response["error"]
                raise RPCError(err.get("code", -1), err.get("message", "Unknown error"))
            return response.get("result")
        raise ConnectionError(f"no JSON-RPC response for id {req_id}")

    def _recv_exact(self, n: int) -> bytes:
        data = b""
        while len(data) < n:
            chunk = self._sock.recv(n - len(data))
            if not chunk:
                raise ConnectionError("Socket closed")
            data += chunk
        return data

    def _ensure_service(self) -> None:
        """Spawn CUAService if not running."""
        if SERVICE_BIN.exists():
            subprocess.Popen(
                [str(SERVICE_BIN), "--socket-path", self.socket_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        elif SERVICE_APP.exists():
            subprocess.Popen(
                ["open", "-a", str(SERVICE_APP), "--args",
                 "--socket-path", self.socket_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    # ---- MCP-compatible convenience methods ----

    def list_apps(self) -> list[dict]:
        return self.call("list_apps")

    def get_app_state(self, app: str, **kwargs) -> dict:
        return self.call("get_app_state", {"app": app, **kwargs})

    def click(self, app: str, **kwargs) -> dict:
        return self.call("click", {"app": app, **kwargs}, retry=False)

    def press_key(self, app: str, key: str) -> dict:
        return self.call("press_key", {"app": app, "key": key}, retry=False)

    def type_text(self, app: str, text: str, after_new_document: bool = False) -> dict:
        params: dict[str, Any] = {"app": app, "text": text}
        if after_new_document:
            params["after_new_document"] = True
        return self.call("type_text", params, retry=False)

    def scroll(self, app: str, direction: str, **kwargs) -> dict:
        return self.call(
            "scroll", {"app": app, "direction": direction, **kwargs}, retry=False
        )

    def set_value(self, app: str, element_index: int, value: str) -> dict:
        return self.call("set_value", {
            "app": app, "element_index": element_index, "value": value
        }, retry=False)

    def drag(self, app: str, from_x: float, from_y: float,
             to_x: float, to_y: float) -> dict:
        return self.call("drag", {
            "app": app, "from_x": from_x, "from_y": from_y,
            "to_x": to_x, "to_y": to_y,
        }, retry=False)

    def select_text(self, app: str, element_index: int, text: str, **kwargs) -> dict:
        return self.call("select_text", {
            "app": app, "element_index": element_index, "text": text, **kwargs,
        }, retry=False)

    def perform_secondary_action(
        self, app: str, element_index: int, action: str
    ) -> dict:
        return self.call("perform_secondary_action", {
            "app": app, "element_index": element_index, "action": action,
        }, retry=False)

    def open_item(self, app: str, **kwargs) -> dict:
        return self.call("open_item", {"app": app, **kwargs}, retry=False)

    def execute_plan(self, app: str, steps: list[dict]) -> dict:
        return self.call(
            "execute_plan", {"app": app, "steps": steps}, retry=False
        )

    def hide_agent_cursor(self) -> dict:
        return self.call("hide_agent_cursor") or {"ok": True}


def owner_pids() -> list[int]:
    """PIDs of the installed CUAService serving the default socket.

    Only processes whose executable is SERVICE_BIN and that were not pointed
    at another socket with --socket-path count; other installs are never
    touched. Fails loud if process discovery itself fails.
    """
    found = subprocess.run(["ps", "-axww", "-o", "pid=,command="],
                           capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=5)
    if found.returncode != 0:
        raise CUAServiceUnavailable(
            f"ps failed ({found.returncode}): {found.stderr.strip()}")
    pids = []
    for line in found.stdout.splitlines():
        pid, _, command = line.strip().partition(" ")
        binary = str(SERVICE_BIN)
        if command != binary and not command.startswith(binary + " "):
            continue
        args = command[len(binary):].split()
        socket_arg = None
        for i, arg in enumerate(args):
            if arg == "--socket-path":
                socket_arg = args[i + 1] if i + 1 < len(args) else ""
            elif arg.startswith("--socket-path="):
                socket_arg = arg.split("=", 1)[1]
        if socket_arg is not None and (
                not socket_arg or Path(socket_arg).expanduser() != DEFAULT_SOCKET):
            continue
        pids.append(int(pid))
    return pids


def _signal(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass


def health(socket_path: str | Path | None = None, timeout: float = 5.0) -> dict:
    """Fail-loud probe: connect (with guarded relaunch) + list_apps round-trip."""
    client = CUAClient(socket_path)
    try:
        client.connect(timeout=timeout)
        apps = client.call("list_apps", retry=False)
        if not isinstance(apps, list) or not apps:
            raise CUAServiceUnavailable(
                f"CUAService list_apps returned {apps!r}. Recover with: {RECOVERY_COMMAND}")
        return {"ok": True, "socket": client.socket_path, "pids": owner_pids(),
                "apps": len(apps), "relaunch": client.relaunch}
    finally:
        client.close()


class RPCError(Exception):
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"RPC error {code}: {message}")
