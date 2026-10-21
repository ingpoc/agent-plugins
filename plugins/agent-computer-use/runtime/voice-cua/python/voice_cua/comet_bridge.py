"""Optional token-private Comet Control lease for one browser task."""

from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


# Lease lifetime. The durable controller renews the server TTL for as long as
# it runs, so TTL only bounds an orphaned controller. The local idle release
# applies to self_test leases only: windows opened for the user's own tasks
# (purpose=user_task, the default) are never closed by Samantha unless the user
# asks — he always has follow-ups.
LEASE_TTL_SECONDS = int(_env_float("VOICE_CUA_COMET_TTL", 300))
LEASE_IDLE_SECONDS = _env_float("VOICE_CUA_COMET_IDLE_S", 90.0)
PURPOSES = ("user_task", "self_test")
DEFAULT_PURPOSE = "user_task"


def _state_path() -> Path:
    """Non-secret pointer to a kept-open user_task lease (workdir + session id).

    The lease token stays inside the controller's private workdir; this file
    only lets a restarted helper re-attach to the same window.
    """
    configured = os.environ.get("VOICE_CUA_COMET_STATE", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".cache/macos-cua/samantha-comet-lease.json"


def redact_url(url: str) -> str:
    """scheme://host/path only — drop query, fragment and userinfo."""
    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return ""
    host = parts.hostname or ""
    if not parts.scheme or not host:
        return ""
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}{parts.path or ''}"[:160]


def redact_actions(actions: Any) -> list[str]:
    """Action types only (never fill values / selectors with user data)."""
    out: list[str] = []
    for action in actions if isinstance(actions, list) else []:
        if isinstance(action, dict):
            kind = str(action.get("type") or "?")
            if kind == "goto":
                kind = f"goto {redact_url(str(action.get('url') or ''))}"
            out.append(kind[:120])
    return out[:20]


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


BROKER_PLIST = Path.home() / "Library/LaunchAgents/local.comet-control.broker.plist"
_BASE_PATH = ("/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")


def _usable(path: str) -> bool:
    return bool(path) and Path(path).is_absolute() and os.access(path, os.X_OK)


def _comet_python() -> str:
    """The interpreter Comet Control's broker was installed with.

    The app is launched by launchd/Finder with PATH=/usr/bin:/bin:..., where
    ``python3`` is the system interpreter without the pinned websockets, so
    ensure-broker.sh's own ``python3`` lookup fails. Resolve deterministically:
    COMET_CONTROL_PYTHON, else the broker LaunchAgent's ProgramArguments[0].
    """
    configured = os.environ.get("COMET_CONTROL_PYTHON", "").strip()
    if _usable(configured):
        return configured
    try:
        import plistlib

        with BROKER_PLIST.open("rb") as handle:
            args = plistlib.load(handle).get("ProgramArguments") or []
        candidate = str(args[0]) if args else ""
    except Exception:  # noqa: BLE001 - missing/bad plist → fall back
        candidate = ""
    return candidate if _usable(candidate) else ""


def _subprocess_env() -> dict[str, str]:
    """Env for plugin scripts that works under launchd's minimal environment."""
    env = dict(os.environ)
    for key in [k for k in env if k.startswith(("_MEI", "_PYI")) or k in ("PYTHONHOME", "PYTHONPATH")]:
        env.pop(key, None)  # PyInstaller bootloader leftovers must not leak into child pythons
    env.setdefault("HOME", str(Path.home()))
    python = _comet_python()
    dirs: list[str] = []
    if python:
        env["COMET_CONTROL_PYTHON"] = python
        dirs.append(str(Path(python).parent))
    dirs += [str(Path.home() / ".pyenv/shims")]
    dirs += [d for d in env.get("PATH", "").split(os.pathsep) if d]
    dirs += list(_BASE_PATH)
    seen: set[str] = set()
    env["PATH"] = os.pathsep.join(d for d in dirs if not (d in seen or seen.add(d)))
    return env


