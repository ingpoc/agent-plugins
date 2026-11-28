"""Rendered CL-3 acceptance. Set LEDGER_TEST_BROWSER to a Chromium executable."""

import json
import os
import sqlite3
import threading
from http.server import ThreadingHTTPServer

from test_viewer import ROOT, ViewerBase, _u
from context_ledger.viewer_local import make_handler

INTEGRATION_REQUIRES = "LEDGER_TEST_BROWSER pointing to a Chromium executable and Python Playwright"


class ViewerBrowserTests(ViewerBase):
    @classmethod
    def setUpClass(cls):
        browser = os.environ.get("LEDGER_TEST_BROWSER")
        if not browser or not os.path.isfile(browser) or not os.access(browser, os.X_OK):
            raise AssertionError(f"integration suite needs {INTEGRATION_REQUIRES}")
        try:
            import playwright.sync_api
        except ImportError as error:
            raise AssertionError(f"integration suite needs {INTEGRATION_REQUIRES}") from error
        super().setUpClass()

    def test_project_lookup_and_text_controls_render(self):
        from playwright.sync_api import sync_playwright

        ids = []
        for project in ("alpha", "beta", "gamma"):
            ids.append(self.store.save({"request_id": _u(), "type": "user_decision", "actor": "tester",
                                        "decision": f"Use the {project} checklist", "why": "Fixture",
                                        "entities": ["release"], "scope": f"project:{project}"}, mcp=True)["decision_id"])
        conn = sqlite3.connect(self.store.db_path)
        for did, project in zip(ids[:2], ("alpha", "other")):
            conn.execute("insert into access_log(ts,tool,caller,n,bytes,ids,ms,project) "
                         "values ('2026-11-26T00:00:00Z','find','tester',1,0,?,0,?)", (json.dumps([did]), project))
        conn.commit()
        conn.close()

        probe = ThreadingHTTPServer(("127.0.0.1", 0), lambda *a, **k: None)
        port = probe.server_address[1]
        probe.server_close()
        page_bytes = (ROOT / "ui/ledger-v2.html").read_bytes()
        server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(self.view, port, page_bytes))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, executable_path=os.environ["LEDGER_TEST_BROWSER"])
                try:
                    page = browser.new_page(viewport={"width": 1440, "height": 900})
                    page.set_default_timeout(5000)
                    page.goto(f"http://127.0.0.1:{port}/")
                    for did, project, lookup in zip(ids, ("alpha", "beta", "gamma"),
                            ("last lookup matched alpha", "last lookup from other · not this project", "no lookup yet")):
                        row = page.locator(f'button.row[data-id="{did}"]')
                        row.wait_for(state="visible")
                        self.assertEqual(row.locator(".proj").inner_text(), f"project {project}")
                        self.assertEqual(row.locator(".lk").inner_text(), lookup)
                        row.click()
                        self.assertEqual(page.locator(".trace dt", has_text="Project").locator("+ dd").inner_text(), f"project {project}")
                        self.assertIn(lookup, page.locator(".trace dt", has_text="Last lookup").locator("+ dd").inner_text())
                    label = page.locator("#sizeLabel")
                    self.assertEqual(label.inner_text(), "100%")
                    before = page.locator(f'button.row[data-id="{ids[0]}"] .proj').evaluate("e => parseFloat(getComputedStyle(e).fontSize)")
                    self.assertGreaterEqual(before, 16)
                    page.locator("#sizeUp").click()
                    self.assertNotEqual(label.inner_text(), "100%")
                    after = page.locator(f'button.row[data-id="{ids[0]}"] .proj').evaluate("e => parseFloat(getComputedStyle(e).fontSize)")
                    self.assertGreater(after, before)
                    page.reload()
                    self.assertGreater(page.locator(f'button.row[data-id="{ids[0]}"] .proj').evaluate("e => parseFloat(getComputedStyle(e).fontSize)"), before)
                    page.locator("#sizeDown").click()
                    self.assertEqual(label.inner_text(), "100%")
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
