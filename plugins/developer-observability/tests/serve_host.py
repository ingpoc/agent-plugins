"""Loopback-only acceptance host, not an installed service or alternate data API."""
import argparse
import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8891)
    args = parser.parse_args()
    subprocess.run(['node','--input-type=module','-e',
                    "import{build}from'esbuild';await build({entryPoints:['tests/host.js'],bundle:true,format:'esm',outfile:'run/host.js'});"],
                   cwd=ROOT,check=True)
    spec = importlib.util.spec_from_file_location('observability_host_server',ROOT/'runtime/server.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    allowed = {'observability_dashboard','observability_projects','observability_issues',
               'observability_inspect_issue','observability_recommendation','observability_decide'}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, content, mime):
            self.send_response(200)
            self.send_header('Content-Type',mime)
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self):
            if self.path == '/':
                self.send(b'<!doctype html><html><title>Observability MCP Apps acceptance</title><body style="margin:0;background:#eee"><p id="status" style="font:12px system-ui;padding:8px">Initializing MCP Apps...</p><iframe id="app" title="Developer observability" sandbox="allow-scripts allow-same-origin" style="width:100%;height:1200px;border:0"></iframe><script type="module" src="/host.js"></script></body></html>','text/html')
            elif self.path == '/host.js':
                self.send((ROOT/'run/host.js').read_bytes(),'text/javascript')
            elif self.path == '/dashboard':
                self.send((ROOT/'ui/dashboard.html').read_bytes(),'text/html')
            else:
                self.send_error(404)

        def do_POST(self):
            if self.path != '/tool' or self.headers.get('Host') != f'127.0.0.1:{args.port}':
                self.send_error(403)
                return
            origin = self.headers.get('Origin')
            if origin != f'http://127.0.0.1:{args.port}':
                self.send_error(403)
                return
            size = int(self.headers.get('Content-Length','0'))
            if not 0 < size <= 20000:
                self.send_error(400)
                return
            params = json.loads(self.rfile.read(size))
            if params.get('name') not in allowed:
                self.send_error(403)
                return
            result = getattr(module,params['name'])(**params.get('arguments',{}))
            self.send(json.dumps(result.model_dump(by_alias=True,exclude_none=True)).encode(),'application/json')

    print(f'Acceptance host http://127.0.0.1:{args.port}',flush=True)
    ThreadingHTTPServer(('127.0.0.1',args.port),Handler).serve_forever()


if __name__ == '__main__':
    main()
