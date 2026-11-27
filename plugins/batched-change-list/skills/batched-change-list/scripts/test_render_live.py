"""Opt-in rendered viewer check; set BCL_COMET_RUNTIME to a Comet Control checkout."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


@unittest.skipUnless(os.environ.get("BCL_COMET_RUNTIME"), "set BCL_COMET_RUNTIME for the visible browser check")
class RenderedViewerTests(unittest.TestCase):
    def test_navigation_font_and_mixed_lane_heading(self):
        skill = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location("bcl_render_live", skill / "scripts/render.py")
        renderer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(renderer)
        data = json.loads((skill / "examples/findings.json").read_text())
        data["tracking"] = {"plan": {"round": 1, "groups": [
            {"id": state, "finding_ids": ["ISSUE-01"], "status": state}
            for state in ("running", "review", "verified", "parked", "blocked", "failed")
        ]}}
        runtime = Path(os.environ["BCL_COMET_RUNTIME"])
        controller = runtime / "skills/comet-control/scripts/durable_lease_controller.py"
        with tempfile.TemporaryDirectory(prefix="bcl-render-live-") as directory:
            root = Path(directory)
            site = root / "site"
            site.mkdir()
            (site / "index.html").write_text(renderer.render(data, skill), encoding="utf-8")
            server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(site)))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            workdir = root / "lease"
            session = "bcl-render-" + uuid.uuid4().hex

            def invoke(*args):
                result = subprocess.run([sys.executable, str(controller), *args], cwd=runtime,
                                        text=True, capture_output=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                return json.loads(result.stdout)

            started = False
            try:
                start = invoke("start", "--session-id", session, "--label", "BCL render test",
                               "--url", f"http://127.0.0.1:{server.server_port}/",
                               "--workdir", str(workdir), "--ttl-seconds", "180")
                self.assertTrue(start.get("ok"), start)
                started = True
                actions = {"actions": [
                    {"type": "locator", "locator": {"by": "css", "selector": ".nav-mode button", "first": True}, "operation": "is_visible"},
                    {"type": "evaluate", "expression": "getComputedStyle(document.querySelector('.nav-mode button')).fontSize"},
                    {"type": "evaluate", "expression": "document.querySelector('.plan-head').innerText"},
                ]}
                response = invoke("send", "--workdir", str(workdir), "--timeout", "30", json.dumps(actions))["response"]
                self.assertTrue(response.get("success"), response)
                visible = response["results"][0]["value"]
                font, heading = [item["result"] for item in response["results"][1:]]
                with self.subTest("navigation text"):
                    self.assertTrue(visible, "navigation button must be visible")
                    self.assertGreaterEqual(float(font.removesuffix("px")), 16)
                with self.subTest("mixed lane heading"):
                    self.assertIn("2 open lanes", heading)
                    self.assertIn("4 settled", heading)
            finally:
                try:
                    if started:
                        close = invoke("closeout", "--workdir", str(workdir))
                        self.assertTrue(close.get("response", {}).get("verified_absent"), close)
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)
                    self.assertFalse(thread.is_alive(), "assert_no_leak: fixture server thread still running")
                    self.assertEqual(server.socket.fileno(), -1, "assert_no_leak: fixture socket still open")


if __name__ == "__main__":
    unittest.main()
