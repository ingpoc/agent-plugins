#!/usr/bin/env python3
"""Integration smoke test for CUAService via the Python client.

Requires the service to be running. Skips gracefully if not.
"""
import json
import os
import socket
import struct
import sys
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


class TestCUAClientIntegration(unittest.TestCase):
    """Integration tests — skip if service not running."""

    @classmethod
    def setUpClass(cls):
        sock_path = Path("~/.cache/macos-cua/cua-service.sock").expanduser()
        if not sock_path.exists():
            raise unittest.SkipTest("CUAService not running")
        try:
            cls.client = CUAClient(str(sock_path))
            cls.client.connect(timeout=2.0)
        except ConnectionError:
            raise unittest.SkipTest("CUAService not reachable")

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "client"):
            cls.client.close()

    def test_list_apps(self):
        apps = self.client.list_apps()
        self.assertIsInstance(apps, list)
        if apps:
            self.assertIn("name", apps[0])
            self.assertIn("pid", apps[0])

    def test_unknown_method(self):
        with self.assertRaises(RPCError) as ctx:
            self.client.call("nonexistent_method")
        self.assertEqual(ctx.exception.code, -32601)


if __name__ == "__main__":
    unittest.main()
