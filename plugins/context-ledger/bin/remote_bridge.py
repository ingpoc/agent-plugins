#!/usr/bin/env python3
"""Stdio JSON-RPC bridge to the remote Streamable HTTP ledger.

Reads one JSON-RPC message per stdin line and POSTs it to the serve-http URL.
Never logs the bearer token. Exits 0 on stdin EOF.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from urllib.parse import urlparse

DEFAULT_URL = "https://gurusharan-mac-codex.taild2e98e.ts.net/mcp"
TOKEN_ENV = "CONTEXT_LEDGER_MCP_TOKEN"
URL_ENV = "CONTEXT_LEDGER_MCP_URL"
TIMEOUT_ENV = "CONTEXT_LEDGER_MCP_TIMEOUT"
PASSTHROUGH = ("Mcp-Session-Id", "Mcp-Protocol-Version")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Fail the request instead of forwarding Authorization to another URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


def _stderr(code: str, message: str, *, retryable: bool) -> None:
    sys.stderr.write(
        json.dumps(
            {
                "ok": False,
                "error": {"code": code, "retryable": retryable, "message": message},
            }
        )
        + "\n"
    )
    sys.stderr.flush()


def _stdout(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _host(url: str) -> str:
    return urlparse(url).hostname or "unknown-host"


def _fail_startup(code: str, message: str) -> int:
    _stderr(code, message, retryable=False)
    return 2


def _timeout() -> float | None:
    raw = os.environ.get(TIMEOUT_ENV, "").strip()
    if raw == "":
        return 30.0
    try:
        value = float(raw)
    except ValueError:
        _fail_startup("INVALID_ARGUMENT", f"{TIMEOUT_ENV} is not a positive number")
        return None
    if value <= 0 or value != value or value == float("inf"):
        _fail_startup("INVALID_ARGUMENT", f"{TIMEOUT_ENV} is not a positive number")
        return None
    return value


def _url() -> str | None:
    raw = os.environ.get(URL_ENV)
    url = DEFAULT_URL if raw is None or raw.strip() == "" else raw.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        _fail_startup("INVALID_ARGUMENT", f"{URL_ENV} must be an http(s) URL with a host")
        return None
    return url


def _request_id(message: dict) -> tuple[object, bool]:
    if "id" not in message or message["id"] is None:
        return None, False
    return message["id"], True


def _capture(headers, session: dict[str, str]) -> None:
    if headers is None:
        return
    for name in PASSTHROUGH:
        value = headers.get(name)
        if value:
            session[name] = value


def _parse_sse(body: bytes) -> list[dict]:
    text = body.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    messages: list[dict] = []
    for event in text.split("\n\n"):
        data_lines: list[str] = []
        for line in event.splitlines():
            if line.endswith("\r"):
                line = line[:-1]
            if line.startswith("data:"):
                value = line[5:]
                if value.startswith(" "):
                    value = value[1:]
                data_lines.append(value)
        if not data_lines:
            continue
        payload = "\n".join(data_lines).strip()
        if not payload or payload == "[DONE]":
            continue
        parsed = json.loads(payload)
        if isinstance(parsed, dict):
            messages.append(parsed)
        elif isinstance(parsed, list):
            for item in parsed:
                if not isinstance(item, dict):
                    raise json.JSONDecodeError("JSON-RPC message is not an object", payload, 0)
                messages.append(item)
        else:
            raise json.JSONDecodeError("JSON-RPC message is not an object", payload, 0)
    return messages


def _parse_json_body(body: bytes) -> list[dict]:
    parsed = json.loads(body.decode("utf-8"))
    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list):
        messages: list[dict] = []
        for item in parsed:
            if not isinstance(item, dict):
                raise json.JSONDecodeError("JSON-RPC message is not an object", body.decode("utf-8"), 0)
            messages.append(item)
        return messages
    raise json.JSONDecodeError("JSON-RPC message is not an object", body.decode("utf-8"), 0)


def _rpc_error(req_id: object, message: str) -> None:
    _stdout(
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32000, "message": message},
        }
    )


def _fail(is_request: bool, req_id: object, code: str, message: str, *, retryable: bool) -> None:
    if is_request:
        _rpc_error(req_id, message)
        return
    _stderr(code, message, retryable=retryable)


def _cause(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.URLError) and not isinstance(exc, urllib.error.HTTPError):
        reason = exc.reason
        if isinstance(reason, BaseException):
            return _cause(reason)
        return "UNREACHABLE"
    if isinstance(exc, TimeoutError):
        return "TIMEOUT"
    return "UNREACHABLE"


def _post(url: str, payload: bytes, token: str, timeout: float, session: dict[str, str]):
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        **session,
    }
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    handlers: list[urllib.request.BaseHandler] = [_NoRedirect()]
    if url.lower().startswith("https://"):
        handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def _forward(message: dict, url: str, token: str, timeout: float, session: dict[str, str]) -> None:
    req_id, is_request = _request_id(message)
    host = _host(url)
    try:
        status, headers, body = _post(
            url,
            json.dumps(message).encode("utf-8"),
            token,
            timeout,
            session,
        )
    except Exception as exc:  # noqa: BLE001 — network failures must become a named JSON-RPC error
        cause = _cause(exc)
        _fail(is_request, req_id, cause, f"{cause} {host}", retryable=True)
        return
    _capture(headers, session)
    if status == 401:
        _fail(is_request, req_id, "UNAUTHORIZED", "UNAUTHORIZED", retryable=False)
        return
    if status == 202 or not body.strip():
        return
    if status < 200 or status >= 300:
        _fail(is_request, req_id, "HTTP_ERROR", f"HTTP {status}", retryable=status >= 500)
        return
    content_type = ""
    if headers is not None:
        content_type = headers.get("Content-Type", "") or ""
    try:
        if "text/event-stream" in content_type.lower():
            messages = _parse_sse(body)
        else:
            messages = _parse_json_body(body)
    except (UnicodeError, json.JSONDecodeError):
        _fail(is_request, req_id, "INVALID_RESPONSE", "INVALID_RESPONSE", retryable=False)
        return
    for item in messages:
        _stdout(item)


def main() -> int:
    token = os.environ.get(TOKEN_ENV, "")
    if token.strip() == "":
        return _fail_startup("TOKEN_MISSING", "CONTEXT_LEDGER_MCP_TOKEN is empty; remote bridge cannot start")
    url = _url()
    if url is None:
        return 2
    timeout = _timeout()
    if timeout is None:
        return 2
    session: dict[str, str] = {}
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            _stderr("PARSE_ERROR", "stdin line is not JSON", retryable=False)
            continue
        if not isinstance(message, dict):
            _stderr("PARSE_ERROR", "stdin line is not a JSON object", retryable=False)
            continue
        _forward(message, url, token, timeout, session)
    return 0


if __name__ == "__main__":
    sys.exit(main())
