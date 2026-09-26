#!/usr/bin/env python3
"""li_settle_preflight: settle a lease's Browser Use daemon before the first navigate.

Why: on 2026-09-23 three LinkedIn leases needed ACU because a stale or duplicate
``browser_harness.daemon`` for the lease's BU_NAME kept the lease bridge's single
client slot (the bridge refuses a second client: "lease already has a Browser Use
client") or sat on a broken session (attach about:blank, "conn: Connection lost",
duplicate CDP responses). Every js()/Page.navigate then died on the helper's 5s
IPC read timeout.

What it does, for ONE lease (``--work`` = the lease workdir holding browser-use.env):
  1. read BU_NAME from browser-use.env (BU_CDP_WS is never printed)
  2. find every ``browser_harness.daemon`` process whose env has BU_NAME=<name>,
     plus the pid-file pid if it is a browser_harness daemon; kill them all
     (before the first navigate there is no in-flight work, so any existing
     daemon is stale or a duplicate by definition)
  3. remove runtime ``bu-<name>.pid`` / ``bu-<name>.sock`` when no live daemon owns them
     (a reused pid that is not a browser_harness daemon is never signalled)
  4. respawn by running one Browser Use program with the lease env
     (browser-harness auto-spawns the daemon) and prove ``js('1+1') == 2`` and
     ``page_info()`` returns a URL with no open JS dialog, within ``--timeout`` s
Any miss exits 3 with ``{"ok": false, "gate": "li_settle_preflight", "stage": ...}``.
No retries and no fallback: escalate per the ACU contract.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

GATE = "li_settle_preflight"
DAEMON_RE = re.compile(r"browser_harness[./]daemon\b")
PROBE_PROGRAM = (
    "import json\n"
    "value = js('1+1')\n"
    "info = page_info()\n"
    "print('SETTLE ' + json.dumps({'js': value, 'url': info.get('url'), "
    "'title': info.get('title'), 'dialog': info.get('dialog')}))\n"
)


class GateFail(SystemExit):
    def __init__(self, stage: str, reason: str, **extra) -> None:
        self.payload = {"ok": False, "gate": GATE, "stage": stage, "reason": reason, **extra}
        super().__init__(3)


def read_lease_env(work: Path) -> dict[str, str]:
    env_file = work / "browser-use.env"
    if not env_file.is_file():
        raise GateFail("lease_env", f"missing {env_file}")
    env: dict[str, str] = {}
    for raw in env_file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, sep, val = line.partition("=")
        if not sep or not key.strip():
            continue
        parts = shlex.split(val) if val.strip() else [""]
        env[key.strip()] = parts[0] if parts else ""
    if not env.get("BU_NAME") or not env.get("BU_CDP_WS"):
        raise GateFail("lease_env", "browser-use.env lacks BU_NAME or BU_CDP_WS")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", env["BU_NAME"]):
        raise GateFail("lease_env", "BU_NAME has unexpected characters")
    return env


def runtime_files(name: str, environ: dict[str, str] | None = None) -> tuple[Path, Path]:
    """Mirror browser_harness paths.runtime_dir() and _ipc._runtime_stem()."""
    e = os.environ if environ is None else environ
    raw = e.get("BH_RUNTIME_DIR")
    if raw:
        base = Path(raw).expanduser()
        stem = f"bu-{name}" if e.get("BH_RUNTIME_DIR_SHARED") else "bu"
    else:
        home = e.get("BH_HOME") or e.get("BROWSER_HARNESS_HOME")
        if home:
            root = Path(home).expanduser()
        elif e.get("XDG_CONFIG_HOME"):
            root = Path(e["XDG_CONFIG_HOME"]).expanduser() / "browser-harness"
        else:
            root = Path.home() / ".config" / "browser-harness"
        base, stem = root / "runtime", f"bu-{name}"
    return base / f"{stem}.pid", base / f"{stem}.sock"


def pid_from_file(path: Path) -> int | None:
    try:
        raw = path.read_text()
    except OSError:
        return None
    try:
        return int(json.loads(raw)["pid"])
    except (ValueError, TypeError, KeyError):
        try:
            return int(raw.split()[0])
        except (ValueError, IndexError):
            return None


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def list_processes() -> list[tuple[int, str]]:
    out = subprocess.run(["ps", "-axww", "-o", "pid=,command="], capture_output=True, text=True, timeout=10).stdout
    rows = []
    for line in out.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if pid.isdigit():
            rows.append((int(pid), cmd.strip()))
    return rows


def process_env_has(pid: int, key: str, value: str) -> bool:
    """True iff the process environment carries key=value (same-user processes)."""
    proc_env = Path(f"/proc/{pid}/environ")
    if proc_env.exists():
        try:
            return f"{key}={value}".encode() in proc_env.read_bytes().split(b"\0")
        except OSError:
            return False
    out = subprocess.run(["ps", "-E", "-ww", "-o", "command=", "-p", str(pid)],
                         capture_output=True, text=True, timeout=10).stdout
    return re.search(rf"(?:^|\s){re.escape(key)}={re.escape(value)}(?:\s|$)", out) is not None


def lease_daemons(name: str, pidfile_pid: int | None, procs=None, env_has=process_env_has) -> list[int]:
    procs = list_processes() if procs is None else procs
    hits = []
    for pid, cmd in procs:
        if not DAEMON_RE.search(cmd):
            continue
        if pid == pidfile_pid or env_has(pid, "BU_NAME", name):
            hits.append(pid)
    return sorted(set(hits))


def kill_all(pids: list[int], grace: float = 2.0) -> list[int]:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and any(pid_alive(p) for p in pids):
        time.sleep(0.1)
    for pid in pids:
        if pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    time.sleep(0.2)
    survivors = [p for p in pids if pid_alive(p)]
    if survivors:
        raise GateFail("kill", "daemon survived SIGKILL", survivors=survivors)
    return pids


def parse_probe(output: str, expect_url: str = "") -> dict:
    line = next((l for l in reversed(output.splitlines()) if l.startswith("SETTLE ")), None)
    if line is None:
        raise GateFail("prove", "probe printed no SETTLE line", tail=output[-600:])
    try:
        proof = json.loads(line[len("SETTLE "):])
    except ValueError:
        raise GateFail("prove", "SETTLE line is not JSON", tail=line[:300])
    if proof.get("js") != 2:
        raise GateFail("prove", "js('1+1') != 2", proof=proof)
    if proof.get("dialog"):
        raise GateFail("prove", "page has an open JS dialog", proof=proof)
    if not proof.get("url"):
        raise GateFail("prove", "page_info returned no url", proof=proof)
    if expect_url and expect_url not in str(proof.get("url")):
        raise GateFail("prove", f"page url does not contain {expect_url!r}", proof=proof)
    return proof


def respawn_and_prove(lease_env: dict[str, str], timeout: float, expect_url: str) -> dict:
    bin_ = os.environ.get("BROWSER_USE_BIN", "uvx")
    cmd = [bin_, "browser-use@latest"] if bin_ == "uvx" else [bin_]
    env = {**os.environ, **lease_env, "PYTHONUNBUFFERED": "1"}
    started = time.monotonic()
    try:
        done = subprocess.run(cmd, input=PROBE_PROGRAM, capture_output=True, text=True,
                              timeout=timeout, env=env, cwd=str(Path.home()))
    except subprocess.TimeoutExpired as exc:
        tail = (exc.stdout or "")[-600:] if isinstance(exc.stdout, str) else ""
        raise GateFail("prove", f"respawn+probe exceeded {timeout:g}s", tail=tail)
    out = (done.stdout or "") + (done.stderr or "")
    if done.returncode != 0:
        raise GateFail("prove", f"browser-use exited {done.returncode}", tail=out[-600:])
    proof = parse_probe(out, expect_url)
    proof["seconds"] = round(time.monotonic() - started, 2)
    return proof


def hold_lock(path: str | None):
    if not path:
        return None
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise GateFail("controller_lock", f"another browser-use controller holds {path}")
    return fd


def run(work: Path, timeout: float, expect_url: str, dry_run: bool, lock: str | None) -> dict:
    lease_env = read_lease_env(work)
    name = lease_env["BU_NAME"]
    pid_file, sock_file = runtime_files(name, {**os.environ, **lease_env})
    pidfile_pid = pid_from_file(pid_file)
    daemons = lease_daemons(name, pidfile_pid)
    plan = {"bu_name": name, "pid_file": str(pid_file), "sock_file": str(sock_file),
            "pidfile_pid": pidfile_pid, "pidfile_pid_alive": pid_alive(pidfile_pid), "daemons": daemons}
    if dry_run:
        return {"ok": True, "gate": GATE, "dry_run": True, **plan}
    fd = hold_lock(lock)
    try:
        killed = kill_all(daemons) if daemons else []
        cleared = []
        owner = pid_from_file(pid_file)
        if not pid_alive(owner) or owner in killed:
            for f in (pid_file, sock_file):
                if f.exists() or f.is_symlink():
                    f.unlink()
                    cleared.append(str(f))
        elif sock_file.exists() or pid_file.exists():
            # Live pid that is not a browser_harness daemon for this lease: pid reuse.
            raise GateFail("runtime_files", "pid file points at a live non-daemon process; refusing to guess",
                           pid=owner)
        proof = respawn_and_prove(lease_env, timeout, expect_url)
    finally:
        if fd is not None:
            os.close(fd)
    return {"ok": True, "gate": GATE, **plan, "killed": killed, "cleared": cleared, "proof": proof}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--work", default=os.environ.get("SMM_COMET_WORK") or os.environ.get("COMET_WORK"),
                   help="lease workdir containing browser-use.env")
    p.add_argument("--timeout", type=float, default=30.0, help="respawn + js/page_info proof budget (s)")
    p.add_argument("--expect-url", default="", help="substring page_info().url must contain, e.g. linkedin.com")
    p.add_argument("--lock", default=os.environ.get("SMM_BROWSER_USE_LOCK"),
                   help="controller flock to hold while settling (default $SMM_BROWSER_USE_LOCK)")
    p.add_argument("--dry-run", action="store_true", help="report daemons/runtime files; kill nothing")
    args = p.parse_args(argv)
    if not args.work:
        print(json.dumps({"ok": False, "gate": GATE, "stage": "args", "reason": "--work or SMM_COMET_WORK required"}))
        return 2
    try:
        result = run(Path(args.work).expanduser(), args.timeout, args.expect_url, args.dry_run, args.lock)
    except GateFail as fail:
        print(json.dumps(fail.payload))
        return 3
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
