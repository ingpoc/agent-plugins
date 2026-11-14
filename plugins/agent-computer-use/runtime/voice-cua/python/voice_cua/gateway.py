"""Local HTTP gateway: tools, island SSE, confirm (voice runs on GPT-Live in the helper)."""

from __future__ import annotations

import json
import hmac
import math
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from voice_cua import SYSTEM_INSTRUCTIONS, __version__, session_instructions
from voice_cua.activity_log import log_path, tail_events
from voice_cua.startup_trace import tail_startup
from voice_cua.auth import openai_status
from voice_cua.inventory import build_inventory, find_by_label
from voice_cua.island_state import ISLAND
from voice_cua.activity_log import log_event
from voice_cua import plugin_island
from voice_cua.island_facade import describe_tool, errors_published, summarize_result
from voice_cua.tools import dispatch, tool_definitions
from voice_cua.voice_settings import load_settings

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = int(os.environ.get("VOICE_CUA_PORT", "8765"))
SSE_HEARTBEAT_S = 10.0
_HTTPD: ThreadingHTTPServer | None = None
_APPROVE_TOKEN: str | None = None
_APPROVE_TOKEN_PATH: Path | None = None
_APPROVE_TOKEN_LOCK = threading.Lock()
MAX_TEXT_WAIT_S = 1800.0


def _approve_token_path() -> Path:
    return Path.home() / ".cache" / "macos-cua" / "typed-approve.token"


def _create_approve_token() -> None:
    global _APPROVE_TOKEN, _APPROVE_TOKEN_PATH
    token = secrets.token_urlsafe(32)
    path = _approve_token_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = -1
            f.write(token)
    finally:
        if fd >= 0:
            os.close(fd)
    with _APPROVE_TOKEN_LOCK:
        _APPROVE_TOKEN = token
        _APPROVE_TOKEN_PATH = path


def _delete_approve_token() -> None:
    global _APPROVE_TOKEN, _APPROVE_TOKEN_PATH
    with _APPROVE_TOKEN_LOCK:
        path, _APPROVE_TOKEN_PATH = _APPROVE_TOKEN_PATH, None
        _APPROVE_TOKEN = None
    if path is not None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _cleanup_gateway_state() -> None:
    try:
        from voice_cua.session_hub import shutdown_turns

        shutdown_turns()
    finally:
        try:
            from voice_cua.dev_tools import shutdown_jobs

            shutdown_jobs()
        finally:
            _delete_approve_token()


def live_model() -> str:
    return (os.environ.get("VOICE_CUA_LIVE_MODEL") or os.environ.get("VOICE_CUA_REALTIME_MODEL")
            or load_settings()["live_model"])


