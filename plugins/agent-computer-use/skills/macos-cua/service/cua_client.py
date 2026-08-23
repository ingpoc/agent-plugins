#!/usr/bin/env python3
"""Thin JSON-RPC client for CUAService over Unix domain socket.

Translates the existing MCP act/state/verify interface into JSON-RPC calls
to the unified Swift service. Auto-spawns the service if the socket is missing.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import time
from pathlib import Path
from typing import Any

DEFAULT_SOCKET = Path("~/.cache/macos-cua/cua-service.sock").expanduser()
SERVICE_APP = Path("~/.cache/macos-cua/CUAService.app").expanduser()
SERVICE_BIN = SERVICE_APP / "Contents" / "MacOS" / "CUAService"

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

    def connect(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        spawned = False
        while time.monotonic() < deadline:
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(15.0)
                sock.connect(self.socket_path)
                self._sock = sock
                return
            except (ConnectionRefusedError, FileNotFoundError, OSError):
                sock.close()
                if not spawned:
                    self._ensure_service()
                    spawned = True
                time.sleep(0.3)
        raise ConnectionError(
            f"CUAService not reachable at {self.socket_path} after {timeout}s"
        )

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


class RPCError(Exception):
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"RPC error {code}: {message}")
