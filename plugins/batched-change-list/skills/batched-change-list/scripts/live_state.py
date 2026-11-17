"""Live tracker registry and per-list viewer lifecycle."""
import hashlib, json, os, re, socket, subprocess, sys, time, urllib.error, urllib.request, uuid
import tracker_lock
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPTS = Path(__file__).resolve().parent
STATE = Path(os.environ.get("BCL_STATE_DIR", Path.home() / ".agents/state/batched-change-list"))

class ViewerPIDProbeError(Exception):
    """A PID could not be checked because the process is not accessible."""

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

def registry():
    path = STATE / "registry.json"
    return json.loads(path.read_text()) if path.exists() else {}

def save_registry(value):
    STATE.mkdir(parents=True, exist_ok=True)
    path = STATE / "registry.json"
    tmp = path.with_suffix(".tmp"); tmp.write_text(json.dumps(value, indent=2) + "\n"); tmp.replace(path)

def report_paths(record, live):
    home = Path(record["home"])
    snapshot = home / record["snapshot"]
    base = snapshot.parent if home.exists() else live.parent
    page = home / record["html"] if home.exists() else STATE / "views" / (safe_component(record["repo"] + "--" + record["id"]) + ".html")
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

def register_rollover(record, live, document):
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
    home = Path(record["home"])
    for artifact in (home / snapshot, home / html):
        if artifact.exists():
            raise ValueError(f"new list report already exists: {artifact}")
    new_record = {**record, "id": new_id, "live": str(target), "snapshot": snapshot,
                  "html": html, "rollover_pending": True}
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

def _reserved_ports(current):
    reserved = set()
    for path in current.parent.glob("*.json"):
        if path == current or path.name.endswith(".viewer.json"):
            continue
        try:
            port = int(json.loads(path.read_text()).get("port", 0))
        except (OSError, ValueError, TypeError) as error:
            raise ValueError(f"invalid viewer state {path}; repair or remove that state file") from error
        if port:
            reserved.add(port)
    return reserved

def _port_is_free(port):
    with socket.socket() as sock:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
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
        if key in records:
            existing = Path(records[key]["live"])
            if path.read_bytes() != existing.read_bytes():
                raise ValueError(f"tracker is already adopted and differs from live state: {existing}")
            return key, records[key], existing
        live = STATE.resolve() / safe_component(repo.replace("/", "--")) / f"{safe_component(ident)}.json"
        live.parent.mkdir(parents=True, exist_ok=True)
        live.write_bytes(path.read_bytes())
        root = Path(git(path.parent, "rev-parse", "--show-toplevel"))
        html_path = Path(render_path).resolve() if render_path else path.with_suffix(".html")
        records[key] = {"live": str(live), "snapshot": str(path.relative_to(root)),
                        "html": str(html_path.relative_to(root)), "home": str(root), "repo": repo, "id": ident}
        save_registry(records)
    record = records.get(key)
    if not record: return key, None, None
    return key, record, Path(record["live"]).resolve()

def ensure_viewer(key, record, live, plugin_version, reject_unverifiable_pid=False):
    state = STATE / "viewers" / (safe_component(key.replace("/", "--")) + ".json")
    state.parent.mkdir(parents=True, exist_ok=True)
    old = json.loads(state.read_text()) if state.exists() else {}
    running = False
    if old.get("pid") and int(old["pid"]) != 87005:
        try: os.kill(int(old["pid"]), 0); running = True
        except ProcessLookupError: running = False
        except PermissionError:
            if reject_unverifiable_pid:
                raise ViewerPIDProbeError(f"[Errno 1] Operation not permitted") from None
            running = True
    owns_old = bool(old.get("data")) and Path(old["data"]).resolve() == Path(live).resolve()
    old_port = int(old.get("port", 0) or 0)
    if running and owns_old and (not old.get("instance") or not _viewer_responds(old_port, old)):
        raise RuntimeError(f"cannot verify viewer ownership on port {old_port}; inspect its state and stop it safely before retrying")
    if running and owns_old and old.get("version") == plugin_version:
        return old
    if running and owns_old:
        request = urllib.request.Request(f"http://127.0.0.1:{old_port}/__bcl_shutdown", method="POST", headers={"X-BCL-Instance": old["instance"]})
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                if response.status != 204:
                    raise RuntimeError("viewer refused shutdown")
        except (OSError, urllib.error.URLError) as error:
            raise RuntimeError(f"viewer shutdown failed on port {old_port}; inspect it before retrying") from error
        if not _wait_for_port(old_port):
            raise RuntimeError(f"viewer on port {old_port} did not stop; inspect it before retrying")
    reserved = _reserved_ports(state)
    candidates = []
    if owns_old and old_port:
        candidates.append(old_port)
    if 53366 not in reserved:
        candidates.append(53366)
    if not owns_old and old_port:
        reserved.add(old_port)
    port = 0
    for candidate in dict.fromkeys(candidates):
        if candidate in reserved:
            continue
        if _port_is_free(candidate):
            port = candidate
            break
        if candidate == 53366:
            print("warning: 127.0.0.1:53366 is occupied; leaving its owner untouched", file=sys.stderr)
    if not port:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
    page, base = report_paths(record, live)
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
        raise RuntimeError(f"viewer failed to start on port {port}; check {log} and retry")
    value = {"port": port, "pid": proc.pid, "version": plugin_version, "instance": instance,
             "data": str(Path(live).resolve()), "started_at": started}
    state.write_text(json.dumps(value, indent=2) + "\n")
    return value

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

def snapshot(record, live):
    home = Path(record["home"])
    snapshot_path = home / record["snapshot"]; html_path = home / record["html"]
    marker = live.with_suffix(".unsnapshotted")
    if not home.exists():
        marker.write_text(hashlib.sha256(live.read_bytes()).hexdigest() + "\n")
        raise RuntimeError(f"snapshot home worktree missing: {home}; restore it and retry")
    lock_path = Path(git(home, "rev-parse", "--git-path", "bcl-snapshot.lock"))
    if not lock_path.is_absolute():
        lock_path = home / lock_path
    with tracker_lock.exclusive_lock(lock_path):
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
        subprocess.run(["git", "-C", str(home), "add", record["snapshot"], record["html"]], check=True)
        changed = subprocess.run(["git", "-C", str(home), "diff", "HEAD", "--quiet", "--", record["snapshot"], record["html"]])
        if changed.returncode not in (0, 1):
            raise RuntimeError(f"snapshot diff failed with exit {changed.returncode}")
        if changed.returncode == 1:
            subprocess.run(["git", "-C", str(home), "commit", "--only", "-m", "Change list: snapshot live tracker", "--", record["snapshot"], record["html"]], check=True)
        with tracker_lock.exclusive_lock(tracker_lock.tracker_lock_path(live)):
            current = live.read_bytes()
            if current != captured:
                marker.write_text(hashlib.sha256(current).hexdigest() + "\n")
                raise RuntimeError("live tracker changed during snapshot; retry")
            marker.unlink(missing_ok=True)
