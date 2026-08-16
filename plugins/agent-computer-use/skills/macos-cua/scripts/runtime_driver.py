# Runtime dependencies are injected by the compatibility facade.
# ruff: noqa: F401, F821
"""cua-driver transport, version checks, and foreground readiness.

Loaded behind the stable macos-cua compatibility facade.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_DRIVER_CALL_STATS = {"calls": 0, "stdout_bytes": 0}
_DRIVER_SOCKET_STATE = {"sock": None}


def reset_driver_call_stats():
    _DRIVER_CALL_STATS["calls"] = 0
    _DRIVER_CALL_STATS["stdout_bytes"] = 0


def driver_call_stats():
    return dict(_DRIVER_CALL_STATS)


def _note_driver_call(stdout):
    _DRIVER_CALL_STATS["calls"] += 1
    if stdout is None:
        return
    if isinstance(stdout, bytes):
        _DRIVER_CALL_STATS["stdout_bytes"] += len(stdout)
    else:
        _DRIVER_CALL_STATS["stdout_bytes"] += len(str(stdout).encode())


def reset_driver_socket():
    sock = _DRIVER_SOCKET_STATE["sock"]
    _DRIVER_SOCKET_STATE["sock"] = None
    if sock is None:
        return
    try:
        sock.close()
    except OSError:
        pass


def _driver_socket_path():
    override = os.environ.get("MACOS_CUA_DRIVER_SOCKET")
    if override:
        return Path(override)
    return Path.home() / "Library" / "Caches" / "cua-driver" / "cua-driver.sock"


def _connect_driver_socket(timeout):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(os.fspath(_driver_socket_path()))
    except OSError:
        sock.close()
        raise
    return sock


def _socket_recv_line(sock):
    chunks = []
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            raise OSError("cua-driver socket closed")
        chunks.append(chunk)
        if b"\n" in chunk:
            break
    return b"".join(chunks).split(b"\n", 1)[0].decode()


def _socket_call(tool_name, params, timeout):
    sock = _DRIVER_SOCKET_STATE["sock"]
    if sock is None:
        sock = _connect_driver_socket(timeout)
        _DRIVER_SOCKET_STATE["sock"] = sock
    else:
        sock.settimeout(timeout)
    sock.sendall(
        (
            json.dumps(
                {
                    "method": "call",
                    "name": tool_name,
                    "args": params or {},
                }
            )
            + "\n"
        ).encode()
    )
    line = _socket_recv_line(sock)
    try:
        envelope = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError("cua-driver socket returned invalid JSON") from exc
    if not isinstance(envelope, dict):
        raise ValueError("cua-driver socket returned a non-object")
    return envelope


def _timeout_error(tool_name, timeout):
    return {
        "error": f"cua-driver socket call '{tool_name}' timed out after {timeout}s"
    }


def _socket_error(tool_name, exc):
    return {"error": str(exc) or f"cua-driver socket call '{tool_name}' failed"}


def _translate_socket_envelope(envelope):
    if envelope.get("ok") is True:
        result = envelope.get("result")
        structured = (
            result.get("structuredContent") if isinstance(result, dict) else None
        )
        if isinstance(structured, dict):
            return structured
        if isinstance(result, dict) and result.get("isError"):
            content = result.get("content")
            text = ""
            if isinstance(content, list) and content:
                first = content[0]
                if isinstance(first, dict):
                    text = str(first.get("text") or "").strip()
            return {"error": text or "cua-driver reported an error with no text"}
        return {
            "error": "cua-driver socket response had no structuredContent",
            "raw": json.dumps(envelope)[:500],
        }
    if envelope.get("ok") is False:
        return {"error": str(envelope.get("error") or "").strip()}
    raise ValueError("cua-driver socket returned an invalid envelope")


def _attempt_socket_call(tool_name, params, timeout):
    return _translate_socket_envelope(_socket_call(tool_name, params, timeout))


def _retry_after_restart(tool_name, params, timeout):
    if not _restart_driver_daemon():
        return None
    try:
        return _attempt_socket_call(tool_name, params, timeout)
    except TimeoutError:
        reset_driver_socket()
        return _timeout_error(tool_name, timeout)
    except OSError as exc:
        reset_driver_socket()
        return _socket_error(tool_name, exc)
    except (ValueError, UnicodeDecodeError) as exc:
        reset_driver_socket()
        return {"error": str(exc)}


def _call_driver_socket(tool_name, params, timeout, recover_timeout):
    try:
        return _attempt_socket_call(tool_name, params, timeout)
    except TimeoutError:
        reset_driver_socket()
        if recover_timeout:
            recovered = _retry_after_restart(tool_name, params, timeout)
            if recovered is not None:
                return recovered
        return _timeout_error(tool_name, timeout)
    except OSError:
        reset_driver_socket()
        try:
            return _attempt_socket_call(tool_name, params, timeout)
        except TimeoutError:
            reset_driver_socket()
            if recover_timeout:
                recovered = _retry_after_restart(tool_name, params, timeout)
                if recovered is not None:
                    return recovered
            return _timeout_error(tool_name, timeout)
        except OSError as retry_exc:
            reset_driver_socket()
            if recover_timeout:
                recovered = _retry_after_restart(tool_name, params, timeout)
                if recovered is not None:
                    return recovered
            return _socket_error(tool_name, retry_exc)
    except (ValueError, UnicodeDecodeError) as exc:
        reset_driver_socket()
        return {"error": str(exc)}


def _restart_driver_daemon():
    reset_driver_socket()
    try:
        result = subprocess.run(
            [
                "launchctl",
                "kickstart",
                "-k",
                f"gui/{os.getuid()}/com.trycua.driver",
            ],
            capture_output=True,
            timeout=15,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def call_driver(tool_name, params=None, timeout=30, _recover_timeout=True):
    started = time.monotonic()
    result = _call_driver_socket(tool_name, params, timeout, _recover_timeout)
    telemetry_record_driver(time.monotonic() - started)
    _note_driver_call(json.dumps(result))
    return result


def _driver_max_image_dimension():
    """Read the active driver's image cap used by window screenshots."""
    try:
        result = subprocess.run(
            [CUA_DRIVER, "config"],
            capture_output=True,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, f"cua-driver config unavailable: {error}"
    if result.returncode != 0:
        error = (result.stderr or result.stdout).strip() or "cua-driver config failed"
        return None, error
    try:
        config = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, "cua-driver config returned invalid JSON"
    value = config.get("max_image_dimension") if isinstance(config, dict) else None
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        return None, "cua-driver config omitted a positive max_image_dimension"
    return float(value), None


def _parse_driver_version(value):
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", str(value or ""))
    return tuple(int(part) for part in match.groups()) if match else None


def driver_version():
    """Report whether the installed driver satisfies this wrapper's live contract."""
    minimum = ".".join(str(part) for part in MIN_CUA_DRIVER_VERSION)
    try:
        result = subprocess.run(
            [CUA_DRIVER, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"ok": False, "minimum": minimum, "error": str(error)}
    raw = (result.stdout or result.stderr or "").strip()
    parsed = _parse_driver_version(raw)
    return {
        "ok": bool(result.returncode == 0 and parsed and parsed >= MIN_CUA_DRIVER_VERSION),
        "installed": ".".join(str(part) for part in parsed) if parsed else None,
        "minimum": minimum,
        "raw": raw[:120],
    }


def _frontmost_pid():
    """Return the current AppKit frontmost PID without AppleScript."""
    try:
        from AppKit import NSWorkspace

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return int(app.processIdentifier()) if app else None
    except (AttributeError, ImportError, TypeError, ValueError):
        try:
            result = subprocess.run(
                [
                    "osascript",
                    "-e",
                    'tell application "System Events" to unix id of first process whose frontmost is true',
                ],
                capture_output=True,
                text=True,
                timeout=1,
                stdin=subprocess.DEVNULL,
            )
            return int((result.stdout or "").strip()) if result.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired, TypeError, ValueError):
            return None


def _system_events_process_frontmost(pid):
    """Independent PID-specific focus readback for multi-instance app bundles."""
    script = (
        'tell application "System Events" to frontmost of '
        f'(first process whose unix id is {int(pid)})'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=1,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired, TypeError, ValueError):
        return False
    return result.returncode == 0 and (result.stdout or "").strip().lower() == "true"


def _wait_for_foreground_readiness(pid, window_id, timeout=1.5):
    """Require stable AppKit focus plus a usable AX window when AX is available."""
    started = time.monotonic()
    matched_at = None
    last_pid = None
    equivalent_frontmost = False
    foreground_method = None
    direct_focus_checked = False
    while time.monotonic() - started < timeout:
        last_pid = _frontmost_pid()
        equivalent_frontmost = last_pid == pid
        if equivalent_frontmost:
            matched_at = matched_at or time.monotonic()
            if time.monotonic() - matched_at >= 0.08:
                foreground_method = "appkit-pid"
                break
        else:
            matched_at = None
            if (
                not direct_focus_checked
                and time.monotonic() - started >= 0.12
            ):
                direct_focus_checked = True
                if _system_events_process_frontmost(pid):
                    equivalent_frontmost = True
                    foreground_method = "system-events-pid"
                    break
        time.sleep(0.02)
    else:
        if _system_events_process_frontmost(pid):
            equivalent_frontmost = True
            foreground_method = "system-events-pid"
        else:
            return {
                "ok": False,
                "error": "foreground acknowledgement timed out",
                "expected_pid": pid,
                "frontmost_pid": last_pid,
                "duration_ms": round((time.monotonic() - started) * 1000),
            }

    windows = list_windows().get("windows", [])
    candidates = _ax_window_candidates(pid, windows)
    target = next(
        (candidate for candidate in candidates if candidate.get("window_id") == window_id),
        None,
    )
    if not target or target.get("ax_minimized") or not (
        target.get("ax_main") or target.get("ax_focused")
    ):
        return {
            "ok": False,
            "error": "foreground window is not AX-ready",
            "window_id": window_id,
            "observed": target,
            "candidate_count": len(candidates),
            "duration_ms": round((time.monotonic() - started) * 1000),
        }
    return {
        "ok": True,
        "pid": pid,
        "window_id": window_id,
        "frontmost_pid": last_pid,
        "frontmost_equivalent": equivalent_frontmost,
        "foreground_method": foreground_method,
        "duration_ms": round((time.monotonic() - started) * 1000),
    }


def bring_resolved_window_to_front(pid, window_id, timeout=5):
    """Foreground one resolved window and independently acknowledge readiness."""
    if _pid_alive(pid):
        activation = _activate_running_identity({"pid": int(pid)})
        raised = _raise_resolved_ax_window(pid, window_id)
        if activation.get("ok") and raised.get("ok"):
            readiness = _wait_for_foreground_readiness(
                pid, window_id, timeout=min(float(timeout), 0.6)
            )
            if readiness.get("ok"):
                return {
                    "ok": True,
                    "readiness": readiness,
                    "foreground_method": "native_activation+ax_raise",
                }
    result = call_driver(
        "bring_to_front",
        {"pid": pid, "window_id": window_id},
        timeout=timeout,
    )
    if result.get("error") or result.get("ok") is False:
        return {
            "error": (
                f"could not foreground pid={pid} window_id={window_id}: "
                f"{result.get('error') or 'driver rejected the request'}"
            ),
            "driver": result,
        }
    readiness = _wait_for_foreground_readiness(
        pid, window_id, timeout=min(float(timeout), 1.5)
    )
    if readiness.get("ok"):
        return {"ok": True, "readiness": readiness}

    raised = _raise_resolved_ax_window(pid, window_id)
    if raised.get("ok"):
        recovered = _wait_for_foreground_readiness(
            pid, window_id, timeout=min(float(timeout), 1.5)
        )
        if recovered.get("ok"):
            return {
                "ok": True,
                "readiness": recovered,
                "recovered": True,
                "recovery": raised,
                "initial_readiness": readiness,
            }

    # A menu or transient panel can leave a live AX window addressable while
    # cua-driver's first foreground dispatch does not become AppKit-frontmost.
    # Recover once through the authoritative running PID, then independently
    # acknowledge focus again. This stays event-driven on the normal path and
    # fails closed after one bounded recovery.
    activation = _activate_running_identity({"pid": int(pid)})
    if activation.get("error"):
        return {
            "error": readiness.get("error") or "foreground acknowledgement failed",
            "readiness": readiness,
            "recovery": activation,
        }
    recovered = _wait_for_foreground_readiness(
        pid, window_id, timeout=min(float(timeout), 1.5)
    )
    if not recovered.get("ok"):
        return {
            "error": recovered.get("error") or "foreground acknowledgement failed",
            "readiness": recovered,
            "initial_readiness": readiness,
            "recovery": activation,
        }
    return {
        "ok": True,
        "readiness": recovered,
        "recovered": True,
        "initial_readiness": readiness,
    }


def driver_status():
    # health_report can wedge the daemon socket; check_permissions is reliable.
    perms = call_driver("check_permissions")
    try:
        r = subprocess.run(
            [CUA_DRIVER, "status"], capture_output=True, text=True, timeout=5
        )
        text = f"{r.stdout or ''}\n{r.stderr or ''}".lower()
        daemon = {
            "running": r.returncode == 0 and "daemon is running" in text,
            "detail": (r.stdout or r.stderr or "").strip()[:200],
        }
    except subprocess.TimeoutExpired:
        daemon = {"running": False, "detail": "cua-driver status timed out"}
    return {"daemon": daemon, "permissions": perms}
