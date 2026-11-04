#!/usr/bin/env python3
"""Mac Mini tab for the hygiene map: disk, big folders, apps, LaunchAgents and
deterministic delete candidates.

Read-only. The slow part (du, mdls, tree probes) is cached in
~/.agents/hygiene/mini-scan.json. A cache older than 6 h is refreshed by a
detached background `hygiene_mini.py --scan`, so `hygiene check` stays quick;
`hygiene check --mini-rescan` rescans in the foreground. Nothing is deleted.

Classifier (classify): personal data and [protected].paths are never
candidates; a tree with a socket, lock or pid file, or a file written in the
last 24 h is live; idle 30+ days is a candidate; anything else is recent.
"""

from __future__ import annotations

import json
import os
import plistlib
import stat
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

SCAN_VERSION = 1
MAX_AGE_S = 6 * 3600
BIG_KB = 300 * 1024
IDLE_DAYS = 30
LIVE_S = 24 * 3600
PROBE_BUDGET_S = 3.0
DU_TIMEOUT = 600
DRILL = ("Library/Application Support", "Library/Caches", "Library/Group Containers",
         ".local/share", "Documents/remote-claude")
PERSONAL = ("Pictures", "Personal", "Documents/Personal")
LOCK_NAMES = {"lock", "LOCK", "SingletonLock", "SingletonSocket", "SingletonCookie", ".lock"}
LOCK_SUFFIXES = (".lock", ".pid", ".sock", ".socket", "-lock")

DESCRIBE = {
    "Library": "macOS per-user app data, caches, containers and settings",
    "Documents": "your documents (personal data)",
    "Pictures": "Photos library and images (personal data)",
    "Downloads": "downloaded files",
    "Desktop": "files on the desktop",
    "Movies": "videos", "Music": "music library",
    "code": "git checkouts, worktrees and scratch projects",
    ".local": "user-installed tools and their data (XDG ~/.local)",
    ".codex": "Codex CLI sessions, logs and caches",
    ".cache": "tool caches (pip, uv, Playwright, CUA); usually safe to regenerate",
    ".pyenv": "Python versions installed by pyenv",
    ".rustup": "Rust toolchains", ".cargo": "Rust packages and build cache",
    ".cursor": "Cursor editor extensions and data", ".npm": "npm package cache",
    ".nvm": "Node.js versions installed by nvm", ".claude": "Claude Code sessions and settings",
    ".agents": "agent plugins, hygiene tool and shared agent state",
    ".t3": "T3 Code app data", ".bun": "Bun runtime and package cache",
    ".docker": "Docker CLI config and contexts", ".ollama": "Ollama local models",
    ".gradle": "Gradle build cache", ".m2": "Maven package cache",
    "Application Support": "apps' saved data and settings",
    "Caches": "app caches; apps rebuild them when needed",
    "Group Containers": "data shared between an app's components (Office, WhatsApp, etc.)",
    "Containers": "sandboxed app data",
    "Developer": "Xcode derived data, simulators and device support",
    "Google": "Chrome / Google app data", "Comet": "Comet browser profile",
    "Cursor": "Cursor editor state", "Code": "VS Code state",
    "Claude": "Claude desktop app data", "Docker": "Docker Desktop data",
    "Homebrew": "Homebrew download cache", "pip": "pip download cache",
    "ms-playwright": "Playwright browsers", "com.apple.python": "Apple Python bytecode cache",
    "opencode": "OpenCode CLI data (protected)", "uv": "uv Python tool data",
    "pnpm": "pnpm store", "remote-claude": "remote-claude project tree (apps, archives)",
    "ai.perplexity.decider": "Perplexity Comet decider cache (protected)",
    "macos-cua": "macOS computer-use agent cache (protected)",
    "agent-plugins-origin-ci": "agent-plugins Origin CI cache (protected)",
}


def describe(path: str) -> str:
    name = Path(path).name
    if name in DESCRIBE:
        return DESCRIBE[name]
    low = name.lower()
    if "whatsapp" in low:
        return "WhatsApp messages and media (personal data)"
    if low.startswith("com.apple."):
        return "macOS system component data"
    if low.endswith("shipit") or "update" in low:
        return "app updater cache"
    return ""


def expand(p) -> Path:
    return Path(os.path.expanduser(str(p)))


