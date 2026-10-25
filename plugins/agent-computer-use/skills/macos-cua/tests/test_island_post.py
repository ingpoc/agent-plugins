"""The shared roster publisher carries its parent and harness identity over the existing transport."""
import contextlib
import importlib.util
from importlib.machinery import SourceFileLoader
from io import StringIO
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
script = ROOT.parents[1] / "bin" / "island-post"
spec = importlib.util.spec_from_loader("island_post", SourceFileLoader("island_post", str(script)))
island_post = importlib.util.module_from_spec(spec)
spec.loader.exec_module(island_post)


class IslandPostRosterTests(unittest.TestCase):
    def test_agent_name_and_parent_id_are_published(self):
        requests = []

        class FakeSocketTransport:
            def __init__(self, path):
                self.path = path

            def call(self, method, path, body=None, **kwargs):
                requests.append((method, path, body))
                return 200, {"id": body["id"]}

        argv = ["island-post", "--source", "agent", "--name", "Claude Code", "--id", "child-1",
                "--parent-id", "parent-1", "--kind", "running", "--title", "Reviewing"]
        with patch.object(sys, "argv", argv), \
             patch.object(island_post, "socket_path", return_value="/tmp/island.sock"), \
             patch.object(island_post, "valid_socket", return_value=True), \
             patch.object(island_post, "SocketTransport", FakeSocketTransport), \
             contextlib.redirect_stdout(StringIO()):
            self.assertEqual(island_post.main(), 0)

        self.assertEqual(len(requests), 1)
        method, path, body = requests[0]
        self.assertEqual((method, path), ("POST", "/api/island/publish"))
        self.assertEqual(body["source"], "agent")
        self.assertEqual(body["name"], "Claude Code")
        self.assertEqual(body["parent_id"], "parent-1")


if __name__ == "__main__":
    unittest.main()
