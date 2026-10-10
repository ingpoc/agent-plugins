#!/usr/bin/env python3
"""serve-http: bearer gate, loopback bind, same four tools. Stdlib unittest."""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import importlib.util
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _reexec_locked_runtime() -> None:
    """This file's server imports mcp 2.2. Discover must still run under plain python3."""
    if importlib.util.find_spec("mcp.server.mcpserver") is not None:
        return
    sha = hashlib.sha256((ROOT / "requirements.lock").read_bytes()).hexdigest()
    py = Path.home() / ".context-ledger" / "runtime" / sha / "bin" / "python3"
    if not py.is_file():
        raise SystemExit(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "UNAVAILABLE",
                        "retryable": False,
                        "message": f"mcp.server.mcpserver is not importable and {py} is missing",
                    },
                }
            )
        )
    sys.stderr.write(f"re-exec {py} because this interpreter has no mcp.server.mcpserver\n")
    if "unittest" in sys.argv[0]:
        os.execv(str(py), [str(py), "-m", "unittest", *sys.argv[1:]])
    os.execv(str(py), [str(py), *sys.argv])


_reexec_locked_runtime()
# Same argv the bootstrap re-execs into (bin/ on sys.path would shadow the package).
CLI = [sys.executable, "-m", "context_ledger"]


