#!/usr/bin/env python3
"""Detached durable owner for one lease_driver.py process.

Blind reviewers must not invent FIFO/shell wrappers that die after lease-ready.
This controller:

- starts lease_driver with owned stdin (PYTHONUNBUFFERED)
- detaches into its own session so the launching shell can exit
- writes ready.json once event:ready is seen (including after LEASE_HELD retry)
- accepts one NDJSON command per request file and appends the response
- closeout only via explicit command (never EOF the driver early)

Usage:
  python3 durable_lease_controller.py start \\
    --session-id SID --label LABEL --url URL --workdir /tmp/SID

  python3 durable_lease_controller.py send --workdir /tmp/SID \\
    '{"actions":[{"type":"page_context"}]}'

  python3 durable_lease_controller.py status --workdir /tmp/SID
  python3 durable_lease_controller.py closeout --workdir /tmp/SID
"""

from __future__ import annotations

import argparse
import json
import os
import select
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

DRIVER = Path(__file__).resolve().parent / "lease_driver.py"


def _default_socket() -> str:
    env = os.environ.get("COMET_CONTROL_BRIDGE_SOCKET")
    if env:
        return env
    wip = os.environ.get("COMET_CONTROL_ROOT")
    if wip:
        return str(Path(wip).expanduser().resolve() / "run" / "comet-control.sock")
    shared = Path.home() / ".agents/plugins/comet-control"
    if (shared / "plugin.json").is_file():
        return str(shared / "run/comet-control.sock")
    here = Path(__file__).resolve()
    fallback: Path | None = None
    for parent in here.parents:
        if (parent / "plugin.json").is_file() and (
            parent / "plugin" / "comet_control"
        ).is_dir():
            sock = parent / "run" / "comet-control.sock"
            if sock.exists():
                return str(sock)
            fallback = sock
            break
        if (parent / "deploy" / "native").is_dir() and (
            parent / "plugin" / "comet_control"
        ).is_dir():
            sock = parent / "run" / "comet-control.sock"
            if sock.exists():
                return str(sock)
            fallback = sock
    if fallback is not None:
        return str(fallback)
    raise RuntimeError("Comet Control runtime root not found")


DEFAULT_SOCKET = _default_socket()


def _paths(workdir: Path) -> dict[str, Path]:
    return {
        "workdir": workdir,
        "log": workdir / "controller.log",
        "pid": workdir / "controller.pid",
        "ready": workdir / "ready.json",
        "request": workdir / "request.json",
        "response": workdir / "response.json",
        "alive": workdir / "controller.alive",
        "send_lock": workdir / "send.lock",
        "seq": workdir / "seq.counter",
    }


def _session_absence_proof(socket_path: str, session_id: str) -> dict[str, object]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(8)
    try:
        client.connect(socket_path)
        client.sendall(
            json.dumps(
                {"type": "sessions", "sessionId": session_id, "timeoutSeconds": 5}
            ).encode()
        )
        client.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        payload = json.loads(b"".join(chunks))
    finally:
        client.close()
    sessions = payload.get("sessions")
    if payload.get("success") is not True or not isinstance(sessions, list):
        raise RuntimeError(str(payload.get("error") or "session inventory unavailable"))
    matches = [item for item in sessions if item.get("session_id") == session_id]
    return {
        "verified_absent": not matches,
        "matching_session_count": len(matches),
    }


def _attach_absence_proof(paths: dict[str, Path], payload: dict) -> tuple[dict, bool]:
    response = payload.get("response")
    if payload.get("event") != "closeout" or not isinstance(response, dict):
        return payload, False
    try:
        ready = json.loads(paths["ready"].read_text())
        session_id = ready["session_id"]
        proof = _session_absence_proof(ready.get("socket_path", DEFAULT_SOCKET), session_id)
        response.update(proof)
    except Exception as error:
        response.update({"verified_absent": False, "error": str(error)})
    if response.get("verified_absent") is not True:
        response.update({
            "success": False,
            "error_code": "LEASE_CLEANUP_INCOMPLETE",
            "error": response.get("error") or "session remains active after closeout",
        })
        return payload, False
    return payload, response.get("success") is True


