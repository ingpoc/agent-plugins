#!/usr/bin/env python3
"""Static HTML map of agent-plugins on this Mac, refreshed after `hygiene check`.

`~/.agents/bin/hygiene.py check` calls `publish_safe(manifest, rows)` after every
run (skip with `--no-map`). It builds a normalized snapshot (clones, worktrees,
local / Origin / GitHub branches, installed plugin versions per harness versus
Origin main, hygiene status per check), hashes it, and rewrites
`~/.agents/hygiene/map.html` only when the hash changed. The write is atomic
(temp file in the same directory, then rename) and the page is self-contained:
no network, no scripts, no external assets.

Branch lists come from the canonical clone's remote-tracking refs (as of its
last fetch); this module never talks to the network.

Fail open: any error logs one line to stderr and leaves the hygiene result alone.
Installed copy: `~/.agents/bin/hygiene_map.py` (source: agent-plugins
`scripts/hygiene_map.py`). Manual run: `hygiene check --json | python3 hygiene_map.py`.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = "agent-plugins"
DEFAULT_OUT = Path("~/.agents/hygiene/map.html")
IST = timezone(timedelta(hours=5, minutes=30), "IST")
GIT_TIMEOUT = 10
SKIP_DIRS = {
    ".git", "node_modules", "Library", ".Trash", ".cache", "venv", ".venv",
    "dist", "build", "target", ".next", ".turbo", "__pycache__",
}
STATUSES = ("PASS", "WARN", "FAIL", "UNKNOWN")
# Where each harness keeps installed plugin copies, as globs under $HOME.
# `{p}` is the plugin name; each match is a directory holding plugin.json.
HARNESS_GLOBS = {
    "agents (~/.agents/plugins)": [".agents/plugins/{p}"],
    "codex": [".codex/plugins/cache/*/{p}/*"],
    "cursor": [".cursor/plugins/cache/*/{p}/*"],
    "claude": [".claude/plugins/cache/*/{p}/*"],
    "grok": [".grok/plugins/{p}", ".grok/plugins/cache/*/{p}/*"],
}
OWNER_PREFIXES = (
    ("codex/", "Codex"), ("cursor/", "Cursor"), ("claude/", "Claude"),
    ("grokbot/", "Grok Bot"), ("grok/", "Grok"), ("t3/", "T3"), ("goal/", "T3"),
    ("acu/", "ACU"), ("efficient/", "Efficient"), ("opencode/", "OpenCode"),
)
OWNER_PATHS = (
    ("/.t3/", "T3"), ("/.codex/", "Codex"), ("/.cursor/", "Cursor"),
    ("/.claude/", "Claude"), ("/.grok/", "Grok"), ("/Library/Caches/", "CI harness"),
    ("agent-plugins-origin-work", "CI harness"),
)


# ---------------------------------------------------------------- helpers

def expand(value: str | Path) -> Path:
    return Path(os.path.expanduser(str(value))).resolve()


def git(path: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(["git", "-C", str(path), *args], capture_output=True,
                              text=True, timeout=GIT_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def tilde(path: Path | str, home: Path) -> str:
    s = str(path)
    h = str(home)
    return "~" + s[len(h):] if s == h or s.startswith(h + os.sep) else s


def norm_remote(url: str) -> str:
    """host/path form: no scheme, no credentials, no .git (safe to display)."""
    url = url.strip().rstrip("/")
    if "://" in url:
        url = url.split("://", 1)[1]
    host, sep, rest = url.partition("/")
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    url = host + sep + rest
    if url.endswith(".git"):
        url = url[:-4]
    return url.lower()


def remotes(repo: Path) -> dict[str, str]:
    # Raw config, not `git remote -v`, which applies url.*.insteadOf rewrites.
    out = git(repo, "config", "--get-regexp", r"^remote\..*\.url$") or ""
    found: dict[str, str] = {}
    for line in out.splitlines():
        key, _, url = line.partition(" ")
        name = key[len("remote."):-len(".url")]
        if name and url:
            found.setdefault(name, url.strip())
    return found


def is_ours(urls: dict[str, str]) -> bool:
    return any(norm_remote(u).rsplit("/", 1)[-1] == REPO for u in urls.values())


def walk_clones(roots: list[Path], depth: int) -> list[Path]:
    found: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        base = len(root.parts)
        for here_s, dirs, _files in os.walk(root):
            here = Path(here_s)
            if len(here.parts) - base >= depth:
                dirs[:] = []
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            if (here / ".git").exists():
                if (here / ".git").is_dir():
                    found.add(here.resolve())
                dirs[:] = []
    return sorted(found, key=str)


def age_days(path: Path) -> int | None:
    try:
        st = path.stat()
    except OSError:
        return None
    born = getattr(st, "st_birthtime", None) or st.st_mtime
    return max(0, int((time.time() - born) // 86400))


def owner_of(path: str, branch: str) -> str:
    for prefix, name in OWNER_PREFIXES:
        if branch.startswith(prefix):
            return name
    for marker, name in OWNER_PATHS:
        if marker in path:
            return name
    return "unknown"


def placement(path: Path, m: dict) -> str:
    p = m.get("paths", {})
    ok = [expand(p[k]) for k in ("checkout_root", "worktree_root") if k in p]
    harness = [expand(x) for x in m.get("exceptions", {}).get("worktree_roots", [])]
    under = lambda root: path == root or root in path.parents  # noqa: E731
    if any(under(r) for r in harness):
        return "harness"
    if any(under(r) for r in ok):
        return "ok"
    return "outside"


def dirty_count(path: Path) -> int | None:
    out = git(path, "status", "--porcelain", "--untracked-files=normal")
    return None if out is None else len([x for x in out.splitlines() if x.strip()])


def remote_names(urls: dict[str, str]) -> tuple[str | None, str | None]:
    origin = github = None
    for name in sorted(urls):
        url = urls[name].lower()
        if "origin.cursor.com" in url and (origin is None or name == "origin-cursor"):
            origin = name
        elif "github.com" in url and github is None:
            github = name
    return origin, github


def refs(repo: Path, prefix: str) -> dict[str, str]:
    out = git(repo, "for-each-ref", "--format=%(refname)\t%(objectname)", prefix) or ""
    found: dict[str, str] = {}
    for line in out.splitlines():
        name, _, sha = line.partition("\t")
        short = name[len(prefix):].lstrip("/")
        if short and short != "HEAD":
            found[short] = sha
    return found


def merged(repo: Path, sha: str, main_sha: str | None) -> bool | None:
    if not main_sha:
        return None
    try:
        proc = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", sha, main_sha],
                              capture_output=True, timeout=GIT_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.returncode == 0 if proc.returncode in (0, 1) else None


def read_version(plugin_dir: Path) -> str | None:
    for rel in ("plugin.json", ".cursor-plugin/plugin.json", ".claude-plugin/plugin.json"):
        try:
            data = json.loads((plugin_dir / rel).read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("version"), str):
            return data["version"]
    return None


def vkey(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else x for x in v.replace("-", ".").split("."))


# ---------------------------------------------------------------- snapshot

def snapshot(m: dict, rows: list[dict], home: Path | None = None) -> dict:
    """Normalized, sorted, timestamp-free view of agent-plugins state."""
    home = expand(home or Path.home())
    p = m.get("paths", {})
    canonical = expand(p.get("checkout_root", "~")) / REPO
    scan = m.get("scan", {})
    roots = [expand(x) for x in scan.get("roots", [])] or [canonical.parent]
    clones = [c for c in walk_clones(roots, int(scan.get("depth", 4))) if is_ours(remotes(c))]
    if canonical.is_dir() and canonical.resolve() not in clones:
        clones.append(canonical.resolve())
    clones.sort(key=str)

    urls = remotes(canonical) if canonical.is_dir() else {}
    o_name, g_name = remote_names(urls)
    main_sha = None
    if o_name:
        main_sha = (git(canonical, "rev-parse", "--verify", "-q", f"refs/remotes/{o_name}/main") or "").strip() or None

    clone_rows, wt_rows, local_rows = [], [], []
    for clone in clones:
        out = git(clone, "worktree", "list", "--porcelain") or ""
        clone_main_sha = main_sha if clone == canonical.resolve() else None
        c_urls = remotes(clone)
        if clone_main_sha is None:
            on, _ = remote_names(c_urls)
            if on:
                clone_main_sha = (git(clone, "rev-parse", "--verify", "-q", f"refs/remotes/{on}/main") or "").strip() or None
        entries, cur = [], {}
        for line in out.splitlines() + [""]:
            if not line.strip():
                if cur:
                    entries.append(cur)
                cur = {}
                continue
            key, _, val = line.partition(" ")
            cur[key] = val or True
        clone_rows.append({
            "path": tilde(clone, home),
            "remotes": sorted(norm_remote(u) for u in set(c_urls.values())),
            "canonical": clone == canonical.resolve(),
            "worktrees": len(entries),
        })
        for e in entries:
            if e.get("bare"):
                continue
            wpath = Path(str(e.get("worktree", "")))
            branch = str(e.get("branch", "")).removeprefix("refs/heads/") if e.get("branch") else ""
            head = str(e.get("HEAD", ""))[:7]
            state = merged(clone, head, clone_main_sha) if head else None
            wt_rows.append({
                "path": tilde(wpath, home),
                "clone": tilde(clone, home),
                "kind": "clone" if wpath.resolve() == clone else "worktree",
                "branch": branch or "(detached)",
                "head": head,
                "dirty": dirty_count(wpath) if wpath.is_dir() else None,
                "age_days": age_days(wpath),
                "owner": owner_of(str(wpath), branch),
                "place": placement(wpath.resolve(), m) if wpath.is_dir() else "missing",
                "in_main": state,
            })
        for name, sha in refs(clone, "refs/heads").items():
            local_rows.append({"clone": tilde(clone, home), "name": name, "head": sha[:7],
                               "in_main": merged(clone, sha, clone_main_sha),
                               "owner": owner_of("", name)})

    origin_b = refs(canonical, f"refs/remotes/{o_name}") if o_name else {}
    github_b = refs(canonical, f"refs/remotes/{g_name}") if g_name else {}
    remote_rows = []
    for where, table, other in (("origin", origin_b, github_b), ("github", github_b, origin_b)):
        for name, sha in table.items():
            remote_rows.append({"where": where, "name": name, "head": sha[:7],
                                "in_main": merged(canonical, sha, main_sha),
                                "on_both": name in other, "owner": owner_of("", name)})

    plugins = set()
    main_versions: dict[str, str | None] = {}
    if main_sha:
        listing = git(canonical, "ls-tree", "--name-only", f"{main_sha}:plugins") or ""
        for name in listing.split():
            raw = git(canonical, "show", f"{main_sha}:plugins/{name}/plugin.json")
            try:
                main_versions[name] = json.loads(raw)["version"] if raw else None
            except (ValueError, KeyError, TypeError):
                main_versions[name] = None
            plugins.add(name)
    agents_dir = home / ".agents" / "plugins"
    if agents_dir.is_dir():
        plugins.update(d.name for d in agents_dir.iterdir() if (d / "plugin.json").is_file())
    plugin_rows = []
    for name in sorted(plugins):
        per = {}
        for harness, globs in HARNESS_GLOBS.items():
            versions = set()
            for g in globs:
                for d in home.glob(g.format(p=name)):
                    v = read_version(d) if d.is_dir() else None
                    if v:
                        versions.add(v)
            per[harness] = sorted(versions, key=vkey)
        plugin_rows.append({"plugin": name, "main": main_versions.get(name), "installed": per})

    counts = {s: sum(1 for r in rows if r.get("status") == s) for s in STATUSES}
    return {
        "repo": REPO,
        "canonical": tilde(canonical, home),
        "origin_main": main_sha[:7] if main_sha else None,
        "clones": clone_rows,
        "worktrees": sorted(wt_rows, key=lambda r: (r["clone"], r["kind"] != "clone", r["path"])),
        "local_branches": sorted(local_rows, key=lambda r: (r["clone"], r["name"])),
        "remote_branches": sorted(remote_rows, key=lambda r: (r["where"], r["name"])),
        "plugins": plugin_rows,
        "hygiene": {
            "counts": counts,
            # Details carry sizes and free space, which change every run; only
            # the per-check status goes into the hash.
            "checks": sorted([[r.get("check", ""), r.get("status", "")] for r in rows]),
        },
    }


def snapshot_hash(snap: dict) -> str:
    blob = json.dumps(snap, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode()).hexdigest()


# ---------------------------------------------------------------- html

CSS = """
:root{--bg:#fff;--fg:#1d1d1f;--mut:#6e6e73;--line:#e5e5ea;--card:#f6f6f8;--ok:#1a7f37;--warn:#9a6700;--bad:#cf222e;--acc:#0969da}
@media (prefers-color-scheme:dark){:root{--bg:#111214;--fg:#ececf1;--mut:#9a9aa3;--line:#2a2b30;--card:#1a1b1f;--ok:#3fb950;--warn:#d29922;--bad:#f85149;--acc:#58a6ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
main{max-width:1200px;margin:0 auto;padding:24px}h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px}
.sub{color:var(--mut);margin:0 0 16px}nav a{color:var(--acc);margin-right:12px;text-decoration:none}
.metrics{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0}.m{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 14px;min-width:150px}
.m b{display:block;font-size:20px}.m span{color:var(--mut);font-size:12px}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mut);font-weight:600}td.n{text-align:right}code{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;word-break:break-all}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.mut{color:var(--mut)}ul{margin:4px 0 0 18px;padding:0}
"""


def esc(v) -> str:
    return html.escape("" if v is None else str(v))


def tag(text: str, cls: str) -> str:
    return f'<span class="{cls}">{esc(text)}</span>'


def main_tag(state) -> str:
    return tag("in main", "ok") if state is True else tag("not in main", "warn") if state is False else tag("unknown", "mut")


def table(headers: list[str], rows: list[list[str]], numeric: set[int] = frozenset()) -> str:
    if not rows:
        return '<p class="mut">None.</p>'
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(
        f'<td class="n">{c}</td>' if i in numeric else f"<td>{c}</td>" for i, c in enumerate(r)) + "</tr>"
        for r in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render_html(snap: dict, rows: list[dict], generated_at: datetime) -> str:
    when = generated_at.astimezone(IST).strftime("%a %d %b %Y, %H:%M IST")
    c = snap["hygiene"]["counts"]
    plugin_ok = sum(1 for p in snap["plugins"]
                    if p["main"] and all(v == [p["main"]] for v in p["installed"].values() if v))
    wt = [w for w in snap["worktrees"] if w["kind"] == "worktree"]
    metrics = [
        (snap["origin_main"] or "unknown", "Origin main"),
        (f'{c["PASS"]} / {c["WARN"]} / {c["FAIL"]}', "Hygiene pass / warn / fail"),
        (str(len(snap["clones"])), "Clones of agent-plugins"),
        (str(len(wt)), "Linked worktrees"),
        (f'{plugin_ok} / {len(snap["plugins"])}', "Plugins matching main everywhere"),
    ]
    out = [
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">",
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>agent-plugins map ({esc(when)})</title><style>{CSS}</style></head><body><main>",
        "<h1>agent-plugins on this Mac: checkouts, worktrees, branches</h1>",
        f'<p class="sub">Last updated {esc(when)}, written by <code>hygiene check</code> when the snapshot changes. '
        f'Canonical clone <code>{esc(snap["canonical"])}</code>. Branch lists come from its remote-tracking refs as of its last fetch.</p>',
        '<nav><a href="#wt">Worktrees</a><a href="#br">Branches</a><a href="#cl">Clones</a><a href="#pl">Plugins</a><a href="#hy">Hygiene</a></nav>',
        '<div class="metrics">' + "".join(f'<div class="m"><b>{esc(v)}</b><span>{esc(k)}</span></div>' for v, k in metrics) + "</div>",
    ]
    wrows = []
    for w in snap["worktrees"]:
        dirty = w["dirty"]
        d = tag("?", "mut") if dirty is None else tag(str(dirty), "warn") if dirty else tag("0", "ok")
        place = {"ok": tag("expected", "ok"), "harness": tag("harness root", "mut"),
                 "outside": tag("outside", "warn"), "missing": tag("missing", "bad")}.get(w["place"], esc(w["place"]))
        wrows.append([f'<code>{esc(w["path"])}</code>', esc(w["kind"]), f'<code>{esc(w["branch"])}</code>',
                      f'<code>{esc(w["head"])}</code>', main_tag(w["in_main"]), d,
                      "" if w["age_days"] is None else f'{w["age_days"]}d', esc(w["owner"]), place])
    out += ['<h2 id="wt">Worktrees and clone folders</h2>',
            table(["Path", "Kind", "Branch", "Head", "Main", "Uncommitted", "Age", "Owner", "Place"], wrows, {5, 6})]
    brows = [[tag("local", "mut"), f'<code>{esc(b["name"])}</code>', f'<code>{esc(b["head"])}</code>',
              main_tag(b["in_main"]), esc(b["owner"]), f'<code>{esc(b["clone"])}</code>'] for b in snap["local_branches"]]
    for b in snap["remote_branches"]:
        note = "on Origin and GitHub" if b["on_both"] else ("Origin only" if b["where"] == "origin" else "GitHub only")
        brows.append([tag(b["where"].capitalize() if b["where"] == "origin" else "GitHub", "mut"),
                      f'<code>{esc(b["name"])}</code>', f'<code>{esc(b["head"])}</code>',
                      main_tag(b["in_main"]), esc(b["owner"]), esc(note)])
    out += ['<h2 id="br">Branches: local, Origin, GitHub</h2>',
            table(["Where", "Branch", "Head", "Main", "Owner", "Clone / note"], brows)]
    crows = [[f'<code>{esc(cl["path"])}</code>', tag("canonical", "ok") if cl["canonical"] else tag("extra", "warn"),
              str(cl["worktrees"]), "<br>".join(f"<code>{esc(u)}</code>" for u in cl["remotes"])] for cl in snap["clones"]]
    out += ['<h2 id="cl">Clones</h2>', table(["Path", "Role", "Worktrees", "Remotes"], crows, {2})]
    harnesses = list(HARNESS_GLOBS)
    prows = []
    for p in snap["plugins"]:
        cells = [f'<code>{esc(p["plugin"])}</code>', esc(p["main"] or "not on main")]
        for h in harnesses:
            vs = p["installed"].get(h) or []
            if not vs:
                cells.append(tag("not installed", "mut"))
            elif p["main"] and vs == [p["main"]]:
                cells.append(tag(vs[0], "ok"))
            else:
                cells.append(tag(", ".join(vs), "warn"))
        prows.append(cells)
    out += ['<h2 id="pl">Plugins: Origin main vs installed per harness</h2>',
            table(["Plugin", "Origin main"] + harnesses, prows),
            '<p class="mut">Green: only the main version is installed. Amber: a different or extra cached version.</p>']
    order = {"FAIL": 0, "WARN": 1, "UNKNOWN": 2, "PASS": 3}
    cls = {"FAIL": "bad", "WARN": "warn", "UNKNOWN": "mut", "PASS": "ok"}
    hrows = []
    for r in sorted(rows, key=lambda r: (order.get(r.get("status"), 9), r.get("check", ""))):
        items = r.get("items") or []
        detail = esc(r.get("detail", ""))
        if items:
            detail += "<ul>" + "".join(f"<li><code>{esc(i)}</code></li>" for i in items) + "</ul>"
        hrows.append([tag(r.get("status", ""), cls.get(r.get("status"), "mut")), f'<code>{esc(r.get("check", ""))}</code>', detail])
    out += [f'<h2 id="hy">Hygiene check: {c["PASS"]} pass, {c["WARN"]} warn, {c["FAIL"]} fail, {c["UNKNOWN"]} unknown</h2>',
            table(["Status", "Check", "Detail"], hrows), "</main></body></html>\n"]
    return "\n".join(out)


# ---------------------------------------------------------------- publish

def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def publish(m: dict, rows: list[dict], out: Path | None = None, home: Path | None = None,
            now: datetime | None = None) -> str:
    """Write the map if the snapshot changed. Returns 'written' or 'unchanged'."""
    home = expand(home or Path.home())
    out = Path(out) if out else home / ".agents" / "hygiene" / "map.html"
    hash_path = out.with_name(out.stem + ".sha256")
    snap = snapshot(m, rows, home)
    digest = snapshot_hash(snap)
    try:
        old = hash_path.read_text().strip()
    except OSError:
        old = ""
    if old == digest and out.is_file():
        return "unchanged"
    atomic_write(out, render_html(snap, rows, now or datetime.now(IST)))
    atomic_write(hash_path, digest + "\n")
    return "written"


def publish_safe(m: dict, rows: list[dict], out: Path | None = None, home: Path | None = None) -> str | None:
    """publish() that never raises; one stderr line on failure."""
    try:
        result = publish(m, rows, out=out, home=home)
    except Exception as exc:  # fail open: the hygiene result must not change
        msg = str(exc).splitlines()[0] if str(exc) else ""
        print(f"hygiene: map not updated: {type(exc).__name__}: {msg}"[:300], file=sys.stderr)
        return None
    if result == "written":
        print(f"hygiene: map updated: {out or '~/.agents/hygiene/map.html'}", file=sys.stderr)
    return result


def main(argv: list[str]) -> int:
    import tomllib
    ap = argparse.ArgumentParser(description="Write the agent-plugins map from `hygiene check --json` rows on stdin.")
    ap.add_argument("--manifest", default="~/.agents/machine.toml")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    with open(expand(args.manifest), "rb") as fh:
        m = tomllib.load(fh)
    rows = json.load(sys.stdin)
    return 0 if publish_safe(m, rows, out=Path(expand(args.out)) if args.out else None) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
