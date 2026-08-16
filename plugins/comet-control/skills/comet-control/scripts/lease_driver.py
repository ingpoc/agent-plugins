#!/usr/bin/env python3
"""Token-private interactive driver for one Comet Control window lease.

The process owns the lease token and accepts newline-delimited JSON commands on
stdin. This removes manual token handling from multi-turn agent browser work,
silently renews the same lease for the lifetime of the process, and attempts one
closeout on EOF, interrupt, or an explicit closeout command.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import socket
import sys
import termios
import threading
import time
from pathlib import Path
from typing import Any


def _default_socket() -> str:
    env = os.environ.get("COMET_CONTROL_BRIDGE_SOCKET")
    if env:
        return env
    wip = os.environ.get("COMET_CONTROL_WIP_ROOT")
    if wip:
        return str(Path(wip).expanduser().resolve() / "run" / "comet-control.sock")
    here = Path(__file__).resolve()
    candidates: list[Path] = []
    for parent in here.parents:
        if (parent / "plugin.json").is_file() and (
            parent / "plugin" / "comet_control"
        ).is_dir():
            candidates.append(parent / "run" / "comet-control.sock")
            break
        if (parent / "deploy" / "native").is_dir() and (
            parent / "plugin" / "comet_control"
        ).is_dir():
            candidates.append(parent / "run" / "comet-control.sock")
    for sock in candidates:
        if sock.exists() or sock.parent.is_dir():
            # Prefer an existing live socket when present.
            if sock.exists():
                return str(sock)
    if candidates:
        return str(candidates[0])
    raise RuntimeError(
        "Could not resolve Comet Control runtime root; set COMET_CONTROL_BRIDGE_SOCKET"
    )


DEFAULT_SOCKET = _default_socket()
MIN_TIMEOUT_SECONDS = 1
MAX_TIMEOUT_SECONDS = 300
TRANSPORT_MARGIN_SECONDS = 35
MIN_RENEW_INTERVAL_SECONDS = 0.05
MAX_RENEW_INTERVAL_SECONDS = 60.0
RENEW_HOST_TIMEOUT_SECONDS = 5
RENEW_TRANSPORT_TIMEOUT_SECONDS = 10.0
CLOSEOUT_MAX_ATTEMPTS = 3
CLOSEOUT_RETRY_DELAY_SECONDS = 0.1
MAX_CONSECUTIVE_RENEWAL_FAILURES = 3


_EMIT_LOCK = threading.Lock()


class TerminationRequested(BaseException):
    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


def bridge(socket_path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    with client:
        client.connect(socket_path)
        client.sendall(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        chunks: list[bytes] = []
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    if not chunks:
        raise RuntimeError("Comet Control bridge returned an empty response")
    result = json.loads(b"".join(chunks))
    if not isinstance(result, dict):
        raise RuntimeError("Comet Control bridge response was not an object")
    return result


def _is_private_key(key: Any) -> bool:
    return str(key).replace("_", "").lower() == "leasetoken"


def redact_private(value: Any, private_token: str = "") -> Any:
    if isinstance(value, dict):
        return {
            key: redact_private(item, private_token)
            for key, item in value.items()
            if not _is_private_key(key)
        }
    if isinstance(value, list):
        return [redact_private(item, private_token) for item in value]
    if isinstance(value, tuple):
        return [redact_private(item, private_token) for item in value]
    if isinstance(value, str) and private_token:
        return value.replace(private_token, "[redacted]")
    return value


def emit(
    payload: dict[str, Any],
    private_token: str = "",
    *,
    kind: str | None = None,
    command_id: Any = None,
) -> None:
    envelope = dict(payload)
    if kind:
        envelope["kind"] = kind
    if command_id is not None:
        envelope["command_id"] = command_id
    with _EMIT_LOCK:
        print(
            json.dumps(redact_private(envelope, private_token), ensure_ascii=False),
            flush=True,
        )


def public_response(payload: dict[str, Any]) -> dict[str, Any]:
    result = redact_private(payload)
    if not isinstance(result, dict):
        raise TypeError("public response must remain an object")
    return result


def validated_lease_identity(
    payload: dict[str, Any], expected_session_id: str
) -> dict[str, Any]:
    session_id = payload.get("session_id")
    if session_id != expected_session_id:
        raise RuntimeError("lease response changed session_id")
    identity: dict[str, Any] = {"session_id": session_id}
    for key in ("window_id", "tab_id"):
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeError(f"lease response missing valid {key}")
        identity[key] = value
    return identity


def bounded_timeout_seconds(value: Any, fallback: int) -> int:
    raw = fallback if value is None else value
    if isinstance(raw, bool) or (isinstance(raw, str) and not raw.strip()):
        raise ValueError("timeoutSeconds must be a finite number")
    try:
        number = float(raw)
    except (TypeError, ValueError) as error:
        raise ValueError("timeoutSeconds must be a finite number") from error
    if not math.isfinite(number):
        raise ValueError("timeoutSeconds must be a finite number")
    return max(MIN_TIMEOUT_SECONDS, min(MAX_TIMEOUT_SECONDS, int(math.ceil(number))))


def transport_timeout(timeout_seconds: int) -> float:
    return float(timeout_seconds + TRANSPORT_MARGIN_SECONDS)


def renewal_interval_seconds(ttl_seconds: Any, override: Any = None) -> float:
    """Renew well before expiry while keeping normal campaigns quiet and cheap."""
    if isinstance(ttl_seconds, bool):
        raise ValueError("ttlSeconds must be a finite number")
    try:
        ttl = float(ttl_seconds)
    except (TypeError, ValueError) as error:
        raise ValueError("ttlSeconds must be a finite number") from error
    if not math.isfinite(ttl):
        raise ValueError("ttlSeconds must be a finite number")

    # The extension clamps leases to at least one second. Mirror that boundary
    # here so an unusual caller value cannot schedule renewal after expiry.
    effective_ttl = max(1.0, ttl)
    automatic = min(
        MAX_RENEW_INTERVAL_SECONDS,
        max(MIN_RENEW_INTERVAL_SECONDS, effective_ttl / 3.0),
    )
    if override is None:
        return automatic
    if isinstance(override, bool):
        raise ValueError("renewIntervalSeconds must be a finite positive number")
    try:
        requested = float(override)
    except (TypeError, ValueError) as error:
        raise ValueError(
            "renewIntervalSeconds must be a finite positive number"
        ) from error
    if not math.isfinite(requested) or requested <= 0:
        raise ValueError("renewIntervalSeconds must be a finite positive number")
    return max(
        MIN_RENEW_INTERVAL_SECONDS,
        min(requested, MAX_RENEW_INTERVAL_SECONDS, effective_ttl / 2.0),
    )


def install_termination_handlers() -> dict[int, Any]:
    previous: dict[int, Any] = {}

    def request_termination(signum: int, _frame: Any) -> None:
        raise TerminationRequested(signum)

    for name in ("SIGTERM", "SIGHUP"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, request_termination)
    return previous


def restore_termination_handlers(previous: dict[int, Any]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def make_tty_input_stream_safe() -> tuple[int, list[Any]] | None:
    """Disable canonical PTY buffering so long NDJSON commands are not truncated."""
    if not sys.stdin.isatty():
        return None
    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    current = termios.tcgetattr(fd)
    current[3] &= ~(termios.ICANON | termios.ECHO)
    current[6][termios.VMIN] = 1
    current[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, current)
    return fd, previous


def restore_tty_input(state: tuple[int, list[Any]] | None) -> None:
    if state is None:
        return
    fd, previous = state
    termios.tcsetattr(fd, termios.TCSANOW, previous)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--ttl-seconds", type=int, default=360)
    parser.add_argument("--timeout-seconds", type=int, default=90)
    parser.add_argument(
        "--renew-interval-seconds",
        type=float,
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--socket",
        default=os.environ.get("COMET_CONTROL_BRIDGE_SOCKET", DEFAULT_SOCKET),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.timeout_seconds = bounded_timeout_seconds(args.timeout_seconds, 35)
    renew_interval = renewal_interval_seconds(
        args.ttl_seconds, args.renew_interval_seconds
    )
    signal_state = install_termination_handlers()
    lease_token = ""
    closed = False
    closeout_attempted = False
    tty_state: tuple[int, list[Any]] | None = None
    exit_code = 0
    had_command_error = False
    bridge_lock = threading.Lock()
    renewal_stop = threading.Event()
    renewal_thread: threading.Thread | None = None
    renewal_failures = 0
    lease_identity: dict[str, Any] = {}

    def serialized_bridge(payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        with bridge_lock:
            return bridge(args.socket, payload, timeout)

    def closeout(reason: str) -> dict[str, Any]:
        nonlocal closed, closeout_attempted
        if closeout_attempted:
            return {
                "success": closed,
                "already_attempted": True,
                "session_id": args.session_id,
            }
        renewal_stop.set()
        last_error: Exception | None = None
        for attempt in range(1, CLOSEOUT_MAX_ATTEMPTS + 1):
            try:
                result = serialized_bridge(
                    {
                        "type": "session_closeout",
                        "sessionId": args.session_id,
                        "leaseToken": lease_token,
                        "reason": reason,
                        "timeoutSeconds": args.timeout_seconds,
                    },
                    transport_timeout(args.timeout_seconds),
                )
            except Exception as error:
                last_error = error
                if attempt < CLOSEOUT_MAX_ATTEMPTS:
                    time.sleep(CLOSEOUT_RETRY_DELAY_SECONDS)
                    continue
                closeout_attempted = True
                raise

            closed = bool(result.get("success"))
            retryable = bool(result.get("retryable"))
            if closed or not retryable or attempt == CLOSEOUT_MAX_ATTEMPTS:
                closeout_attempted = True
                response = public_response(result)
                if attempt > 1:
                    response["attempts"] = attempt
                return response
            time.sleep(CLOSEOUT_RETRY_DELAY_SECONDS)

        closeout_attempted = True
        raise RuntimeError(str(last_error or "Comet Control closeout failed"))

    def renew_once() -> bool:
        with bridge_lock:
            # Closeout sets the stop bit before waiting on this lock. Re-check it
            # here so a queued heartbeat cannot run after the release request.
            if renewal_stop.is_set():
                return False
            result = bridge(
                args.socket,
                {
                    "type": "session_renew",
                    "sessionId": args.session_id,
                    "leaseToken": lease_token,
                    "ttlSeconds": args.ttl_seconds,
                    "timeoutSeconds": RENEW_HOST_TIMEOUT_SECONDS,
                },
                RENEW_TRANSPORT_TIMEOUT_SECONDS,
            )
            if not result.get("success"):
                raise RuntimeError(str(result.get("error") or "lease renewal failed"))
            renewed_identity = validated_lease_identity(result, args.session_id)
            for key, expected in lease_identity.items():
                if renewed_identity[key] != expected:
                    raise RuntimeError(f"lease renewal changed {key}")
            return True

    def renew_lease() -> None:
        nonlocal renewal_failures
        while not renewal_stop.wait(renew_interval):
            try:
                if not renew_once():
                    return
                recovered_failures = renewal_failures
                renewal_failures = 0
                if recovered_failures and not renewal_stop.is_set():
                    emit(
                        {
                            "event": "renewal_recovered",
                            "failures": recovered_failures,
                        },
                        lease_token,
                        kind="notification",
                    )
            except Exception as error:
                renewal_failures += 1
                # One event per failure streak is enough for diagnosis; retries
                # stay automatic and never create a replacement lease.
                if renewal_failures == 1 and not renewal_stop.is_set():
                    emit(
                        {
                            "event": "renewal_failed",
                            "error": str(error),
                            "retrying_same_lease": True,
                        },
                        lease_token,
                        kind="notification",
                    )
                if (
                    renewal_failures >= MAX_CONSECUTIVE_RENEWAL_FAILURES
                    and not renewal_stop.is_set()
                ):
                    emit(
                        {
                            "event": "renewal_exhausted",
                            "failures": renewal_failures,
                            "error": str(error),
                        },
                        lease_token,
                        kind="notification",
                    )
                    os.kill(os.getpid(), signal.SIGTERM)
                    return

    try:
        preflight_payload = {
            "type": "session_preflight",
            "sessionId": args.session_id,
            "agentLabel": args.label,
            "url": args.url,
            "isolation": "window",
            "ttlSeconds": args.ttl_seconds,
            "timeoutSeconds": args.timeout_seconds,
        }
        preflight = bridge(
            args.socket,
            preflight_payload,
            transport_timeout(args.timeout_seconds),
        )
        # One same-session-id retry after an orphaned prior owner becomes
        # reclaimable (LEASE_HELD / renew-stale). Never invent a new session id.
        if (
            not preflight.get("success")
            and not preflight.get("lease_token")
            and (
                preflight.get("error_code") == "LEASE_HELD"
                or "already leased by another caller" in str(preflight.get("error") or "")
            )
        ):
            wait_s = min(MAX_RENEW_INTERVAL_SECONDS, max(MIN_RENEW_INTERVAL_SECONDS, renew_interval)) * 2
            emit(
                {
                    "event": "preflight_retry_waiting",
                    "error_code": preflight.get("error_code") or "LEASE_HELD",
                    "wait_seconds": wait_s,
                    "retrying_same_session_id": True,
                }
            )
            time.sleep(wait_s)
            preflight = bridge(
                args.socket,
                preflight_payload,
                transport_timeout(args.timeout_seconds),
            )
        if not preflight.get("success") or not preflight.get("lease_token"):
            # A proof-gated acquisition failure may retain a partial owned
            # target and return its private retry capability. Keep it only in
            # this process so the finally block performs one authenticated
            # closeout; never expose it in NDJSON.
            lease_token = str(preflight.get("lease_token") or "")
            emit(
                {"event": "preflight_failed", "response": public_response(preflight)},
                lease_token,
            )
            return 1

        lease_token = str(preflight["lease_token"])
        try:
            lease_identity = validated_lease_identity(preflight, args.session_id)
        except Exception as error:
            emit({"event": "preflight_failed", "error": str(error)}, lease_token)
            return 1

        # Prove the deployed extension supports nonvisual renewal before telling
        # an agent that this process owns a persistent campaign lease.
        try:
            renew_once()
        except Exception as error:
            emit(
                {"event": "renewal_handshake_failed", "error": str(error)},
                lease_token,
            )
            return 1
        renewal_thread = threading.Thread(
            target=renew_lease,
            name=f"comet-control-renew-{args.session_id}",
            daemon=True,
        )
        renewal_thread.start()
        # Publish readiness only after the keepalive owner is running. Agents
        # may signal or command the process as soon as they receive this line.
        emit(
            {
                "event": "ready",
                "lease": public_response(preflight),
                "persistent": True,
                "renew_interval_seconds": renew_interval,
            },
            lease_token,
        )
        tty_state = make_tty_input_stream_safe()

        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            command_id: Any = None

            def reply(payload: dict[str, Any]) -> None:
                emit(
                    payload,
                    lease_token,
                    kind="reply",
                    command_id=command_id,
                )

            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError("command must be a JSON object")
                command_id = request.pop("_controller_command_id", None)
                command = request.get("command", "run")
                if command == "run":
                    actions = request.get("actions")
                    if not isinstance(actions, list) or not actions:
                        raise ValueError("run requires a non-empty actions array")
                    run_timeout_seconds = bounded_timeout_seconds(
                        request.get("timeoutSeconds"), args.timeout_seconds
                    )
                    result = serialized_bridge(
                        {
                            "type": "run",
                            "sessionId": args.session_id,
                            "leaseToken": lease_token,
                            "agentLabel": args.label,
                            "timeoutSeconds": run_timeout_seconds,
                            "actions": actions,
                        },
                        transport_timeout(run_timeout_seconds),
                    )
                    # One stdout event per command. Fold sessions inventory into
                    # the same envelope so durable_lease_controller cannot desync
                    # by consuming a trailing run_diagnostic as the next reply.
                    run_event: dict[str, Any] = {
                        "event": "run",
                        "response": public_response(result),
                    }
                    if not result.get("success"):
                        inventory = serialized_bridge(
                            {"type": "sessions", "sessionId": args.session_id},
                            transport_timeout(args.timeout_seconds),
                        )
                        run_event["diagnostic"] = public_response(inventory)
                        had_command_error = True
                    reply(run_event)
                elif command == "native_handoff":
                    claim_timeout_seconds = bounded_timeout_seconds(
                        request.get("ttlSeconds"), 120
                    )
                    result = serialized_bridge(
                        {
                            "type": "cua_runtime_claim",
                            "intent": "native-dialog",
                            "sessionId": args.session_id,
                            "leaseToken": lease_token,
                            "ttlSeconds": claim_timeout_seconds,
                            # Same-session orphan reclaim: dead CUA processes leave
                            # claims until TTL; the owning lease may replace them.
                            "reclaim": True,
                        },
                        transport_timeout(args.timeout_seconds),
                    )
                    reply(
                        {"event": "native_handoff", "response": public_response(result)},
                    )
                    if not result.get("success"):
                        had_command_error = True
                elif command == "sessions":
                    result = serialized_bridge(
                        {"type": "sessions", "sessionId": args.session_id},
                        transport_timeout(args.timeout_seconds),
                    )
                    reply(
                        {"event": "sessions", "response": public_response(result)},
                    )
                elif command == "closeout":
                    result = closeout("driver-closeout")
                    reply({"event": "closeout", "response": result})
                    if not result.get("success"):
                        had_command_error = True
                    break
                else:
                    raise ValueError(f"unsupported command: {command}")
            except Exception as error:  # keep the owned lease available for closeout
                had_command_error = True
                reply({"event": "command_error", "error": str(error)})
    except TerminationRequested as error:
        exit_code = 128 + error.signum
        emit({"event": "terminated", "signal": error.signum}, lease_token)
    except KeyboardInterrupt:
        exit_code = 130
        emit({"event": "interrupted"}, lease_token)
    except Exception as error:
        exit_code = 1
        emit({"event": "driver_error", "error": str(error)}, lease_token)
    finally:
        renewal_stop.set()
        if renewal_thread is not None and renewal_thread.is_alive():
            renewal_thread.join(timeout=RENEW_TRANSPORT_TIMEOUT_SECONDS)
        if lease_token and not closeout_attempted:
            try:
                emit(
                    {"event": "closeout", "response": closeout("driver-exit")},
                    lease_token,
                )
            except Exception as error:
                emit({"event": "closeout_error", "error": str(error)}, lease_token)
        try:
            restore_tty_input(tty_state)
        finally:
            restore_termination_handlers(signal_state)
    if lease_token and not closed:
        return 1
    if exit_code:
        return exit_code
    if renewal_failures:
        return 1
    return 1 if had_command_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
