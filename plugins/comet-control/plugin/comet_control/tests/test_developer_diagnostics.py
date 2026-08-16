#!/usr/bin/env python3
"""Live progressive console and network diagnostics acceptance test."""

from __future__ import annotations

import json
import socket
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from test_multi_agent_isolation import (
    SOCK,
    TestFailure,
    bridge,
    close_session,
    preflight,
    require,
    require_success,
    result_of,
    session_inventory,
    stop_fixture,
)


class DiagnosticHandler(BaseHTTPRequestHandler):
    nonce = ""

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _send(self, status: int, body: str, content_type: str = "text/html") -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        parsed = urlparse(self.path)
        if parsed.path == "/blank":
            self._send(
                200,
                '<!doctype html><main id="blank">Blank '
                '<a id="diagnostics" href="/diagnostics">Run diagnostics</a></main>',
            )
            return
        if parsed.path == "/missing":
            self._send(503, "diagnostic service unavailable", "text/plain")
            return
        if parsed.path == "/abort":
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()
            return
        if parsed.path == "/diagnostics":
            nonce = json.dumps(self.nonce)
            self._send(
                200,
                f"""<!doctype html>
<html><body><main id="loading">Diagnostics</main>
<script>
const nonce = {nonce};
console.log(`diag-log-${{nonce}}`);
console.warn(`diag-warn-${{nonce}}`);
console.error(`diag-error-${{nonce}}`);
setTimeout(() => {{ throw new Error(`diag-uncaught-${{nonce}}`); }}, 0);
Promise.allSettled([
  fetch(`/missing?nonce=${{nonce}}`),
  fetch(`/abort?nonce=${{nonce}}`).catch((error) => {{
    console.error(`diag-fetch-error-${{nonce}}`, error.message);
    throw error;
  }})
]).then(() => {{ document.querySelector('main').id = 'ready'; }});
</script></body></html>""",
            )
            return
        self._send(404, "not found", "text/plain")


