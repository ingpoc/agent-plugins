#!/usr/bin/env python3
"""Live CUAService integration cases for the Python client (macOS only).

Not a test_*.py file, so unit discovery never collects it. scripts/check.py
lists it as an integration suite: without AGENT_PLUGINS_INTEGRATION=1 it
prints "integration: skipped ..., needs <INTEGRATION_REQUIRES>"; with it set,
this module runs under scripts/run_strict.py and a missing or unreachable
service is a failure, never a skip. The same assertions run against a fake
service on every platform in test_cua_client.py.
"""
import sys
import unittest
from pathlib import Path

INTEGRATION_REQUIRES = "macOS CUAService on ~/.cache/macos-cua/cua-service.sock"

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from cua_client import CUAClient, RPCError


class TestCUAClientIntegration(unittest.TestCase):
    """Real-socket cases. Fail loudly when the service is absent."""

    @classmethod
    def setUpClass(cls):
        if sys.platform != "darwin":
            raise AssertionError(f"integration suite needs {INTEGRATION_REQUIRES}; host is {sys.platform}")
        sock_path = Path("~/.cache/macos-cua/cua-service.sock").expanduser()
        if not sock_path.exists():
            raise AssertionError(f"CUAService not running: no socket at {sock_path}")
        cls.client = CUAClient(str(sock_path))
        cls.client.connect(timeout=2.0)

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
