"""Passive Chrome 146 live-session exposure check (bookmark #157, narrowed).

comet-control never attaches to a browser-wide DevTools port; diagnostics only
warn when one is exposed. The check must never connect to the port.
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("cc_diagnostics", ROOT / "diagnostics.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class RemoteDebuggingCheckTests(unittest.TestCase):
    def setUp(self):
        self.d = _load()

    def test_absent_marker_is_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self.d._remote_debugging_check(tmp, listening=lambda p: self.fail("must not probe"))
            self.assertIs(c["ok"], True)
            self.assertEqual(c["severity"], "warning")

    def test_stale_marker_is_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "DevToolsActivePort").write_text("55119\n/devtools/browser/x\n")
            c = self.d._remote_debugging_check(tmp, listening=lambda p: False)
            self.assertIs(c["ok"], True)
            self.assertIn("stale", c["detail"])

    def test_live_port_warns_and_never_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "DevToolsActivePort").write_text("9222\n/devtools/browser/x\n")
            seen = []
            c = self.d._remote_debugging_check(tmp, listening=lambda p: seen.append(p) or True)
            self.assertEqual(seen, [9222])
            self.assertIs(c["ok"], False)
            self.assertEqual(c["severity"], "warning")
            self.assertNotIn("/devtools/browser/x", c["detail"])  # never echo the ws path

    def test_unknown_listener_and_garbage(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "DevToolsActivePort").write_text("9222\n")
            self.assertIsNone(self.d._remote_debugging_check(tmp, listening=lambda p: None)["ok"])
            Path(tmp, "DevToolsActivePort").write_text("nope\n")
            self.assertIsNone(self.d._remote_debugging_check(tmp, listening=lambda p: True)["ok"])

    def test_source_never_connects(self):
        src = (ROOT / "diagnostics.py").read_text()
        self.assertNotIn("socket.socket", src)
        self.assertNotIn("create_connection", src)
        self.assertNotIn("websocket", src.lower().replace("websockets_version", ""))


if __name__ == "__main__":
    unittest.main()
