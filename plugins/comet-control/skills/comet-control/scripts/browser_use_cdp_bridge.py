#!/usr/bin/env python3
"""Lease-scoped CDP WebSocket adapter for Browser Use."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shlex
import threading
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from websockets.exceptions import ConnectionClosed
from websockets.protocol import State
from websockets.sync.server import ServerConnection, serve


class CDPError(RuntimeError):
    def __init__(self, message: str, code: int = -32000) -> None:
        super().__init__(message)
        self.code = code


class BrowserUseCDPBridge:
    """Expose exactly one leased Comet tab as a browser-level CDP endpoint."""

    def __init__(
        self,
        session_id: str,
        run_action: Callable[[dict[str, Any]], dict[str, Any]],
        page_info: Callable[[], dict[str, Any]],
        hide_cursor: Callable[[], None],
    ) -> None:
        digest = hashlib.sha256(session_id.encode()).hexdigest()[:20]
        self.target_id = f"comet-{digest}"
        self.session_id = f"session-{digest}"
        self.name = f"comet-{digest[:10]}"
        self.run_action = run_action
        self.page_info = page_info
        self.hide_cursor = hide_cursor
        self.path = f"/devtools/browser/{secrets.token_urlsafe(32)}"
        self.server = serve(self._handle, "127.0.0.1", 0, max_size=16 * 1024 * 1024)
        self._server_thread = threading.Thread(
            target=self.server.serve_forever,
            name=f"comet-browser-use-{digest[:8]}",
            daemon=True,
        )
        self._client_lock = threading.Lock()
        self._client_active = False
        self._active_websocket: ServerConnection | None = None

    def start(self, env_file: Path) -> dict[str, str]:
        self._server_thread.start()
        port = int(self.server.socket.getsockname()[1])
        ws_url = f"ws://127.0.0.1:{port}{self.path}"
        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.write_text(
            f"export BU_CDP_WS={shlex.quote(ws_url)}\n"
            f"export BU_NAME={shlex.quote(self.name)}\n"
            "export BH_OPEN_LIVE_URL=0\n"
            "export BH_DOMAIN_SKILLS=0\n"
            "export BH_RECORD=0\n"
        )
        os.chmod(env_file, 0o600)
        return {"cdp_env_file": str(env_file), "browser_use_name": self.name}

    def stop(self) -> None:
        with self._client_lock:
            active = self._active_websocket
        if active is not None:
            active.close(code=1001, reason="Comet Control closeout")
        self.server.shutdown()
        if self._server_thread.is_alive():
            self._server_thread.join(timeout=2)

    def _target_info(self) -> dict[str, Any]:
        page = self.page_info()
        return {
            "targetId": self.target_id,
            "type": "page",
            "title": str(page.get("title") or "Comet"),
            "url": str(page.get("url") or "about:blank"),
            "attached": True,
            "canAccessOpener": False,
        }

    def _browser_command(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "Browser.getVersion":
            user_agent = "Comet Control"
            try:
                evaluated = self.run_action(
                    {
                        "type": "browser_use_cdp",
                        "method": "Runtime.evaluate",
                        "params": {
                            "expression": "navigator.userAgent",
                            "returnByValue": True,
                        },
                    }
                )
                user_agent = str(evaluated.get("result", {}).get("value") or user_agent)
            except Exception:
                pass
            return {
                "protocolVersion": "1.3",
                "product": "Comet/Browser Use Bridge",
                "revision": "comet-control",
                "userAgent": user_agent,
                "jsVersion": "V8",
            }
        if method == "Target.getTargets":
            return {"targetInfos": [self._target_info()]}
        if method == "Target.getTargetInfo":
            self._require_target(params.get("targetId", self.target_id))
            return {"targetInfo": self._target_info()}
        if method == "Target.attachToTarget":
            self._require_target(params.get("targetId"))
            return {"sessionId": self.session_id}
        if method == "Target.createTarget":
            url = str(params.get("url") or "about:blank")
            current_url = str(self._target_info().get("url") or "")
            if not (
                url.startswith("about:blank")
                and not current_url.startswith("about:blank")
            ):
                self.run_action(
                    {
                        "type": "browser_use_cdp",
                        "method": "Page.navigate",
                        "params": {"url": url},
                    }
                )
            return {"targetId": self.target_id}
        if method in {
            "Target.activateTarget",
            "Target.detachFromTarget",
            "Target.setAutoAttach",
            "Target.setDiscoverTargets",
        }:
            if params.get("targetId") is not None:
                self._require_target(params["targetId"])
            return {}
        if method == "Target.getBrowserContexts":
            return {"browserContextIds": []}
        if method in {"Target.closeTarget", "Page.close", "Page.crash"}:
            raise CDPError(
                "The leased Comet tab is closed only by Comet Control closeout"
            )
        if method.startswith(("Target.", "Browser.")):
            raise CDPError(
                f"Browser-wide CDP method is outside the leased tab: {method}"
            )
        return self.run_action(
            {"type": "browser_use_cdp", "method": method, "params": params}
        )

    def _require_target(self, target_id: Any) -> None:
        if str(target_id or "") != self.target_id:
            raise CDPError("Target is outside the leased Comet tab")

    def _event_pump(
        self,
        websocket: ServerConnection,
        stop: threading.Event,
        send_lock: threading.Lock,
        cursor: int,
    ) -> None:
        transient_failures = 0
        try:
            while not stop.is_set():
                try:
                    batch = self.run_action(
                        {
                            "type": "cdp_events",
                            "afterSequence": cursor,
                            "limit": 200,
                            "timeoutMs": 0,
                        }
                    )
                    transient_failures = 0
                except (OSError, RuntimeError):
                    # Extension reloads can briefly interrupt the leased tab's
                    # event channel. Keep the single bridge alive and retry a
                    # bounded number of times instead of killing Browser Use.
                    transient_failures += 1
                    if transient_failures > 5:
                        raise
                    stop.wait(0.2 * transient_failures)
                    continue
                if batch.get("truncated"):
                    raise RuntimeError("CDP event history was truncated")
                events = batch.get("events") or []
                for event in events:
                    message = {
                        "method": event.get("method"),
                        "params": event.get("params") or {},
                        "sessionId": self.session_id,
                    }
                    with send_lock:
                        websocket.send(json.dumps(message, ensure_ascii=False))
                cursor = int(batch.get("cursor") or cursor)
                if not batch.get("has_more"):
                    stop.wait(0.1)
        except ConnectionClosed:
            return
        except (OSError, RuntimeError):
            # A live command socket without events makes clients hang indefinitely.
            websocket.close(code=1011, reason="Comet CDP event stream interrupted")

    def _handle(self, websocket: ServerConnection) -> None:
        if not secrets.compare_digest(urlsplit(websocket.request.path).path, self.path):
            websocket.close(code=1008, reason="invalid bridge capability")
            return
        with self._client_lock:
            previous = self._active_websocket
            # A killed client's socket is CLOSED as soon as the peer drops, but
            # its handler frees the slot only after the in-flight serialized
            # call returns (up to the lease timeout). Hand the slot over then,
            # so a respawned Browser Use daemon never collides with a dead one.
            if self._client_active and not (
                previous is not None
                and previous.state in (State.CLOSING, State.CLOSED)
            ):
                websocket.close(
                    code=1008, reason="lease already has a Browser Use client"
                )
                return
            self._client_active = True
            self._active_websocket = websocket

        stop = threading.Event()
        send_lock = threading.Lock()
        event_thread = None
        try:
            # Mark before accepting commands so Runtime.enable cannot race the mark.
            cursor = int(self.run_action({"type": "cdp_events"}).get("cursor") or 0)
            event_thread = threading.Thread(
                target=self._event_pump,
                args=(websocket, stop, send_lock, cursor),
                name=f"comet-cdp-events-{self.target_id[-8:]}",
                daemon=True,
            )
            event_thread.start()
            for raw in websocket:
                request: Any = None
                try:
                    request = json.loads(raw)
                    if not isinstance(request, dict) or not isinstance(
                        request.get("id"), int
                    ):
                        raise CDPError("Invalid CDP request", -32600)
                    method = str(request.get("method") or "")
                    params = request.get("params") or {}
                    if not method or not isinstance(params, dict):
                        raise CDPError("Invalid CDP method or params", -32602)
                    supplied_session = request.get("sessionId")
                    if supplied_session not in (None, self.session_id):
                        raise CDPError("Session is outside the leased Comet tab")
                    response = {
                        "id": request["id"],
                        "result": self._browser_command(method, params),
                    }
                    if supplied_session is not None:
                        response["sessionId"] = self.session_id
                except CDPError as error:
                    response = {
                        "id": request.get("id") if isinstance(request, dict) else None,
                        "error": {"code": error.code, "message": str(error)},
                    }
                except Exception as error:
                    response = {
                        "id": request.get("id") if isinstance(request, dict) else None,
                        "error": {"code": -32000, "message": str(error)},
                    }
                with send_lock:
                    websocket.send(json.dumps(response, ensure_ascii=False))
        except ConnectionClosed:
            pass
        except (OSError, RuntimeError):
            websocket.close(code=1011, reason="Comet CDP event stream unavailable")
        finally:
            stop.set()
            if event_thread is not None and event_thread.is_alive():
                event_thread.join(timeout=2)
            with self._client_lock:
                owner = self._active_websocket is websocket
            if owner:
                try:
                    self.hide_cursor()
                except Exception:
                    pass
            with self._client_lock:
                # A successor may have taken the slot while this handler was
                # blocked; never clear its ownership.
                if self._active_websocket is websocket:
                    self._client_active = False
                    self._active_websocket = None