def _log(log_path: Path, msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}\n"
    with log_path.open("a") as fh:
        fh.write(line)


def _read_driver_json(driver: subprocess.Popen[str], timeout: float) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if driver.poll() is not None:
            rest = ""
            try:
                rest = driver.stdout.read() if driver.stdout else ""
            except Exception:
                pass
            return {
                "error": "driver_exited",
                "code": driver.returncode,
                "tail": (rest or "")[-4000:],
            }
        assert driver.stdout is not None
        ready, _, _ = select.select([driver.stdout], [], [], 0.5)
        if not ready:
            continue
        line = driver.stdout.readline()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return {"error": "timeout_waiting_driver_json"}


def _controller_main(args: argparse.Namespace) -> int:
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    workdir = Path(args.workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    p = _paths(workdir)
    for key in ("log", "ready", "request", "response", "alive"):
        if p[key].exists():
            p[key].unlink()
    p["log"].write_text("")
    p["pid"].write_text(str(os.getpid()))
    p["alive"].write_text(str(os.getpid()))

    env = os.environ.copy()
    env["COMET_CONTROL_BRIDGE_SOCKET"] = args.socket
    env["PYTHONUNBUFFERED"] = "1"

    _log(p["log"], "starting lease_driver")
    driver = subprocess.Popen(
        [
            sys.executable,
            "-u",
            str(DRIVER),
            "--session-id",
            args.session_id,
            "--label",
            args.label,
            "--url",
            args.url,
            "--ttl-seconds",
            str(args.ttl_seconds),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
        start_new_session=True,
    )

    first = _read_driver_json(driver, timeout=float(args.ready_timeout))
    _log(p["log"], f"event0={json.dumps(first)[:800]}")
    if first.get("event") == "preflight_retry_waiting" or first.get(
        "retrying_same_session_id"
    ):
        first = _read_driver_json(driver, timeout=float(args.ready_timeout))
        _log(p["log"], f"event_retry={json.dumps(first)[:800]}")

    lease_ready = first.get("event") == "ready" or bool(first.get("lease"))
    if not lease_ready:
        p["ready"].write_text(
            json.dumps({"ok": False, "error": "no_ready", "first": first}, indent=2)
            + "\n"
        )
        try:
            driver.kill()
        except Exception:
            pass
        p["alive"].unlink(missing_ok=True)
        return 2

    lease_ready_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    p["ready"].write_text(
        json.dumps(
            {
                "ok": True,
                "lease_ready_at": lease_ready_at,
                "session_id": args.session_id,
                "socket_path": args.socket,
                "first": first,
            },
            indent=2,
        )
        + "\n"
    )
    _log(p["log"], f"LEASE_READY {lease_ready_at}")

    # Command loop: wait for request.json, write response.json, delete request.
    while True:
        if driver.poll() is not None:
            _log(p["log"], f"driver_exited code={driver.returncode}")
            p["alive"].unlink(missing_ok=True)
            return 1
        if not p["request"].exists():
            time.sleep(0.05)
            continue
        try:
            raw = p["request"].read_text().strip()
        except Exception as exc:
            _log(p["log"], f"request_read_error {exc}")
            time.sleep(0.1)
            continue
        if not raw:
            time.sleep(0.05)
            continue
        try:
            p["request"].unlink()
        except Exception:
            pass
        # Envelope: {"seq": N, "body": <driver command>}. Bare commands still
        # accepted (seq=null) for manual debugging.
        seq: int | None = None
        try:
            parsed_req = json.loads(raw)
            if (
                isinstance(parsed_req, dict)
                and "body" in parsed_req
                and ("seq" in parsed_req or "id" in parsed_req)
            ):
                seq = parsed_req.get("seq", parsed_req.get("id"))
                parsed_cmd = parsed_req["body"]
            else:
                parsed_cmd = parsed_req
            driver_command = dict(parsed_cmd)
            driver_command["_controller_command_id"] = seq
            line = json.dumps(driver_command, ensure_ascii=False)
        except json.JSONDecodeError as exc:
            envelope = {
                "seq": seq,
                "result": {"ok": False, "error": f"invalid_request_json: {exc}"},
            }
            p["response"].write_text(json.dumps(envelope) + "\n")
            continue
        _log(p["log"], f"cmd seq={seq} {line[:500]}")
        assert driver.stdin is not None
        try:
            driver.stdin.write(line + "\n")
            driver.stdin.flush()
        except Exception as exc:
            envelope = {
                "seq": seq,
                "result": {"ok": False, "error": f"stdin_write_failed: {exc}"},
            }
            p["response"].write_text(json.dumps(envelope) + "\n")
            continue

        wait_s = 180.0
        raw_wait = None
        if isinstance(parsed_req, dict):
            raw_wait = parsed_req.get("wait_seconds")
        if raw_wait is None and isinstance(parsed_cmd, dict):
            raw_wait = parsed_cmd.get("timeoutSeconds")
        if raw_wait is not None:
            try:
                wait_s = max(5.0, min(300.0, float(raw_wait)))
            except (TypeError, ValueError):
                wait_s = 180.0
        resp = _read_driver_json(driver, timeout=wait_s)
        # Current drivers mark notifications and correlate replies. The event
        # allowlist remains only for older drivers already running during deploy.
        while (
            resp.get("kind") == "notification"
            or (
                resp.get("kind") == "reply"
                and resp.get("command_id") != seq
            )
            or resp.get("event")
            in {"renew", "heartbeat", "renewal_failed", "renewal_recovered", "run_diagnostic"}
        ):
            _log(p["log"], f"evt={json.dumps(resp)[:300]}")
            resp = _read_driver_json(driver, timeout=wait_s)
        if resp.get("error") or (resp.get("response") or {}).get("error"):
            _log(p["log"], f"rsp_error seq={seq} {json.dumps(resp)[:800]}")
        else:
            _log(p["log"], f"rsp seq={seq} keys={list(resp.keys())}")
        # Always wrap with seq so send() never consumes a prior response.
        p["response"].write_text(
            json.dumps({"seq": seq, "result": resp}, ensure_ascii=False) + "\n"
        )

        closeout = (
            isinstance(parsed_cmd, dict) and parsed_cmd.get("command") == "closeout"
        )
        if closeout:
            try:
                driver.stdin.close()
            except Exception:
                pass
            try:
                driver.wait(timeout=45)
            except Exception:
                try:
                    driver.kill()
                except Exception:
                    pass
            p["alive"].unlink(missing_ok=True)
            _log(p["log"], "closeout_done")
            return 0


def cmd_start(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    p = _paths(workdir)
    # If already alive, refuse
    if p["alive"].exists() and p["pid"].exists():
        try:
            os.kill(int(p["pid"].read_text().strip()), 0)
            print(json.dumps({"ok": False, "error": "controller_already_running"}))
            return 1
        except Exception:
            pass

    child_args = [
        sys.executable,
        "-u",
        str(Path(__file__).resolve()),
        "_run",
        "--session-id",
        args.session_id,
        "--label",
        args.label,
        "--url",
        args.url,
        "--workdir",
        str(workdir),
        "--socket",
        args.socket,
        "--ttl-seconds",
        str(args.ttl_seconds),
        "--ready-timeout",
        str(args.ready_timeout),
    ]
    # Detach: new session, stdio to files
    stdout_f = (workdir / "controller.stdout").open("w")
    stderr_f = (workdir / "controller.stderr").open("w")
    proc = subprocess.Popen(
        child_args,
        stdin=subprocess.DEVNULL,
        stdout=stdout_f,
        stderr=stderr_f,
        start_new_session=True,
        cwd=str(workdir),
    )
    # Wait for ready.json
    deadline = time.time() + float(args.ready_timeout) + 30
    while time.time() < deadline:
        if p["ready"].exists():
            ready = json.loads(p["ready"].read_text())
            print(json.dumps(ready, indent=2))
            return 0 if ready.get("ok") else 2
        if proc.poll() is not None and not p["ready"].exists():
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": "controller_exited_before_ready",
                        "code": proc.returncode,
                    }
                )
            )
            return 2
        time.sleep(0.1)
    print(json.dumps({"ok": False, "error": "timeout_waiting_ready.json"}))
    return 2


