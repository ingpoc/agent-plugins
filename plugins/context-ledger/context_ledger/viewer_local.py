"""Mini localhost fallback for the ledger viewer: the same page and the same read-only data
as the MCP App, bound to 127.0.0.1 only. GET only; no write route exists.

python -m context_ledger.viewer_local --plugin-data ~/.agents/plugin-data/context-ledger --port 8790
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from context_ledger.contracts import LedgerError, canonical_json
from context_ledger.viewer import LedgerView

UI_PAGE = Path(__file__).resolve().parent.parent / "ui" / "ledger-v2.html"
HOST = "127.0.0.1"
CSP = ("default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; font-src data:; "
       "img-src data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


def make_handler(view: LedgerView, port: int, page: bytes):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    routes = {"/api/query": view.query, "/api/activity": view.activity, "/api/stats": view.stats}

    class Handler(BaseHTTPRequestHandler):
        server_version = "context-ledger-viewer"
        sys_version = ""

        def log_message(self, fmt, *args):  # no request logging; queries stay off disk
            return

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            # Host check blocks DNS-rebinding pages from reading the JSON.
            if self.headers.get("Host") not in allowed_hosts:
                self._send(403, b"forbidden", "text/plain")
                return
            parts = urlsplit(self.path)
            if parts.path in ("/", "/index.html"):
                self._send(200, page, "text/html; charset=utf-8")
                return
            fn = routes.get(parts.path)
            if fn is None:
                self._send(404, b"not found", "text/plain")
                return
            raw = parse_qs(parts.query).get("a", ["{}"])[0]
            try:
                args = json.loads(raw)
                if not isinstance(args, dict):
                    raise ValueError
            except ValueError:
                args = None
            try:
                if args is None:
                    raise LedgerError("INVALID_ARGUMENT")
                payload = {"ok": True, "data": fn(args)}
                code = 200
            except LedgerError as exc:
                payload = {"ok": False, "error": exc.as_dict().get("error", {"code": exc.code})}
                code = 400
            except Exception:  # never leak paths or tracebacks
                payload = {"ok": False, "error": {"code": "INTERNAL"}}
                code = 500
            self._send(code, canonical_json(payload).encode("utf-8"), "application/json")

        def do_POST(self) -> None:  # noqa: N802
            self._send(405, b"read-only", "text/plain")

        do_PUT = do_DELETE = do_PATCH = do_POST

    return Handler


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--plugin-data", type=Path, required=True)
    ap.add_argument("--port", type=int, default=8790)
    ns = ap.parse_args(argv)
    view = LedgerView(ns.plugin_data)
    page = UI_PAGE.read_bytes()
    httpd = ThreadingHTTPServer((HOST, ns.port), make_handler(view, ns.port, page))
    sys.stderr.write(f"context-ledger viewer on http://{HOST}:{ns.port}/ (read-only)\n")
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
