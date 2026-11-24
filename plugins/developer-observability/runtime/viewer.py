"""Local loopback browser viewer for the existing observability dashboard.

One saved port per project, never the reserved Samantha port. The page is the
MCP App dashboard; tools are the same backend used by the stdio server.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RESERVED_PORTS = {8765}
PORT_FLOOR = 8780
STATE_NAME = "viewer-ports.json"
ALLOWED_TOOLS = {
    "observability_dashboard",
    "observability_projects",
    "observability_issues",
    "observability_inspect_issue",
    "observability_version_scopes",
    "observability_recommendation",
    "observability_decide",
}
ROOT = Path(__file__).resolve().parents[1]


def state_path(state_dir=None):
    base = Path(state_dir or os.environ.get("DEVOBS_VIEWER_STATE")
                or Path.home() / ".local/share/developer-observability")
    return base.expanduser() / STATE_NAME


def load_ports(path):
    try:
        data = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}
    except ValueError:
        return {}
    ports = data.get("ports") if isinstance(data, dict) else None
    if not isinstance(ports, dict):
        return {}
    return {str(key): int(value) for key, value in ports.items()
            if isinstance(value, int) and not isinstance(value, bool)}


def save_ports(path, ports):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ports": ports}, indent=2) + "\n")


def _bind(port):
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    return sock


def allocate_port(project, requested=None, state_dir=None, opener=_bind):
    """Return a loopback port for this project, reusing a saved one when it is still free."""
    if requested is not None:
        if requested in RESERVED_PORTS:
            raise ValueError("reserved_port: the Samantha service port cannot be used")
        if requested < 1 or requested > 65535:
            raise ValueError("invalid_port")
    path = state_path(state_dir)
    ports = load_ports(path)
    key = str(project or "default")
    candidates = []
    if requested is not None:
        candidates.append(requested)
    saved = ports.get(key)
    if saved is not None and saved not in RESERVED_PORTS and saved not in candidates:
        candidates.append(saved)
    if requested is None:
        start = PORT_FLOOR
        for port in range(start, 65535):
            if port not in RESERVED_PORTS and port not in candidates:
                candidates.append(port)
            if len(candidates) > 64:
                break
    last_error = None
    for port in candidates:
        if port in RESERVED_PORTS:
            continue
        try:
            sock = opener(port)
        except OSError as error:
            last_error = error
            continue
        ports[key] = port
        save_ports(path, ports)
        return port, sock
    raise OSError(f"no_free_port: could not bind a loopback viewer port ({last_error})")


def dashboard_html():
    html = (ROOT / "ui" / "dashboard.html").read_text()
    snippet = "<script>window.DEVOBS_VIEWER=true;</script>"
    if "DEVOBS_VIEWER" in html:
        return html
    if "<head>" in html:
        return html.replace("<head>", "<head>" + snippet, 1)
    return snippet + html


def tool_payload(name, arguments):
    if str(ROOT / 'runtime') not in __import__('sys').path:
        __import__('sys').path.insert(0, str(ROOT / 'runtime'))
    import server as module

    if name not in ALLOWED_TOOLS:
        raise ValueError("unknown_tool")
    return getattr(module, name)(**(arguments or {}))


class ViewerHandler(BaseHTTPRequestHandler):
    server_version = "devobs-viewer/0.1"

    def log_message(self, *_):
        pass

    def _send(self, content, mime, status=200):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content if isinstance(content, bytes) else content.encode())

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/dashboard"):
            self._send(dashboard_html(), "text/html; charset=utf-8")
            return
        self.send_error(404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        port = self.server.server_address[1]
        if path != "/tool" or self.headers.get("Host") not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            self.send_error(403)
            return
        origin = self.headers.get("Origin")
        if origin not in {None, f"http://127.0.0.1:{port}", f"http://localhost:{port}"}:
            self.send_error(403)
            return
        size = int(self.headers.get("Content-Length", "0") or 0)
        if not 0 < size <= 20000:
            self.send_error(400)
            return
        try:
            params = json.loads(self.rfile.read(size))
        except ValueError:
            self.send_error(400)
            return
        name = params.get("name")
        if name not in ALLOWED_TOOLS:
            self.send_error(403)
            return
        result = tool_payload(name, params.get("arguments") or {})
        body = json.dumps(result.model_dump(by_alias=True, exclude_none=True)).encode()
        self._send(body, "application/json")


def serve(project, port=None, state_dir=None, server_class=ThreadingHTTPServer):
    bound, sock = allocate_port(project, requested=port, state_dir=state_dir)
    sock.close()
    httpd = server_class(("127.0.0.1", bound), ViewerHandler)
    return httpd, f"http://127.0.0.1:{bound}/"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Serve the observability dashboard on a fixed loopback port")
    parser.add_argument("--project", default="agent-plugins", help="project id used to remember the port")
    parser.add_argument("--port", type=int, help="override the saved port; the reserved Samantha port is refused")
    parser.add_argument("--state-dir", help="where the per-project port map is stored")
    args = parser.parse_args(argv)
    httpd, url = serve(args.project, port=args.port, state_dir=args.state_dir)
    print(url, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
