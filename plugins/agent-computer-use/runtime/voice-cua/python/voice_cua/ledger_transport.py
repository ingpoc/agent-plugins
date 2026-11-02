"""Bounded local transport for the existing Context Ledger MCP server.

This module exposes no model-facing tools. The retained ledger helper owns it.
"""
from __future__ import annotations

import json
import os
import selectors
import subprocess
import threading
import time
from pathlib import Path
from typing import Any


class LedgerUnavailable(RuntimeError):
    pass


class LedgerTransport:
    def __init__(self, package: Path | None = None, data: Path | None = None):
        self.package = package or Path.home() / '.agents/plugins/context-ledger'
        self.data = data or Path.home() / '.context-ledger'
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._id = 0

    def _start(self) -> None:
        try:
            manifest = json.loads((self.package / 'plugin.json').read_text())
            launcher = self.package / 'bin/context-ledger-mcp'
            if manifest.get('name') != 'context-ledger' or not launcher.is_file() or not (self.data / 'binding.json').is_file():
                raise LedgerUnavailable('installed local ledger unavailable')
            env = {'HOME': str(Path.home()), 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin',
                   'CONTEXT_LEDGER_DATA': str(self.data), 'PYTHONUNBUFFERED': '1'}
            self._process = subprocess.Popen([str(launcher), 'serve'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                             stderr=subprocess.DEVNULL, env=env, bufsize=0)
            self._exchange('initialize', {'protocolVersion': '2024-11-05', 'capabilities': {},
                                         'clientInfo': {'name': 'samantha-ledger-helper', 'version': '1'}})
            self._send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        except (OSError, ValueError, TypeError) as exc:
            self.close()
            raise LedgerUnavailable('installed local ledger unavailable') from exc

    def _send(self, obj: dict[str, Any]) -> None:
        assert self._process and self._process.stdin
        data = (json.dumps(obj, separators=(',', ':')) + '\n').encode()
        if len(data) > 16384:
            raise LedgerUnavailable('ledger request too large')
        fd = self._process.stdin.fileno()
        os.set_blocking(fd, False)
        selector = selectors.DefaultSelector()
        try:
            selector.register(fd, selectors.EVENT_WRITE)
            deadline = time.monotonic() + 3
            view = memoryview(data)
            while view:
                left = deadline - time.monotonic()
                if left <= 0 or not selector.select(left):
                    raise LedgerUnavailable('ledger write timed out')
                try:
                    count = os.write(fd, view)
                except BlockingIOError:
                    continue
                if count <= 0:
                    raise LedgerUnavailable('ledger closed input')
                view = view[count:]
        finally:
            selector.close()


    def _exchange(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._id += 1
        request_id = self._id
        self._send({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})
        assert self._process and self._process.stdout
        selector = selectors.DefaultSelector()
        try:
            selector.register(self._process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + 3
            line = bytearray()
            while not line.endswith(b'\n'):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(timeout=remaining):
                    raise LedgerUnavailable('ledger timed out')
                chunk = os.read(self._process.stdout.fileno(), 1)
                if not chunk or len(line) >= 65536:
                    raise LedgerUnavailable('invalid ledger response')
                line.extend(chunk)
            response = json.loads(line)
            if response.get('id') != request_id or 'error' in response:
                raise LedgerUnavailable('ledger request failed')
            return response.get('result', {})
        finally:
            selector.close()

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in {'find', 'record'}:
            raise ValueError('unsupported ledger operation')
        with self._lock:
            try:
                if self._process is None or self._process.poll() is not None:
                    self._start()
                result = self._exchange('tools/call', {'name': name, 'arguments': arguments})
                if result.get('isError'):
                    raise LedgerUnavailable('ledger refused request')
                content = result.get('content', [])
                if len(content) != 1 or content[0].get('type') != 'text':
                    raise LedgerUnavailable('invalid ledger result')
                payload = json.loads(content[0]['text'])
                if not isinstance(payload, dict):
                    raise LedgerUnavailable('invalid ledger result')
                return payload
            except (OSError, ValueError, TypeError, KeyError, LedgerUnavailable) as exc:
                self.close()
                raise LedgerUnavailable('ledger unavailable') from exc

    def close(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            if process.stdin:
                process.stdin.close()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if process.stdout:
                process.stdout.close()
