#!/usr/bin/env python3
"""Fail-closed boundary between macos-cua and the Comet Control runtime.

The managed Comet process is a shared runtime, not an ordinary native app.
Read-only checks expose only public lease state. Native-dialog authorization
requires the short-lived claim capability issued by the owning private driver;
public inventory can never grant native-computer access.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
SOCKET_PATH = Path(
    os.environ.get("COMET_CONTROL_BRIDGE_SOCKET", ROOT / "run/comet-control.sock")
)
USER_DATA_DIR = Path(
    os.environ.get(
        "COMET_CONTROL_USER_DATA_DIR",
        Path.home() / "Library/Application Support/Comet",
    )
).resolve()
VERSION = "coexistence-v1"


class BoundaryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def process_command(pid: int) -> str:
    result = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "command="],
        capture_output=True,
        text=True,
        timeout=2,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise BoundaryError("TARGET_NOT_RUNNING", f"target pid {pid} is not running")
    return result.stdout.strip()


def _user_data_dir(command: str) -> Path | None:
    # ps renders arguments without preserving quotes. Stop only at the next
    # option so the default macOS profile path may contain spaces.
    matched = re.search(r"(?:^|\s)--user-data-dir(?:=|\s+)(.*?)(?=\s--|$)", command)
    if matched:
        value = matched.group(1).strip().strip("\"'")
        return Path(value).expanduser().resolve() if value else None
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    for index, part in enumerate(parts):
        if part.startswith("--user-data-dir="):
            value = part.split("=", 1)[1]
            return Path(value).expanduser().resolve() if value else None
        if part == "--user-data-dir" and index + 1 < len(parts):
            return Path(parts[index + 1]).expanduser().resolve()
    return None


def _is_chromium_browser(command: str) -> bool:
    lowered = command.lower()
    return (
        "/google chrome.app/" in lowered
        or "/comet.app/" in lowered
        or "/chromium.app/" in lowered
        or "chrome helper" in lowered
        or "comet helper" in lowered
        or "chromium helper" in lowered
    )


def _is_default_comet(command: str) -> bool:
    return "/comet.app/contents/macos/comet" in command.lower()


def bridge(payload: dict[str, Any], *, timeout: float = 3.0) -> dict[str, Any]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    with client:
        client.connect(str(SOCKET_PATH))
        client.sendall(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        chunks: list[bytes] = []
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    if not chunks:
        raise BoundaryError("COMET_CONTROL_RUNTIME_UNAVAILABLE", "Comet Control broker returned no data")
    try:
        response = json.loads(b"".join(chunks))
    except json.JSONDecodeError as exc:
        raise BoundaryError(
            "COMET_CONTROL_RUNTIME_UNAVAILABLE", "Comet Control broker returned invalid JSON"
        ) from exc
    if not isinstance(response, dict):
        raise BoundaryError("COMET_CONTROL_RUNTIME_UNAVAILABLE", "Comet Control broker reply is not an object")
    return response


def evaluate(
    target_pid: int,
    intent: str,
    session_id: str | None = None,
    *,
    acquire: bool = False,
    claim_token: str | None = None,
    ttl_seconds: int = 120,
    process_command_fn: Callable[[int], str] = process_command,
    bridge_fn: Callable[[dict[str, Any]], dict[str, Any]] = bridge,
) -> dict[str, Any]:
    command = process_command_fn(target_pid)
    target_user_data = _user_data_dir(command)
    if target_user_data is None and _is_default_comet(command):
        target_user_data = USER_DATA_DIR
    base = {
        "version": VERSION,
        "target_pid": target_pid,
        "intent": intent,
    }
    if target_user_data != USER_DATA_DIR:
        if _is_chromium_browser(command) and intent == "native-app":
            raise BoundaryError(
                "BROWSER_PAGE_BOUNDARY",
                "Chromium browser targets require explicit native-dialog or comet-admin intent; use Comet Control for page control",
            )
        return {
            **base,
            "safe": True,
            "reason": "unmanaged-browser-shell" if _is_chromium_browser(command) else "disjoint-native-app",
            "managed_browser": False,
        }

    try:
        status = bridge_fn({"type": "broker_status", "timeoutSeconds": 2})
        broker = status.get("broker") if isinstance(status, dict) else None
        if (
            not status.get("success")
            or not isinstance(broker, dict)
            or not broker.get("runtime_verified")
            or int(broker.get("browser_pid") or 0) != target_pid
            or Path(str(broker.get("user_data_dir") or "")).resolve() != USER_DATA_DIR
        ):
            raise BoundaryError(
                "COMET_CONTROL_RUNTIME_UNAVAILABLE",
                "Comet identity is not broker-attested",
            )
        if intent == "native-dialog" and not str(session_id or "").strip():
            raise BoundaryError(
                "COMET_CONTROL_HANDOFF_REQUIRED",
                "native-dialog work requires the owning Comet Control session id and claim token",
            )
        if claim_token:
            validation = bridge_fn({
                "type": "cua_runtime_validate",
                "claimToken": claim_token,
                "intent": intent,
                "timeoutSeconds": 2,
            })
            claim = validation.get("claim") if isinstance(validation, dict) else None
            if not validation.get("success") or not isinstance(claim, dict):
                raise BoundaryError(
                    validation.get("error_code") or "INVALID_CUA_CLAIM_TOKEN",
                    validation.get("error") or "native handoff claim is not active",
                )
            if session_id and claim.get("session_id") != session_id:
                raise BoundaryError(
                    "INVALID_CUA_CLAIM_TOKEN",
                    "native handoff claim belongs to a different Comet Control session",
                )
            return {
                **base,
                "safe": True,
                "managed_browser": True,
                "browser_pid": target_pid,
                "reason": "authenticated-native-dialog-claim",
                "claim_id": claim.get("claim_id"),
                "claim_token": claim_token,
                "expires_at": claim.get("expires_at"),
                "session_id": claim.get("session_id"),
            }
        if acquire:
            if intent != "comet-admin":
                raise BoundaryError(
                    "COMET_CONTROL_HANDOFF_REQUIRED",
                    "native-dialog claim must be issued by the token-private Comet Control driver",
                )
            acquisition = bridge_fn({
                "type": "cua_runtime_claim",
                "intent": intent,
                "ttlSeconds": ttl_seconds,
                "timeoutSeconds": 2,
            })
            claim = acquisition.get("claim") if isinstance(acquisition, dict) else None
            token = acquisition.get("claim_token") if isinstance(acquisition, dict) else None
            if not acquisition.get("success") or not isinstance(claim, dict) or not token:
                raise BoundaryError(
                    acquisition.get("error_code") or "COMET_CONTROL_RUNTIME_BUSY",
                    acquisition.get("error") or "could not acquire managed Comet claim",
                )
            return {
                **base,
                "safe": True,
                "managed_browser": True,
                "browser_pid": target_pid,
                "reason": "atomic-empty-runtime-admin-claim",
                "active_sessions": acquisition.get("active_sessions", 0),
                "claim_id": claim.get("claim_id"),
                "claim_token": token,
                "expires_at": claim.get("expires_at"),
            }
        if intent == "native-dialog":
            raise BoundaryError(
                "COMET_CONTROL_HANDOFF_REQUIRED",
                "native-dialog work requires an authenticated claim from the token-private Comet Control driver",
            )
        if intent != "comet-admin":
            raise BoundaryError(
                "COMET_CONTROL_MANAGED_BROWSER",
                "Comet Control is not a generic macos-cua target",
            )
        inventory = bridge_fn({"type": "sessions", "timeoutSeconds": 2})
        sessions = inventory.get("sessions") if isinstance(inventory, dict) else None
        if not inventory.get("success") or not isinstance(sessions, list):
            raise BoundaryError(
                "COMET_CONTROL_RUNTIME_UNAVAILABLE", "Comet Control lease inventory is unavailable"
            )
    except (OSError, TimeoutError, ValueError) as exc:
        raise BoundaryError(
            "COMET_CONTROL_RUNTIME_UNAVAILABLE", f"cannot verify Comet Control ownership: {exc}"
        ) from exc

    public = {
        **base,
        "managed_browser": True,
        "browser_pid": target_pid,
        "active_sessions": len(sessions),
    }
    if intent == "comet-admin":
        if sessions:
            raise BoundaryError(
                "COMET_CONTROL_RUNTIME_BUSY",
                f"Comet administration requires zero leases; found {len(sessions)}",
            )
        return {**public, "safe": True, "reason": "empty-runtime-admin-handoff"}

    raise BoundaryError(
        "COMET_CONTROL_MANAGED_BROWSER",
        "Comet Control is not a generic macos-cua target",
    )


def release_claim(
    claim_token: str,
    *,
    bridge_fn: Callable[[dict[str, Any]], dict[str, Any]] = bridge,
) -> dict[str, Any]:
    response = bridge_fn({
        "type": "cua_runtime_release",
        "claimToken": claim_token,
        "timeoutSeconds": 2,
    })
    if not response.get("success"):
        raise BoundaryError(
            response.get("error_code") or "CUA_CLAIM_RELEASE_FAILED",
            response.get("error") or "could not release managed Comet claim",
        )
    return {
        "version": VERSION,
        "safe": True,
        "released": bool(response.get("released")),
        "already_released": bool(response.get("already_released")),
        "claim_id": response.get("claim_id"),
    }


def _blocked(target_pid: int, intent: str, error: BoundaryError) -> dict[str, Any]:
    return {
        "version": VERSION,
        "safe": False,
        "target_pid": target_pid,
        "intent": intent,
        "error_code": error.code,
        "error": str(error),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-pid", type=int)
    parser.add_argument(
        "--intent",
        choices=("native-app", "native-dialog", "comet-admin"),
        default="native-app",
    )
    parser.add_argument("--session-id")
    parser.add_argument("--acquire", action="store_true")
    parser.add_argument("--claim-token")
    parser.add_argument("--release-claim")
    parser.add_argument("--ttl-seconds", type=int, default=120)
    args = parser.parse_args()
    if args.release_claim:
        try:
            result = release_claim(args.release_claim)
        except BoundaryError as exc:
            result = _blocked(0, "release", exc)
            print(json.dumps(result, separators=(",", ":")))
            return 2
        print(json.dumps(result, separators=(",", ":")))
        return 0
    if not args.target_pid:
        parser.error("--target-pid is required unless --release-claim is used")
    try:
        result = evaluate(
            args.target_pid,
            args.intent,
            args.session_id,
            acquire=args.acquire,
            claim_token=args.claim_token,
            ttl_seconds=args.ttl_seconds,
        )
    except BoundaryError as exc:
        result = _blocked(args.target_pid, args.intent, exc)
        print(json.dumps(result, separators=(",", ":")))
        return 2
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