def request_shutdown() -> None:
    """Stop gateway and exit the voice stack process (CUAService Samantha OFF)."""
    global _HTTPD
    httpd = _HTTPD

    def _exit_stack() -> None:
        import time

        time.sleep(0.05)
        _cleanup_gateway_state()
        if httpd is not None:
            try:
                httpd.shutdown()
            except OSError:
                pass
        os._exit(0)

    threading.Thread(target=_exit_stack, name="voice-shutdown", daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    server_version = f"VoiceCUAGateway/{__version__}"

    def log_message(self, fmt: str, *args: Any) -> None:
        # Avoid leaking paths with secrets into default stderr noise
        sys_stderr = __import__("sys").stderr
        print(f"[voice-cua] {self.address_string()} {fmt % args}", file=sys_stderr)

    def _json(self, code: int, payload: Any) -> None:
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

    def _authorize_typed_approval(self) -> bool:
        with _APPROVE_TOKEN_LOCK:
            expected = _APPROVE_TOKEN or ""
        supplied = self.headers.get("X-Voice-CUA-Approve", "")
        if expected and hmac.compare_digest(supplied, expected):
            return True
        self._json(403, {"ok": False, "error": "typed approval unauthorized"})
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
                "model": live_model(),
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
                "instructions": session_instructions(),
                "tools": tool_definitions(),
                "model": live_model(),
            })
            return
        if path == "/api/session/status":
            from voice_cua.session_hub import status as session_status

            self._json(200, session_status())
            return
        if path == "/api/session/turn":
            if not self._authorize_typed_approval():
                return
            from voice_cua.session_hub import get_turn

            turn_id = (query.get("id") or [""])[0]
            if not turn_id:
                self._json(400, {"ok": False, "error": "id query param required"})
                return
            turn = get_turn(turn_id)
            self._json(200 if turn else 404, turn or {"ok": False, "error": "unknown turn_id"})
            return
        if path == "/api/confirm/pending":
            if not self._authorize_typed_approval():
                return
            self._json(200, ISLAND.pending_confirm())
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
            # Realtime browser client secrets went with the Realtime backend (GPT-Live only).
            self._json(410, {"ok": False, "error": "Realtime client secrets were removed; voice runs on GPT-Live"})
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
            kind, title, detail, app = describe_tool(name, arguments)
            app = plugin_island.island_app(name, app)
            ISLAND.publish(kind, title=title, detail=detail, app=app, step=name)
            plugin_island.tool_started(name, arguments)
            errors_before = errors_published()
            try:
                result = dispatch(name, arguments)
            finally:
                plugin_island.tool_finished(name, arguments)
            rkind, rtitle, rdetail = summarize_result(name, result, fallback=detail)
            if not (rkind == "error" and errors_published() > errors_before):
                ISLAND.publish(rkind, title=rtitle, detail=rdetail, app=app)
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
                expanded=bool(body.get("expanded")),
                step_index=int(body.get("step_index") or 0),
                step_total=int(body.get("step_total") or 0),
                agents=list(body["agents"]) if isinstance(body.get("agents"), list) else None,
                agent_approval=(body["agent_approval"] if isinstance(body.get("agent_approval"), dict) else ({} if "agent_approval" in body else None)),  # explicit null clears
            )
            self._json(200, {"ok": True, "state": ISLAND.state.to_dict()})
            return
        if path == "/api/agent/approval":
            # Stub until agents can raise approvals: clears the card, records the answer.
            body = self._read_json()
            ok = ISLAND.resolve_agent_approval(str(body.get("id") or ""), bool(body.get("approved")))
            log_event("agent_approval_answer", id=str(body.get("id") or "")[:80], approved=bool(body.get("approved")), ok=ok)
            self._json(200, {"ok": ok})
            return
        if path == "/api/island/action":
            # Card controls (stop speaking / cancel / stop task / replay / retry) run on the live session.
            from voice_cua import session_hub

            if not self._authorize_shutdown():   # same per-launch X-Voice-CUA-Control token as shutdown
                return
            body = self._read_json()
            action = str(body.get("action") or "")[:40]
            handled = session_hub.island_control(action)
            log_event("island_action", action=action, handled=handled)
            self._json(200, {"ok": True, "handled": handled})
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
            if not isinstance(body.get("approved"), bool):
                self._json(400, {"ok": False, "error": "approved must be a boolean"})
                return
            approved = body["approved"]
            ok = ISLAND.resolve_confirm(cid, approved)
            self._json(200 if ok else 409, {"ok": ok, "confirm_id": cid, "approved": approved})
            return
        if path == "/api/session/approve":
            body = self._read_json()
            cid = str(body.get("confirm_id") or "")
            if not isinstance(body.get("approved"), bool):
                self._json(400, {"ok": False, "error": "approved must be a boolean"})
                return
            if not self._authorize_typed_approval():
                return
            approved = body["approved"]
            ok = ISLAND.resolve_confirm(cid, approved)
            self._json(200 if ok else 409, {"ok": ok, "confirm_id": cid, "approved": approved})
            return
        if path == "/api/confirm/request":
            body = self._read_json()
            cid = str(body.get("confirm_id") or "")
            prompt = str(body.get("prompt") or "Confirm?")
            raw_timeout = body.get("timeout", 120.0)
            try:
                timeout = float(raw_timeout)
            except (TypeError, ValueError, OverflowError):
                timeout = 0.0
            if (isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float))
                    or not math.isfinite(timeout) or timeout <= 0):
                self._json(400, {"ok": False, "error": "timeout must be finite and greater than zero"})
                return
            if not cid:
                self._json(400, {"ok": False, "error": "confirm_id required"})
                return
            origin_turn_id = body.get("origin_turn_id")
            if origin_turn_id is not None:
                from voice_cua.session_hub import has_turn

                if not isinstance(origin_turn_id, str) or not has_turn(origin_turn_id):
                    self._json(409, {"ok": False, "error": "unknown originating turn"})
                    return
            outcome = ISLAND.request_confirm(cid, prompt, timeout=timeout,
                                             origin_turn_id=origin_turn_id)
            ok = outcome != "error"
            self._json(200 if ok else 409, {
                "ok": ok, "confirm_id": cid, "outcome": outcome,
                "approved": outcome == "approved", "timed_out": outcome == "timed_out",
            })
            return
        if path == "/api/session/set-listening":
            ISLAND.publish("listening", title="Listening", detail="")
            self._json(200, {"ok": True})
            return
        if path == "/api/session/text":
            from voice_cua.session_hub import start_turn

            body = self._read_json()
            raw_text = body.get("text")
            text = raw_text.strip() if isinstance(raw_text, str) else ""
            if not text:
                self._json(400, {"ok": False, "error": "text required"})
                return
            raw_timeout = body.get("timeout", 90.0)
            if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float)):
                self._json(400, {"ok": False, "error": "timeout must be a finite number greater than zero"})
                return
            try:
                timeout = float(raw_timeout)
            except (TypeError, ValueError, OverflowError):
                self._json(400, {"ok": False, "error": "timeout must be a finite number greater than zero"})
                return
            if not math.isfinite(timeout) or timeout <= 0:
                self._json(400, {"ok": False, "error": "timeout must be a finite number greater than zero"})
                return
            if "async" in body and not isinstance(body["async"], bool):
                self._json(400, {"ok": False, "error": "async must be a boolean"})
                return
            asynchronous = body.get("async") is True
            result = start_turn(text, timeout=min(timeout, MAX_TEXT_WAIT_S), asynchronous=asynchronous)
            code = 202 if result.get("status") == "running" else (200 if result.get("ok") else 503)
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
        self.send_header("X-Voice-CUA", "island")
        self.end_headers()
        pending: dict = {"state": None}
        lock = threading.Lock()
        ready = threading.Event()

        def on_state(state) -> None:
            with lock:
                pending["state"] = state.to_dict()
            ready.set()

        ISLAND.subscribe(on_state)
        try:
            # Initial snapshot immediately.
            payload = json.dumps(ISLAND.state.to_dict())
            self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
            self.wfile.flush()
            timeout = SSE_HEARTBEAT_S  # read at call time — tests patch the module global
            while True:
                if ready.wait(timeout=timeout):
                    with lock:
                        item = pending["state"]
                        pending["state"] = None
                        ready.clear()
                    if item is not None:
                        self.wfile.write(f"data: {json.dumps(item)}\n\n".encode("utf-8"))
                        self.wfile.flush()
                        continue
                self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        finally:
            ISLAND.unsubscribe(on_state)


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    global _HTTPD
    _HTTPD = ThreadingHTTPServer((host, port), Handler)
    try:
        _create_approve_token()
        print(f"voice-cua gateway http://{host}:{port} model={live_model()}")
        _HTTPD.serve_forever()
    finally:
        _cleanup_gateway_state()
        _HTTPD.server_close()
        _HTTPD = None


def main() -> None:
    serve()


if __name__ == "__main__":
    main()