def cmd_send(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir).resolve()
    p = _paths(workdir)
    if not p["alive"].exists():
        print(json.dumps({"ok": False, "error": "controller_not_alive"}))
        return 1
    payload = args.payload
    if args.payload_file:
        payload = Path(args.payload_file).read_text()
    if not payload or not str(payload).strip():
        print(json.dumps({"ok": False, "error": "empty_payload"}))
        return 1
    try:
        body = json.loads(payload)
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "error": f"invalid_payload_json: {exc}"}))
        return 1

    # Propagate controller wait budget into the host run deadline when the
    # caller omitted timeoutSeconds. SPA remounts (Seller Dispatch) need ≥90s;
    # default send --timeout is 180.
    if isinstance(body, dict) and "timeoutSeconds" not in body and "command" not in body:
        try:
            body["timeoutSeconds"] = max(35, min(300, int(float(args.timeout))))
        except (TypeError, ValueError):
            body["timeoutSeconds"] = 90

    # Serialize senders and match responses by seq. Never delete response.json
    # before write — that raced with the controller and caused one-behind
    # mismatches (fill errors returned for page_context / sessions / etc.).
    import fcntl

    with p["send_lock"].open("a+") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            cur = 0
            if p["seq"].exists():
                try:
                    cur = int(p["seq"].read_text().strip() or "0")
                except ValueError:
                    cur = 0
            seq = cur + 1
            p["seq"].write_text(str(seq))
            # Wait until no pending request (controller deleted it).
            wait_deadline = time.time() + float(args.timeout)
            while p["request"].exists() and time.time() < wait_deadline:
                time.sleep(0.02)
            if p["request"].exists():
                print(json.dumps({"ok": False, "error": "timeout_waiting_request_slot"}))
                return 1
            envelope = {"seq": seq, "body": body, "wait_seconds": float(args.timeout)}
            p["request"].write_text(json.dumps(envelope, ensure_ascii=False) + "\n")
            deadline = time.time() + float(args.timeout)
            while time.time() < deadline:
                if p["response"].exists():
                    try:
                        wrapped = json.loads(p["response"].read_text())
                    except json.JSONDecodeError:
                        time.sleep(0.02)
                        continue
                    if wrapped.get("seq") != seq:
                        time.sleep(0.02)
                        continue
                    result = wrapped.get("result", wrapped)
                    text = json.dumps(result, ensure_ascii=False)
                    print(text if text.endswith("\n") else text + "\n", end="")
                    return 0
                if not p["alive"].exists():
                    print(json.dumps({"ok": False, "error": "controller_died"}))
                    return 1
                time.sleep(0.02)
            print(json.dumps({"ok": False, "error": "timeout_waiting_response", "seq": seq}))
            return 1
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def cmd_status(args: argparse.Namespace) -> int:
    """Read-only controller health (no driver command)."""
    workdir = Path(args.workdir).resolve()
    p = _paths(workdir)
    alive = p["alive"].exists()
    pid = None
    if p["pid"].exists():
        try:
            pid = int(p["pid"].read_text().strip() or "0") or None
        except ValueError:
            pid = None
    seq = None
    if p["seq"].exists():
        try:
            seq = int(p["seq"].read_text().strip() or "0")
        except ValueError:
            seq = None
    ready = None
    if p["ready"].exists():
        try:
            ready = json.loads(p["ready"].read_text())
        except json.JSONDecodeError:
            ready = {"ok": False, "error": "ready_json_invalid"}
    last_event = None
    if p["response"].exists():
        try:
            wrapped = json.loads(p["response"].read_text())
            result = wrapped.get("result", wrapped)
            if isinstance(result, dict):
                last_event = result.get("event")
        except json.JSONDecodeError:
            last_event = "invalid_response_json"
    running = False
    if pid:
        try:
            os.kill(pid, 0)
            running = True
        except OSError:
            running = False
    payload = {
        "ok": bool(alive and running and (ready or {}).get("ok")),
        "alive": alive,
        "controller_pid": pid,
        "controller_running": running,
        "seq": seq,
        "session_id": (ready or {}).get("session_id"),
        "lease_ready_at": (ready or {}).get("lease_ready_at"),
        "last_response_event": last_event,
        "workdir": str(workdir),
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload["ok"] else 1


def cmd_closeout(args: argparse.Namespace) -> int:
    # Drain one-behind desync from older drivers: keep sending closeout until
    # the matched response is closeout or the controller dies. Only the final
    # closeout JSON is printed.
    import io
    from contextlib import redirect_stdout

    p = _paths(Path(args.workdir).resolve())
    last_text = ""
    rc = 1
    for _ in range(8):
        if not p["alive"].exists():
            payload, ok = _attach_absence_proof(p, {
                "event": "closeout",
                "response": {"success": True, "already_dead": True},
            })
            print(json.dumps(payload, ensure_ascii=False))
            return 0 if ok else 1
        args.payload = json.dumps({"command": "closeout"})
        args.payload_file = None
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_send(args)
        last_text = buf.getvalue().strip()
        event = None
        try:
            result = json.loads(last_text) if last_text else {}
            event = result.get("event")
        except json.JSONDecodeError:
            event = None
        if event == "closeout" or not p["alive"].exists():
            break
    deadline = time.time() + 45
    while time.time() < deadline and p["alive"].exists():
        time.sleep(0.1)
    if last_text:
        try:
            payload, verified = _attach_absence_proof(p, json.loads(last_text))
        except json.JSONDecodeError:
            print(last_text if last_text.endswith("\n") else last_text + "\n", end="")
            return rc
        print(json.dumps(payload, ensure_ascii=False))
        return 0 if rc == 0 and verified else 1
    elif not p["alive"].exists():
        payload, ok = _attach_absence_proof(p, {
            "event": "closeout",
            "response": {"success": True, "already_dead": True},
        })
        print(json.dumps(payload, ensure_ascii=False))
        return 0 if ok else 1
    return rc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    start = sub.add_parser("start")
    start.add_argument("--session-id", required=True)
    start.add_argument("--label", required=True)
    start.add_argument("--url", required=True)
    start.add_argument("--workdir", required=True)
    start.add_argument("--socket", default=DEFAULT_SOCKET)
    start.add_argument("--ttl-seconds", type=int, default=600)
    start.add_argument("--ready-timeout", type=float, default=180)

    send = sub.add_parser("send")
    send.add_argument("--workdir", required=True)
    send.add_argument("payload", nargs="?", default=None)
    send.add_argument("--payload-file")
    send.add_argument("--timeout", type=float, default=180)

    status = sub.add_parser("status")
    status.add_argument("--workdir", required=True)

    closeout = sub.add_parser("closeout")
    closeout.add_argument("--workdir", required=True)
    closeout.add_argument("--timeout", type=float, default=180)

    run = sub.add_parser("_run", help=argparse.SUPPRESS)
    run.add_argument("--session-id", required=True)
    run.add_argument("--label", required=True)
    run.add_argument("--url", required=True)
    run.add_argument("--workdir", required=True)
    run.add_argument("--socket", default=DEFAULT_SOCKET)
    run.add_argument("--ttl-seconds", type=int, default=600)
    run.add_argument("--ready-timeout", type=float, default=180)

    args = parser.parse_args()
    if args.cmd == "start":
        return cmd_start(args)
    if args.cmd == "send":
        return cmd_send(args)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "closeout":
        return cmd_closeout(args)
    if args.cmd == "_run":
        return _controller_main(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