def tilde(path, home: Path) -> str:
    s = str(path)
    h = str(home)
    return "~" + s[len(h):] if s == h or s.startswith(h + "/") else s


def protected_paths(m: dict) -> list[Path]:
    return [expand(p) for p in m.get("protected", {}).get("paths", [])]


def _under(p: Path, root: Path) -> bool:
    return p == root or root in p.parents


# ---------------------------------------------------------------- classifier

def probe(path: Path, now: float, budget_s: float = PROBE_BUDGET_S) -> dict:
    """Walk a tree (no symlink follow) for live markers and the newest mtime.
    Stops at the first live marker or when the time budget runs out."""
    t0 = time.monotonic()
    newest = 0.0
    stack = [path]
    while stack:
        if time.monotonic() - t0 > budget_s:
            return {"newest": newest, "live": None, "complete": False}
        d = stack.pop()
        try:
            st = os.lstat(d)
        except OSError:
            continue
        newest = max(newest, st.st_mtime)
        if not stat.S_ISDIR(st.st_mode):
            continue
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                name = e.name
                if stat.S_ISSOCK(st.st_mode):
                    return {"newest": max(newest, st.st_mtime), "live": f"socket {name}", "complete": False}
                if name in LOCK_NAMES or name.endswith(LOCK_SUFFIXES):
                    return {"newest": max(newest, st.st_mtime), "live": f"lock/pid file {name}", "complete": False}
                if st.st_mtime > now - LIVE_S and not stat.S_ISDIR(st.st_mode):
                    return {"newest": st.st_mtime, "live": f"{name} written in the last 24 h", "complete": False}
                newest = max(newest, st.st_mtime)
                if stat.S_ISDIR(st.st_mode):
                    stack.append(Path(e.path))
    return {"newest": newest, "live": None, "complete": True}


def personal_reason(p: Path, home: Path) -> str | None:
    if p == home / "Documents":
        return "personal data: Documents root"
    for rel in PERSONAL:
        if _under(p, home / rel):
            return f"personal data: ~/{rel}"
    if "whatsapp" in str(p).lower():
        return "personal data: WhatsApp"
    return None


