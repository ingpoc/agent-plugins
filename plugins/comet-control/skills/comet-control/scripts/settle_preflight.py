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
     (default 60, the same as browser-harness's remote daemon startup window)
Any miss exits 3 with ``{"ok": false, "gate": "li_settle_preflight", "stage": ...}``,
a ``cause`` and per-stage ``timings``. On any prove miss the lease's daemons are
reaped (probe process group + BU_NAME daemons) so nothing keeps the bridge's one
Browser Use slot. The only retry is the bounded ``slot_busy`` backoff: the lease
bridge frees its slot only after the dead client's in-flight call returns, so a
respawn right after a kill can get 1008 "lease already has a Browser Use client".
Every run appends one JSON line (no secrets) to ``<work>/settle-preflight.jsonl``.

One overall deadline (``--timeout``, default 75 s) covers discovery, kill, slot wait, the
probe (about the harness's 60 s window) and a cleanup reserve; slow discovery fails as
``discovery_slow`` and a failed or unverified reap as ``cleanup_failed`` (never retried).
The probe pid is written to ``<work>/settle-probe.pid`` so ``--reap-only`` can clean up
after a caller killed this script mid-probe. Log tails, daemon-log copies and the jsonl
are redacted (BU_CDP_WS, ws/wss URLs, token=/key= values).

2026-09-26 (lease d620efc3): the first probe ran past 30 s (cause cdp_slow/unknown;
the daemon log was truncated by the next spawn), its subprocess timeout left the
detached daemon holding the slot, and the next preflight's respawn got 1008.
"""
from __future__ import annotations

import argparse
import fcntl
import functools
import hashlib
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
DEFAULT_TIMEOUT_S = 75.0          # whole run; leaves the probe ~the harness 60 s startup window
CLEANUP_RESERVE_S = 8.0           # held back inside the deadline for reap + verify
DISCOVERY_CAP_S = 10.0            # per ps / env lookup
PROBE_PID_FILE = "settle-probe.pid"
SLOT_WAIT_S = 3.0                 # after killing a live client: bridge join (<=2 s) + hide_cursor
SLOT_BUSY_BACKOFF_S = (1.0, 2.0, 4.0)
SLOT_BUSY_RE = re.compile(r"lease already has a Browser Use client")
DAEMON_RE = re.compile(r"browser_harness[./]daemon\b")
PROBE_PROGRAM = (
    "import json\n"
    "value = js('1+1')\n"
    "info = page_info()\n"
    "print('SETTLE ' + json.dumps({'js': value, 'url': info.get('url'), "
    "'title': info.get('title'), 'dialog': info.get('dialog')}))\n"
)


_SECRETS: set[str] = set()
WS_URL_RE = re.compile(r"\b(wss?)://[^\s\"'<>]+")
TOKEN_RE = re.compile(r"(?i)\b(token|access_token|key|api_key|secret|password|auth)=([^&\s\"'<>]+)")


def redact(text: str) -> str:
    """Mask BU_CDP_WS (and any registered secret), ws/wss URLs and token=/key= style values."""
    if not text:
        return text
    for s in sorted(_SECRETS, key=len, reverse=True):
        if s:
            text = text.replace(s, "[redacted]")
    text = WS_URL_RE.sub(lambda m: m.group(1) + "://[redacted]", text)
    return TOKEN_RE.sub(lambda m: m.group(1) + "=[redacted]", text)


def scrub(obj):
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, list):
        return [scrub(x) for x in obj]
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items()}
    return obj


class Exhausted(Exception):
    """The overall deadline ran out during a process scan."""


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
    _SECRETS.add(env["BU_CDP_WS"])
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


def list_processes(timeout: float = DISCOVERY_CAP_S) -> list[tuple[int, str]]:
    out = subprocess.run(["ps", "-axww", "-o", "pid=,command="], capture_output=True, text=True, timeout=timeout).stdout
    rows = []
    for line in out.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if pid.isdigit():
            rows.append((int(pid), cmd.strip()))
    return rows


def process_env_has(pid: int, key: str, value: str, timeout: float = DISCOVERY_CAP_S) -> bool:
    """True iff the process environment carries key=value (same-user processes)."""
    proc_env = Path(f"/proc/{pid}/environ")
    if proc_env.exists():
        try:
            return f"{key}={value}".encode() in proc_env.read_bytes().split(b"\0")
        except OSError:
            return False
    out = subprocess.run(["ps", "-E", "-ww", "-o", "command=", "-p", str(pid)],
                         capture_output=True, text=True, timeout=timeout).stdout
    return re.search(rf"(?:^|\s){re.escape(key)}={re.escape(value)}(?:\s|$)", out) is not None


def _slice(deadline: float | None) -> float:
    if deadline is None:
        return DISCOVERY_CAP_S
    left = deadline - time.monotonic()
    if left < 0.5:
        raise Exhausted(f"deadline passed ({left:.1f}s left)")
    return min(DISCOVERY_CAP_S, left)


def lease_daemons(name: str, pidfile_pid: int | None, procs=None, env_has=process_env_has,
                  deadline: float | None = None, lister=None) -> list[int]:
    """Every browser_harness daemon for this lease. With a deadline, each ps / env lookup gets
    min(10 s, time left) and running out raises Exhausted (TimeoutExpired is mapped too)."""
    try:
        if procs is None:
            procs = (lister or list_processes)(_slice(deadline))
        hits = []
        for pid, cmd in procs:
            if not DAEMON_RE.search(cmd):
                continue
            if pid == pidfile_pid:
                hits.append(pid)
            elif deadline is None and env_has(pid, "BU_NAME", name):
                hits.append(pid)
            elif deadline is not None and env_has(pid, "BU_NAME", name, timeout=_slice(deadline)):
                hits.append(pid)
    except subprocess.TimeoutExpired as exc:
        raise Exhausted(f"process scan timed out: {exc}") from exc
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


def daemon_log_path(name: str, environ: dict[str, str] | None = None) -> Path:
    """Mirror browser_harness paths.tmp_dir() / _ipc.log_path()."""
    e = os.environ if environ is None else environ
    raw = e.get("BH_TMP_DIR")
    if raw:
        return Path(raw).expanduser() / (f"bu-{name}.log" if e.get("BH_TMP_DIR_SHARED") == "1" else "bu.log")
    home = e.get("BH_HOME") or e.get("BROWSER_HARNESS_HOME")
    if home:
        root = Path(home).expanduser()
    elif e.get("XDG_CONFIG_HOME"):
        root = Path(e["XDG_CONFIG_HOME"]).expanduser() / "browser-harness"
    else:
        root = Path.home() / ".config" / "browser-harness"
    return root / "tmp" / f"bu-{name}.log"


def read_tail(path: Path, limit: int = 600) -> str:
    try:
        return path.read_text(errors="replace")[-limit:]
    except OSError:
        return ""


def keep_daemon_log(log: Path, work: Path, tag: str) -> str:
    """The daemon truncates its log on every spawn; keep a copy in the lease dir first."""
    text = read_tail(log, 20000)
    if not text:
        return ""
    dest = work / f"settle-daemon-{tag}.log"
    try:
        dest.write_text(redact(text))
    except OSError:
        return ""
    return str(dest)


def classify(out: str, log_tail: str, timed_out: bool) -> str:
    text = f"{out}\n{log_tail}"
    if SLOT_BUSY_RE.search(text):
        return "slot_busy"
    if "Received duplicate response" in log_tail or re.search(r"^enable \w+ on \S+: *$", log_tail, re.M):
        return "cdp_slow"
    if "listening on" in log_tail:
        return "probe_slow" if timed_out else "probe_failed"
    if "connecting to" in log_tail:
        return "attach_slow" if timed_out else "attach_failed"
    if not log_tail.strip():
        return "spawn_slow" if timed_out else "spawn_failed"
    return "unknown"


def run_probe(cmd: list[str], env: dict[str, str], timeout: float,
              pid_path: Path | None = None) -> tuple[int | None, str]:
    """Run the probe in its own process group; on timeout kill the whole group. The group id is
    recorded in pid_path while it runs so --reap-only can kill it if this script is killed."""
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, env=env, cwd=str(Path.home()), start_new_session=True)
    if pid_path is not None:
        try:
            pid_path.write_text(json.dumps({"pgid": proc.pid, "cmd": Path(cmd[0]).name}))
        except OSError:
            pass
    try:
        out, _ = proc.communicate(PROBE_PROGRAM, timeout=timeout)
        return proc.returncode, out or ""
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            out, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out = ""
        return None, out or ""
    finally:
        if pid_path is not None:
            try:
                pid_path.unlink()
            except OSError:
                pass


def respawn_and_prove(lease_env: dict[str, str], timeout: float, expect_url: str, *,
                      reap=lambda: [], work: Path | None = None, probe=run_probe,
                      sleep=time.sleep) -> dict:
    bin_ = os.environ.get("BROWSER_USE_BIN", "uvx")
    cmd = [bin_, "browser-use@latest"] if bin_ == "uvx" else [bin_]
    env = {**os.environ, **lease_env, "PYTHONUNBUFFERED": "1"}
    log = daemon_log_path(lease_env["BU_NAME"], env)
    started = time.monotonic()
    attempts: list[dict] = []

    def reap_or_fail(probe_cause: str, **ctx) -> list[int]:
        # Cleanup failure is its own classified GateFail and is never retried.
        try:
            return reap()
        except GateFail as cf:
            cf.payload.update(probe_cause=probe_cause, attempts=attempts, **ctx)
            raise

    while True:
        remaining = timeout - (time.monotonic() - started)
        if remaining < 1:
            raise GateFail("prove", f"respawn+probe exceeded {timeout:g}s", cause=attempts[-1]["cause"] if attempts else "budget",
                           attempts=attempts)
        t0 = time.monotonic()
        rc, out = probe(cmd, env, remaining)
        spent = round(time.monotonic() - t0, 2)
        if rc == 0:
            try:
                proof = parse_probe(out, expect_url)
            except GateFail as fail:
                attempts.append({"s": spent, "rc": 0, "cause": "bad_proof"})
                reaped = reap_or_fail("bad_proof", tail=out[-600:])
                fail.payload.update(cause="bad_proof", attempts=attempts, reaped=reaped)
                raise
            attempts.append({"s": spent, "rc": 0, "cause": "ok"})
            proof["seconds"] = round(time.monotonic() - started, 2)
            proof["attempts"] = attempts
            return proof
        log_tail = read_tail(log)
        cause = classify(out, log_tail, rc is None)
        attempts.append({"s": spent, "rc": rc, "cause": cause})
        kept = keep_daemon_log(log, work, f"a{len(attempts)}") if work else ""
        reaped = reap_or_fail(cause, tail=out[-600:], daemon_log=log_tail, daemon_log_copy=kept)
        n_busy = sum(1 for a in attempts if a["cause"] == "slot_busy")
        if cause == "slot_busy" and n_busy <= len(SLOT_BUSY_BACKOFF_S):
            left = timeout - (time.monotonic() - started)
            if left > SLOT_BUSY_BACKOFF_S[n_busy - 1] + 1:
                sleep(SLOT_BUSY_BACKOFF_S[n_busy - 1])
                continue
        reason = f"respawn+probe exceeded {timeout:g}s" if rc is None else f"browser-use exited {rc}"
        raise GateFail("prove", reason, cause=cause, attempts=attempts, reaped=reaped,
                       tail=out[-600:], daemon_log=log_tail, daemon_log_copy=kept)


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


def append_timings(work: Path, record: dict) -> None:
    try:
        with (work / "settle-preflight.jsonl").open("a") as fh:
            fh.write(json.dumps(scrub(record)) + "\n")
    except OSError:
        pass


def cleanup_fail(reason: str, **extra) -> GateFail:
    return GateFail("cleanup", reason, cause="cleanup_failed", **extra)


def reap_lease(name: str, pid_file: Path, deadline: float, lister=None) -> list[int]:
    """Kill every daemon for this lease, then re-scan to prove none is left. The harness daemon
    runs in its own session, so the probe's killpg never reaches it; the BU_NAME scan does."""
    try:
        pids = lease_daemons(name, pid_from_file(pid_file), deadline=deadline, lister=lister)
        killed = kill_all(pids) if pids else []
        left = lease_daemons(name, pid_from_file(pid_file), deadline=deadline, lister=lister)
    except GateFail as exc:  # kill_all: survived SIGKILL
        raise cleanup_fail("lease daemon survived cleanup", survivors=exc.payload.get("survivors"),
                           detail=exc.payload.get("reason")) from None
    except (Exhausted, OSError, subprocess.SubprocessError) as exc:
        raise cleanup_fail("cleanup could not finish", detail=f"{type(exc).__name__}: {exc}"[:300]) from None
    if left:
        raise cleanup_fail("lease daemon still present after kill", survivors=left, killed=killed)
    return killed


def reap_only(work: Path, budget: float = 15.0) -> dict:
    """After a caller killed this script mid-probe: kill the recorded probe group and every
    BU_NAME daemon, verify, report. A reused pid that is not our probe is never signalled."""
    deadline = time.monotonic() + budget
    lease_env = read_lease_env(work)
    name = lease_env["BU_NAME"]
    pid_file, _ = runtime_files(name, {**os.environ, **lease_env})
    pf = work / PROBE_PID_FILE
    probe = {"recorded": False, "killed": False}
    try:
        rec = json.loads(pf.read_text())
        pgid = int(rec["pgid"])
        probe["recorded"] = True
    except (OSError, ValueError, KeyError, TypeError):
        pgid = 0
    if pgid > 0:
        try:
            os.killpg(pgid, 0)
            group_alive = True
        except (ProcessLookupError, PermissionError):
            group_alive = False
        ours = group_alive
        if group_alive and pid_alive(pgid):  # leader still up: confirm it is our probe command
            cmdline = dict(list_processes(_slice(deadline))).get(pgid, "")
            ours = any(tok and tok in cmdline for tok in (rec.get("cmd"), "browser-use", "browser_use"))
            probe["cmd_matched"] = ours
        if ours:
            try:
                os.killpg(pgid, signal.SIGKILL)
                probe["killed"] = True
            except (ProcessLookupError, PermissionError):
                pass
    killed = reap_lease(name, pid_file, deadline)
    try:
        pf.unlink()
    except OSError:
        pass
    result = {"ok": True, "gate": GATE, "reap_only": True, "bu_name": name, "probe": probe, "killed": killed}
    append_timings(work, {"t": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **result})
    return result


def close_lease_daemons(work: Path, budget: float = 15.0, lister=None) -> dict:
    """Lease closeout (durable_lease_controller): kill every daemon for this lease's BU_NAME and
    prove none is left. Raises cleanup_failed (GateFail) otherwise."""
    deadline = time.monotonic() + budget
    lease_env = read_lease_env(work)
    name = lease_env["BU_NAME"]
    pid_file, _ = runtime_files(name, {**os.environ, **lease_env})
    killed = reap_lease(name, pid_file, deadline, lister=lister)
    result = {"verified_absent": True, "bu_name": name, "killed": killed}
    append_timings(work, {"t": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "gate": GATE, "closeout": True, **result})
    return result


LEASE_NAME_RE = re.compile(r"comet-[0-9a-f]{10}")


def lease_name(session_id: str) -> str:
    """Mirror browser_use_cdp_bridge.BrowserUseCDPBridge.name."""
    return "comet-" + hashlib.sha256(session_id.encode()).hexdigest()[:10]


def daemon_env(pid: int, timeout: float = DISCOVERY_CAP_S) -> dict[str, str]:
    """BU_NAME and BU_CDP_WS of a same-user daemon (values stay in memory; never printed)."""
    keys = ("BU_NAME", "BU_CDP_WS")
    proc_env = Path(f"/proc/{pid}/environ")
    if proc_env.exists():
        try:
            items = proc_env.read_bytes().split(b"\0")
        except OSError:
            return {}
        pairs = (i.decode(errors="replace").partition("=") for i in items)
        return {k: v for k, _, v in pairs if k in keys}
    out = subprocess.run(["ps", "-E", "-ww", "-o", "command=", "-p", str(pid)],
                         capture_output=True, text=True, timeout=timeout).stdout
    found = {}
    for key in keys:
        m = re.search(rf"(?:^|\s){key}=(\S+)", out)
        if m:
            found[key] = m.group(1)
    return found


def bridge_listening(ws_url: str | None, timeout: float = 0.5) -> bool:
    """True if the lease bridge's local websocket port still accepts connections (lease alive).
    Unknown / unparsable counts as alive, so the sweep never kills on doubt."""
    import socket
    from urllib.parse import urlparse

    if not ws_url:
        return True
    try:
        u = urlparse(ws_url)
        host, port = u.hostname, u.port
    except ValueError:
        return True
    if not host or not port:
        return True
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def sweep_stale(inventory, *, dry_run: bool = False, budget: float = 30.0, lister=None,
                env_of=daemon_env, listening=bridge_listening) -> dict:
    """Kill comet-* harness daemons whose lease is gone: session absent from the broker inventory
    AND its bridge port closed. Fails closed (kills nothing) when the inventory or scan fails.
    Daemons of live leases and non-comet BU_NAMEs are never touched."""
    deadline = time.monotonic() + budget
    try:
        sessions = inventory()
    except Exception as exc:
        raise GateFail("sweep", "session inventory unavailable; killed nothing",
                       detail=f"{type(exc).__name__}: {exc}"[:300]) from None
    live = {lease_name(str(s.get("session_id"))) for s in sessions if s.get("session_id")}
    try:
        procs = (lister or list_processes)(_slice(deadline))
        stale, kept = [], 0
        for pid, cmd in procs:
            if not DAEMON_RE.search(cmd):
                continue
            env = env_of(pid, timeout=_slice(deadline))
            name = env.get("BU_NAME")
            if (name and LEASE_NAME_RE.fullmatch(name) and name not in live
                    and not listening(env.get("BU_CDP_WS"))):
                stale.append({"pid": pid, "bu_name": name})
            else:
                kept += 1
    except (Exhausted, subprocess.TimeoutExpired) as exc:
        raise GateFail("sweep", "process scan did not finish; killed nothing", detail=str(exc)[:300]) from None
    pids = [d["pid"] for d in stale]
    if pids and not dry_run:
        try:
            kill_all(pids)
        except GateFail as exc:
            raise cleanup_fail("stale daemon survived sweep", survivors=exc.payload.get("survivors")) from None
    return {"ok": True, "gate": GATE, "sweep_stale": True, "dry_run": dry_run, "live_leases": len(live),
            "stale": stale, "killed": [] if dry_run else pids, "kept": kept}


def _controller_inventory():
    import importlib.util

    path = Path(__file__).resolve().with_name("durable_lease_controller.py")
    spec = importlib.util.spec_from_file_location("durable_lease_controller", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return lambda: mod._session_inventory(mod.DEFAULT_SOCKET)


def run(work: Path, timeout: float, expect_url: str, dry_run: bool, lock: str | None) -> dict:
    t_start = time.monotonic()
    deadline = t_start + timeout                 # one overall budget for the whole preflight
    work_deadline = deadline - CLEANUP_RESERVE_S  # discovery/kill/probe stop here; cleanup keeps the rest
    timings: dict[str, float] = {}

    def lap(key: str, t0: float) -> None:
        timings[key] = round(time.monotonic() - t0, 2)

    fd = None
    try:
        lease_env = read_lease_env(work)
        name = lease_env["BU_NAME"]
        pid_file, sock_file = runtime_files(name, {**os.environ, **lease_env})
        pidfile_pid = pid_from_file(pid_file)
        t0 = time.monotonic()
        try:
            daemons = lease_daemons(name, pidfile_pid, deadline=work_deadline)
        except Exhausted as exc:
            raise GateFail("discovery", f"process discovery ran out of the {timeout:g}s budget",
                           cause="discovery_slow", detail=str(exc)[:300]) from None
        lap("discovery_s", t0)
        plan = {"bu_name": name, "pid_file": str(pid_file), "sock_file": str(sock_file),
                "pidfile_pid": pidfile_pid, "pidfile_pid_alive": pid_alive(pidfile_pid), "daemons": daemons}
        if dry_run:
            return {"ok": True, "gate": GATE, "dry_run": True, **plan}

        def reap() -> list[int]:
            return reap_lease(name, pid_file, deadline)

        t0 = time.monotonic()
        fd = hold_lock(lock)
        lap("lock_s", t0)
        t0 = time.monotonic()
        if daemons:
            keep_daemon_log(daemon_log_path(name, {**os.environ, **lease_env}), work, "pre-kill")
        killed = kill_all(daemons) if daemons else []
        lap("kill_s", t0)
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
        if killed:
            t0 = time.monotonic()
            time.sleep(SLOT_WAIT_S)
            lap("slot_wait_s", t0)
        budget = work_deadline - time.monotonic()
        if budget < 1:
            raise GateFail("prove", f"no probe budget left inside {timeout:g}s", cause="budget")
        timings["probe_budget_s"] = round(budget, 2)
        t0 = time.monotonic()
        try:
            proof = respawn_and_prove(lease_env, budget, expect_url, reap=reap, work=work,
                                      probe=functools.partial(run_probe, pid_path=work / PROBE_PID_FILE))
        finally:
            lap("prove_s", t0)
    except GateFail as fail:
        timings["total_s"] = round(time.monotonic() - t_start, 2)
        fail.payload["timings"] = timings
        if not dry_run:
            append_timings(work, {"t": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **fail.payload})
        raise
    finally:
        if fd is not None:
            os.close(fd)
    timings["total_s"] = round(time.monotonic() - t_start, 2)
    result = {"ok": True, "gate": GATE, **plan, "killed": killed, "cleared": cleared, "proof": proof,
              "timings": timings}
    append_timings(work, {"t": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "ok": True, "bu_name": name,
                          "killed": killed, "timings": timings, "attempts": proof.get("attempts"),
                          "proof_s": proof.get("seconds")})
    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--work", default=os.environ.get("SMM_COMET_WORK") or os.environ.get("COMET_WORK"),
                   help="lease workdir containing browser-use.env")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                   help="overall budget (s): discovery + kill + probe + cleanup reserve; default 75 leaves ~60 s probe")
    p.add_argument("--expect-url", default="", help="substring page_info().url must contain, e.g. linkedin.com")
    p.add_argument("--lock", default=os.environ.get("SMM_BROWSER_USE_LOCK"),
                   help="controller flock to hold while settling (default $SMM_BROWSER_USE_LOCK)")
    p.add_argument("--dry-run", action="store_true", help="report daemons/runtime files; kill nothing")
    p.add_argument("--reap-only", action="store_true",
                   help="kill the recorded probe group + this lease's daemons, verify, exit (after a caller timeout)")
    p.add_argument("--sweep-stale", action="store_true",
                   help="kill comet-* harness daemons whose lease is gone from the broker (with --dry-run: list only)")
    args = p.parse_args(argv)
    if args.sweep_stale:
        try:
            result = sweep_stale(_controller_inventory(), dry_run=args.dry_run)
        except GateFail as fail:
            print(json.dumps(scrub(fail.payload)))
            return 3
        except Exception as exc:
            print(json.dumps({"ok": False, "gate": GATE, "stage": "sweep", "reason": f"{type(exc).__name__}: {exc}"[:300]}))
            return 3
        print(json.dumps(scrub(result)))
        return 0
    if not args.work:
        print(json.dumps({"ok": False, "gate": GATE, "stage": "args", "reason": "--work or SMM_COMET_WORK required"}))
        return 2
    try:
        if args.reap_only:
            result = reap_only(Path(args.work).expanduser())
        else:
            result = run(Path(args.work).expanduser(), args.timeout, args.expect_url, args.dry_run, args.lock)
    except GateFail as fail:
        print(json.dumps(scrub(fail.payload)))
        return 3
    print(json.dumps(scrub(result)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
