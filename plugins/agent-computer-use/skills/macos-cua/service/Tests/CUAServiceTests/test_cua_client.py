#!/usr/bin/env python3
"""Unit tests for the CUAService Python client.

Every case runs on any platform. Wire cases talk to an in-process fake
CUAService on a private Unix socket, so the client's real framing, request
shape, and error mapping are exercised without the macOS service. The
real-socket cases live in integration_cua_client.py and run only when
scripts/check.py is given AGENT_PLUGINS_INTEGRATION=1.
"""
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add service dir to path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from cua_client import CUAClient, RPCError


class TestFrameCodec(unittest.TestCase):
    """Test the length-prefixed frame encoding/decoding."""

    def test_roundtrip(self):
        msg = {"jsonrpc": "2.0", "method": "test", "id": 1}
        body = json.dumps(msg).encode("utf-8")
        frame = struct.pack("<I", len(body)) + body

        # Decode
        length = struct.unpack("<I", frame[:4])[0]
        decoded = json.loads(frame[4 : 4 + length])
        self.assertEqual(decoded["method"], "test")

    def test_empty_frame(self):
        body = b""
        frame = struct.pack("<I", 0) + body
        length = struct.unpack("<I", frame[:4])[0]
        self.assertEqual(length, 0)


class TestCUAClientUnit(unittest.TestCase):
    """Unit tests for client without live socket."""

    def test_init_default_path(self):
        client = CUAClient()
        self.assertIn("cua-service.sock", client.socket_path)

    def test_init_custom_path(self):
        client = CUAClient("/tmp/test.sock")
        self.assertEqual(client.socket_path, "/tmp/test.sock")

    def test_rpc_error(self):
        err = RPCError(-32601, "Method not found")
        self.assertEqual(err.code, -32601)
        self.assertIn("Method not found", str(err))

    def test_mutating_timeout_is_not_replayed(self):
        client = CUAClient("/tmp/test.sock")
        client._call_once = MagicMock(side_effect=TimeoutError("slow input"))
        client.close = MagicMock()
        with self.assertRaises(TimeoutError):
            client.call("click", {"app": "Finder"}, retry=False)
        client._call_once.assert_called_once()
        client.close.assert_called_once()

    def test_click_disables_retry(self):
        client = CUAClient("/tmp/test.sock")
        client.call = MagicMock(return_value={"ok": True})
        client.click("Finder", label="Apps")
        client.call.assert_called_once_with(
            "click", {"app": "Finder", "label": "Apps"}, retry=False
        )

    def test_native_plan_disables_retry(self):
        client = CUAClient("/tmp/test.sock")
        client.call = MagicMock(return_value={"ok": True})
        steps = [{"method": "open_item", "params": {"path": "/tmp"}}]
        client.execute_plan("Finder", steps)
        client.call.assert_called_once_with(
            "execute_plan", {"app": "Finder", "steps": steps}, retry=False
        )


class FakeCUAService:
    """Minimal CUAService stand-in: length-prefixed JSON-RPC over AF_UNIX.

    Replies mirror MethodRouter.swift: list_apps returns the AppResolver
    listApps() rows, any other method is -32601 "Method not found: <m>".
    """

    APPS = [
        {"bundleId": "com.apple.finder", "name": "Finder", "pid": 501, "isActive": True},
    ]

    def __init__(self) -> None:
        self._dir = tempfile.TemporaryDirectory(prefix="cua-")
        self.socket_path = os.path.join(self._dir.name, "s.sock")
        self.requests: list[dict] = []
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(self.socket_path)
        self._listener.listen(1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _recv_exact(self, conn: socket.socket, n: int) -> bytes | None:
        data = b""
        while len(data) < n:
            chunk = conn.recv(n - len(data))
            if not chunk:
                return None
            data += chunk
        return data

    def _reply(self, request: dict) -> dict:
        if request.get("method") == "list_apps":
            return {"jsonrpc": "2.0", "id": request.get("id"), "result": self.APPS}
        return {
            "jsonrpc": "2.0",
            "id": request.get("id"),
            "error": {"code": -32601, "message": f"Method not found: {request.get('method')}"},
        }

    def _serve(self) -> None:
        try:
            conn, _ = self._listener.accept()
        except OSError:
            return
        with conn:
            while True:
                header = self._recv_exact(conn, 4)
                if header is None:
                    return
                body = self._recv_exact(conn, struct.unpack("<I", header)[0])
                if body is None:
                    return
                request = json.loads(body)
                self.requests.append(request)
                out = json.dumps(self._reply(request)).encode("utf-8")
                conn.sendall(struct.pack("<I", len(out)) + out)

    def close(self) -> None:
        self._listener.close()
        self._thread.join(timeout=2.0)
        self._dir.cleanup()


class TestCUAClientWire(unittest.TestCase):
    """The former live-socket cases, against FakeCUAService on every platform."""

    def setUp(self):
        # A failed probe must never reach _recover(), which can spawn or
        # relaunch the real CUAService on macOS.
        recover = patch.object(
            CUAClient, "_recover", side_effect=AssertionError("fake socket probe failed")
        )
        recover.start()
        self.addCleanup(recover.stop)
        self.service = FakeCUAService()
        self.addCleanup(self.service.close)
        self.client = CUAClient(self.service.socket_path)
        self.client.connect(timeout=2.0)
        self.addCleanup(self.client.close)

    def test_list_apps(self):
        apps = self.client.list_apps()
        self.assertIsInstance(apps, list)
        self.assertTrue(apps)
        self.assertIn("name", apps[0])
        self.assertIn("pid", apps[0])
        request = self.service.requests[-1]
        self.assertEqual(request["jsonrpc"], "2.0")
        self.assertEqual(request["method"], "list_apps")
        self.assertEqual(request["params"], {})
        self.assertIsInstance(request["id"], int)

    def test_unknown_method(self):
        with self.assertRaises(RPCError) as ctx:
            self.client.call("nonexistent_method")
        self.assertEqual(ctx.exception.code, -32601)
        self.assertIn("nonexistent_method", str(ctx.exception))
        self.assertEqual(self.service.requests[-1]["method"], "nonexistent_method")


if __name__ == "__main__":
    unittest.main()
