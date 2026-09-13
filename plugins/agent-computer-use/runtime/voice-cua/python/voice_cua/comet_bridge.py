"""Optional token-private Comet Control lease for one browser task."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any


ALLOWED_ACTIONS = frozenset({
    "back", "click_selector", "click_text", "cursor_click", "cursor_double_click",
    "cursor_drag", "cursor_key", "cursor_move", "cursor_right_click", "cursor_scroll",
    "cursor_status", "cursor_triple_click", "cursor_type", "fill_selector", "forward",
    "goto", "locator", "page_context", "reload_page", "screenshot", "snapshot", "text",
    "wait", "wait_for_selector", "wait_for_url_change", "zoom",
})


def _runtime_root() -> Path:
    configured = os.environ.get("COMET_CONTROL_ROOT", "").strip()
    root = Path(configured).expanduser() if configured else Path.home() / ".agents/plugins/comet-control"
    controller = root / "skills/comet-control/scripts/durable_lease_controller.py"
    if not (root / "plugin.json").is_file() or not controller.is_file():
        raise RuntimeError("Comet Control is not installed at ~/.agents/plugins/comet-control")
    return root


def _run_json(command: list[str], *, timeout: float) -> dict[str, Any]:
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": str(exc)}
    raw = proc.stdout.strip()
    if not raw:
        return {"ok": False, "error": (proc.stderr.strip() or f"command exited {proc.returncode}")[:300]}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        lines = [line for line in raw.splitlines() if line.strip()]
        try:
            payload = json.loads(lines[-1])
        except json.JSONDecodeError:
            return {"ok": False, "error": "Comet Control returned invalid JSON"}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "Comet Control returned a non-object response"}
    if proc.returncode and "ok" not in payload:
        payload["ok"] = False
    return payload


def _public(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _public(item)
            for key, item in value.items()
            if "token" not in key.lower() and "capability" not in key.lower()
        }
    if isinstance(value, list):
        return [_public(item) for item in value]
    return value


class CometLease:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._session_id = ""
        self._workdir: Path | None = None
        self._python = ""
        self._controller: Path | None = None

    def active(self) -> bool:
        with self._lock:
            return bool(self._session_id)

    def begin(self, url: str) -> dict[str, Any]:
        if not url.startswith(("https://", "http://")):
            return {"ok": False, "error": "url must start with https:// or http://"}
        with self._lock:
            if self._session_id:
                return {"ok": False, "error": "a Comet task lease is already active; end it first"}
            root = _runtime_root()
            probe = _run_json([str(root / "scripts/ensure-broker.sh"), "probe", "--json"], timeout=30)
            broker = probe.get("broker") if isinstance(probe.get("broker"), dict) else {}
            if not (
                probe.get("success")
                and broker.get("runtime_verified")
                and broker.get("extension_connected")
            ):
                return {"ok": False, "error": "Comet Control runtime or extension is unavailable"}
            python = Path(str(broker.get("python_executable") or ""))
            if not python.is_absolute() or not os.access(python, os.X_OK):
                return {"ok": False, "error": "Comet Control did not report a usable Python runtime"}
            session_id = f"samantha-{uuid.uuid4().hex}"
            workdir = Path(tempfile.mkdtemp(prefix="samantha-comet-"))
            controller = root / "skills/comet-control/scripts/durable_lease_controller.py"
            started = _run_json(
                [
                    str(python),
                    str(controller),
                    "start",
                    "--session-id",
                    session_id,
                    "--label",
                    "Samantha",
                    "--url",
                    url,
                    "--workdir",
                    str(workdir),
                    "--socket",
                    str(broker.get("socket_path") or root / "run/comet-control.sock"),
                    "--ttl-seconds",
                    "600",
                ],
                timeout=190,
            )
            if not started.get("ok"):
                closed = _run_json(
                    [
                        str(python),
                        str(controller),
                        "closeout",
                        "--workdir",
                        str(workdir),
                        "--timeout",
                        "45",
                    ],
                    timeout=55,
                )
                if closed.get("ok"):
                    shutil.rmtree(workdir, ignore_errors=True)
                return {"ok": False, "error": str(started.get("error") or "Comet lease failed")[:300]}
            self._session_id = session_id
            self._workdir = workdir
            self._python = str(python)
            self._controller = controller
            return {"ok": True, "lease": "isolated", "session_id": session_id}

    def act(self, actions: list[dict[str, Any]], *, timeout: float = 45) -> dict[str, Any]:
        if not actions:
            return {"ok": False, "error": "actions must be a non-empty array"}
        unsupported = sorted({str(action.get("type") or "") for action in actions} - ALLOWED_ACTIONS)
        if unsupported:
            return {"ok": False, "error": f"unsupported Comet action: {', '.join(unsupported)}"}
        with self._lock:
            if not self._session_id or self._workdir is None or self._controller is None:
                return {"ok": False, "error": "no active Comet task lease; call comet_begin first"}
            result = _run_json(
                [
                    self._python,
                    str(self._controller),
                    "send",
                    "--workdir",
                    str(self._workdir),
                    json.dumps({"actions": actions, "timeoutSeconds": timeout}),
                    "--timeout",
                    str(timeout + 40),
                ],
                timeout=timeout + 50,
            )
            public = _public(result)
            response = public.get("response") if isinstance(public.get("response"), dict) else {}
            public["ok"] = bool(public.get("ok", response.get("success")))
            return public

    def end(self) -> dict[str, Any]:
        with self._lock:
            if not self._session_id or self._workdir is None or self._controller is None:
                return {"ok": True, "closed": False, "already_closed": True}
            result = _run_json(
                [
                    self._python,
                    str(self._controller),
                    "closeout",
                    "--workdir",
                    str(self._workdir),
                    "--timeout",
                    "45",
                ],
                timeout=55,
            )
            public = _public(result)
            response = public.get("response") if isinstance(public.get("response"), dict) else {}
            ok = bool(public.get("ok", response.get("success")))
            public["ok"] = ok
            if ok:
                shutil.rmtree(self._workdir, ignore_errors=True)
                self._session_id = ""
                self._workdir = None
                self._python = ""
                self._controller = None
            return public


_LEASE = CometLease()


def comet_is_active() -> bool:
    return _LEASE.active()


def comet_begin(url: str) -> dict[str, Any]:
    return _LEASE.begin(url)


def comet_act(actions: list[dict[str, Any]], *, timeout: float = 45) -> dict[str, Any]:
    return _LEASE.act(actions, timeout=timeout)


def comet_end() -> dict[str, Any]:
    return _LEASE.end()
