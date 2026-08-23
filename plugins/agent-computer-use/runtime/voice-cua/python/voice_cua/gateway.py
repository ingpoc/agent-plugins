"""Local HTTP gateway: Realtime client-secret, tools, island SSE, confirm."""

from __future__ import annotations

import json
import hmac
import os
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from voice_cua import SYSTEM_INSTRUCTIONS, __version__
from voice_cua.activity_log import log_path, tail_events
from voice_cua.startup_trace import tail_startup
from voice_cua.auth import openai_configured, openai_status, resolve_openai_api_key
from voice_cua.inventory import build_inventory, find_by_label
from voice_cua.island_state import ISLAND
from voice_cua.tools import dispatch, tool_definitions
from voice_cua.voice_settings import load_settings

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = int(os.environ.get("VOICE_CUA_PORT", "8765"))
_HTTPD: ThreadingHTTPServer | None = None


def realtime_model() -> str:
    return os.environ.get("VOICE_CUA_REALTIME_MODEL") or load_settings()["realtime_model"]


def request_shutdown() -> None:
    """Stop gateway and exit the voice stack process (CUAService Samantha OFF)."""
    global _HTTPD
    httpd = _HTTPD

    def _exit_stack() -> None:
        import time

        time.sleep(0.05)
        if httpd is not None:
            try:
                httpd.shutdown()
            except OSError:
                pass
        os._exit(0)

    threading.Thread(target=_exit_stack, name="voice-shutdown", daemon=True).start()


