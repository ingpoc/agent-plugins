#!/usr/bin/env python3
"""Serve one generated change-list page on loopback for live refresh."""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib.util
import json
from pathlib import Path

RENDERER_PATH = Path(__file__).with_name("render.py")
SPEC = importlib.util.spec_from_file_location("change_list_renderer", RENDERER_PATH)
RENDERER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RENDERER)


def handler_for(page, data=None, inventory=None):
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
                if data is None:
                    content = page.read_bytes()
                else:
                    document = json.loads(data.read_text(encoding='utf-8'))
                    installed = RENDERER.load_inventory(inventory) if inventory else None
                    content = RENDERER.render(document, data.resolve().parent, installed).encode('utf-8')
            except (OSError, ValueError, json.JSONDecodeError):
                self.send_error(503, 'Report unavailable')
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('ETag', '"' + hashlib.sha256(content).hexdigest() + '"')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            if body:
                self.wfile.write(content)

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--port', type=int, default=53366)
    parser.add_argument('--data', type=Path, help='authoritative tracker JSON to render on each request')
    parser.add_argument('--inventory', type=Path, help='optional installed-plugin inventory JSON')
    args = parser.parse_args()
    if args.inventory and not args.data:
        parser.error('--inventory requires --data')
    page = args.report.resolve(strict=True)
    if not page.is_file() or page.suffix.lower() not in ('.html', '.htm'):
        parser.error('report must be an existing HTML file')
    data = args.data.resolve(strict=True) if args.data else None
    if data and (not data.is_file() or data.suffix.lower() != '.json'):
        parser.error('--data must be a JSON file')
    inventory = args.inventory.resolve(strict=True) if args.inventory else None
    if inventory and (not inventory.is_file() or inventory.suffix.lower() != '.json'):
        parser.error('--inventory must be a JSON file')
    with HTTPServer(('127.0.0.1', args.port), handler_for(page, data, inventory)) as server:
        print(f'http://127.0.0.1:{server.server_port}/', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
