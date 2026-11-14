#!/usr/bin/env python3
"""Serve one generated change-list page on loopback for live refresh."""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
import live_state

RENDERER_PATH = Path(__file__).with_name("render.py")
SPEC = importlib.util.spec_from_file_location("change_list_renderer", RENDERER_PATH)
RENDERER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RENDERER)


def response_state(page, data=None, inventory=None, viewer_metadata=None):
    if data is None:
        content = page.read_bytes()
        return hashlib.sha256(content).hexdigest(), content
    tracker_bytes = data.read_bytes()
    document = json.loads(tracker_bytes)
    current = datetime.now(timezone.utc)
    if inventory:
        inventory_bytes = inventory.read_bytes()
        installed = json.loads(inventory_bytes)
        if not isinstance(installed, dict):
            raise ValueError("inventory must be a JSON object")
        inventory_fingerprint = inventory_bytes
    else:
        installed = RENDERER.discover_installed_plugins(now=current)
        inventory_fingerprint = json.dumps(
            {"versions": installed.get("versions", {}), "sources": installed.get("sources", {})},
            sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(tracker_bytes + b"\0" + inventory_fingerprint + b"\0" + RENDERER.TEMPLATE.read_bytes()).hexdigest()
    if viewer_metadata:
        digest = hashlib.sha256((digest.encode() + viewer_metadata.read_bytes())).hexdigest()
    return digest, (document, installed, current)


def handler_for(page, data=None, inventory=None, viewer_metadata=None, base=None):
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
                fingerprint, state = response_state(page, data, inventory, viewer_metadata)
                if not body:
                    content = None
                elif data is None:
                    content = state
                else:
                    content = RENDERER.render(state[0], base or live_state.evidence_base(data), state[1], state[2],
                                              json.loads(viewer_metadata.read_text()) if viewer_metadata else None).encode('utf-8')
            except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
                self.send_error(503, 'Report unavailable')
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('ETag', '"' + fingerprint + '"')
            if content is not None:
                self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            if body and content is not None:
                self.wfile.write(content)

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--port', type=int, default=53366)
    parser.add_argument('--data', type=Path, help='authoritative tracker JSON to render on each request')
    parser.add_argument('--inventory', type=Path, help='optional installed-plugin inventory JSON')
    parser.add_argument('--viewer-metadata', type=Path)
    parser.add_argument('--base', type=Path, help=argparse.SUPPRESS)
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
    viewer_metadata = args.viewer_metadata.resolve(strict=True) if args.viewer_metadata else None
    base = args.base.resolve(strict=True) if args.base else None
    base = base or (live_state.evidence_base(data) if data else None)
    with HTTPServer(('127.0.0.1', args.port), handler_for(page, data, inventory, viewer_metadata, base)) as server:
        print(f'http://127.0.0.1:{server.server_port}/', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