def _env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("CONTEXT_LEDGER_MCP_TOKEN", None)
    env.pop("CONTEXT_LEDGER_MCP_ALLOWED_HOSTS", None)
    env.update(
        {
            "PYTHONPATH": str(ROOT),
            "PYTHONNOUSERSITE": "1",
        }
    )
    env.update(extra or {})
    return env


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _init(data: Path) -> None:
    proc = subprocess.run(
        [*CLI, "--data", str(data), "init", "--scope", "test", "--actor", "test"],
        env=_env(),
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


def _post(port: int, body: dict, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/mcp",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **headers,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}


class StartupTests(unittest.TestCase):
    def _start(self, token: str | None, extra: list[str] | None = None) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir(mode=0o700)
            env = _env({"CONTEXT_LEDGER_MCP_TOKEN": token} if token is not None else None)
            return subprocess.run(
                [*CLI, "--data", str(data), "serve-http", *(extra or [])],
                env=env,
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                timeout=30,
            )

    def test_refuses_without_token(self) -> None:
        for token in (None, "", "short-token-value"):
            proc = self._start(token)
            self.assertEqual(proc.returncode, 2)
            self.assertIn("UNAUTHENTICATED_CONFIG", proc.stderr)
            if token:
                self.assertNotIn(token, proc.stderr)

    def test_refuses_host_override(self) -> None:
        proc = self._start(secrets.token_hex(32), ["--host", "0.0.0.0"])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("INVALID_ARGUMENT", proc.stderr)


class LiveServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        data = Path(cls.tmp.name) / "data"
        data.mkdir(mode=0o700)
        _init(data)
        cls.token = secrets.token_hex(32)
        cls.port = _free_port()
        cls.proc = subprocess.Popen(
            [
                *CLI,
                "--data",
                str(data),
                "serve-http",
                "--port",
                str(cls.port),
                "--allowed-host",
                "ledger.example.test",
            ],
            env=_env({"CONTEXT_LEDGER_MCP_TOKEN": cls.token}),
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", cls.port), timeout=0.5):
                    return
            except OSError:
                if cls.proc.poll() is not None:
                    raise RuntimeError(cls.proc.stderr.read())
                time.sleep(0.2)
        raise RuntimeError("serve-http did not listen")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.proc.terminate()
        try:
            cls.stderr = cls.proc.communicate(timeout=10)[1]
        except subprocess.TimeoutExpired:
            cls.proc.kill()
            cls.stderr = cls.proc.communicate()[1]
        cls.tmp.cleanup()
        assert cls.token not in cls.stderr
        assert '"reason":"missing"' in cls.stderr, cls.stderr
        assert '"reason":"mismatch"' in cls.stderr, cls.stderr
        assert '"reason":"malformed"' in cls.stderr, cls.stderr

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def test_rejects_missing_wrong_and_non_bearer(self) -> None:
        cases = [
            {},
            {"Authorization": "Bearer " + secrets.token_hex(32)},
            {"Authorization": "Basic " + self.token},
            {"Authorization": "Bearer " + self.token[:-1]},
        ]
        for headers in cases:
            status, resp_headers, body = _post(self.port, INIT, headers)
            self.assertEqual(status, 401)
            self.assertEqual(resp_headers.get("www-authenticate") or resp_headers.get("WWW-Authenticate"), "Bearer")
            self.assertEqual(json.loads(body)["error"]["code"], "UNAUTHORIZED")

    def test_initialize_and_exactly_four_tools(self) -> None:
        status, _, body = _post(self.port, INIT, self._auth())
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["result"]["serverInfo"]["name"], "context-ledger")
        status, _, body = _post(self.port, LIST, self._auth())
        self.assertEqual(status, 200, body)
        names = sorted(t["name"] for t in json.loads(body)["result"]["tools"])
        self.assertEqual(names, ["append_event", "find", "get", "record"])

    def test_host_allow_list(self) -> None:
        status, _, _ = _post(self.port, LIST, {**self._auth(), "Host": "evil.example.test"})
        self.assertNotEqual(status, 200)
        status, _, body = _post(self.port, LIST, {**self._auth(), "Host": "ledger.example.test"})
        self.assertEqual(status, 200, body)

    def test_find_round_trip(self) -> None:
        call = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "find", "arguments": {"query": "anything", "entities": [], "constraints": []}},
        }
        status, _, body = _post(self.port, call, self._auth())
        self.assertEqual(status, 200, body)
        result = json.loads(body)["result"]
        self.assertFalse(result.get("isError"), result)

    def test_cursor_client_handshake(self) -> None:
        status, headers, body = _raw(self.port, "POST", "/mcp", INIT, {"Content-Type": "application/json"})
        self.assertEqual(status, 401, body)
        self.assertEqual(headers.get("www-authenticate"), "Bearer")
        self.assertEqual(json.loads(body)["error"]["code"], "UNAUTHORIZED")

        status, headers, _ = _raw(
            self.port,
            "GET",
            "/mcp",
            headers={"Accept": "text/event-stream"},
        )
        self.assertEqual(status, 401)
        self.assertEqual(headers.get("www-authenticate"), "Bearer")

        probes = [
            ("GET", "/.well-known/oauth-protected-resource"),
            ("GET", "/.well-known/oauth-protected-resource/mcp"),
            ("GET", "/.well-known/oauth-authorization-server"),
            ("GET", "/.well-known/openid-configuration"),
            ("POST", "/register"),
        ]
        for method, path in probes:
            status, headers, body = _raw(self.port, method, path, {} if method == "POST" else None)
            self.assertEqual(status, 404, (method, path, body))
            self.assertNotIn("www-authenticate", headers)
            self.assertEqual(json.loads(body)["error"]["code"], "NOT_FOUND")

        sse_headers = {
            **self._auth(),
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "mcp-protocol-version": "2025-06-18",
        }
        status, headers, body = _raw(self.port, "POST", "/mcp", INIT, sse_headers)
        self.assertEqual(status, 200, body)
        self.assertTrue(headers.get("content-type", "").startswith("text/event-stream"), headers)
        self.assertEqual(_sse_json(body)["result"]["serverInfo"]["name"], "context-ledger")

        status, headers, body = _raw(
            self.port,
            "POST",
            "/mcp",
            INIT,
            {**self._auth(), "Content-Type": "application/json", "Accept": "application/json"},
        )
        self.assertEqual(status, 200, body)
        self.assertTrue(headers.get("content-type", "").startswith("application/json"), headers)
        self.assertEqual(json.loads(body)["result"]["serverInfo"]["name"], "context-ledger")

        note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        status, _, body = _raw(self.port, "POST", "/mcp", note, sse_headers)
        self.assertEqual(status, 202, body)

        status, headers, body = _raw(self.port, "POST", "/mcp", LIST, sse_headers)
        self.assertEqual(status, 200, body)
        names = sorted(tool["name"] for tool in _sse_json(body)["result"]["tools"])
        self.assertEqual(names, ["append_event", "find", "get", "record"])

        find = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "find", "arguments": {"query": "cursor", "entities": [], "constraints": []}},
        }
        status, _, body = _raw(self.port, "POST", "/mcp", find, sse_headers)
        self.assertEqual(status, 200, body)
        self.assertFalse(_sse_json(body)["result"].get("isError"))

        status, headers, body = _raw(
            self.port,
            "GET",
            "/mcp",
            headers={**self._auth(), "Accept": "text/event-stream"},
        )
        self.assertEqual(status, 405, body)
        self.assertIn("SSE stream", json.loads(body)["error"]["message"])
        status, _, body = _raw(self.port, "DELETE", "/mcp", headers=self._auth())
        self.assertEqual(status, 405, body)
        self.assertIn("Session termination not supported", json.loads(body)["error"]["message"])

    def test_sdk_streamable_http_client(self) -> None:
        try:
            import httpx2
            from mcp.client.session import ClientSession
            from mcp.client.streamable_http import streamable_http_client
        except ImportError:
            try:
                import httpx2
                from mcp.client.session import ClientSession
                from mcp.client.streamable_http import streamablehttp_client as streamable_http_client
            except ImportError:
                self.skipTest("mcp streamable http client is not installed")

        token = self.token
        port = self.port

        async def run() -> list[str]:
            async with httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {token}"},
                timeout=20,
                trust_env=False,
            ) as http:
                async with streamable_http_client(
                    f"http://127.0.0.1:{port}/mcp",
                    http_client=http,
                ) as streams:
                    read_stream, write_stream = streams[0], streams[1]
                    async with ClientSession(read_stream, write_stream) as session:
                        init = await session.initialize()
                        if init.server_info.name != "context-ledger":
                            raise AssertionError(init.server_info)
                        listed = await session.list_tools()
                        return sorted(tool.name for tool in listed.tools)

        self.assertEqual(asyncio.run(run()), ["append_event", "find", "get", "record"])


def _raw(
    port: int,
    method: str,
    path: str,
    body: dict | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        payload = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=payload, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        hdrs = {key.lower(): value for key, value in resp.getheaders()}
        return resp.status, hdrs, data
    finally:
        conn.close()


def _sse_json(body: bytes) -> dict:
    data = [line[5:].lstrip() for line in body.decode().splitlines() if line.startswith("data:")]
    if not data:
        raise AssertionError(body)
    return json.loads("\n".join(data))


if __name__ == "__main__":
    unittest.main()
