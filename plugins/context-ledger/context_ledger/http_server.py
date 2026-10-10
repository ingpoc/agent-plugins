"""Token-protected Streamable HTTP facade: same four tools, loopback only."""

from __future__ import annotations

import hmac
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from context_ledger.contracts import MAX_REQUEST_LINE, canonical_json

TOKEN_ENV = "CONTEXT_LEDGER_MCP_TOKEN"
HOSTS_ENV = "CONTEXT_LEDGER_MCP_ALLOWED_HOSTS"
BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
MCP_PATH = "/mcp"
MIN_TOKEN_CHARS = 32

_UNAUTHORIZED = canonical_json({"ok": False, "error": {"code": "UNAUTHORIZED"}}).encode()


def _auth_logger() -> logging.Logger:
    log = logging.getLogger("context_ledger.http.auth")
    if not log.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(handler)
    log.setLevel(logging.WARNING)
    log.propagate = False
    return log


def _header(scope: dict[str, Any], name: bytes) -> bytes | None:
    for key, value in scope.get("headers") or []:
        if key.lower() == name:
            return value
    return None


class BearerAuth:
    """Pure ASGI gate. Rejections are logged without the header value."""

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self._expected = b"Bearer " + token.encode("utf-8")
        self._log = _auth_logger()

    def _reason(self, scope: dict[str, Any]) -> str | None:
        raw = _header(scope, b"authorization")
        if raw is None:
            return "missing"
        if not raw.startswith(b"Bearer ") or len(raw) <= len(b"Bearer "):
            return "malformed"
        if not hmac.compare_digest(raw, self._expected):
            return "mismatch"
        return None

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        reason = self._reason(scope)
        if reason is None:
            await self.app(scope, receive, send)
            return
        client = (scope.get("client") or ("-", 0))[0]
        fwd = _header(scope, b"cf-connecting-ip") or _header(scope, b"x-forwarded-for") or b"-"
        self._log.warning(
            canonical_json(
                {
                    "event": "auth_rejected",
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "client": client,
                    "forwarded_for": fwd.decode("latin-1")[:200],
                    "method": scope.get("method", "-"),
                    "path": scope.get("path", "-")[:200],
                    "reason": reason,
                }
            )
        )
        if scope["type"] != "http":
            await send({"type": "websocket.close", "code": 1008})
            return
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b"Bearer"),
                    (b"content-length", str(len(_UNAUTHORIZED)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": _UNAUTHORIZED})


_NOT_FOUND = canonical_json(
    {"ok": False, "error": {"code": "NOT_FOUND", "retryable": False}}
).encode()


def _is_discovery(path: str) -> bool:
    """OAuth/OIDC probes. 401 here makes clients hang in discovery."""
    return (
        path in {"/register", "/.well-known"}
        or path.startswith("/register/")
        or path.startswith("/.well-known/")
    )


def _media_types(accept: str) -> tuple[bool, bool]:
    types = [part.strip().split(";")[0].strip().lower() for part in accept.split(",") if part.strip()]
    wild = "*/*" in types
    has_json = wild or any(item in ("application/json", "application/*") for item in types)
    has_sse = wild or any(item in ("text/event-stream", "text/*") for item in types)
    return has_json, has_sse


def _header_pairs(scope: dict[str, Any]) -> list[tuple[bytes, bytes]]:
    return [(key, value) for key, value in (scope.get("headers") or [])]


async def _send_bytes(
    send: Any,
    status: int,
    body: bytes,
    content_type: bytes,
    extra: list[tuple[bytes, bytes]] | None = None,
) -> None:
    headers = [
        (b"content-type", content_type),
        (b"content-length", str(len(body)).encode()),
        *(extra or []),
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def _jsonrpc_error(message: str) -> bytes:
    return canonical_json(
        {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": message},
        }
    ).encode()


def _as_sse(payload: bytes) -> bytes:
    text = payload.decode("utf-8")
    data = "".join(f"data: {line}\n" for line in text.split("\n"))
    return f"event: message\n{data}\n".encode()


class DiscoveryShield:
    """Answer OAuth discovery with 404 before the bearer gate."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and _is_discovery(scope.get("path") or ""):
            await _send_bytes(send, 404, _NOT_FOUND, b"application/json")
            return
        await self.app(scope, receive, send)


class StatelessMethods:
    """Stateless MCP has no session stream and no session to terminate."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and scope.get("path") == MCP_PATH:
            method = scope.get("method")
            if method == "GET":
                await _send_bytes(
                    send,
                    405,
                    _jsonrpc_error("Method Not Allowed: Server does not offer an SSE stream"),
                    b"application/json",
                )
                return
            if method == "DELETE":
                await _send_bytes(
                    send,
                    405,
                    _jsonrpc_error("Method Not Allowed: Session termination not supported"),
                    b"application/json",
                )
                return
        await self.app(scope, receive, send)


class AcceptNegotiate:
    """json_response mode rejects event-stream-only Accept with 406.

    Inject application/json so the SDK answers, then encode that JSON body as
    one SSE message when the client did not accept application/json. JSON-only
    and dual Accept stay application/json. 202 notifications are not rewritten.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        headers = _header_pairs(scope)
        accept = ", ".join(value.decode("latin-1") for key, value in headers if key.lower() == b"accept")
        has_json, has_sse = _media_types(accept)
        sse_only = has_sse and not has_json
        if not sse_only:
            await self.app(scope, receive, send)
            return
        rewritten = [(key, value) for key, value in headers if key.lower() != b"accept"]
        rewritten.append((b"accept", b"application/json, text/event-stream"))
        scoped = dict(scope)
        scoped["headers"] = rewritten

        async def convert(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start" and message.get("status") == 200:
                ctype = b""
                for key, value in message.get("headers") or []:
                    if key.lower() == b"content-type":
                        ctype = value
                        break
                if ctype.split(b";", 1)[0].strip().lower() == b"application/json":
                    convert.pending = message
                    return
            if message["type"] == "http.response.body" and convert.pending is not None:
                convert.chunks.extend(message.get("body") or b"")
                if message.get("more_body"):
                    return
                sse = _as_sse(bytes(convert.chunks))
                start = convert.pending
                kept = [
                    (key, value)
                    for key, value in (start.get("headers") or [])
                    if key.lower() not in (b"content-type", b"content-length")
                ]
                kept.append((b"content-type", b"text/event-stream"))
                kept.append((b"content-length", str(len(sse)).encode()))
                convert.pending = None
                await send({"type": "http.response.start", "status": 200, "headers": kept})
                await send({"type": "http.response.body", "body": sse})
                return
            await send(message)

        convert.pending = None
        convert.chunks = bytearray()
        await self.app(scoped, receive, convert)
        if convert.pending is not None:
            raise RuntimeError("SSE negotiation held response headers with no body")


def _fail(code: str) -> int:
    # Fail loud with the code only; never echo the token or its length.
    sys.stderr.write(canonical_json({"ok": False, "error": {"code": code, "retryable": False}}) + "\n")
    return 2


def _parse(args: list[str]) -> tuple[int, list[str]] | None:
    port = DEFAULT_PORT
    hosts = [h.strip() for h in os.environ.get(HOSTS_ENV, "").split(",") if h.strip()]
    i = 0
    while i < len(args):
        if args[i] in ("--port", "--allowed-host") and i + 1 < len(args):
            value = args[i + 1]
            if args[i] == "--port":
                if not value.isdigit() or not 0 < int(value) < 65536:
                    return None
                port = int(value)
            else:
                if not value or "/" in value or ":" in value:
                    return None
                hosts.append(value)
            i += 2
            continue
        return None
    return port, hosts


def transport_security(hosts: list[str]) -> Any:
    from mcp.server.transport_security import TransportSecuritySettings

    allowed_hosts = ["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*"]
    allowed_origins = ["http://127.0.0.1:*", "http://localhost:*"]
    for host in hosts:
        allowed_hosts += [host, f"{host}:*"]
        allowed_origins += [f"https://{host}", f"https://{host}:*"]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


def build_app(store: Any, token: str, hosts: list[str]) -> Any:
    from context_ledger.server import build_server

    server = build_server(store)
    app = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        max_request_body_size=MAX_REQUEST_LINE,
        transport_security=transport_security(hosts),
        host=BIND_HOST,
    )
    return DiscoveryShield(BearerAuth(StatelessMethods(AcceptNegotiate(app)), token))


def serve_http(plugin_data: Path, args: list[str]) -> int:
    token = os.environ.pop(TOKEN_ENV, "")
    if len(token) < MIN_TOKEN_CHARS:
        return _fail("UNAUTHENTICATED_CONFIG")
    parsed = _parse(args)
    if parsed is None:
        return _fail("INVALID_ARGUMENT")
    port, hosts = parsed

    import uvicorn

    from context_ledger.store import Store

    store = Store(plugin_data, actor_channel="agent")
    store.open()
    app = build_app(store, token, hosts)
    del token
    _auth_logger().warning(
        canonical_json({"event": "listening", "host": BIND_HOST, "port": port, "path": MCP_PATH})
    )
    config = uvicorn.Config(
        app,
        host=BIND_HOST,
        port=port,
        lifespan="on",
        access_log=False,
        log_level="warning",
        log_config=None,
        server_header=False,
    )
    uvicorn.Server(config).run()
    return 0
