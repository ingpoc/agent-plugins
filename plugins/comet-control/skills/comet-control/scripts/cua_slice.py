#!/usr/bin/env python3
"""Atomic Comet Control → macos-cua slice → release → browser-ready.

Replaces improvised agent loops that desynced the durable controller or left
orphan CUA claims. Never prints or forwards the Comet Control lease token.

Usage:
  python3 skills/comet-control/scripts/cua_slice.py --workdir "$WORK" state
  python3 skills/comet-control/scripts/cua_slice.py --workdir "$WORK" run @plan.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

def _runtime_root() -> Path:
    configured = os.environ.get("COMET_CONTROL_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    shared = Path.home() / ".agents/plugins/comet-control"
    if (shared / "plugin.json").is_file():
        return shared
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "plugin.json").is_file() and (
            parent / "plugin" / "comet_control"
        ).is_dir():
            return parent
    raise RuntimeError("Comet Control runtime root not found")


PLUGIN_ROOT = _runtime_root()
CTRL = Path(__file__).resolve().parent / "durable_lease_controller.py"
DEFAULT_CUA = Path.home() / ".agents/plugins/agent-computer-use/skills/macos-cua/scripts/macos-cua.py"
COEXIST = PLUGIN_ROOT / "scripts" / "check-cua-coexistence.py"


def _emit(payload: dict[str, Any], *, code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return code


def _send(workdir: Path, body: dict[str, Any], *, timeout: float = 90) -> dict[str, Any]:
    proc = subprocess.run(
        [
            sys.executable,
            str(CTRL),
            "send",
            "--workdir",
            str(workdir),
            "--timeout",
            str(timeout),
            json.dumps(body),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    raw = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    if not raw:
        raise RuntimeError(f"empty controller response rc={proc.returncode}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid controller JSON: {raw[:400]}") from exc


def _session_id(workdir: Path) -> str:
    ready = json.loads((workdir / "ready.json").read_text())
    sid = ready.get("session_id")
    if not sid:
        raise RuntimeError("ready.json missing session_id")
    return str(sid)


def _browser_pid() -> int:
    probe = subprocess.run(
        [str(PLUGIN_ROOT / "scripts" / "ensure-broker.sh"), "probe", "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    data = json.loads(probe.stdout or "{}")
    pid = data.get("browser_pid") or (data.get("broker") or {}).get("browser_pid")
    if not pid:
        raise RuntimeError("could not resolve Comet Control browser_pid")
    return int(pid)


def _handoff(workdir: Path, ttl: int) -> dict[str, Any]:
    # Retry briefly: a pre-fix driver may still have one-behind stdout desync;
    # each send advances the queue until native_handoff lands.
    last: dict[str, Any] | None = None
    for _ in range(8):
        event = _send(
            workdir,
            {"command": "native_handoff", "ttlSeconds": ttl},
            timeout=max(60, ttl + 30),
        )
        last = event
        if event.get("event") != "native_handoff":
            continue
        response = event.get("response") or {}
        if response.get("success") and response.get("claim_token"):
            return response
        err = response.get("error") or response.get("error_code") or "native_handoff failed"
        # Orphan claim without reclaim (stale extension): wait once for TTL tip.
        if "already claimed" in str(err).lower() or response.get("error_code") == "CUA_RUNTIME_CLAIMED":
            m = re.search(r"until (\d+)", str(err))
            if m:
                exp = int(m.group(1))
                wait = max(0.2, (exp - int(time.time() * 1000)) / 1000 + 0.3)
                time.sleep(min(wait, 35))
            continue
        raise RuntimeError(err)
    raise RuntimeError(
        f"expected native_handoff event, got {json.dumps(last)[:500] if last else 'none'}"
    )


def _release(claim_token: str) -> dict[str, Any]:
    proc = subprocess.run(
        [
            sys.executable,
            str(COEXIST),
            "--release-claim",
            claim_token,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return json.loads((proc.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        return {
            "safe": False,
            "error": (proc.stdout or proc.stderr or "")[:400],
            "rc": proc.returncode,
        }


def _run_cua(
    *,
    claim_token: str,
    session_id: str,
    browser_pid: int,
    cua_args: list[str],
    cua_bin: Path,
) -> dict[str, Any]:
    if not cua_args:
        raise RuntimeError("missing macos-cua command args")
    sub, *rest = cua_args
    cmd = [
        sys.executable,
        str(cua_bin),
        "--browser-intent",
        "native-dialog",
        "--browser-session-id",
        session_id,
        "--browser-claim-token",
        claim_token,
        sub,
        f"pid:{browser_pid}",
        *rest,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    raw = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    try:
        payload = json.loads(raw) if raw else {"ok": False, "error": "empty_cua_output"}
    except json.JSONDecodeError:
        payload = {"ok": False, "error": raw[:800], "rc": proc.returncode}
    payload.setdefault("rc", proc.returncode)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--ttl-seconds", type=int, default=45)
    parser.add_argument("--browser-pid", type=int)
    parser.add_argument(
        "--cua-bin",
        default=str(os.environ.get("MACOS_CUA_BIN") or DEFAULT_CUA),
    )
    parser.add_argument(
        "--query",
        help="Forwarded to macos-cua state --query",
    )
    parser.add_argument(
        "cua_command",
        nargs=argparse.REMAINDER,
        help="macos-cua subcommand + args, e.g. state   or   run @plan.json",
    )
    args = parser.parse_args()
    workdir = Path(args.workdir).expanduser().resolve()
    if not (workdir / "controller.alive").exists():
        return _emit({"ok": False, "error": "controller_not_alive", "workdir": str(workdir)}, code=2)

    cua_command = list(args.cua_command)
    if cua_command and cua_command[0] == "--":
        cua_command = cua_command[1:]
    if not cua_command:
        cua_command = ["state", "--compact"]
    elif cua_command[0] == "state" and "--compact" not in cua_command:
        cua_command.append("--compact")
    if args.query and cua_command[0] == "state":
        cua_command.extend(["--query", args.query])

    session_id = _session_id(workdir)
    browser_pid = args.browser_pid or _browser_pid()
    claim_token = None
    release_info: dict[str, Any] | None = None
    started = time.time()
    try:
        handoff = _handoff(workdir, max(15, min(300, int(args.ttl_seconds))))
        claim_token = handoff["claim_token"]
        cua_result = _run_cua(
            claim_token=claim_token,
            session_id=session_id,
            browser_pid=browser_pid,
            cua_args=cua_command,
            cua_bin=Path(args.cua_bin).expanduser(),
        )
    except Exception as exc:
        return _emit(
            {
                "ok": False,
                "error": str(exc),
                "session_id": session_id,
                "browser_pid": browser_pid,
                "duration_ms": int((time.time() - started) * 1000),
            },
            code=1,
        )
    finally:
        if claim_token:
            release_info = _release(claim_token)

    ok = bool(cua_result.get("ok") or cua_result.get("success") or cua_result.get("ready"))
    # state returns ok:true; run returns ok/accepted/verified
    if "ok" in cua_result:
        ok = bool(cua_result.get("ok"))
    shot = None
    if isinstance(cua_result.get("screenshot"), dict):
        shot = cua_result["screenshot"].get("path")
    elif isinstance(cua_result.get("final"), dict) and isinstance(
        cua_result["final"].get("screenshot"), dict
    ):
        shot = cua_result["final"]["screenshot"].get("path")
    if not shot:
        m = re.search(r"/Users[^\"\\ ]+\.png", json.dumps(cua_result))
        if m:
            shot = m.group(0)

    released_ok = bool((release_info or {}).get("safe"))
    return _emit(
        {
            "ok": ok and released_ok,
            "session_id": session_id,
            "browser_pid": browser_pid,
            "claim_id": (handoff.get("claim") or {}).get("claim_id"),
            "released": release_info,
            "screenshot": shot,
            "cua": {
                k: cua_result.get(k)
                for k in (
                    "ok",
                    "ready",
                    "accepted",
                    "verified",
                    "code",
                    "error",
                    "error_code",
                    "text",
                    "element_count",
                )
                if k in cua_result
            },
            "duration_ms": int((time.time() - started) * 1000),
            "comet_control_resume": "same-lease",
        },
        code=0 if ok and released_ok else 1,
    )


if __name__ == "__main__":
    raise SystemExit(main())
