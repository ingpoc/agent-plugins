#!/usr/bin/env python3
"""Lease-scoped CDP WebSocket adapter for Browser Use."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from websockets.exceptions import ConnectionClosed
from websockets.sync.server import ServerConnection, serve


HARNESS_RUNTIME = Path.home() / ".config/browser-harness/runtime"
HARNESS_DAEMON_MARKER = "browser_harness.daemon"
SAFE_HARNESS_NAME = re.compile(r"^[a-zA-Z0-9._-]+$")


def _harness_paths(name: str) -> tuple[Path, Path]:
    if not SAFE_HARNESS_NAME.fullmatch(name):
        raise ValueError("invalid Browser Harness name")
    stem = HARNESS_RUNTIME / f"bu-{name}"
    return stem.with_suffix(".pid"), stem.with_suffix(".sock")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _process_command(pid: int) -> str:
    try:
        return subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "command="],
            text=True,
            timeout=2,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def recover_browser_harness(
    name: str,
    *,
    expected_pid: int | None = None,
) -> dict[str, Any]:
    """Reap only the daemon and runtime artifacts owned by one bridge name."""
    pid_path, sock_path = _harness_paths(name)
    pid: int | None = None
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
        except (OSError, ValueError):
            pid = None

    if expected_pid is not None and pid != expected_pid:
        return {"recovered": False, "reason": "daemon_changed"}

    killed = False
    if pid is not None and _pid_alive(pid):
        command = _process_command(pid)
        other_names = {
            match
            for match in re.findall(r"\bbu-(comet-[a-zA-Z0-9._-]+)", command)
        }
        if HARNESS_DAEMON_MARKER not in command or (
            other_names and name not in other_names
        ):
            return {"recovered": False, "reason": "pid_not_owned"}
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 1.0
        while _pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        if _pid_alive(pid):
            os.kill(pid, signal.SIGKILL)
        killed = True

    for path in (pid_path, sock_path):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    return {"recovered": True, "killed": killed, "cleared": True}


def _harness_pid(name: str) -> int | None:
    pid_path, _ = _harness_paths(name)
    try:
        return int(pid_path.read_text().strip())
    except (FileNotFoundError, OSError, ValueError):
        return None


def _connection_lost(error: Exception) -> bool:
    return "connection lost" in str(error).lower()


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
        # This bridge owns a fresh random endpoint. A daemon under the same
        # deterministic name can only point at an older endpoint.
        recover_browser_harness(self.name)
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
            if self._client_active:
                websocket.close(
                    code=1008, reason="lease already has a Browser Use client"
                )
                return
            self._client_active = True
            self._active_websocket = websocket

        stop = threading.Event()
        send_lock = threading.Lock()
        event_thread = None
        harness_pid = _harness_pid(self.name)
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
                if "error" in response and _connection_lost(error):
                    websocket.close(code=1011, reason="Comet CDP connection lost")
                    break
        except ConnectionClosed:
            pass
        except (OSError, RuntimeError):
            websocket.close(code=1011, reason="Comet CDP event stream unavailable")
        finally:
            stop.set()
            if event_thread is not None and event_thread.is_alive():
                event_thread.join(timeout=2)
            try:
                self.hide_cursor()
            except Exception:
                pass
            with self._client_lock:
                self._client_active = False
                self._active_websocket = None
            # A disconnected daemon cannot safely retain Browser Harness's
            # exclusive slot. expected_pid prevents reaping a replacement that
            # raced this connection's teardown.
            if harness_pid is not None:
                recover_browser_harness(self.name, expected_pid=harness_pid)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    recover = subparsers.add_parser(
        "recover",
        help="reap one named stale Browser Harness daemon and its artifacts",
    )
    recover.add_argument("--name", required=True)
    args = parser.parse_args()
    result = recover_browser_harness(args.name)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("recovered") else 1


if __name__ == "__main__":
    raise SystemExit(main())