def mint_client_secret() -> dict[str, Any]:
    api_key = resolve_openai_api_key()
    model = realtime_model()
    body = json.dumps({
        "session": {
            "type": "realtime",
            "model": model,
        }
    }).encode("utf-8")
    # OpenAI ephemeral client secrets for Realtime (GA path).
    req = urllib.request.Request(
        "https://api.openai.com/v1/realtime/client_secrets",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Fallback: sessions endpoint used by some SDK versions
        err_body = exc.read().decode("utf-8", errors="replace")
        if exc.code in {404, 405}:
            return _mint_session_fallback(api_key)
        raise RuntimeError(f"client_secret mint failed HTTP {exc.code}: {err_body[:400]}") from exc


def _mint_session_fallback(api_key: str) -> dict[str, Any]:
    model = realtime_model()
    body = json.dumps({
        "model": model,
        "voice": "alloy",
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openai.com/v1/realtime/sessions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


class Handler(BaseHTTPRequestHandler):
    server_version = f"VoiceCUAGateway/{__version__}"

    def log_message(self, fmt: str, *args: Any) -> None:
        # Avoid leaking paths with secrets into default stderr noise
        sys_stderr = __import__("sys").stderr
        print(f"[voice-cua] {self.address_string()} {fmt % args}", file=sys_stderr)

    def _json(self, code: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _reject_browser_origin(self) -> bool:
        if not self.headers.get("Origin"):
            return False
        self._json(403, {"ok": False, "error": "browser-origin requests are not allowed"})
        return True

    def _authorize_shutdown(self) -> bool:
        expected = os.environ.get("VOICE_CUA_CONTROL_TOKEN", "")
        if not expected:
            return True
        supplied = self.headers.get("X-Voice-CUA-Control", "")
        if supplied and hmac.compare_digest(supplied, expected):
            return True
        self._json(403, {"ok": False, "error": "invalid voice control token"})
        return False

    def _read_json(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}

    def do_OPTIONS(self) -> None:  # noqa: N802
        if self._reject_browser_origin():
            return
        self.send_response(204)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)
        if path in {"/", "/health"}:
            self._json(200, {"ok": True, "service": "voice-cua", "version": __version__})
            return
        if path == "/api/realtime/status":
            status = openai_status()
            self._json(200, {
                "configured": status["configured"],
                "model": realtime_model(),
                "tools": [t["name"] for t in tool_definitions()],
                "auth": {
                    "source": status["source"],
                    "catalog_id": status["catalog_id"],
                    "label": status["label"],
                    "in_keychain": status["in_keychain"],
                },
            })
            return
        if path == "/api/session/instructions":
            self._json(200, {
                "instructions": SYSTEM_INSTRUCTIONS,
                "tools": tool_definitions(),
                "model": realtime_model(),
            })
            return
        if path == "/api/session/status":
            from voice_cua.session_hub import status as session_status

            self._json(200, session_status())
            return
        if path == "/api/island/state":
            self._json(200, ISLAND.state.to_dict())
            return
        if path == "/api/island/stream":
            self._sse_island()
            return
        if path == "/api/secrets/inventory":
            platform = (query.get("platform") or [""])[0]
            q = (query.get("q") or query.get("query") or [""])[0]
            available_only = (query.get("available_only") or ["false"])[0].lower() in {
                "1",
                "true",
                "yes",
            }
            inv = build_inventory(platform=platform, query=q, available_only=available_only)
            self._json(200, {"ok": True, **inv})
            return
        if path == "/api/secrets/label":
            label = (query.get("label") or [""])[0].strip()
            if not label:
                self._json(400, {"ok": False, "error": "label query param required"})
                return
            item = find_by_label(label)
            if not item:
                self._json(404, {"ok": False, "error": "unknown label", "label": label})
                return
            self._json(200, {"ok": True, **item})
            return
        if path == "/api/activity/tail":
            limit = int((query.get("limit") or ["80"])[0])
            self._json(200, {
                "ok": True,
                "path": str(log_path()),
                "events": tail_events(limit=min(limit, 500)),
            })
            return
        if path == "/api/startup/tail":
            limit = int((query.get("limit") or ["80"])[0])
            self._json(200, {
                "ok": True,
                "path": str(log_path()),
                "events": tail_startup(limit=min(limit, 200)),
            })
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self._reject_browser_origin():
            return
        path = urlparse(self.path).path
        if path == "/api/realtime/client-secret":
            if not openai_configured():
                self._json(503, {"ok": False, "error": "OPENAI_API_KEY not set"})
                return
            try:
                data = mint_client_secret()
                self._json(200, {"ok": True, "data": data, "model": realtime_model()})
            except Exception as exc:  # noqa: BLE001
                self._json(502, {"ok": False, "error": str(exc)})
            return
        if path == "/api/tools/call":
            body = self._read_json()
            name = str(body.get("name") or "")
            arguments = body.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            ISLAND.publish("thinking", title="Tool", detail=name)
            result = dispatch(name, arguments)
            self._json(200, {"ok": True, "name": name, "result": result})
            return
        if path == "/api/island/publish":
            body = self._read_json()
            kind = str(body.get("kind") or "idle")
            ISLAND.publish(
                kind,  # type: ignore[arg-type]
                title=str(body.get("title") or ""),
                detail=str(body.get("detail") or ""),
                app=str(body.get("app") or ""),
                step=str(body.get("step") or ""),
                confirm_id=str(body.get("confirm_id") or ""),
                confirm_prompt=str(body.get("confirm_prompt") or ""),
                active_apps=list(body.get("active_apps") or []),
            )
            self._json(200, {"ok": True, "state": ISLAND.state.to_dict()})
            return
        if path == "/api/island/voice":
            body = self._read_json()
            ISLAND.set_voice_meter(
                str(body.get("voice_side") or "idle"),
                float(body.get("voice_level") or 0.0),
                list(body.get("voice_levels") or []),
            )
            self._json(200, {"ok": True})
            return
        if path == "/api/confirm":
            body = self._read_json()
            cid = str(body.get("confirm_id") or "")
            approved = bool(body.get("approved"))
            ok = ISLAND.resolve_confirm(cid, approved)
            self._json(200, {"ok": ok, "confirm_id": cid, "approved": approved})
            return
        if path == "/api/confirm/request":
            body = self._read_json()
            cid = str(body.get("confirm_id") or "")
            prompt = str(body.get("prompt") or "Confirm?")
            timeout = float(body.get("timeout") or 120.0)
            if not cid:
                self._json(400, {"ok": False, "error": "confirm_id required"})
                return
            approved = ISLAND.request_confirm(cid, prompt, timeout=timeout)
            self._json(200, {"ok": True, "confirm_id": cid, "approved": approved})
            return
        if path == "/api/session/set-listening":
            ISLAND.publish("listening", title="Listening", detail="")
            self._json(200, {"ok": True})
            return
        if path == "/api/session/text":
            from voice_cua.session_hub import send_text_and_wait

            body = self._read_json()
            text = str(body.get("text") or "").strip()
            if not text:
                self._json(400, {"ok": False, "error": "text required"})
                return
            timeout = float(body.get("timeout") or 90.0)
            result = send_text_and_wait(text, timeout=min(timeout, 120.0))
            code = 200 if result.get("ok") else 503
            self._json(code, result)
            return
        if path == "/api/shutdown":
            if not self._authorize_shutdown():
                return
            self._json(200, {"ok": True, "shutting_down": True})
            request_shutdown()
            return
        self._json(404, {"ok": False, "error": "not found"})

    def _sse_island(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        q: list = []
        lock = threading.Lock()

        def on_state(state) -> None:
            with lock:
                q.append(state.to_dict())

        ISLAND.subscribe(on_state)
        # Initial
        payload = json.dumps(ISLAND.state.to_dict())
        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()
        try:
            while True:
                item = None
                with lock:
                    if q:
                        item = q.pop(0)
                if item is None:
                    import time

                    time.sleep(0.2)
                    # heartbeat
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                raw = json.dumps(item)
                self.wfile.write(f"data: {raw}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    global _HTTPD
    _HTTPD = ThreadingHTTPServer((host, port), Handler)
    print(f"voice-cua gateway http://{host}:{port} model={realtime_model()}")
    _HTTPD.serve_forever()


def main() -> None:
    serve()


if __name__ == "__main__":
    main()
