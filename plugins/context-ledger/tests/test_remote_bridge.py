"""Remote stdio bridge and serve-mode switch. Stdlib unittest."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "bin" / "context-ledger-mcp"
BRIDGE = ROOT / "bin" / "remote_bridge.py"
TOKEN = "bridge-test-token-9f3a"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else b""
        self.server.seen.append((dict(self.headers.items()), body))
        spec = self.server.script.pop(0) if self.server.script else {"status": 500, "body": b""}
        if spec.get("hang"):
            while not self.server.stop.is_set():
                time.sleep(0.05)
            return
        payload = spec.get("body", b"")
        self.send_response(spec["status"])
        if payload:
            self.send_header("Content-Type", spec.get("content_type", "application/json"))
        self.send_header("Content-Length", str(len(payload)))
        for key, value in spec.get("headers", {}).items():
            self.send_header(key, value)
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def log_message(self, fmt: str, *args) -> None:
        return


class ScriptedServer(ThreadingHTTPServer):
    def __init__(self, script: list[dict]) -> None:
        super().__init__(("127.0.0.1", 0), Handler)
        self.script = list(script)
        self.seen: list[tuple[list[tuple[str, str]], bytes]] = []
        self.stop = threading.Event()


class _ServerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.httpd: ScriptedServer | None = None
        self.thread: threading.Thread | None = None

    def tearDown(self) -> None:
        if self.httpd is not None:
            self.httpd.stop.set()
            self.httpd.shutdown()
            self.httpd.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2)

    def _start(self, script: list[dict]) -> str:
        self.httpd = ScriptedServer(script)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        port = self.httpd.server_address[1]
        return f"http://127.0.0.1:{port}/mcp"

    def _env(self, home: Path, **extra: str) -> dict[str, str]:
        env = os.environ.copy()
        for key in (
            "CONTEXT_LEDGER_MCP_TOKEN",
            "CONTEXT_LEDGER_MCP_URL",
            "CONTEXT_LEDGER_MCP_TIMEOUT",
            "CONTEXT_LEDGER_DATA",
            "CONTEXT_LEDGER_CURSOR_HOME",
        ):
            env.pop(key, None)
        env["HOME"] = str(home)
        env["CONTEXT_LEDGER_PYTHON"] = sys.executable
        env.update(extra)
        return env

    def _bridge(self, stdin: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-u", str(BRIDGE)],
            input=stdin,
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )

    def _launcher(self, args: list[str], env: dict[str, str], stdin: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(LAUNCHER), *args],
            input=stdin,
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )

    def _assert_token_hidden(self, proc: subprocess.CompletedProcess[str], token: str = TOKEN) -> None:
        self.assertNotIn(token, proc.stdout)
        self.assertNotIn(token, proc.stderr)
        self.assertNotIn("Bearer", proc.stdout)
        self.assertNotIn("Bearer", proc.stderr)

    def _auth(self, index: int = 0) -> str:
        assert self.httpd is not None
        headers = dict(self.httpd.seen[index][0])
        return headers.get("Authorization", "")


class BridgeTests(_ServerCase):
    def test_json_reply_and_session_headers(self) -> None:
        url = self._start(
            [
                {
                    "status": 200,
                    "body": json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}).encode(),
                    "headers": {
                        "Mcp-Session-Id": "sess-abc",
                        "Mcp-Protocol-Version": "2025-06-18",
                    },
                },
                {
                    "status": 200,
                    "body": json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"tools": ["find"]}}).encode(),
                },
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            env = self._env(
                Path(tmp),
                CONTEXT_LEDGER_MCP_TOKEN=TOKEN,
                CONTEXT_LEDGER_MCP_URL=url,
            )
            proc = self._bridge(
                '{"jsonrpc":"2.0","id":1,"method":"initialize"}\n'
                '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n',
                env,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self._assert_token_hidden(proc)
        lines = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual(lines[0]["result"], {"ok": True})
        self.assertEqual(lines[1]["id"], 2)
        self.assertEqual(self._auth(0), f"Bearer {TOKEN}")
        self.assertNotIn("Mcp-Session-Id", dict(self.httpd.seen[0][0]))
        second = dict(self.httpd.seen[1][0])
        self.assertEqual(second.get("Mcp-Session-Id"), "sess-abc")
        self.assertEqual(second.get("Mcp-Protocol-Version"), "2025-06-18")
        self.assertEqual(second.get("Accept"), "application/json, text/event-stream")
        self.assertEqual(second.get("Content-Type"), "application/json")

    def test_sse_reply(self) -> None:
        sse = (
            "event: message\n"
            'data: {"jsonrpc":"2.0","id":7,"result":{"n":1}}\n'
            "\n"
            "event: message\n"
            'data: {"jsonrpc":"2.0","id":7,\n'
            'data: "result":{"n":2}}\n'
            "\n"
        )
        url = self._start(
            [{"status": 200, "content_type": "text/event-stream", "body": sse.encode()}]
        )
        with tempfile.TemporaryDirectory() as tmp:
            env = self._env(Path(tmp), CONTEXT_LEDGER_MCP_TOKEN=TOKEN, CONTEXT_LEDGER_MCP_URL=url)
            proc = self._bridge('{"jsonrpc":"2.0","id":7,"method":"tools/list"}\n', env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self._assert_token_hidden(proc)
        lines = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual([line["result"]["n"] for line in lines], [1, 2])

    def test_202_notification_writes_nothing(self) -> None:
        url = self._start([{"status": 202, "body": b""}])
        with tempfile.TemporaryDirectory() as tmp:
            env = self._env(Path(tmp), CONTEXT_LEDGER_MCP_TOKEN=TOKEN, CONTEXT_LEDGER_MCP_URL=url)
            proc = self._bridge('{"jsonrpc":"2.0","method":"notifications/initialized"}\n', env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self._assert_token_hidden(proc)
        self.assertEqual(len(self.httpd.seen), 1)

    def test_401_request_is_jsonrpc_error(self) -> None:
        url = self._start([{"status": 401, "body": b'{"ok":false,"error":{"code":"UNAUTHORIZED"}}'}])
        with tempfile.TemporaryDirectory() as tmp:
            env = self._env(Path(tmp), CONTEXT_LEDGER_MCP_TOKEN=TOKEN, CONTEXT_LEDGER_MCP_URL=url)
            proc = self._bridge('{"jsonrpc":"2.0","id":5,"method":"find"}\n', env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self._assert_token_hidden(proc)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["id"], 5)
        self.assertEqual(payload["error"]["message"], "UNAUTHORIZED")
        self.assertNotIn("ok", payload)

    def test_401_notification_is_stderr_only(self) -> None:
        url = self._start([{"status": 401, "body": b""}])
        with tempfile.TemporaryDirectory() as tmp:
            env = self._env(Path(tmp), CONTEXT_LEDGER_MCP_TOKEN=TOKEN, CONTEXT_LEDGER_MCP_URL=url)
            proc = self._bridge('{"jsonrpc":"2.0","method":"notifications/initialized"}\n', env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self._assert_token_hidden(proc)
        err = json.loads(proc.stderr)
        self.assertEqual(err["error"]["code"], "UNAUTHORIZED")

    def test_http_error_includes_status(self) -> None:
        url = self._start([{"status": 503, "body": b"nope"}])
        with tempfile.TemporaryDirectory() as tmp:
            env = self._env(Path(tmp), CONTEXT_LEDGER_MCP_TOKEN=TOKEN, CONTEXT_LEDGER_MCP_URL=url)
            proc = self._bridge('{"jsonrpc":"2.0","id":9,"method":"find"}\n', env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self._assert_token_hidden(proc)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["id"], 9)
        self.assertEqual(payload["error"]["message"], "HTTP 503")
        self.assertNotIn("nope", proc.stdout)

    def test_unreachable_names_host(self) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        url = f"http://127.0.0.1:{port}/mcp"
        with tempfile.TemporaryDirectory() as tmp:
            env = self._env(Path(tmp), CONTEXT_LEDGER_MCP_TOKEN=TOKEN, CONTEXT_LEDGER_MCP_URL=url)
            proc = self._bridge('{"jsonrpc":"2.0","id":"abc","method":"find"}\n', env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self._assert_token_hidden(proc)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["id"], "abc")
        self.assertIn("UNREACHABLE", payload["error"]["message"])
        self.assertIn("127.0.0.1", payload["error"]["message"])
        self.assertNotIn(TOKEN, payload["error"]["message"])

    def test_timeout_names_host(self) -> None:
        url = self._start([{"hang": True}])
        with tempfile.TemporaryDirectory() as tmp:
            env = self._env(
                Path(tmp),
                CONTEXT_LEDGER_MCP_TOKEN=TOKEN,
                CONTEXT_LEDGER_MCP_URL=url,
                CONTEXT_LEDGER_MCP_TIMEOUT="0.3",
            )
            proc = self._bridge('{"jsonrpc":"2.0","id":4,"method":"find"}\n', env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self._assert_token_hidden(proc)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["id"], 4)
        self.assertIn("TIMEOUT", payload["error"]["message"])
        self.assertIn("127.0.0.1", payload["error"]["message"])

    def test_missing_token_fails_at_startup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = self._env(Path(tmp))
            proc = self._bridge('{"jsonrpc":"2.0","id":1,"method":"initialize"}\n', env)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        err = json.loads(proc.stderr)
        self.assertEqual(err["error"]["code"], "TOKEN_MISSING")
        self.assertIn("CONTEXT_LEDGER_MCP_TOKEN", err["error"]["message"])


class ServeSwitchTests(_ServerCase):
    def test_binding_uses_local_path(self) -> None:
        url = self._start([{"status": 200, "body": b'{"jsonrpc":"2.0","id":1,"result":{}}'}])
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            data = home / "ledger"
            data.mkdir()
            (data / "binding.json").write_text("{}\n", encoding="utf-8")
            env = self._env(home, CONTEXT_LEDGER_MCP_TOKEN=TOKEN, CONTEXT_LEDGER_MCP_URL=url)
            proc = self._launcher(["--data", str(data), "serve"], env, '{"jsonrpc":"2.0","id":1,"method":"initialize"}\n')
            env_default = self._env(
                home,
                CONTEXT_LEDGER_MCP_TOKEN=TOKEN,
                CONTEXT_LEDGER_MCP_URL=url,
                CONTEXT_LEDGER_DATA=str(data),
            )
            proc_default = self._launcher(["serve"], env_default, '{"jsonrpc":"2.0","id":1,"method":"initialize"}\n')
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("UNAVAILABLE", proc.stderr)
        self.assertNotIn("LEDGER_UNBOUND", proc.stderr)
        self._assert_token_hidden(proc)
        self.assertNotEqual(proc_default.returncode, 0)
        self.assertIn("UNAVAILABLE", proc_default.stderr)
        self.assertEqual(self.httpd.seen, [])

    def test_token_without_binding_uses_bridge(self) -> None:
        url = self._start(
            [{"status": 200, "body": json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"bridged": True}}).encode()}]
        )
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            data = home / "missing-ledger"
            env = self._env(home, CONTEXT_LEDGER_MCP_TOKEN=TOKEN, CONTEXT_LEDGER_MCP_URL=url)
            proc = self._launcher(
                ["--data", str(data), "serve"],
                env,
                '{"jsonrpc":"2.0","id":1,"method":"initialize"}\n',
            )
            self.assertFalse(data.exists())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self._assert_token_hidden(proc)
        self.assertEqual(json.loads(proc.stdout)["result"], {"bridged": True})
        self.assertEqual(self._auth(), f"Bearer {TOKEN}")

    def test_neither_binding_nor_token_fails_without_creating_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            data = home / "no-ledger"
            env = self._env(home)
            proc = self._launcher(["--data", str(data), "serve"], env, "{}\n")
            default = self._launcher(["serve"], env, "{}\n")
            self.assertFalse(data.exists())
            self.assertFalse((home / ".context-ledger").exists())
        self.assertNotEqual(proc.returncode, 0)
        err = json.loads(proc.stderr)
        self.assertEqual(err["error"]["code"], "LEDGER_UNBOUND")
        self.assertIn(f"no binding at {data}/binding.json", err["error"]["message"])
        self.assertIn("CONTEXT_LEDGER_MCP_TOKEN", err["error"]["message"])
        self.assertEqual(proc.stdout, "")
        default_err = json.loads(default.stderr)
        self.assertEqual(default_err["error"]["code"], "LEDGER_UNBOUND")
        self.assertIn(str(home / ".context-ledger" / "binding.json"), default_err["error"]["message"])

    def test_serve_http_is_not_the_bridge(self) -> None:
        url = self._start([{"status": 200, "body": b'{"jsonrpc":"2.0","id":1,"result":{}}'}])
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            data = home / "http-data"
            data.mkdir()
            env = self._env(home, CONTEXT_LEDGER_MCP_TOKEN=TOKEN, CONTEXT_LEDGER_MCP_URL=url)
            proc = self._launcher(["--data", str(data), "serve-http"], env)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("UNAVAILABLE", proc.stderr)
        self.assertNotIn("LEDGER_UNBOUND", proc.stderr)
        self.assertEqual(self.httpd.seen, [])
        self._assert_token_hidden(proc)


if __name__ == "__main__":
    unittest.main()
