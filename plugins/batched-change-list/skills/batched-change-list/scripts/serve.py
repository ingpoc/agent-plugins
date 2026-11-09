#!/usr/bin/env python3
"""Serve one generated change-list page on loopback for live refresh."""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


def handler_for(page):
    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self):
            self.respond(False)

        def do_GET(self):
            self.respond(True)

        def respond(self, body):
            if self.path not in ('/', '/index.html'):
                self.send_error(404)
                return
            try:
                data = page.read_bytes()
            except OSError:
                self.send_error(503, 'Report unavailable')
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('ETag', '"' + hashlib.sha256(data).hexdigest() + '"')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            if body:
                self.wfile.write(data)

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--port', type=int, default=53366)
    args = parser.parse_args()
    page = args.report.resolve(strict=True)
    if not page.is_file() or page.suffix.lower() not in ('.html', '.htm'):
        parser.error('report must be an existing HTML file')
    with HTTPServer(('127.0.0.1', args.port), handler_for(page)) as server:
        print(f'http://127.0.0.1:{server.server_port}/', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