def main() -> int:
    print("comet-control progressive developer diagnostics suite\n")
    if not SOCK.exists():
        print(f"SKIP: bridge socket not found at {SOCK}")
        return 2
    try:
        status = bridge({"type": "status", "timeoutSeconds": 5}, timeout=7)
    except OSError as exc:
        print(f"SKIP: bridge unavailable ({exc})")
        return 2
    if status.get("success") is not True:
        print(f"SKIP: bridge status failed ({status.get('error')})")
        return 2

    nonce = uuid.uuid4().hex[:10]
    DiagnosticHandler.nonce = nonce
    server = ThreadingHTTPServer(("127.0.0.1", 0), DiagnosticHandler)
    server.daemon_threads = True
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    session = None
    failure: str | None = None
    cleanup_failures: list[str] = []

    try:
        session = preflight(
            f"comet-control-devtools-{nonce}", f"Diagnostics {nonce}", f"{base_url}/blank"
        )
        baseline = require_success(
            bridge(
                {
                    "type": "run",
                    "sessionId": session.session_id,
                    "leaseToken": session.lease_token,
                    "actions": [{"type": "page_context"}],
                }
            ),
            "diagnostic baseline",
        )
        baseline_context = result_of(baseline, "page_context")
        require(
            baseline_context.get("network_capture_enabled") is False,
            "network capture stays off during normal browser use",
        )
        require(
            "network_error_count" not in baseline_context,
            "normal page context does not spend tokens on network detail",
        )
        response = require_success(
            bridge(
                {
                    "type": "run",
                    "sessionId": session.session_id,
                    "leaseToken": session.lease_token,
                    "timeoutSeconds": 20,
                    "actions": [
                        {"type": "network_watch", "clear": True},
                        {"type": "click_selector", "selector": "#diagnostics"},
                        {"type": "wait_for_selector", "selector": "#ready", "timeout": 5_000},
                        {"type": "network_summary"},
                        {"type": "network_errors", "filter": nonce, "limit": 10},
                        {
                            "type": "console_tail",
                            "levels": ["error"],
                            "filter": nonce,
                            "limit": 10,
                        },
                        {
                            "type": "console_tail",
                            "levels": ["log"],
                            "filter": nonce,
                            "limit": 5,
                        },
                        {"type": "page_context"},
                    ],
                },
                timeout=25,
            ),
            "diagnostic action batch",
        )
        results = response.get("results", [])
        summary = result_of(response, "network_summary")
        require(summary.get("capture_started_now") is False, "network watch precedes summary")
        require(summary.get("request_count", 0) >= 3, "network summary counts requests")
        require(summary.get("http_error_count", 0) >= 1, "network summary counts HTTP errors")
        require(summary.get("failed_count", 0) >= 1, "network summary counts failed loads")
        require(summary.get("error_count", 0) >= 2, "network summary stays compact but actionable")

        network = result_of(response, "network_errors")
        entries = network.get("entries", [])
        require(0 < len(entries) <= 10, "network detail obeys its explicit limit")
        require(any(entry.get("status") == 503 for entry in entries), "network detail includes HTTP 503")
        require(
            any(entry.get("kind") in {"loading_failed", "blocked"} for entry in entries),
            "network detail includes the aborted request",
        )
        require(all(nonce in str(entry.get("url")) for entry in entries), "network filter is applied")

        console_results = [item for item in results if item.get("type") == "console_tail"]
        require(len(console_results) == 2, "console detail supports separate progressive slices")
        errors, logs = console_results
        require(errors.get("count", 0) >= 1, "filtered console errors are returned")
        require(
            all(
                entry.get("level") in {"error", "page_error", "unhandledrejection", "assert"}
                for entry in errors.get("entries", [])
            ),
            "console level filter excludes verbose logs",
        )
        require(
            any(entry.get("level") == "page_error" for entry in errors.get("entries", [])),
            "error level includes uncaught page exceptions",
        )
        require(logs.get("count", 0) >= 1, "verbose console logs are opt-in")
        require(
            all(entry.get("level") == "log" for entry in logs.get("entries", [])),
            "verbose console slice contains only requested levels",
        )

        context = result_of(response, "page_context")
        require(context.get("network_capture_enabled") is True, "page context exposes capture state")
        require(context.get("network_error_count", 0) >= 2, "page context exposes only compact network count")
        require(len(json.dumps(context)) < 8_000, "page context remains token-bounded")

        navigation = require_success(
            bridge(
                {
                    "type": "run",
                    "sessionId": session.session_id,
                    "leaseToken": session.lease_token,
                    "actions": [
                        {"type": "back", "waitMs": 200},
                        {"type": "wait_for_selector", "selector": "#blank", "timeout": 3_000},
                        {"type": "forward", "waitMs": 200},
                        {"type": "wait_for_selector", "selector": "#ready", "timeout": 3_000},
                        {"type": "reload_page", "waitMs": 200},
                        {"type": "wait_for_selector", "selector": "#ready", "timeout": 3_000},
                    ],
                }
            ),
            "history and reload slice",
        )
        require(result_of(navigation, "back").get("url", "").endswith("/blank"), "back navigation works")
        require(
            result_of(navigation, "forward").get("url", "").endswith("/diagnostics"),
            "forward navigation works",
        )
        require(
            result_of(navigation, "reload_page").get("url", "").endswith("/diagnostics"),
            "page reload works",
        )

        cleared = require_success(
            bridge(
                {
                    "type": "run",
                    "sessionId": session.session_id,
                    "leaseToken": session.lease_token,
                    "actions": [
                        {"type": "network_errors", "limit": 5, "clear": True},
                        {"type": "network_summary"},
                    ],
                }
            ),
            "diagnostic clear slice",
        )
        require(
            result_of(cleared, "network_summary").get("error_count") == 0,
            "network clear starts a clean diagnostic slice",
        )
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        if session is not None:
            try:
                close_session(session)
            except Exception as exc:
                cleanup_failures.append(f"closeout: {exc}")
        stopped, port_closed = stop_fixture(server, thread, port)
        if not stopped:
            cleanup_failures.append("fixture thread is still alive")
        if not port_closed:
            cleanup_failures.append(f"fixture port {port} is still listening")
        try:
            if session and session.session_id in session_inventory():
                cleanup_failures.append("diagnostic lease remains")
        except TestFailure as exc:
            cleanup_failures.append(f"inventory: {exc}")

    print("\n=== SUMMARY ===")
    if failure:
        print(f"  [FAIL] {failure}")
    for item in cleanup_failures:
        print(f"  [FAIL] cleanup: {item}")
    if not failure and not cleanup_failures:
        print("  [PASS] console and network diagnostics are compact, filtered, and clean")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
