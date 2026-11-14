"""Live tracker registry and per-list viewer lifecycle."""
import hashlib, json, os, re, signal, socket, subprocess, sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPTS = Path(__file__).resolve().parent
STATE = Path(os.environ.get("BCL_STATE_DIR", Path.home() / ".agents/state/batched-change-list"))

def safe_component(value):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(value)).strip(".-") or "list"

def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()

def repo_id(path):
    root = git(path, "rev-parse", "--show-toplevel")
    remotes = git(root, "remote", "-v").splitlines()
    for wanted in ("github", "origin"):
        for line in remotes:
            name, url, *_ = line.split()
            if name == wanted:
                match = re.search(r"(?:github\.com[:/])([^/]+/[^/.]+)", url)
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
        page.write_text(render.render(json.loads(live.read_text()), base))
    return page, base

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
        live = STATE / safe_component(repo.replace("/", "--")) / f"{safe_component(ident)}.json"
        live.parent.mkdir(parents=True, exist_ok=True)
        live.write_bytes(path.read_bytes())
        root = Path(git(path.parent, "rev-parse", "--show-toplevel"))
        html_path = Path(render_path).resolve() if render_path else path.with_suffix(".html")
        records[key] = {"live": str(live), "snapshot": str(path.relative_to(root)),
                        "html": str(html_path.relative_to(root)), "home": str(root), "repo": repo, "id": ident}
        save_registry(records)
    record = records.get(key)
    if not record: return key, None, None
    return key, record, Path(record["live"])

def ensure_viewer(key, record, live, plugin_version):
    state = STATE / "viewers" / (safe_component(key.replace("/", "--")) + ".json")
    state.parent.mkdir(parents=True, exist_ok=True)
    old = json.loads(state.read_text()) if state.exists() else {}
    running = False
    if old.get("pid") and int(old["pid"]) != 87005:
        try: os.kill(int(old["pid"]), 0); running = True
        except OSError: pass
    if running and (old.get("version") != plugin_version or old.get("data") != str(live)):
        os.kill(int(old["pid"]), signal.SIGTERM); running = False
    if running: return old
    port = int(old.get("port", 0))
    if not port or port == 53366:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
    else:
        with socket.socket() as sock:
            try: sock.bind(("127.0.0.1", port))
            except OSError: port = 0
            else: sock.close()
        if not port:
            with socket.socket() as sock: sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
    started = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%b %-d, %Y, %-I:%M %p IST")
    metadata = state.with_suffix(".viewer.json")
    metadata.write_text(json.dumps({"path": str(live), "version": plugin_version, "started": started}))
    page, base = report_paths(record, live)
    proc = subprocess.Popen([sys.executable, str(SCRIPTS / "serve.py"), str(page),
                             "--port", str(port), "--data", str(live), "--base", str(base), "--viewer-metadata", str(metadata)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           start_new_session=True)
    value = {"port": port, "pid": proc.pid, "version": plugin_version, "data": str(live), "started_at": started}
    state.write_text(json.dumps(value, indent=2) + "\n")
    return value

def snapshot(record, live):
    home = Path(record["home"])
    snapshot_path = home / record["snapshot"]; html_path = home / record["html"]
    marker = live.with_suffix(".unsnapshotted")
    if not home.exists():
        marker.write_text(hashlib.sha256(live.read_bytes()).hexdigest() + "\n")
        return
    import render
    doc = json.loads(live.read_text())
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_bytes(live.read_bytes())
    html_path.write_text(render.render(doc, snapshot_path.parent))
    subprocess.run(["git", "-C", str(home), "add", record["snapshot"], record["html"]], check=True)
    if subprocess.run(["git", "-C", str(home), "diff", "HEAD", "--quiet", "--", record["snapshot"], record["html"]]).returncode:
        subprocess.run(["git", "-C", str(home), "commit", "--only", "-m", "Change list: snapshot live tracker", "--", record["snapshot"], record["html"]], check=True)
    marker.unlink(missing_ok=True)
