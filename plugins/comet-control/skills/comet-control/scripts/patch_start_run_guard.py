#!/usr/bin/env python3
from pathlib import Path

ctrl = Path("/Users/gurusharan/.agents/plugins/comet-control/skills/comet-control/scripts/durable_lease_controller.py")
text = ctrl.read_text()

start_mark = "def _live_controller_pid(workdir: Path, session_id: str) -> int | None:"
end_mark = "def _session_absence_proof(socket_path: str, session_id: str) -> dict[str, object]:"
i = text.find(start_mark)
j = text.find(end_mark)
if i < 0 or j < 0 or j <= i:
    raise SystemExit(f"helper markers missing i={i} j={j}")

new_helpers = r'''def _live_controller_pid(workdir: Path, session_id: str | None = None) -> int | None:
    """Return a live _run (preferred) or lease_driver pid for this session."""
    p = _paths(workdir)
    if p["pid"].exists():
        try:
            pid = int(p["pid"].read_text().strip() or "0")
        except ValueError:
            pid = 0
        if pid and _pid_alive(pid) and pid != os.getpid():
            return pid
    sid = str(session_id or "").strip()
    if not sid:
        return None
    try:
        out = subprocess.check_output(
            ["ps", "-ax", "-o", "pid=,command="],
            text=True,
            timeout=2,
        )
    except Exception:
        return None
    run_needle = f"durable_lease_controller.py _run --session-id {sid}"
    driver_needle = f"lease_driver.py --session-id {sid}"
    run_pid = None
    driver_pid = None
    for line in out.splitlines():
        raw = line.strip()
        if "awk " in raw:
            continue
        try:
            pid = int(raw.split(None, 1)[0])
        except ValueError:
            continue
        if pid == os.getpid() or not _pid_alive(pid):
            continue
        if run_needle in raw:
            run_pid = pid
        elif driver_needle in raw:
            driver_pid = pid
    return run_pid or driver_pid


def _session_id_from_ready(p: dict[str, Path]) -> str | None:
    if not p["ready"].exists():
        return None
    try:
        data = json.loads(p["ready"].read_text())
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    sid = data.get("session_id")
    if sid:
        return str(sid)
    first = data.get("first")
    if isinstance(first, dict) and first.get("session_id"):
        return str(first["session_id"])
    return None


def _repair_heartbeat(
    p: dict[str, Path],
    pid: int,
    *,
    session_id: str | None = None,
    socket_path: str | None = None,
) -> dict:
    p["pid"].write_text(str(pid) + "\n")
    p["alive"].write_text(str(pid) + "\n")
    existing: dict = {}
    if p["ready"].exists():
        try:
            loaded = json.loads(p["ready"].read_text())
            if isinstance(loaded, dict):
                existing = loaded
        except Exception:
            existing = {}
    existing["ok"] = True
    existing["restored"] = True
    existing["controller_pid"] = pid
    if session_id:
        existing["session_id"] = session_id
    elif not existing.get("session_id"):
        sid = _session_id_from_ready(p)
        if sid:
            existing["session_id"] = sid
    if socket_path:
        existing["socket_path"] = socket_path
    if not existing.get("lease_ready_at"):
        existing["lease_ready_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    p["ready"].write_text(json.dumps(existing, indent=2) + "\n")
    return existing


'''
text = text[:i] + new_helpers + text[j:]

old_run = '''    p = _paths(workdir)
    for key in ("log", "ready", "request", "response", "alive"):
        if p[key].exists():
            p[key].unlink()
'''
new_run = '''    p = _paths(workdir)
    live = _live_controller_pid(workdir, args.session_id)
    if live is not None and live != os.getpid():
        _repair_heartbeat(
            p,
            live,
            session_id=args.session_id,
            socket_path=args.socket,
        )
        _log(p["log"], f"controller_already_running pid={live}")
        return 1
    for key in ("log", "ready", "request", "response", "alive"):
        if p[key].exists():
            p[key].unlink()
'''
if old_run not in text:
    raise SystemExit("_run unlink marker missing")
if text.count(old_run) != 1:
    raise SystemExit(f"_run unlink marker count={text.count(old_run)}")
text = text.replace(old_run, new_run, 1)

old_start = '''    p = _paths(workdir)
    live = _live_controller_pid(workdir, args.session_id)
    if live is not None:
        _repair_alive(p, live)
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "controller_already_running",
                    "controller_pid": live,
                    "restored_alive": True,
                }
            )
        )
        return 1
'''
new_start = '''    p = _paths(workdir)
    live = _live_controller_pid(workdir, args.session_id)
    if live is not None:
        ready = _repair_heartbeat(
            p,
            live,
            session_id=args.session_id,
            socket_path=args.socket,
        )
        print(json.dumps(ready, indent=2))
        return 0
'''
if old_start not in text:
    raise SystemExit("cmd_start marker missing")
text = text.replace(old_start, new_start, 1)

old_send = '''    if not p["alive"].exists():
        print(json.dumps({"ok": False, "error": "controller_not_alive"}))
        return 1
'''
new_send = '''    if not p["alive"].exists():
        sid = _session_id_from_ready(p) or getattr(args, "session_id", None)
        live = _live_controller_pid(workdir, sid)
        if live is None:
            print(json.dumps({"ok": False, "error": "controller_not_alive"}))
            return 1
        _repair_heartbeat(
            p,
            live,
            session_id=sid,
            socket_path=getattr(args, "socket", None),
        )
'''
if old_send not in text:
    raise SystemExit("cmd_send marker missing")
# only the first occurrence in cmd_send; closeout also checks alive
count = text.count(old_send)
if count != 1:
    raise SystemExit(f"cmd_send alive marker count={count}")
text = text.replace(old_send, new_send, 1)

if "_repair_alive" in text:
    raise SystemExit("leftover _repair_alive")
if "lease_driver.py --session-id" not in text:
    raise SystemExit("driver needle missing")

ctrl.write_text(text)
print("patched", ctrl)
