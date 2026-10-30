#!/usr/bin/env python3
"""Lease-scoped CDP WebSocket adapter for Browser Use."""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import secrets
import shlex
import threading
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from websockets.exceptions import ConnectionClosed
from websockets.protocol import State
from websockets.sync.server import ServerConnection, serve


# Debugger-free upload_file: browser_harness calls DOM.getDocument -> DOM.querySelector ->
# DOM.setFileInputFiles. When chrome.debugger is policy-blocked, emulate exactly that chain:
# bytes are staged through userScripts Runtime.evaluate and attached with a DataTransfer.
_POLICY_BLOCK = re.compile(r"needs chrome\.debugger|restricted by policy", re.I)
UPLOAD_MAX_BYTES = 50 * 1024 * 1024
UPLOAD_CHUNK_CHARS = int(os.environ.get("COMET_BRIDGE_UPLOAD_CHUNK") or 400_000)
_UPLOAD_FAIL = "upload_file fallback (userScripts DataTransfer) failed: "


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
        self._handler_threads: set[threading.Thread] = set()
        self._dom_shim = False
        self._dom_shim_nodes: dict[int, str] = {}
        self._dom_shim_next = 1000

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
            handlers = [
                thread
                for thread in self._handler_threads
                if thread is not threading.current_thread()
            ]
        if active is not None:
            active.close(code=1001, reason="Comet Control closeout")
        self.server.shutdown()
        if self._server_thread.is_alive():
            self._server_thread.join(timeout=2)
        # The handler hides the cursor in finally, after its event pump joins
        # (up to 2s). Closeout must observe that hide before stop returns.
        for thread in handlers:
            thread.join(timeout=5)

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
        if method in {"DOM.getDocument", "DOM.querySelector", "DOM.setFileInputFiles"}:
            return self._dom_file_command(method, params)
        return self._page(method, params)

    def _page(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return self.run_action(
            {"type": "browser_use_cdp", "method": method, "params": params}
        )

    def _evaluate(self, expression: str) -> Any:
        out = self._page(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        details = out.get("exceptionDetails")
        if details:
            text = details.get("text") or (details.get("exception") or {}).get("description")
            raise CDPError(_UPLOAD_FAIL + str(text or "evaluate failed")[:300])
        return (out.get("result") or {}).get("value")

    def _dom_file_command(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "DOM.getDocument":
            try:
                result = self._page(method, params)
            except Exception as error:
                if not _POLICY_BLOCK.search(str(error)):
                    raise
                self._dom_shim = True
                self._dom_shim_nodes = {}
                url = ""
                try:
                    url = str(self.page_info().get("url") or "")
                except Exception:
                    pass
                return {"root": {"nodeId": 1, "backendNodeId": 1, "nodeType": 9,
                                 "nodeName": "#document", "localName": "", "nodeValue": "",
                                 "childNodeCount": 1, "documentURL": url}}
            self._dom_shim = False
            return result
        if method == "DOM.querySelector" and self._dom_shim and params.get("nodeId") == 1:
            selector = str(params.get("selector") or "")
            if not self._evaluate(f"!!document.querySelector({json.dumps(selector)})"):
                return {"nodeId": 0}
            self._dom_shim_next += 1
            self._dom_shim_nodes[self._dom_shim_next] = selector
            return {"nodeId": self._dom_shim_next}
        if method == "DOM.setFileInputFiles" and params.get("nodeId") in self._dom_shim_nodes:
            self._attach_files(self._dom_shim_nodes[params["nodeId"]], params.get("files"))
            return {}
        try:
            return self._page(method, params)
        except Exception as error:
            if method == "DOM.setFileInputFiles" and _POLICY_BLOCK.search(str(error)):
                raise CDPError(
                    "DOM.setFileInputFiles needs chrome.debugger (policy-blocked); the "
                    "debugger-free fallback supports nodeId from DOM.querySelector after "
                    "DOM.getDocument only (upload_file path), not backendNodeId/objectId"
                ) from error
            raise

    def _attach_files(self, selector: str, files: Any) -> None:
        if not isinstance(files, list) or not files:
            raise CDPError(_UPLOAD_FAIL + "files must be a non-empty list of absolute paths")
        paths = []
        for raw in files:
            path = Path(str(raw))
            if not path.is_absolute() or not path.is_file():
                raise CDPError(_UPLOAD_FAIL + f"not an absolute path to a regular file: {raw}")
            if path.stat().st_size > UPLOAD_MAX_BYTES:
                raise CDPError(_UPLOAD_FAIL + f"{path.name} exceeds {UPLOAD_MAX_BYTES} bytes")
            paths.append(path)
        sel = json.dumps(selector)
        pre = self._evaluate(
            f"(() => {{ const el = document.querySelector({sel});"
            " if (!el) return {ok:false, why:'no element'};"
            " if (el.tagName !== 'INPUT' || el.type !== 'file') return {ok:false, why:'not an input[type=file]: <' + el.tagName.toLowerCase() + ' type=' + el.type + '>'};"
            " if (el.disabled) return {ok:false, why:'input is disabled'};"
            " return {ok:true, multiple: !!el.multiple}; })()"
        ) or {}
        if not pre.get("ok"):
            raise CDPError(_UPLOAD_FAIL + f"{selector}: {pre.get('why') or 'preflight returned nothing'}")
        if len(paths) > 1 and not pre.get("multiple"):
            raise CDPError(_UPLOAD_FAIL + f"{selector} does not accept multiple files")
        token = "u" + secrets.token_hex(8)
        keys = []
        metas = []
        try:
            for index, path in enumerate(paths):
                data = path.read_bytes()
                b64 = base64.b64encode(data).decode("ascii")
                key = json.dumps(f"{token}_{index}")
                keys.append(key)
                sent = 0
                for start in range(0, len(b64), max(1, UPLOAD_CHUNK_CHARS)) or [0]:
                    chunk = b64[start:start + UPLOAD_CHUNK_CHARS]
                    sent += len(chunk)
                    got = self._evaluate(
                        "((g) => { g.__ccUpload = g.__ccUpload || {};"
                        f" g.__ccUpload[{key}] = (g.__ccUpload[{key}] || '') + {json.dumps(chunk)};"
                        f" return g.__ccUpload[{key}].length; }})(globalThis)"
                    )
                    if got != sent:
                        raise CDPError(_UPLOAD_FAIL + f"staging {path.name}: page holds {got} of {sent} chars")
                mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                metas.append({"key": f"{token}_{index}", "name": path.name, "type": mime,
                              "lastModified": int(path.stat().st_mtime * 1000), "size": len(data)})
            done = self._evaluate(
                f"(() => {{ const el = document.querySelector({sel}); const metas = {json.dumps(metas)};"
                " const g = globalThis; const store = g.__ccUpload || {};"
                " try { if (!el) return {ok:false, why:'element vanished'};"
                " const dt = new DataTransfer();"
                " for (const m of metas) { const bin = atob(store[m.key] || ''); const u = new Uint8Array(bin.length);"
                " for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i);"
                " dt.items.add(new File([u], m.name, {type: m.type, lastModified: m.lastModified})); }"
                " el.files = dt.files;"
                " el.dispatchEvent(new Event('input', {bubbles:true, composed:true}));"
                " el.dispatchEvent(new Event('change', {bubbles:true}));"
                " return {ok:true, count: el.files.length, names: [...el.files].map(f => f.name), sizes: [...el.files].map(f => f.size)};"
                " } finally { for (const m of metas) delete store[m.key]; } })()"
            ) or {}
            want = [m["size"] for m in metas]
            if not done.get("ok") or done.get("count") != len(metas) or done.get("sizes") != want:
                raise CDPError(_UPLOAD_FAIL + f"{selector}: attach result {done!r}, expected sizes {want}")
            keys = []
        finally:
            if keys:
                try:
                    self._evaluate(
                        "((g) => { const s = g.__ccUpload || {};"
                        f" for (const k of {json.dumps([json.loads(k) for k in keys])}) delete s[k]; return true; }})(globalThis)"
                    )
                except Exception:
                    pass

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
        thread = threading.current_thread()
        with self._client_lock:
            self._handler_threads.add(thread)
        try:
            self._handle_client(websocket)
        finally:
            with self._client_lock:
                self._handler_threads.discard(thread)

    def _handle_client(self, websocket: ServerConnection) -> None:
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