def classify(path, m: dict, home: Path, now: float, pr: dict | None,
             parents: tuple = DRILL) -> tuple[str, str]:
    """('candidate'|'live'|'protected'|'personal'|'parent'|'recent'|'unknown', reason)."""
    p, home = expand(path), expand(home)
    why = personal_reason(p, home)
    if why:
        return "personal", why
    for root in protected_paths(m):
        if _under(p, root):
            return "protected", f"protected in machine.toml: {tilde(root, home)}"
        if _under(root, p):
            return "protected", f"contains protected path {tilde(root, home)}"
    if p == home or any(_under(home / r, p) for r in parents) or p == home / "Library":
        return "parent", "folder holding other scanned folders; see its drill-down"
    if pr is None:
        return "unknown", "not probed yet"
    if pr.get("live"):
        return "live", f"in use: {pr['live']}"
    if not pr.get("complete"):
        return "unknown", "too many files to confirm idle within the scan budget"
    idle = int((now - pr["newest"]) // 86400) if pr.get("newest") else None
    if idle is not None and idle >= IDLE_DAYS:
        return "candidate", f"idle {idle} days, no sockets/locks/pid files, not protected"
    return "recent", f"last written {idle} days ago (under {IDLE_DAYS})"


# ---------------------------------------------------------------- scan

def du_children(parent: Path) -> list[dict]:
    try:
        kids = [str(c) for c in parent.iterdir() if not c.is_symlink()]
    except OSError:
        return []
    out = []
    for i in range(0, len(kids), 200):
        try:
            r = subprocess.run(["du", "-skx", *kids[i:i + 200]], capture_output=True, text=True,
                               timeout=DU_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            continue
        for line in r.stdout.splitlines():
            kb, _, path = line.partition("\t")
            if kb.isdigit():
                out.append({"path": path, "kb": int(kb)})
    return sorted(out, key=lambda x: -x["kb"])


def last_used(app: Path) -> str | None:
    try:
        r = subprocess.run(["mdls", "-raw", "-name", "kMDItemLastUsedDate", str(app)],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    v = r.stdout.strip()
    return v[:10] if v and v != "(null)" else None


def scan_apps(home: Path) -> list[dict]:
    apps = []
    for root in (Path("/Applications"), home / "Applications"):
        try:
            apps += [a for a in root.iterdir() if a.suffix == ".app"]
        except OSError:
            pass

    def one(a: Path) -> dict:
        kb = None
        try:
            r = subprocess.run(["du", "-sk", str(a)], capture_output=True, text=True, timeout=120)
            kb = int(r.stdout.split("\t")[0]) if r.stdout else None
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        return {"name": a.stem, "path": str(a), "kb": kb, "last_used": last_used(a)}

    with ThreadPoolExecutor(8) as ex:
        return sorted(ex.map(one, apps), key=lambda x: -(x["kb"] or 0))


def scan(home: Path | None = None, now: float | None = None) -> dict:
    home = expand(home or Path.home())
    now = now or time.time()
    parents = [home] + [home / d for d in DRILL]
    with ThreadPoolExecutor(6) as ex:
        sizes = dict(zip([str(p) for p in parents], ex.map(du_children, parents)))
        apps_f = ex.submit(scan_apps, home)
        big = {e["path"] for lst in sizes.values() for e in lst if e["kb"] >= BIG_KB}
        probes = dict(zip(big, ex.map(lambda p: probe(Path(p), now), big)))
        apps = apps_f.result()
    return {"version": SCAN_VERSION, "scanned_at": now, "home": str(home),
            "sizes": sizes, "probes": probes, "apps": apps}


def cache_path(home: Path) -> Path:
    return expand(home) / ".agents" / "hygiene" / "mini-scan.json"


def save(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".mini-scan.", dir=str(path.parent))
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def load(path: Path) -> dict | None:
    try:
        d = json.loads(path.read_text())
        return d if d.get("version") == SCAN_VERSION else None
    except (OSError, ValueError):
        return None


_BACKGROUND: list = []


def _scan_running(lock: Path) -> bool:
    try:
        pid = int(lock.read_text().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def refresh_in_background(home: Path) -> bool:
    lock = cache_path(home).with_suffix(".pid")
    if _scan_running(lock):
        return True
    lock.parent.mkdir(parents=True, exist_ok=True)
    p = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--scan"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    _BACKGROUND.append(p)  # keep a reference: no ResourceWarning while it runs
    lock.write_text(str(p.pid))
    return True


def scan_data(home: Path, rescan: bool = False, now: float | None = None) -> tuple[dict | None, str]:
    """Return (cached scan, state). Only the real home auto-refreshes."""
    home = expand(home)
    now = now or time.time()
    path = cache_path(home)
    if rescan:
        data = scan(home, now)
        save(data, path)
        return data, "fresh"
    data = load(path)
    stale = data is None or now - data.get("scanned_at", 0) > MAX_AGE_S
    auto = os.environ.get("HYGIENE_MINI_BACKGROUND", "1") != "0"
    if stale and auto and home.resolve() == Path.home().resolve():
        try:
            refresh_in_background(home)
            return data, "refreshing"
        except OSError:
            pass
    return data, "stale" if stale else "fresh"


# ---------------------------------------------------------------- live bits

def disk() -> dict | None:
    try:
        r = subprocess.run(["df", "-k", "/"], capture_output=True, text=True, timeout=10)
        f = r.stdout.splitlines()[1].split()
        return {"total_kb": int(f[1]), "used_kb": int(f[2]), "free_kb": int(f[3])}
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return None


def loaded_labels() -> set[str]:
    try:
        r = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return set()
    out = set()
    for line in r.stdout.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) == 3:
            out.add(parts[2].strip())
    return out


def launch_agents(home: Path, loaded: set[str] | None = None) -> list[dict]:
    d = expand(home) / "Library" / "LaunchAgents"
    loaded = loaded_labels() if loaded is None else loaded
    out = []
    for f in sorted(d.glob("*.plist")) if d.is_dir() else []:
        try:
            with open(f, "rb") as fh:
                pl = plistlib.load(fh)
        except Exception:  # noqa: BLE001 - unreadable plist is itself a finding
            out.append({"file": f.name, "label": f.stem, "program": None, "exists": False,
                        "loaded": f.stem in loaded, "broken": True, "note": "unreadable plist"})
            continue
        label = str(pl.get("Label") or f.stem)
        args = pl.get("ProgramArguments") or []
        prog = pl.get("Program") or (args[0] if args else None)
        exists = bool(prog) and Path(str(prog)).exists()
        out.append({"file": f.name, "label": label, "program": prog, "exists": exists,
                    "loaded": label in loaded, "broken": not exists,
                    "note": "" if exists else "program missing: broken leftover"})
    return out


def build(m: dict, home: Path, rescan: bool = False, now: float | None = None) -> dict:
    """Everything the tab renders. Never raises on missing data."""
    home = expand(home)
    now = now or time.time()
    data, state = scan_data(home, rescan, now)
    tab = {"disk": disk(), "state": state, "agents": launch_agents(home),
           "scanned_at": None, "sections": [], "apps": [], "candidates": []}
    if not data:
        return tab
    tab["scanned_at"] = datetime.fromtimestamp(data["scanned_at"], timezone.utc).isoformat()
    tab["apps"] = data.get("apps", [])
    probes = data.get("probes", {})
    seen = {}
    for parent, entries in data.get("sizes", {}).items():
        rows = []
        for e in entries:
            if e["kb"] < BIG_KB:
                continue
            cls, why = classify(e["path"], m, home, now, probes.get(e["path"]))
            row = {"path": tilde(e["path"], home), "kb": e["kb"], "desc": describe(e["path"]),
                   "class": cls, "reason": why}
            rows.append(row)
            seen[row["path"]] = row
        tab["sections"].append({"parent": tilde(parent, home), "rows": rows})
    tab["candidates"] = sorted((r for r in seen.values() if r["class"] == "candidate"),
                               key=lambda r: -r["kb"])
    return tab


def digest_part(tab: dict) -> dict:
    """Stable subset for the map's change hash (free disk rounded to GiB)."""
    d = tab.get("disk") or {}
    return {"scanned_at": tab.get("scanned_at"), "free_gib": (d.get("free_kb") or 0) // 1048576,
            "agents": [(a["label"], a["exists"], a["loaded"]) for a in tab.get("agents", [])]}


# ---------------------------------------------------------------- html

CSS = """
.tabs{display:flex;gap:4px;padding:0 18px;background:var(--panel);border-bottom:1px solid var(--line)}
.tabs button{border:0;border-bottom:3px solid transparent;border-radius:0;background:none;padding:8px 12px;font-size:13px;font-weight:600}
.tabs button.on{border-bottom-color:var(--acc);color:var(--acc)}
.tabp{flex:1;display:flex;flex-direction:column;min-height:0}.tabp[hidden]{display:none}
#mini{overflow:auto;padding:14px 18px 40px}#mini h2{font-size:15px;margin:22px 0 6px}
.dbar{display:flex;height:22px;border-radius:6px;overflow:hidden;border:1px solid var(--line);max-width:900px}
.dbar i{display:block}.du{background:var(--warn)}.do{background:var(--stale)}.df{background:var(--ok)}
.brow{display:grid;grid-template-columns:minmax(220px,360px) 90px 1fr;gap:8px;align-items:center;font-size:13px;padding:2px 0}
.brow .b{height:12px;background:var(--acc);border-radius:3px;min-width:2px}.brow .d{grid-column:1/-1;color:var(--mut);font-size:12px;margin:-2px 0 4px}
table.mt{border-collapse:collapse;font-size:13px;width:100%;max-width:1100px}table.mt td,table.mt th{border-top:1px solid var(--line);padding:4px 6px;text-align:left;vertical-align:top}
.c-candidate{color:var(--warn);font-weight:600}.c-live,.c-protected,.c-personal{color:var(--ok)}.c-broken{color:var(--fail);font-weight:600}
"""

JS = r"""
(function(){var b=document.querySelectorAll('.tabs button');[].forEach.call(b,function(x){x.addEventListener('click',function(){
[].forEach.call(b,function(y){y.classList.toggle('on',y===x);document.getElementById(y.dataset.t).hidden=y!==x});
try{location.hash=x.dataset.t}catch(e){}})});if(location.hash==='#tab-mini'&&b[1])b[1].click();})();
"""


def gib(kb) -> str:
    if kb is None:
        return "?"
    return f"{kb / 1048576:.1f} GB" if kb >= 1048576 else f"{kb / 1024:.0f} MB"


def render_tab(tab: dict, esc) -> str:
    h = []
    d = tab.get("disk")
    if d:
        t = d["total_kb"] or 1
        other = max(t - d["used_kb"] - d["free_kb"], 0)
        h.append(f'<h2>Disk ({gib(t)} total)</h2><div class="dbar">'
                 f'<i class="du" style="width:{d["used_kb"] * 100 / t:.2f}%" title="used by this volume"></i>'
                 f'<i class="do" style="width:{other * 100 / t:.2f}%" title="other APFS volumes"></i>'
                 f'<i class="df" style="width:{d["free_kb"] * 100 / t:.2f}%" title="free"></i></div>'
                 f'<p class="sub">{gib(d["free_kb"])} free · {gib(d["used_kb"])} used by / · '
                 f'{gib(other)} other APFS volumes (system, data, snapshots) · from <code>df -k /</code></p>')
    when = tab.get("scanned_at") or "never"
    h.append(f'<p class="sub">Folder scan: {esc(when)} UTC ({esc(tab.get("state"))}). '
             "Refreshed after 6 h in the background, or now with <code>hygiene check --mini-rescan</code>. "
             "Read-only: nothing here deletes anything.</p>")
    for s in tab.get("sections", []):
        rows = s["rows"]
        h.append(f'<h2>{"Home folders" if s["parent"] == "~" else esc(s["parent"])} (over 300 MB)</h2>')
        if not rows:
            h.append('<p class="hint">Nothing over 300 MB.</p>')
        top = max([r["kb"] for r in rows] or [1])
        for r in rows:
            h.append(f'<div class="brow"><code>{esc(r["path"])}</code><span>{gib(r["kb"])}</span>'
                     f'<div class="b" style="width:{r["kb"] * 100 / top:.1f}%"></div>'
                     f'<div class="d">{esc(r["desc"] or "")}{" · " if r["desc"] else ""}'
                     f'<span class="c-{esc(r["class"])}">{esc(r["class"])}</span>: {esc(r["reason"])}</div></div>')
    c = tab.get("candidates", [])
    h.append(f'<h2>Delete candidates ({len(c)}, {gib(sum(r["kb"] for r in c))})</h2>'
             '<p class="sub">Idle 30+ days, no sockets, locks or pid files, not protected or personal. '
             "Review before moving anything to ~/.Trash.</p><table class=\"mt\"><tr><th>Path</th><th>Size</th><th>Why</th></tr>")
    h += [f'<tr><td><code>{esc(r["path"])}</code><div class="sub">{esc(r["desc"])}</div></td>'
          f'<td>{gib(r["kb"])}</td><td>{esc(r["reason"])}</td></tr>' for r in c]
    h.append("</table>")
    ag = tab.get("agents", [])
    nb = sum(1 for a in ag if a["broken"])
    h.append(f'<h2>LaunchAgents ({len(ag)}, {nb} broken)</h2><table class="mt"><tr><th>Label</th><th>Program</th>'
             "<th>Program exists</th><th>Loaded</th></tr>")
    for a in ag:
        h.append(f'<tr><td><code>{esc(a["label"])}</code></td><td><code>{esc(a["program"] or "")}</code></td>'
                 f'<td class="{"c-broken" if a["broken"] else ""}">{"yes" if a["exists"] else "no: broken leftover"}</td>'
                 f'<td>{"loaded" if a["loaded"] else "not loaded"}</td></tr>')
    h.append("</table>")
    apps = tab.get("apps", [])
    h.append(f'<h2>Apps ({len(apps)})</h2><table class="mt"><tr><th>App</th><th>Size</th><th>Last used</th></tr>')
    h += [f'<tr><td>{esc(a["name"])} <span class="sub">{esc(a["path"])}</span></td><td>{gib(a["kb"])}</td>'
          f'<td>{esc(a["last_used"] or "unknown")}</td></tr>' for a in apps]
    h.append("</table>")
    return "".join(h)


if __name__ == "__main__":
    if sys.argv[1:] == ["--scan"]:
        home = Path.home()
        try:
            save(scan(home), cache_path(home))
        finally:
            try:
                cache_path(home).with_suffix(".pid").unlink()
            except OSError:
                pass
        raise SystemExit(0)
    print("usage: hygiene_mini.py --scan", file=sys.stderr)
    raise SystemExit(2)
