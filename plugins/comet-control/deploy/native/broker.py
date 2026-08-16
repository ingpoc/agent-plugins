#!/usr/bin/env python3
"""Comet-only broker between the extension and local agent clients."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import importlib.metadata
import importlib.util
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from websockets.exceptions import ConnectionClosed
from websockets.sync.server import ServerConnection, serve


PROTOCOL_VERSION = 1
MAX_CLIENT_REQUEST_BYTES = 4 * 1024 * 1024
MAX_EXTENSION_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_PENDING_REQUESTS = 64
FLIGHT_RECORDER_LIMIT = 20
DEFAULT_TIMEOUT_SECONDS = 90.0
MAX_TIMEOUT_SECONDS = 300.0
BROKER_HOST = "127.0.0.1"
BROKER_PORT = int(os.environ.get("COMET_CONTROL_BROKER_PORT", "38927"))
EXTENSION_LIVENESS_SECONDS = 40.0
VISUAL_FOCUS_WAIT_SECONDS = 30.0
VISUAL_REQUEST_TYPES = frozenset({"reload", "session_preflight", "session_closeout", "run"})
VISUAL_LOCK_SCRIPT = Path(
    os.environ.get(
        "MACOS_CUA_VISUAL_LOCK_MODULE",
        str(Path.home() / ".agents/skills/macos-cua/scripts/visual_focus_lock.py"),
    )
)


def _runtime_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "plugin.json").is_file() and (
            parent / "plugin" / "comet_control"
        ).is_dir():
            return parent
        if parent.name == "comet-control-cursor-wip" and (
            parent / "plugin" / "comet_control"
        ).is_dir():
            return parent
    raise RuntimeError("Comet Control broker could not resolve its repository root")


def _default_socket_path() -> Path:
    """Prefer COMET_CONTROL_BRIDGE_SOCKET. Otherwise bind <plugin>/run/comet-control.sock.

    Never fall back to a legacy ~/.comet-control socket.
    """
    env = os.environ.get("COMET_CONTROL_BRIDGE_SOCKET")
    if env:
        return Path(env)
    return _runtime_root() / "run" / "comet-control.sock"


SOCKET_PATH = _default_socket_path()
PAIRING_PATH = SOCKET_PATH.parent / "comet-control-pairing.json"
BROKER_BUILD_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
BROKER_STARTED_AT_MS = int(time.time() * 1000)

pending: dict[str, queue.Queue[dict[str, Any]]] = {}
pending_generation: dict[str, int] = {}
pending_lock = threading.Lock()
outbound: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=MAX_PENDING_REQUESTS)
socket_ready = threading.Event()
socket_error: list[str] = []
socket_identity: tuple[int, int] | None = None
extension_seen_at = 0.0
extension_seen_lock = threading.Lock()
extension_connection_lock = threading.RLock()
extension_send_lock = threading.Lock()
active_extension: ServerConnection | None = None
active_extension_generation = 0
active_extension_info: dict[str, Any] = {}
active_extension_connected_at = 0.0
pairing_lock = threading.Lock()
_VISUAL_LOCK_MODULE = None
visual_module_lock = threading.Lock()
_cua_claim_public: dict[str, Any] | None = None
cua_claim_lock = threading.Lock()


def _expected_user_data_dir() -> Path | None:
    raw = os.environ.get("COMET_CONTROL_EXPECTED_USER_DATA_DIR", "").strip()
    return Path(raw).expanduser().resolve() if raw else None


def _has_exact_command_value(command: str, flag: str, expected: str) -> bool:
    value = re.escape(expected)
    pattern = rf"(?:^|\s){re.escape(flag)}(?:=|\s+)(?:{value}|\"{value}\"|'{value}')(?=\s|$)"
    return re.search(pattern, command) is not None


def _process_field(pid: int, field: str) -> str:
    completed = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", f"{field}="],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return completed.stdout.strip()


def _attest_comet_runtime() -> dict[str, Any]:
    """Find the exact configured Comet executable and profile."""
    expected = _expected_user_data_dir()
    expected_browser_raw = os.environ.get("COMET_CONTROL_EXPECTED_BROWSER_EXECUTABLE", "").strip()
    expected_origin = os.environ.get("COMET_CONTROL_EXPECTED_EXTENSION_ORIGIN", "").strip()
    if expected is None or not expected_browser_raw or not expected_origin:
        return {
            "verified": False,
            "error_code": "RUNTIME_CONTRACT_MISSING",
            "error": "Comet path, profile directory, and extension origin are required",
        }
    expected_browser = Path(expected_browser_raw).expanduser().resolve()
    try:
        completed = subprocess.run(
            ["/usr/bin/pgrep", "-x", "Comet"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if completed.returncode not in (0, 1):
            raise RuntimeError(f"pgrep exited {completed.returncode}")
    except Exception as exc:
        return {
            "verified": False,
            "expected_user_data_dir": str(expected),
            "error_code": "RUNTIME_PROCESS_UNREADABLE",
            "error": f"Could not inspect Comet processes: {exc}",
        }
    default_user_data_dir = (
        Path.home() / "Library/Application Support/Comet"
    ).resolve()
    for raw_pid in completed.stdout.splitlines():
        try:
            pid = int(raw_pid.strip())
            executable = Path(_process_field(pid, "comm")).resolve()
            command = _process_field(pid, "command")
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
        if executable != expected_browser:
            continue
        has_user_data_flag = re.search(r"(?:^|\s)--user-data-dir(?:=|\s)", command) is not None
        runtime_matches = (
            not has_user_data_flag
            if expected == default_user_data_dir
            else _has_exact_command_value(command, "--user-data-dir", str(expected))
        )
        if runtime_matches:
            return {
                "verified": True,
                "expected_user_data_dir": str(expected),
                "browser_pid": pid,
            }
    return {
        "verified": False,
        "expected_user_data_dir": str(expected),
        "error_code": "COMET_RUNTIME_NOT_FOUND",
        "error": "The configured logged-in Comet runtime is not running",
    }


def _write_runtime_diagnostic(attestation: dict[str, Any]) -> None:
    raw = os.environ.get("COMET_CONTROL_DIAGNOSTIC_LOG", "").strip()
    if not raw:
        return
    path = Path(raw).expanduser().resolve()
    payload = {
        key: attestation.get(key)
        for key in (
            "verified",
            "error_code",
            "error",
            "expected_user_data_dir",
            "browser_pid",
        )
        if attestation.get(key) is not None
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, sort_keys=True) + "\n")
        path.chmod(0o600)
    except OSError:
        pass


def _screenshot_dir() -> Path:
    """Keep captures inside the plugin tree, never under ~/.comet-control."""
    return _runtime_root() / "run" / "cache" / "comet-control"


# ── Comet extension loopback transport ──────────────────────────────────────

def _expected_extension_origin() -> str:
    return os.environ.get("COMET_CONTROL_EXPECTED_EXTENSION_ORIGIN", "").strip().rstrip("/")


def _mark_extension_seen() -> None:
    global extension_seen_at
    with extension_seen_lock:
        extension_seen_at = time.monotonic()


def _extension_is_live() -> bool:
    with extension_connection_lock:
        connected = active_extension is not None
    with extension_seen_lock:
        return connected and extension_seen_at > 0 and time.monotonic() - extension_seen_at <= EXTENSION_LIVENESS_SECONDS


def _pairing_digest() -> str | None:
    try:
        payload = json.loads(PAIRING_PATH.read_text())
        digest = str(payload.get("secret_sha256") or "")
        return digest if re.fullmatch(r"[0-9a-f]{64}", digest) else None
    except (OSError, json.JSONDecodeError):
        return None


def _accept_pairing_secret(secret: Any) -> bool:
    value = str(secret or "")
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        return False
    digest = hashlib.sha256(value.encode()).hexdigest()
    with pairing_lock:
        expected = _pairing_digest()
        if expected is not None:
            return hmac.compare_digest(digest, expected)
        PAIRING_PATH.parent.mkdir(parents=True, exist_ok=True)
        PAIRING_PATH.parent.chmod(0o700)
        payload = json.dumps({
            "version": 1,
            "secret_sha256": digest,
            "created_at_ms": int(time.time() * 1000),
        }, sort_keys=True) + "\n"
        try:
            descriptor = os.open(PAIRING_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            expected = _pairing_digest()
            return expected is not None and hmac.compare_digest(digest, expected)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(payload)
        return True


def _validate_extension_hello(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 64 * 1024:
        raise ValueError("invalid extension hello size")
    message = json.loads(raw)
    if not isinstance(message, dict) or message.get("type") != "broker_hello":
        raise ValueError("extension hello required")
    if message.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError("protocol version mismatch")
    if not _accept_pairing_secret(message.get("pairing_secret")):
        raise ValueError("extension pairing mismatch")
    build = str(message.get("extension_build_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", build):
        raise ValueError("invalid extension build identity")
    capabilities = message.get("capabilities")
    if not isinstance(capabilities, list) or not all(isinstance(item, str) for item in capabilities):
        raise ValueError("invalid extension capabilities")
    return {
        "protocol_version": PROTOCOL_VERSION,
        "extension_version": str(message.get("extension_version") or "")[:64],
        "extension_build_sha256": build,
        "capabilities": sorted(set(capabilities))[:64],
    }


def _fail_generation(generation: int, code: str, error: str) -> None:
    with pending_lock:
        targets = [
            (request_id, pending[request_id])
            for request_id, request_generation in pending_generation.items()
            if request_generation == generation and request_id in pending
        ]
    for request_id, response_q in targets:
        try:
            response_q.put_nowait({
                "id": request_id,
                "success": False,
                "error_code": code,
                "error": error,
                "retryable": True,
            })
        except queue.Full:
            pass


def _register_extension(websocket: ServerConnection, info: dict[str, Any]) -> int:
    global active_extension, active_extension_generation
    global active_extension_info, active_extension_connected_at
    with extension_connection_lock:
        previous = active_extension
        previous_generation = active_extension_generation
        active_extension_generation += 1
        generation = active_extension_generation
        active_extension = websocket
        active_extension_info = dict(info)
        active_extension_connected_at = time.monotonic()
    if previous is not None and previous is not websocket:
        previous.close(code=1012, reason="superseded extension connection")
        _fail_generation(
            previous_generation,
            "EXTENSION_REPLACED",
            "Comet Control extension connection was replaced; verify page state before retrying",
        )
    _mark_extension_seen()
    return generation


def _extension_is_current(websocket: ServerConnection, generation: int) -> bool:
    with extension_connection_lock:
        return active_extension is websocket and active_extension_generation == generation


def _unregister_extension(websocket: ServerConnection, generation: int) -> None:
    global active_extension, active_extension_info, active_extension_connected_at
    with extension_connection_lock:
        if active_extension is not websocket or active_extension_generation != generation:
            return
        active_extension = None
        active_extension_info = {}
        active_extension_connected_at = 0.0
    with extension_seen_lock:
        global extension_seen_at
        extension_seen_at = 0.0
    _fail_generation(
        generation,
        "EXTENSION_DISCONNECTED",
        "Comet Control extension disconnected; verify page state before retrying",
    )
    while True:
        try:
            item = outbound.get_nowait()
        except queue.Empty:
            break
        if int(item.get("generation", -1)) != generation:
            try:
                outbound.put_nowait(item)
            except queue.Full:
                pass


def _active_extension_snapshot() -> tuple[int, dict[str, Any]] | None:
    with extension_connection_lock:
        if active_extension is None:
            return None
        return active_extension_generation, dict(active_extension_info)


def _broker_status(attestation: dict[str, Any]) -> dict[str, Any]:
    with extension_connection_lock:
        connected = active_extension is not None
        generation = active_extension_generation if connected else None
        info = dict(active_extension_info)
        connected_at = active_extension_connected_at
    with extension_seen_lock:
        heartbeat_age_ms = (
            int((time.monotonic() - extension_seen_at) * 1000)
            if extension_seen_at > 0 else None
        )
    with pending_lock:
        pending_count = len(pending)
    try:
        websockets_version = importlib.metadata.version("websockets")
    except importlib.metadata.PackageNotFoundError:
        websockets_version = "unknown"
    ready = bool(attestation.get("verified")) and connected and _extension_is_live()
    return {
        "success": ready,
        "broker": {
            "pid": os.getpid(),
            "browser_pid": attestation.get("browser_pid"),
            "socket_path": str(SOCKET_PATH),
            "runtime_verified": bool(attestation.get("verified")),
            "extension_connected": connected and _extension_is_live(),
            "user_data_dir": attestation.get("expected_user_data_dir"),
            "protocol_version": PROTOCOL_VERSION,
            "broker_build_sha256": BROKER_BUILD_SHA256,
            "broker_started_at_ms": BROKER_STARTED_AT_MS,
            "python_executable": str(Path(sys.executable).resolve()),
            "websockets_version": websockets_version,
            "pairing_established": _pairing_digest() is not None,
            "connection_generation": generation,
            "connection_age_ms": (
                int((time.monotonic() - connected_at) * 1000)
                if connected_at > 0 else None
            ),
            "heartbeat_age_ms": heartbeat_age_ms,
            "pending_count": pending_count,
            "outbound_queue_depth": outbound.qsize(),
            **info,
        },
        **({} if ready else {
            "error_code": attestation.get("error_code", "EXTENSION_NOT_CONNECTED"),
            "error": attestation.get("error", "Comet Control extension is not connected"),
        }),
    }


def extension_connection(websocket: ServerConnection) -> None:
    actual_origin = str(websocket.request.headers.get("Origin") or "").strip().rstrip("/")
    expected_origin = _expected_extension_origin()
    if not expected_origin or not hmac.compare_digest(actual_origin, expected_origin):
        websocket.close(code=1008, reason="wrong extension origin")
        return
    if not _attest_comet_runtime().get("verified"):
        websocket.close(code=1008, reason="Comet runtime not verified")
        return
    try:
        info = _validate_extension_hello(websocket.recv(timeout=45))
    except (ConnectionClosed, TimeoutError, ValueError, json.JSONDecodeError):
        websocket.close(code=1008, reason="Comet Control handshake rejected")
        return
    generation = _register_extension(websocket, info)
    websocket.send(json.dumps({
        "type": "broker_hello_ack",
        "protocol_version": PROTOCOL_VERSION,
        "broker_build_sha256": BROKER_BUILD_SHA256,
        "connection_generation": generation,
    }))
    last_sent_at = time.monotonic()
    try:
        while _extension_is_current(websocket, generation):
            try:
                item = None
                with extension_send_lock:
                    if not _extension_is_current(websocket, generation):
                        return
                    try:
                        item = outbound.get(timeout=0.25)
                    except queue.Empty:
                        pass
                    if item is not None:
                        request_id = str(item["message"]["id"])
                        if item["generation"] != generation:
                            _fail_generation(
                                int(item["generation"]),
                                "EXTENSION_REPLACED",
                                "Comet Control extension generation changed before dispatch",
                            )
                        elif int(item["deadline_at_ms"]) <= int(time.time() * 1000):
                            with pending_lock:
                                response_q = pending.get(request_id)
                            if response_q is not None:
                                try:
                                    response_q.put_nowait({
                                        "id": request_id,
                                        "success": False,
                                        "error_code": "REQUEST_EXPIRED",
                                        "error": "Comet Control request expired before extension dispatch",
                                    })
                                except queue.Full:
                                    pass
                        else:
                            websocket.send(json.dumps(item["message"], ensure_ascii=False))
                            last_sent_at = time.monotonic()
                if item is None and time.monotonic() - last_sent_at >= 20:
                    websocket.send(json.dumps({"id": str(uuid.uuid4()), "type": "broker_ping"}))
                    last_sent_at = time.monotonic()
                try:
                    raw = websocket.recv(timeout=0.01)
                except TimeoutError:
                    continue
                if raw is None:
                    return
                if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_EXTENSION_RESPONSE_BYTES:
                    websocket.close(code=1009, reason="invalid response size")
                    return
                message = json.loads(raw)
                if not isinstance(message, dict) or not str(message.get("id") or ""):
                    websocket.close(code=1007, reason="invalid extension response")
                    return
                _mark_extension_seen()
                with pending_lock:
                    response_q = pending.get(str(message["id"]))
                    response_generation = pending_generation.get(str(message["id"]))
                if response_q is not None and response_generation == generation:
                    try:
                        response_q.put_nowait(message)
                    except queue.Full:
                        pass
            except json.JSONDecodeError:
                websocket.close(code=1007, reason="invalid extension response")
                return
    except ConnectionClosed:
        return
    finally:
        _unregister_extension(websocket, generation)


def extension_server() -> None:
    with serve(
        extension_connection,
        BROKER_HOST,
        BROKER_PORT,
        origins=[_expected_extension_origin()],
        ping_interval=20,
        ping_timeout=20,
        max_size=MAX_EXTENSION_RESPONSE_BYTES,
        max_queue=16,
    ) as server:
        server.serve_forever()


def _outbound_request(
    request_id: str, request: dict[str, Any], deadline_at_ms: int
) -> dict[str, Any]:
    """Broker-owned correlation identity always wins over client input."""
    return {**request, "id": request_id, "deadlineAt": deadline_at_ms}


def _visual_lock_module():
    global _VISUAL_LOCK_MODULE
    if _VISUAL_LOCK_MODULE is not None:
        return _VISUAL_LOCK_MODULE
    with visual_module_lock:
        if _VISUAL_LOCK_MODULE is None:
            spec = importlib.util.spec_from_file_location(
                "comet_control_broker_visual_focus_lock", VISUAL_LOCK_SCRIPT
            )
            if spec is None or spec.loader is None:
                raise RuntimeError(f"cannot load visual focus owner: {VISUAL_LOCK_SCRIPT}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _VISUAL_LOCK_MODULE = module
    return _VISUAL_LOCK_MODULE


def _active_cua_claim() -> dict[str, Any] | None:
    global _cua_claim_public
    with cua_claim_lock:
        claim = _cua_claim_public
        if claim is None:
            return None
        try:
            expired = float(claim.get("expires_at")) <= time.time() * 1000
        except (TypeError, ValueError):
            expired = False
        if expired:
            _cua_claim_public = None
            return None
        return dict(claim)


def _set_cua_claim(claim: Any) -> None:
    global _cua_claim_public
    safe_claim = dict(claim) if isinstance(claim, dict) else None
    with cua_claim_lock:
        _cua_claim_public = safe_claim


def _sync_cua_claim(request: dict[str, Any], response: dict[str, Any]) -> None:
    if not response.get("success"):
        return
    request_type = request.get("type")
    if request_type in {"cua_runtime_claim", "cua_runtime_validate"}:
        _set_cua_claim(response.get("claim"))
    elif request_type == "cua_runtime_release":
        _set_cua_claim(None)
    elif request_type in {"sessions", "status"}:
        _set_cua_claim(response.get("cua_claim"))


def _cua_claim_rejection(claim: dict[str, Any]) -> dict[str, Any]:
    return {
        "success": False,
        "error_code": "CUA_RUNTIME_CLAIMED",
        "error": (
            f"Comet is reserved for {claim.get('intent', 'native control')} "
            f"until {claim.get('expires_at', 'claim expiry')}"
        ),
        "cua_claim": claim,
    }


def _forward_extension_request(
    request: dict[str, Any], timeout_seconds: float
) -> dict[str, Any]:
    request_id = str(uuid.uuid4())
    deadline_at_ms = int(time.time() * 1000 + timeout_seconds * 1000)
    response_q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
    with extension_connection_lock:
        if active_extension is None or not _extension_is_live():
            return {
                "id": request_id,
                "success": False,
                "error_code": "EXTENSION_NOT_CONNECTED",
                "error": "Comet Control extension is not connected",
                "retryable": True,
            }
        generation = active_extension_generation
        with pending_lock:
            if len(pending) >= MAX_PENDING_REQUESTS:
                return {
                    "id": request_id,
                    "success": False,
                    "error_code": "BROKER_BUSY",
                    "error": "Comet Control broker has reached its pending request limit",
                    "retryable": True,
                }
            pending[request_id] = response_q
            pending_generation[request_id] = generation
        try:
            outbound.put_nowait({
                "generation": generation,
                "deadline_at_ms": deadline_at_ms,
                "message": _outbound_request(request_id, request, deadline_at_ms),
            })
        except queue.Full:
            with pending_lock:
                pending.pop(request_id, None)
                pending_generation.pop(request_id, None)
            return {
                "id": request_id,
                "success": False,
                "error_code": "BROKER_BUSY",
                "error": "Comet Control broker outbound queue is full",
                "retryable": True,
            }
    try:
        # The extension enforces the browser-operation deadline. Keep the host
        # alive briefly longer so it can frame/materialize that reply.
        response = response_q.get(timeout=timeout_seconds + 2.0)
        response = _materialize_response(response)
    except queue.Empty:
        operation = str(request.get("type") or "request")
        response = {
            "id": request_id,
            "success": False,
            "error_code": "EXTENSION_TIMEOUT",
            "error": f"Comet Control extension timed out during {operation}",
        }
    finally:
        with pending_lock:
            pending.pop(request_id, None)
            pending_generation.pop(request_id, None)
    _sync_cua_claim(request, response)
    return response


def _acquire_visual_focus(
    request: dict[str, Any], timeout_seconds: float
) -> tuple[Any | None, dict[str, Any] | None]:
    if request.get("type") not in VISUAL_REQUEST_TYPES:
        return None, None

    claim = _active_cua_claim()
    if claim:
        return None, _cua_claim_rejection(claim)

    try:
        focus = _visual_lock_module()
    except (ImportError, OSError, RuntimeError) as exc:
        return None, {
            "success": False,
            "error_code": "VISUAL_FOCUS_UNAVAILABLE",
            "error": str(exc),
        }

    owner = f"comet-control-broker:{request.get('type')}:{request.get('sessionId') or 'runtime'}"
    try:
        return focus.acquire(owner, timeout=0), None
    except focus.VisualFocusBusy:
        # A sibling broker request can hold focus while the extension is busy
        # serving it. Do not query that same extension before waiting.
        claim = _active_cua_claim()
        if claim:
            return None, _cua_claim_rejection(claim)
        try:
            wait_seconds = min(VISUAL_FOCUS_WAIT_SECONDS, timeout_seconds)
            lease = focus.acquire(owner, timeout=wait_seconds)
            claim = _active_cua_claim()
            if claim:
                lease.release()
                return None, _cua_claim_rejection(claim)
            return lease, None
        except focus.VisualFocusBusy:
            return None, {
                "success": False,
                "error_code": "VISUAL_FOCUS_BUSY",
                "error": f"macOS visual focus remained busy for {wait_seconds:.1f}s",
            }
        except (OSError, RuntimeError) as exc:
            return None, {
                "success": False,
                "error_code": "VISUAL_FOCUS_UNAVAILABLE",
                "error": str(exc),
            }
    except (OSError, RuntimeError) as exc:
        return None, {
            "success": False,
            "error_code": "VISUAL_FOCUS_UNAVAILABLE",
            "error": str(exc),
        }


# ── Unix socket server (this process ↔ local agent clients) ─────────────────

def _socket_listener_is_active() -> bool:
    if not SOCKET_PATH.exists():
        return False
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.2)
    try:
        probe.connect(str(SOCKET_PATH))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _remove_owned_socket() -> None:
    if socket_identity is None:
        return
    try:
        current = SOCKET_PATH.stat()
        if (current.st_dev, current.st_ino) == socket_identity:
            SOCKET_PATH.unlink()
    except FileNotFoundError:
        pass


def socket_server() -> None:
    global socket_identity
    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
    home = Path.home().resolve()
    parent = SOCKET_PATH.parent.resolve()
    if parent == home or home in parent.parents:
        SOCKET_PATH.parent.chmod(0o700)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        if _socket_listener_is_active():
            raise RuntimeError(f"Another Comet Control broker owns {SOCKET_PATH}")
        SOCKET_PATH.unlink(missing_ok=True)
        server.bind(str(SOCKET_PATH))
        SOCKET_PATH.chmod(0o600)
        current = SOCKET_PATH.stat()
        socket_identity = (current.st_dev, current.st_ino)
        server.listen(16)
        socket_ready.set()
        while True:
            conn, _ = server.accept()
            # An in-flight visual request must outlive a client disconnect so
            # its focus lease remains held through the extension response.
            threading.Thread(target=handle_client, args=(conn,), daemon=False).start()
    except Exception as exc:
        socket_error.append(str(exc))
        socket_ready.set()
    finally:
        server.close()


def handle_client(conn: socket.socket) -> None:
    visual_lease = None
    with conn:
        conn.settimeout(5.0)
        chunks: list[bytes] = []
        try:
            request: dict[str, Any] | None = None
            total = 0
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                total += len(chunk)
                if total > MAX_CLIENT_REQUEST_BYTES:
                    raise ValueError(f"Request exceeds {MAX_CLIENT_REQUEST_BYTES} bytes")
                chunks.append(chunk)
                try:
                    decoded = json.loads(b"".join(chunks).decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(decoded, dict):
                    raise ValueError("Request must be a JSON object")
                request = decoded
                break

            timeout_seconds = _validated_timeout_seconds(request.get("timeoutSeconds"))
            if request.get("type") == "broker_status":
                attestation = _attest_comet_runtime()
                response = _broker_status(attestation)
                conn.sendall(json.dumps(response, ensure_ascii=False).encode("utf-8"))
                return
            visual_lease, focus_error = _acquire_visual_focus(request, timeout_seconds)
            response = focus_error or _forward_extension_request(request, timeout_seconds)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            response = {"success": False, "error_code": "INVALID_REQUEST", "error": str(exc)}
        try:
            conn.sendall(json.dumps(response, ensure_ascii=False).encode("utf-8"))
        except OSError:
            pass
        finally:
            if visual_lease is not None:
                visual_lease.release()


def _validated_timeout_seconds(raw: Any) -> float:
    if raw is None:
        return DEFAULT_TIMEOUT_SECONDS
    if isinstance(raw, bool):
        raise ValueError("timeoutSeconds must be a finite number")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeoutSeconds must be a finite number") from exc
    if not (value == value and value not in (float("inf"), float("-inf"))):
        raise ValueError("timeoutSeconds must be a finite number")
    if value < 1 or value > MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeoutSeconds must be between 1 and {int(MAX_TIMEOUT_SECONDS)}")
    return value


def _safe_screenshot_extension(raw: Any) -> str:
    value = str(raw or "png").lower()
    if value == "jpg":
        value = "jpeg"
    if value not in {"png", "jpeg"}:
        raise ValueError(f"Unsupported screenshot format: {value}")
    return value


def _safe_capture_id(raw: Any) -> str:
    value = "".join(ch for ch in str(raw or "capture") if ch.isalnum() or ch in "-_")
    return value[:96] or "capture"


def _materialize_screenshot_item(
    item: dict[str, Any], response_id: Any, suffix: str
) -> None:
    data = item.pop("base64", None)
    if not isinstance(data, str):
        return
    screenshot_dir = _screenshot_dir()
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    ext = _safe_screenshot_extension(item.get("format", "png"))
    path = screenshot_dir / f"{_safe_capture_id(response_id)}-{suffix}.{ext}"
    try:
        decoded = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Screenshot response contained invalid base64") from exc
    path.write_bytes(decoded)
    path.chmod(0o600)
    item["screenshot_path"] = str(path)


def _retain_failure_record(response: dict[str, Any]) -> None:
    record = response.get("failure_record")
    if not isinstance(record, dict):
        return
    screenshot = record.get("screenshot")
    if isinstance(screenshot, dict):
        _materialize_screenshot_item(screenshot, response.get("id"), "failure")
    directory = _screenshot_dir().parent / "flight-recorder"
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    timestamp = int(time.time() * 1000)
    path = directory / f"{timestamp}-{_safe_capture_id(response.get('id'))}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    path.chmod(0o600)
    records = sorted(directory.glob("*.json"), key=lambda candidate: candidate.stat().st_mtime_ns)
    for stale in records[:-FLIGHT_RECORDER_LIMIT]:
        stale.unlink(missing_ok=True)
    response["failure_record_path"] = str(path)


def _materialize_response(response: dict[str, Any]) -> dict[str, Any]:
    results = response.get("results")
    if isinstance(results, list):
        for index, item in enumerate(results):
            if isinstance(item, dict) and item.get("type") == "screenshot":
                _materialize_screenshot_item(item, response.get("id"), str(index))
    _retain_failure_record(response)

    return response


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    if not _expected_user_data_dir() or not os.environ.get(
        "COMET_CONTROL_EXPECTED_BROWSER_EXECUTABLE", ""
    ).strip() or not _expected_extension_origin():
        print("Comet path, profile directory, and extension origin are required", file=sys.stderr)
        return 73
    threading.Thread(target=socket_server, daemon=True).start()
    if not socket_ready.wait(timeout=2) or socket_error:
        if socket_error:
            print(socket_error[-1], file=sys.stderr)
        return 1
    try:
        extension_server()
        return 0
    except OSError as exc:
        print(f"Could not bind Comet extension broker: {exc}", file=sys.stderr)
        return 1
    finally:
        _remove_owned_socket()


def probe() -> int:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(3.0)
    try:
        client.connect(str(SOCKET_PATH))
        client.sendall(json.dumps({"type": "broker_status"}).encode("utf-8"))
        client.shutdown(socket.SHUT_WR)
        payload = json.loads(client.recv(65536).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        payload = {
            "success": False,
            "error_code": "BROKER_UNAVAILABLE",
            "error": str(exc),
            "broker": {"socket_path": str(SOCKET_PATH)},
        }
    finally:
        client.close()
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload.get("success") else 2


if __name__ == "__main__":
    raise SystemExit(probe() if sys.argv[1:] == ["probe"] else main())
