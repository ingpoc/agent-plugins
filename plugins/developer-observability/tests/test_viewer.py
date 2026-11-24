from __future__ import annotations

import json
import socket
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "runtime"))
import viewer  # noqa: E402


class ViewerPortTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)

    def test_reserved_port_is_refused(self):
        with self.assertRaisesRegex(ValueError, "reserved_port"):
            viewer.allocate_port("agent-plugins", requested=8765, state_dir=self.state)

    def test_saved_port_is_reused_per_project(self):
        held = set()

        def opener(port):
            if port in held:
                raise OSError("in use")
            sock = mock.Mock()
            sock.getsockname.return_value = ("127.0.0.1", port)
            sock.close.side_effect = lambda: held.discard(port)
            held.add(port)
            return sock

        first, first_sock = viewer.allocate_port("agent-plugins", requested=8781, state_dir=self.state, opener=opener)
        first_sock.close()
        second, _ = viewer.allocate_port("agent-plugins", state_dir=self.state, opener=opener)
        other, _ = viewer.allocate_port("samantha", requested=8782, state_dir=self.state, opener=opener)
        self.assertEqual(first, 8781)
        self.assertEqual(second, 8781)
        self.assertEqual(other, 8782)
        self.assertNotEqual(first, other)

    def test_saved_reserved_port_is_not_reused(self):
        viewer.save_ports(viewer.state_path(self.state), {"agent-plugins": 8765})

        def opener(port):
            if port == 8765:
                raise AssertionError("must not bind reserved port")
            sock = mock.Mock()
            return sock

        port, _ = viewer.allocate_port("agent-plugins", state_dir=self.state, opener=opener)
        self.assertNotEqual(port, 8765)
        self.assertGreaterEqual(port, viewer.PORT_FLOOR)


class ViewerHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Bind an ephemeral port first, then serve — still not 8765.
        sock = socket.socket(); sock.bind(("127.0.0.1", 0)); free = sock.getsockname()[1]; sock.close()
        self.assertNotEqual(free, 8765)
        self.httpd, self.url = viewer.serve("agent-plugins", port=free, state_dir=self.tmp.name,
                                            server_class=ThreadingHTTPServer)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.httpd.shutdown)
        self.addCleanup(self.httpd.server_close)

    def test_page_is_the_calm_dashboard_with_http_flag(self):
        from urllib.request import urlopen
        page = urlopen(self.url, timeout=5).read().decode()
        self.assertIn("window.DEVOBS_VIEWER=true", page)
        self.assertIn("Needs you", page)
        self.assertIn('id="smaller"', page)
        self.assertNotIn("maximum-scale", page)
        self.assertNotIn("user-scalable=no", page)

    def test_tool_post_uses_the_existing_backend(self):
        from urllib.request import Request, urlopen
        payload = json.dumps({"name": "observability_projects", "arguments": {}}).encode()
        request = Request(self.url.rstrip("/") + "/tool", data=payload,
                          headers={"Content-Type": "application/json",
                                   "Origin": self.url.rstrip("/")})
        # The live backend may be present; either a projects list or a structured error is fine,
        # but the route must not 403/404 when talking to the shared tools.
        try:
            body = json.loads(urlopen(request, timeout=8).read().decode())
        except Exception as error:
            # HTTP error still has a body we can inspect
            if hasattr(error, "read"):
                body = json.loads(error.read().decode())
            else:
                raise
        self.assertTrue("structuredContent" in body or "error" in body or "projects" in body
                        or "content" in body)

    def test_foreign_origin_is_rejected(self):
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError
        payload = json.dumps({"name": "observability_projects", "arguments": {}}).encode()
        request = Request(self.url.rstrip("/") + "/tool", data=payload,
                          headers={"Content-Type": "application/json", "Origin": "http://example.com"})
        with self.assertRaises(HTTPError) as raised:
            urlopen(request, timeout=5)
        self.assertEqual(raised.exception.code, 403)



class ViewerFlagInjectionTests(unittest.TestCase):
    def test_flag_is_injected_when_bundle_already_mentions_the_name(self):
        import tempfile
        from pathlib import Path
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ui = Path(tmp.name) / "ui"
        ui.mkdir()
        (ui / "dashboard.html").write_text("<head><script>globalThis.DEVOBS_VIEWER</script></head>")
        original = viewer.ROOT
        viewer.ROOT = Path(tmp.name)
        self.addCleanup(lambda: setattr(viewer, "ROOT", original))
        page = viewer.dashboard_html()
        self.assertIn("window.DEVOBS_VIEWER=true", page)
        self.assertTrue(page.index("window.DEVOBS_VIEWER=true") != page.index("globalThis.DEVOBS_VIEWER"))

    def test_marker_is_preferred_injection_point(self):
        import tempfile
        from pathlib import Path
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ui = Path(tmp.name) / "ui"
        ui.mkdir()
        (ui / "dashboard.html").write_text("<html><!--DEVOBS_VIEWER--><body></body></html>")
        original = viewer.ROOT
        viewer.ROOT = Path(tmp.name)
        self.addCleanup(lambda: setattr(viewer, "ROOT", original))
        page = viewer.dashboard_html()
        self.assertIn("<!--DEVOBS_VIEWER--><script>window.DEVOBS_VIEWER=true;</script>", page)


class ViewerPageContractTests(unittest.TestCase):
    def test_dashboard_build_keeps_zoom_and_scope_transport(self):
        source = (ROOT / "ui" / "app.js").read_text()
        template = (ROOT / "ui" / "template.html").read_text()
        self.assertIn("DEVOBS_VIEWER", source)
        self.assertIn("generation", source)
        self.assertIn("api/issues", source)
        self.assertIn('id="smaller"', template)
        self.assertIn('id="larger"', template)
        self.assertIn("width=device-width,initial-scale=1", template)
        self.assertNotIn("maximum-scale", template)
        self.assertNotIn("user-scalable=no", template)
        self.assertIn("Round lens", template)
        self.assertIn("Timeframe", template)


if __name__ == "__main__":
    unittest.main()