def _tail(text: str, limit: int = 240) -> str:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    return (lines[-1] if lines else "")[:limit]


def _run_json(command: list[str], *, timeout: float) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False, env=_subprocess_env()
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": str(exc)[:300]}
    raw = proc.stdout.strip()
    if not raw:
        return {"ok": False, "error": (_tail(proc.stderr) or f"command exited {proc.returncode}")[:300]}
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


def _probe_failure(probe: dict[str, Any], broker: dict[str, Any]) -> str:
    """Specific, secret-free reason the Comet Control probe did not pass."""
    if probe.get("success") and not broker.get("runtime_verified"):
        reason = "broker runtime not verified"
    elif probe.get("success") and not broker.get("extension_connected"):
        reason = "Comet extension not connected (open Comet)"
    else:
        reason = str(probe.get("error") or probe.get("error_code") or probe.get("message") or "probe failed")
    return f"Comet Control unavailable: {_tail(reason, 200)}"


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
        self._purpose = DEFAULT_PURPOSE
        self._url = ""
        self._adopt_tried = False
        self._idle_timer: threading.Timer | None = None
        self._idle_gen = 0
        self._listeners: list[Callable[[bool], None]] = []

    def add_listener(self, fn: Callable[[bool], None]) -> None:
        """fn(active) after the lease opens or closes (incl. idle auto-release)."""
        if fn not in self._listeners:
            self._listeners.append(fn)

    def remove_listener(self, fn: Callable[[bool], None]) -> None:
        if fn in self._listeners:
            self._listeners.remove(fn)

    def _notify(self, active: bool) -> None:
        for fn in list(self._listeners):
            try:
                fn(active)
            except Exception:  # noqa: BLE001 — listeners are best effort
                pass

    # -- idle release (self_test only) ------------------------------------
    def _arm_idle(self) -> None:
        self._cancel_idle()
        if LEASE_IDLE_SECONDS <= 0 or self._purpose != "self_test":
            return
        gen = self._idle_gen
        timer = threading.Timer(LEASE_IDLE_SECONDS, self._idle_release, args=(gen,))
        timer.daemon = True
        self._idle_timer = timer
        timer.start()

    def _cancel_idle(self) -> None:
        # Bumping the generation invalidates a timer that already fired and is
        # blocked on _lock behind a long act() — it must not end a fresh lease.
        self._idle_gen += 1
        timer, self._idle_timer = self._idle_timer, None
        if timer is not None:
            timer.cancel()

    def _idle_release(self, gen: int | None = None) -> None:
        with self._lock:
            if gen is not None and gen != self._idle_gen:
                return
            if not self._session_id or self._purpose != "self_test":
                return
        try:
            from voice_cua.activity_log import log_event

            log_event("comet_idle_release", idle_s=LEASE_IDLE_SECONDS)
        except Exception:  # noqa: BLE001
            pass
        self.end(_idle_gen=gen)

    # -- kept-open user_task lease persistence ----------------------------
    def _save_state_locked(self) -> None:
        if self._purpose != "user_task" or not self._session_id or self._workdir is None:
            return
        path = _state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "session_id": self._session_id,
                "workdir": str(self._workdir),
                "python": self._python,
                "controller": str(self._controller or ""),
                "purpose": self._purpose,
                "url": redact_url(self._url),
            }))
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError:
            pass

    def _clear_state(self) -> None:
        try:
            _state_path().unlink()
        except OSError:
            pass

    def _adopt_locked(self) -> bool:
        """Re-attach to a user_task lease a previous helper left open."""
        if self._session_id or self._adopt_tried:
            return bool(self._session_id)
        self._adopt_tried = True
        try:
            state = json.loads(_state_path().read_text())
        except (OSError, json.JSONDecodeError):
            return False
        if not isinstance(state, dict):
            return False
        workdir = Path(str(state.get("workdir") or ""))
        python = str(state.get("python") or "")
        controller = Path(str(state.get("controller") or ""))
        session_id = str(state.get("session_id") or "")
        if not (session_id and workdir.is_dir() and python and controller.is_file()):
            self._clear_state()
            return False
        status = _run_json(
            [python, str(controller), "status", "--workdir", str(workdir)], timeout=15
        )
        if not (status.get("ok") and status.get("session_id") == session_id):
            self._clear_state()
            return False
        self._session_id = session_id
        self._workdir = workdir
        self._python = python
        self._controller = controller
        self._purpose = "user_task"
        self._url = str(state.get("url") or "")
        return True

    # -- controller calls (caller holds _lock) ------------------------------
    def _send_locked(self, actions: list[dict[str, Any]], timeout: float) -> dict[str, Any]:
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

    def _closeout_locked(self) -> dict[str, Any]:
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
            if self._workdir is not None:
                shutil.rmtree(self._workdir, ignore_errors=True)
            if self._purpose == "user_task":
                self._clear_state()
            self._session_id = ""
            self._workdir = None
            self._python = ""
            self._controller = None
            self._url = ""
            self._purpose = DEFAULT_PURPOSE
        return public

    # -- public API ---------------------------------------------------------
    def active(self) -> bool:
        with self._lock:
            if not self._session_id:
                self._adopt_locked()
            return bool(self._session_id)

    def purpose(self) -> str:
        with self._lock:
            return self._purpose if self._session_id else ""

    def begin(self, url: str, *, purpose: str = DEFAULT_PURPOSE) -> dict[str, Any]:
        if not url.startswith(("https://", "http://")):
            return {"ok": False, "error": "url must start with https:// or http://"}
        purpose = purpose if purpose in PURPOSES else DEFAULT_PURPOSE
        notify: bool | None = None
        with self._lock:
            self._adopt_locked()
            if self._session_id and self._purpose == "user_task" and purpose == "self_test":
                # Never run internal checks in (or close) the user's window.
                return {"ok": False, "error": "the user's Comet window is open; self-tests must wait or use comet_act only if the user asked"}
            if self._session_id and self._purpose == "user_task":
                # Follow-up: re-attach to the user's window instead of a new one.
                self._cancel_idle()
                moved = self._send_locked([{"type": "goto", "url": url}], 45)
                if moved.get("ok"):
                    self._url = url
                    self._save_state_locked()
                    return {
                        "ok": True,
                        "lease": "isolated",
                        "reused": True,
                        "purpose": "user_task",
                        "window": "open",
                        "session_id": self._session_id,
                    }
                # Window gone (user closed it?) — clean up the dead lease, start fresh.
                if not self._closeout_locked().get("ok"):
                    return {"ok": False, "error": "existing Comet window is unresponsive and could not be released"}
                notify = False
            elif self._session_id:
                if purpose == "self_test":
                    return {"ok": False, "error": "a self-test Comet lease is already active; end it first"}
                # A finished self-test window is ours to close before the user's task.
                if not self._closeout_locked().get("ok"):
                    return {"ok": False, "error": "could not release the previous self-test lease"}
                notify = False
            started = self._start_locked(url, purpose)
        if notify is False and not started.get("ok"):
            self._notify(False)
        if started.get("ok"):
            self._notify(True)
        return started

    def _start_locked(self, url: str, purpose: str) -> dict[str, Any]:
        root = _runtime_root()
        probe = _run_json([str(root / "scripts/ensure-broker.sh"), "probe", "--json"], timeout=30)
        broker = probe.get("broker") if isinstance(probe.get("broker"), dict) else {}
        if not (
            probe.get("success")
            and broker.get("runtime_verified")
            and broker.get("extension_connected")
        ):
            return {"ok": False, "error": _probe_failure(probe, broker)}
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
                str(LEASE_TTL_SECONDS),
            ],
            timeout=190,
        )
        if not started.get("ok"):
            closed = _run_json(
                [str(python), str(controller), "closeout", "--workdir", str(workdir), "--timeout", "45"],
                timeout=55,
            )
            if closed.get("ok"):
                shutil.rmtree(workdir, ignore_errors=True)
            return {"ok": False, "error": str(started.get("error") or "Comet lease failed")[:300]}
        self._session_id = session_id
        self._workdir = workdir
        self._python = str(python)
        self._controller = controller
        self._purpose = purpose
        self._url = url
        self._save_state_locked()
        self._arm_idle()
        return {
            "ok": True,
            "lease": "isolated",
            "reused": False,
            "purpose": purpose,
            "window": "open",
            "session_id": session_id,
        }

    def act(self, actions: list[dict[str, Any]], *, timeout: float = 45) -> dict[str, Any]:
        if not actions:
            return {"ok": False, "error": "actions must be a non-empty array"}
        unsupported = sorted({str(action.get("type") or "") for action in actions} - ALLOWED_ACTIONS)
        if unsupported:
            return {"ok": False, "error": f"unsupported Comet action: {', '.join(unsupported)}"}
        with self._lock:
            self._adopt_locked()
            if not self._session_id or self._workdir is None or self._controller is None:
                return {"ok": False, "error": "no active Comet task lease; call comet_begin first"}
            self._cancel_idle()
            public = self._send_locked(actions, timeout)
            for action in actions:
                if action.get("type") == "goto" and public.get("ok"):
                    self._url = str(action.get("url") or self._url)
            self._save_state_locked()
            self._arm_idle()
            return public

    def end(self, *, user_requested: bool = False, _idle_gen: int | None = None) -> dict[str, Any]:
        closed_now = False
        with self._lock:
            if _idle_gen is not None and _idle_gen != self._idle_gen:
                return {"ok": True, "closed": False, "skipped": "lease active again"}
            self._adopt_locked()
            if not self._session_id or self._workdir is None or self._controller is None:
                self._cancel_idle()
                return {"ok": True, "closed": False, "already_closed": True, "window": "closed"}
            if self._purpose == "user_task" and not user_requested:
                # Never close the user's window on our own; follow-ups reuse it.
                return {
                    "ok": True,
                    "closed": False,
                    "kept_open": True,
                    "window": "open",
                    "note": "User's Comet window stays open; follow-ups reuse it. Close only if the user asks.",
                }
            self._cancel_idle()
            public = self._closeout_locked()
            if public["ok"]:
                closed_now = True
                public["closed"] = True
                public["window"] = "closed"
        if closed_now:
            self._notify(False)
        return public

    def release_at_exit(self) -> None:
        """Exit: close self_test leases only.

        A user_task window stays open: its durable controller keeps renewing
        and the state file lets the next helper re-attach (no orphan).
        """
        with self._lock:
            if not self._session_id or self._purpose != "self_test":
                return
        self.end()


_LEASE = CometLease()


def comet_is_active() -> bool:
    return _LEASE.active()


def comet_purpose() -> str:
    return _LEASE.purpose()


def comet_begin(url: str, *, purpose: str = DEFAULT_PURPOSE) -> dict[str, Any]:
    return _LEASE.begin(url, purpose=purpose)


def comet_act(actions: list[dict[str, Any]], *, timeout: float = 45) -> dict[str, Any]:
    return _LEASE.act(actions, timeout=timeout)


def comet_end(*, user_requested: bool = False) -> dict[str, Any]:
    return _LEASE.end(user_requested=user_requested)


def comet_add_listener(fn: Callable[[bool], None]) -> None:
    _LEASE.add_listener(fn)


def comet_remove_listener(fn: Callable[[bool], None]) -> None:
    _LEASE.remove_listener(fn)


def _end_at_exit() -> None:
    """Never leave a self-test lease holding Comet after the helper exits."""
    try:
        _LEASE.release_at_exit()
    except Exception:  # noqa: BLE001 — exit path is best effort
        pass


atexit.register(_end_at_exit)
