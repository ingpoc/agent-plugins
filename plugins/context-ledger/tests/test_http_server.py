#!/usr/bin/env python3
"""serve-http: bearer gate, loopback bind, same four tools. Stdlib unittest."""

from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
