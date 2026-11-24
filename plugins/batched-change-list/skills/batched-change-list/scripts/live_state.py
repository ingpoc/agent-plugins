"""Live tracker registry and per-list viewer lifecycle."""
import errno, hashlib, json, os, re, socket, subprocess, sys, time, urllib.error, urllib.request, uuid
import tracker_lock
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPTS = Path(__file__).resolve().parent
STATE = Path(os.environ.get("BCL_STATE_DIR", Path.home() / ".agents/state/batched-change-list"))

class ViewerPIDProbeError(Exception):
    """A PID could not be checked because the process is not accessible."""

def _windows_pid_status(pid):
    """Return whether a Windows process is live, or None if it is unverifiable."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False if ctypes.get_last_error() == 87 else None  # ERROR_INVALID_PARAMETER
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return None
        return exit_code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)

def _pid_is_running(pid, reject_unverifiable_pid=False):
    if os.name == "nt":
        running = _windows_pid_status(pid)
        if running is None:
            if reject_unverifiable_pid:
                raise ViewerPIDProbeError("[Errno 1] Operation not permitted")
            return True
        return running
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        if reject_unverifiable_pid:
            raise ViewerPIDProbeError("[Errno 1] Operation not permitted") from None
        return True

def safe_component(value):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(value)).strip(".-") or "list"

def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()

def repo_id(path, provider="gh"):
    root = git(path, "rev-parse", "--show-toplevel")
    remotes = git(root, "remote", "-v").splitlines()
    for wanted in (("origin", "github") if provider == "origin" else ("github", "origin")):
        for line in remotes:
            name, url, *_ = line.split()
            if name == wanted:
                match = (re.search(r"/codebase/([^/]+/[^/.]+)", url) or
                         re.search(r"(?:origin\.cursor\.com[:/])([^/]+/[^/.]+)", url)) if provider == "origin" else None
                match = match or re.search(r"(?:github\.com[:/])([^/]+/[^/.]+)", url)
                if match: return match.group(1).lower()
    common = Path(git(root, "rev-parse", "--git-common-dir"))
    common = (common if common.is_absolute() else root / common).resolve()
    return "local/" + hashlib.sha256(str(common).encode()).hexdigest()[:16]

def list_id(doc, source):
    tracking = doc.get("tracking", {})
    url = tracking.get("pull_request", {}).get("url", "")
    match = re.search(r"/pull/(\d+)", url)
    return match.group(1) if match else str(tracking.get("list_id") or Path(source).stem)


def archive_path(live):
    return Path(live).resolve().parent / "archive.json"


def parked_path(live):
    return Path(live).resolve().parent / "parked.json"


def registry():
    path = STATE / "registry.json"
    return json.loads(path.read_text()) if path.exists() else {}

def save_registry(value):
    STATE.mkdir(parents=True, exist_ok=True)
    path = STATE / "registry.json"
    tmp = path.with_suffix(".tmp"); tmp.write_text(json.dumps(value, indent=2) + "\n"); tmp.replace(path)

def _same_path(left, right):
    left, right = Path(left), Path(right)
    if left.resolve() == right.resolve():
        return True
    try:
        return left.exists() and right.exists() and os.path.samefile(left, right)
    except OSError:
        return False

def _guard_output(path, protected):
    for label, source in protected:
        if source and _same_path(path, source):
            raise ValueError(f"refusing to write {path}: it aliases the {label} {source}")

def _project_port(repo, records=None):
    records = records if records is not None else registry()
    existing = next((int(record["viewer_port"]) for record in records.values()
                     if record.get("repo") == repo and record.get("viewer_port")), None)
    if existing:
        return existing
    ports = {int(record["viewer_port"]) for record in records.values()
             if record.get("repo") != repo and record.get("viewer_port")}
    candidate = 53366 if 53366 not in ports and _port_is_free(53366) else 0
    if not candidate:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            candidate = sock.getsockname()[1]
    return candidate

def report_paths(record, live):
    home = Path(record["home"])
    snapshot = home / record["snapshot"]
    base = snapshot.parent if home.exists() else live.parent
    page = home / record["html"] if home.exists() else STATE / "views" / (safe_component(record["repo"] + "--" + record["id"]) + ".html")
    _guard_output(page, (("tracker", live), ("snapshot", snapshot)))
    page.parent.mkdir(parents=True, exist_ok=True)
    if not page.exists():
        import render
        try:
            page.write_text(render.render(json.loads(live.read_text()), base))
        except FileNotFoundError as error:
            missing = Path(error.filename or "")
            if not missing.is_absolute() or not missing.resolve().is_relative_to(base.resolve()):
                raise
            print(f"warning: report capture is missing ({missing.relative_to(base.resolve())}); page render skipped", file=sys.stderr)
    return page, base

def scripts_identity():
    digest = hashlib.sha256()
    for path in sorted(path for path in SCRIPTS.rglob("*") if path.suffix in (".py", ".json")):
        digest.update(path.relative_to(SCRIPTS).as_posix().encode() + b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()

def evidence_base(live):
    live = Path(live).resolve()
    if not live.is_relative_to(STATE.resolve()):
        return live.parent
    for record in registry().values():
        if Path(record["live"]).resolve() == live:
            return (Path(record["home"]) / record["snapshot"]).resolve().parent
    return live.parent

def _renamed_report_path(value, old_id, new_id, fallback):
    path = Path(value)
    name = re.sub(rf"(?<!\d){re.escape(str(old_id))}(?!\d)", str(new_id), path.name, count=1)
    if name == path.name:
        name = fallback
    return str(path.with_name(name))

def register_rollover(record, live, document, worktree):
    new_id = list_id(document, live)
    new_key = f"{record['repo']}/{new_id}"
    records = registry()
    if new_key in records:
        raise ValueError(f"new list is already adopted: {new_key}")
    old_id = record["id"]
    target = STATE.resolve() / safe_component(record["repo"].replace("/", "--")) / f"{safe_component(new_id)}.json"
    snapshot = _renamed_report_path(record["snapshot"], old_id, new_id, f"pr-{new_id}-findings.json")
    html = _renamed_report_path(record["html"], old_id, new_id, f"pr-{new_id}.html")
    if target.exists():
        raise ValueError(f"new live list already exists: {target}")
    old_home = Path(record["home"])
    try:
        entries = git(old_home, "worktree", "list", "--porcelain").split("\n\n")
    except (OSError, subprocess.CalledProcessError):
        entries = []
    home = next((Path(lines[0][9:]) for entry in entries if (lines := entry.splitlines())
                 and lines[0].startswith("worktree ")
                 and (Path(lines[0][9:]).name == str(worktree)
                      or f"branch refs/heads/{worktree}" in lines)), old_home.parent / worktree)
    for artifact in (home / snapshot, home / html):
        if artifact.exists():
            raise ValueError(f"new list report already exists: {artifact}")
    new_record = {**record, "id": new_id, "live": str(target), "snapshot": snapshot,
                  "html": html, "rollover_pending": True}
    new_record["home"] = str(home)
    target.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with target.open("xb") as output:
            created = True
            output.write((json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        records[new_key] = new_record
        save_registry(records)
    except BaseException:
        if created:
            target.unlink(missing_ok=True)
        raise
    return new_key, new_record, target

def _port_is_free(port):
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as error:
            if error.errno == errno.EADDRINUSE:
                return False
            raise
    return True

def _wait_for_port(port):
    deadline = time.monotonic() + 1
    while not _port_is_free(port):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True

def locate(path, adopt=False, render_path=None):
    path = Path(path).resolve()
    if path.is_relative_to(STATE.resolve()):
        for key, record in registry().items():
            if Path(record["live"]).resolve() == path: return key, record, path
    doc = json.loads(path.read_text())
    repo = repo_id(path.parent); ident = list_id(doc, path)
    key = f"{repo}/{ident}"
    records = registry()
    if adopt:
        live = STATE.resolve() / safe_component(repo.replace("/", "--")) / f"{safe_component(ident)}.json"
        if live.exists() or _same_path(path, live):
            raise ValueError(f"adoption destination already exists: {live}")
        live.parent.mkdir(parents=True, exist_ok=True)
        root = Path(git(path.parent, "rev-parse", "--show-toplevel")).resolve()
        html_path = Path(render_path).resolve() if render_path else path.with_suffix(".html")
        snapshot = root / path.relative_to(root)
        _guard_output(html_path, (("tracker", path), ("snapshot", snapshot)))
        records[key] = {"live": str(live), "snapshot": str(path.relative_to(root)),
                        "html": str(html_path.relative_to(root)), "home": str(root), "repo": repo, "id": ident,
                        "viewer_port": _project_port(repo, records)}
        with live.open("xb") as output:
            output.write(path.read_bytes())
        save_registry(records)
    record = records.get(key)
    if not record: return key, None, None
    return key, record, Path(record["live"]).resolve()

def ensure_viewer(key, record, live, plugin_version, reject_unverifiable_pid=False):
    project = record.get("repo")
    state = STATE / "viewers" / (safe_component(project.replace("/", "--") if project else key.replace("/", "--")) + ".json")
    state.parent.mkdir(parents=True, exist_ok=True)
    old_state = state
    old = json.loads(state.read_text()) if state.exists() else {}
    if not old and project:
        project_lives = {Path(item["live"]).resolve() for item in registry().values()
                         if item.get("repo") == project and item.get("live")}
        for candidate in state.parent.glob("*.json"):
            if candidate == state:
                continue
            candidate_data = json.loads(candidate.read_text())
            if (candidate_data.get("instance") and candidate_data.get("data") and
                    Path(candidate_data["data"]).resolve() in project_lives):
                old_state = candidate
                old = candidate_data
                break
    running = False
    if old.get("pid"):
        running = _pid_is_running(int(old["pid"]), reject_unverifiable_pid)
    owns_old = bool(old.get("data")) and Path(old["data"]).resolve() == Path(live).resolve()
    old_port = int(old.get("port", 0) or 0)
    if running:
        if not old.get("instance"):
            raise RuntimeError(f"cannot verify viewer ownership on port {old_port}; inspect its state before retrying")
        if not _viewer_responds(old_port, old):
            raise RuntimeError(f"cannot verify viewer ownership on port {old_port}; inspect its state before retrying")
        page, base = report_paths(record, live)
        scripts = scripts_identity()
        same_target = old.get("report") == str(page.resolve()) and old.get("base") == str(base.resolve()) and old.get("scripts") == str(SCRIPTS.resolve())
        same_live = Path(old.get("data", "")).resolve() == Path(live).resolve()
        if same_live and old.get("version") == plugin_version and old.get("scripts_identity") == scripts and same_target:
            return old
        shutdown_metadata = old_state.with_suffix(".viewer.json")
        request = urllib.request.Request(f"http://127.0.0.1:{old_port}/__bcl_shutdown", method="POST", headers={"X-BCL-Instance": old["instance"]})
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                if response.status != 204:
                    raise RuntimeError("viewer refused shutdown")
        except (OSError, urllib.error.URLError) as error:
            raise RuntimeError(f"viewer shutdown failed on port {old_port}; inspect it before retrying") from error
        if not _wait_for_port(old_port):
            raise RuntimeError(f"viewer on port {old_port} did not stop; inspect it before retrying")
        if old_state != state:
            old_state.unlink(missing_ok=True)
            shutdown_metadata.unlink(missing_ok=True)
    else:
        page, base = report_paths(record, live)
        scripts = scripts_identity()
    port = int(record.get("viewer_port") or old_port or _project_port(project or key))
    if not _port_is_free(port):
        if not running or port != old_port:
            raise RuntimeError(f"project viewer port {port} is occupied; inspect it before retrying")
    if not page.exists():
        raise RuntimeError(f"viewer report missing: {page}; render the report and retry")
    started = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%b %-d, %Y, %-I:%M %p IST")
    metadata = state.with_suffix(".viewer.json")
    instance = uuid.uuid4().hex
    metadata.write_text(json.dumps({"path": str(live), "version": plugin_version, "started": started, "instance": instance}))
    log = state.with_suffix(".log")
    with log.open("a") as errors:
        proc = subprocess.Popen([sys.executable, str(SCRIPTS / "serve.py"), str(page),
                                 "--port", str(port), "--data", str(live), "--base", str(base), "--viewer-metadata", str(metadata)],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors,
                               start_new_session=True)
    expected = {"pid": proc.pid, "data": str(Path(live).resolve()), "instance": instance}
    if not _viewer_ready(proc, port, expected):
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
        cause = log.read_text().strip()
        detail = f": {cause}" if cause else ""
        raise RuntimeError(f"viewer failed to start on port {port}{detail}; check {log} and retry")
    value = {"port": port, "pid": proc.pid, "version": plugin_version, "instance": instance,
             "data": str(Path(live).resolve()), "report": str(page.resolve()), "base": str(base.resolve()),
             "scripts": str(SCRIPTS.resolve()), "scripts_identity": scripts, "started_at": started}
    state.write_text(json.dumps(value, indent=2) + "\n")
    return value

def _viewer_process(pid, report):
    try:
        args = _process_arguments(pid)
    except (OSError, ValueError):
        return False
    return (args is not None and len(args) >= 3 and Path(os.fsdecode(args[0])).name.startswith("python") and
            Path(os.fsdecode(args[1])).name == "serve.py" and
            Path(os.fsdecode(args[2])).resolve() == Path(report).resolve())

def _process_arguments(pid):
    if sys.platform == "darwin":
        import ctypes
        sysctl = ctypes.CDLL(None, use_errno=True).sysctl
        sysctl.argtypes = (ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                           ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t)
        sysctl.restype = ctypes.c_int
        mib = (ctypes.c_int * 3)(1, 49, pid)  # KERN_PROCARGS2
        size = ctypes.c_size_t()
        if sysctl(mib, 3, None, ctypes.byref(size), None, 0) or not 4 < size.value <= 1_048_576:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        if sysctl(mib, 3, buffer, ctypes.byref(size), None, 0):
            return None
        raw = buffer.raw[:size.value]
        argc = int.from_bytes(raw[:4], byteorder=sys.byteorder, signed=True)
        if not 0 < argc < 4096:
            return None
        offset = raw.find(b"\0", 4)
        if offset < 0:
            return None
        while offset < len(raw) and raw[offset] == 0:
            offset += 1
        args = []
        for _ in range(argc):
            end = raw.find(b"\0", offset)
            if end < 0:
                return None
            args.append(raw[offset:end])
            offset = end + 1
        return args
    if sys.platform.startswith("linux"):
        return Path(f"/proc/{pid}/cmdline").read_bytes().rstrip(b"\0").split(b"\0")
    return None

def stop_viewer(key, record):
    state = STATE / "viewers" / (safe_component(key.replace("/", "--")) + ".json")
    if not state.exists():
        return False
    viewer = json.loads(state.read_text())
    pid = int(viewer.get("pid", 0) or 0)
    if pid and _pid_is_running(pid):
        report = viewer.get("report")
        if (Path(viewer.get("data", "")).resolve() != Path(record["live"]).resolve() or
                not isinstance(report, str) or not report or not _viewer_process(pid, report)):
            raise RuntimeError(f"cannot verify viewer PID {pid} for its recorded report")
        try:
            os.kill(pid, 15)
        except ProcessLookupError as error:
            raise RuntimeError(f"viewer process {pid} exited before it could be stopped") from error
        if not _wait_for_pid(pid):
            raise RuntimeError(f"viewer process {pid} did not stop")
    state.unlink()
    return True

def _wait_for_pid(pid):
    deadline = time.monotonic() + 2
    while _pid_is_running(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True

def _viewer_responds(port, expected):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/__bcl_health", timeout=0.5) as response:
            if response.status != 200 or json.load(response) != {
                "pid": expected["pid"], "path": str(Path(expected["data"]).resolve()), "instance": expected["instance"]}:
                return False
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as response:
            return response.status == 200 and bool(response.read(1))
    except (OSError, urllib.error.URLError, ValueError, KeyError):
        return False

def _viewer_ready(proc, port, expected):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and proc.poll() is None:
        if _viewer_responds(port, expected):
            return proc.poll() is None
        time.sleep(0.05)
    return False

def snapshot_lock_path(home):
    home = Path(home)
    lock_path = Path(git(home, "rev-parse", "--git-path", "bcl-snapshot.lock"))
    return lock_path if lock_path.is_absolute() else home / lock_path

def _active_snapshot_jobs(document):
    tracking = document.get("tracking") if isinstance(document.get("tracking"), dict) else {}
    plan = tracking.get("plan") if isinstance(tracking.get("plan"), dict) else {}
    groups = plan.get("groups") if isinstance(plan.get("groups"), list) else []
    active = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        job = (group.get("codex") or {}).get("job")
        if not isinstance(job, dict):
            continue
        finished_at = job.get("finished_at")
        if job.get("exit_code") is None and not (isinstance(finished_at, str) and finished_at.strip()):
            active.append(str(group.get("id", "unknown")))
    return active

def snapshot(record, live, *, pending_only=False):
    home = Path(record["home"])
    snapshot_path = home / record["snapshot"]; html_path = home / record["html"]
    _guard_output(snapshot_path, (("tracker", live),))
    _guard_output(html_path, (("tracker", live), ("snapshot", snapshot_path)))
    marker = live.with_suffix(".unsnapshotted")
    _guard_output(marker, (("tracker", live), ("snapshot", snapshot_path), ("report", html_path)))
    if not home.exists():
        marker.write_text(hashlib.sha256(live.read_bytes()).hexdigest() + "\n")
        raise RuntimeError(f"snapshot home worktree missing: {home}; restore it and retry")
    home_lock = snapshot_lock_path(home)
    tracker_path = tracker_lock.tracker_lock_path(live)

    def body():
        if pending_only and not marker.exists():
            return "nothing to save"
        if pending_only:
            active = _active_snapshot_jobs(json.loads(live.read_bytes()))
            if active:
                raise RuntimeError("active codex.job receipt(s): " + ", ".join(active))
        existed = marker.exists()
        import render
        captured = live.read_bytes()
        marker.write_text(hashlib.sha256(captured).hexdigest() + "\n")
        doc = json.loads(captured)
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            rendered = render.render(doc, snapshot_path.parent)
        except FileNotFoundError as error:
            missing = Path(error.filename or "")
            if not missing.is_absolute() or not missing.resolve().is_relative_to(snapshot_path.parent.resolve()):
                raise
            marker.write_text(hashlib.sha256(live.read_bytes()).hexdigest() + "\n")
            raise RuntimeError(f"snapshot capture missing: {missing}; restore it and retry") from error
        snapshot_path.write_bytes(captured)
        html_path.write_text(rendered)
        should_stage = pending_only
        if not pending_only:
            previous = subprocess.run(["git", "-C", str(home), "show", f"HEAD:{record['snapshot']}"], capture_output=True, text=True)
            current_tracking = doc.get("tracking", {})
            current_identity = (current_tracking.get("workflow", {}).get("stage"), current_tracking.get("cycle", 1))
            try:
                previous_tracking = json.loads(previous.stdout).get("tracking", {}) if previous.returncode == 0 else None
                previous_identity = (previous_tracking.get("workflow", {}).get("stage"), previous_tracking.get("cycle", 1))
            except (ValueError, AttributeError):
                previous_identity = None
            should_stage = previous_identity is None or current_identity != previous_identity
        committed = False
        if should_stage:
            subprocess.run(["git", "-C", str(home), "add", record["snapshot"], record["html"]], check=True)
            changed = subprocess.run(["git", "-C", str(home), "diff", "--cached", "HEAD", "--quiet", "--", record["snapshot"], record["html"]])
            if changed.returncode not in (0, 1):
                raise RuntimeError(f"snapshot diff failed with exit {changed.returncode}")
            if changed.returncode == 1:
                subprocess.run(["git", "-C", str(home), "commit", "--quiet", "--only", "-m", "Change list: snapshot live tracker", "--", record["snapshot"], record["html"]], check=True)
                committed = True
        def finalize():
            current = live.read_bytes()
            if current != captured:
                marker.write_text(hashlib.sha256(current).hexdigest() + "\n")
                raise RuntimeError("live tracker changed during snapshot; retry")
            if pending_only:
                marker.unlink(missing_ok=True)
                if committed:
                    return subprocess.run(["git", "-C", str(home), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
                return "already saved"
            if existed and not committed:
                return None
            marker.unlink(missing_ok=True)
            return None
        if pending_only:
            return finalize()
        with tracker_lock.exclusive_lock(tracker_path):
            return finalize()

    if pending_only:
        with tracker_lock.exclusive_lock(home_lock, blocking=False):
            with tracker_lock.exclusive_lock(tracker_path, blocking=False):
                return body()
    with tracker_lock.exclusive_lock(home_lock):
        return body()
