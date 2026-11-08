#!/usr/bin/env python3
"""Interactive HTML hygiene map of this Mac, refreshed after `hygiene check`.

The page is a collapsible tree rooted at the Mac Mini: every clone under the
manifest scan roots grouped by repo, each with its worktrees, local branches and
extra clones, plus a "Machine checks" branch for findings that are not about a
clone. Nodes are coloured by status (fail / warn / cleanup-candidate / ok) from
the `hygiene check` rows plus git state; clicking one opens a side panel with
path, branch, merged, dirty count, local-only commits, age, owner and the
plain-English reason for every failing or warning check. All CSS, JS and data
are inline; the page makes no network requests.

`hygiene check` (scripts/hygiene.py, every mode) refreshes the map after each
run (skip with `--no-map`); `hygiene map` forces a rewrite and opens it. It
computes git_snapshot() on a thread while the checks run, then builds a normalized snapshot (clones, worktrees,
local / Origin / GitHub branches, installed plugin versions per harness versus
Origin main, hygiene status per check), hashes it, and rewrites
`~/.agents/hygiene/map.html` only when the hash changed. The write is atomic
(temp file in the same directory, then rename) and the page is self-contained:
no network, no external assets; data and script are inline.

Branch lists come from cached remote-tracking refs. Bounded read-only remote
and PR observations are separate; an unavailable observation is unknown.

Fail open: any error logs one line to stderr and leaves the hygiene result alone.
Installed copy: `~/.agents/bin/hygiene_map.py` next to `hygiene.py` (source:
agent-plugins `scripts/`, installed by `scripts/install-hygiene.sh`). The stdin
mode still works: `hygiene check --json --no-map | python3 hygiene_map.py`.
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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = "agent-plugins"
DEFAULT_OUT = Path("~/.agents/hygiene/map.html")
IST = timezone(timedelta(hours=5, minutes=30), "IST")
GIT_TIMEOUT = 10
# Read-only scan: --no-optional-locks keeps `git status` from taking index.lock,
# so a running agent's git never trips over the map.
GIT = ("git", "--no-optional-locks")
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
        proc = subprocess.run([*GIT, "-C", str(path), *args], capture_output=True,
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


def observe_remotes(repo: Path, origin: str | None, github: str | None) -> dict:
    """Observe live heads and active PRs without changing Git refs."""
    def heads(remote):
        if not remote:
            return None
        out = git(repo, 'ls-remote', '--heads', remote)
        if out is None:
            return None
        rows = {}
        for line in out.splitlines():
            sha, sep, ref = line.partition('\t')
            if not sep or len(sha) != 40 or not ref.startswith('refs/heads/'):
                return None
            rows[ref.removeprefix('refs/heads/')] = sha
        return rows

    prs = None
    try:
        proc = subprocess.run(['origin', 'pr', 'list', '--state', 'all', '--limit', '200',
                               '--json', 'number,status,headRef,headSha,baseRef'], cwd=repo,
                              capture_output=True, text=True, timeout=10, check=True)
        entries = json.loads(proc.stdout)
        if isinstance(entries, list) and len(entries) < 200 and all(
            isinstance(p, dict) and isinstance(p.get('headRef'), str) and
            p.get('status') in ('open', 'draft', 'merged', 'closed') and
            isinstance(p.get('number'), int) and isinstance(p.get('headSha'), str) and
            len(p['headSha']) == 40 and isinstance(p.get('baseRef'), str) for p in entries
        ):
            prs = {}
            for p in entries:
                if p['status'] in ('open', 'draft'):
                    for ref in (p['headRef'], p['baseRef']):
                        prs[ref] = {'number': p['number'], 'status': p['status'], 'head': p['headSha']}
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return {'origin': heads(origin), 'github': heads(github), 'active_prs': prs,
            'observed_at': datetime.now(timezone.utc).isoformat()}


def merged(repo: Path, sha: str, main_sha: str | None) -> bool | None:
    if not main_sha:
        return None
    try:
        proc = subprocess.run([*GIT, "-C", str(repo), "merge-base", "--is-ancestor", sha, main_sha],
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


# ---------------------------------------------------------------- machine tree

MAIN_NAMES = ("main", "master")
REMOTE_PREF = ("origin-cursor", "origin")
MAX_BRANCH_PROBES = 200  # cap per clone on `rev-list --count` calls
SCAN_WORKERS = 8


def repo_name(clone: Path, urls: dict[str, str]) -> str:
    for name in (*REMOTE_PREF, *sorted(urls)):
        if name in urls:
            return norm_remote(urls[name]).rsplit("/", 1)[-1].rsplit(":", 1)[-1] or clone.name
    return clone.name


def main_ref(clone: Path) -> str | None:
    """The ref 'merged' is measured against: a remote main/master, else a local one."""
    remote = refs(clone, "refs/remotes")
    names = sorted({k.split("/", 1)[0] for k in remote})
    for r in [x for x in REMOTE_PREF if x in names] + [x for x in names if x not in REMOTE_PREF]:
        for b in MAIN_NAMES:
            if f"{r}/{b}" in remote:
                return f"refs/remotes/{r}/{b}"
    local = refs(clone, "refs/heads")
    for b in MAIN_NAMES:
        if b in local:
            return f"refs/heads/{b}"
    return None


def local_only(clone: Path, sha: str, has_remotes: bool) -> int | None:
    """Commits reachable from sha that no remote-tracking ref has (as of last fetch)."""
    if not has_remotes:
        return None
    out = git(clone, "rev-list", "--count", sha, "--not", "--remotes")
    try:
        return int((out or "").strip())
    except ValueError:
        return None


def worktree_entries(clone: Path) -> list[dict]:
    out = git(clone, "worktree", "list", "--porcelain") or ""
    entries, cur = [], {}
    for line in out.splitlines() + [""]:
        if not line.strip():
            if cur:
                entries.append(cur)
            cur = {}
            continue
        key, _, val = line.partition(" ")
        cur[key] = val or True
    return [e for e in entries if not e.get("bare")]


def scan_clone(clone: Path, m: dict, home: Path, now: float) -> dict:
    urls = remotes(clone)
    has_remotes = bool(urls)
    mref = main_ref(clone)
    merged_set = set()
    if mref:
        out = git(clone, "for-each-ref", "--format=%(refname:short)", "--merged", mref, "refs/heads") or ""
        merged_set = {x.strip() for x in out.splitlines() if x.strip()}
    checked_out: dict[str, str] = {}
    worktrees = []
    for e in worktree_entries(clone):
        wpath = Path(str(e.get("worktree", "")))
        branch = str(e.get("branch", "")).removeprefix("refs/heads/") if e.get("branch") else ""
        sha = str(e.get("HEAD", ""))
        exists = wpath.is_dir()
        if branch:
            checked_out[branch] = tilde(wpath, home)
        in_main = None
        if mref and sha:
            in_main = (branch in merged_set) if branch else merged(clone, sha, mref)
        worktrees.append({
            "path": tilde(wpath, home),
            "kind": "clone" if exists and wpath.resolve() == clone else "worktree",
            "branch": branch or "(detached)",
            "head": sha[:7],
            "dirty": dirty_count(wpath) if exists else None,
            "local_only": local_only(clone, sha, has_remotes) if sha and in_main is not True else (0 if in_main else None),
            "age_days": age_days(wpath),
            "owner": owner_of(str(wpath), branch),
            "place": placement(wpath.resolve(), m) if exists else "missing",
            "in_main": in_main,
        })
    fmt = "%(refname:short)%09%(objectname)%09%(upstream:short)%09%(committerdate:unix)"
    out = git(clone, "for-each-ref", f"--format={fmt}", "refs/heads") or ""
    branches, probes = [], 0
    for line in sorted(out.splitlines()):
        parts = line.split("\t")
        if len(parts) != 4 or not parts[0]:
            continue
        name, sha, upstream, when = parts
        in_main = (name in merged_set) if mref else None
        lo: int | None = 0 if in_main else None
        if not in_main and probes < MAX_BRANCH_PROBES:
            probes += 1
            lo = local_only(clone, sha, has_remotes)
        try:
            age = max(0, int((now - int(when)) // 86400))
        except ValueError:
            age = None
        branches.append({"name": name, "head": sha[:7], "upstream": upstream or None, "in_main": in_main,
                         "local_only": lo, "age_days": age, "owner": owner_of("", name),
                         "checked_out": checked_out.get(name)})
    return {
        "path": tilde(clone, home),
        "repo": repo_name(clone, urls),
        "remotes": sorted(norm_remote(u) for u in set(urls.values())),
        "main_ref": mref.removeprefix("refs/remotes/").removeprefix("refs/heads/") if mref else None,
        "place": placement(clone, m),
        "worktrees": sorted(worktrees, key=lambda w: (w["kind"] != "clone", w["path"])),
        "branches": branches,
    }


def machine_snapshot(m: dict, home: Path, now: float | None = None) -> list[dict]:
    """Every clone under the scan roots (any repo), with worktrees and local branches."""
    now = time.time() if now is None else now
    scan = m.get("scan", {})
    p = m.get("paths", {})
    roots = [expand(x) for x in scan.get("roots", [])]
    if not roots and "checkout_root" in p:
        roots = [expand(p["checkout_root"])]
    clones = walk_clones(roots, int(scan.get("depth", 4)))
    if len(clones) < 2:
        return [scan_clone(c, m, home, now) for c in clones]
    # git is I/O bound; scanning clones side by side keeps `hygiene check` fast.
    with ThreadPoolExecutor(max_workers=min(SCAN_WORKERS, len(clones))) as pool:
        return list(pool.map(lambda c: scan_clone(c, m, home, now), clones))


# ---------------------------------------------------------------- snapshot

def git_snapshot(m: dict, home: Path | None = None) -> dict:
    """The git and filesystem half of the snapshot; needs no hygiene rows.

    `hygiene check` computes this on a thread while its own checks run, then
    hands it to publish(base=...).
    """
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
    live = observe_remotes(canonical, o_name, g_name) if canonical.is_dir() else {
        'origin': None, 'github': None, 'active_prs': None, 'observed_at': None}
    live['cached_main_matches'] = bool(main_sha and live['origin'] is not None and
                                       live['origin'].get('main') == main_sha)

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

    origin_b = live['origin'] if live['origin'] is not None else (refs(canonical, f"refs/remotes/{o_name}") if o_name else {})
    github_b = live['github'] if live['github'] is not None else (refs(canonical, f"refs/remotes/{g_name}") if g_name else {})
    remote_rows = []
    for where, table, other in (("origin", origin_b, github_b), ("github", github_b, origin_b)):
        for name, sha in table.items():
            remote_rows.append({"where": where, "name": name, "head": sha[:7], 'live': live[where] is not None,
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

    return {
        "repo": REPO,
        "canonical": tilde(canonical, home),
        "origin_main": main_sha[:7] if main_sha else None,
        "remote_observation": live,
        "clones": clone_rows,
        "worktrees": sorted(wt_rows, key=lambda r: (r["clone"], r["kind"] != "clone", r["path"])),
        "local_branches": sorted(local_rows, key=lambda r: (r["clone"], r["name"])),
        "remote_branches": sorted(remote_rows, key=lambda r: (r["where"], r["name"])),
        "plugins": plugin_rows,
        "machine": machine_snapshot(m, home),
    }


def snapshot(m: dict, rows: list[dict], home: Path | None = None, base: dict | None = None) -> dict:
    """Normalized, sorted, timestamp-free view: git state plus hygiene status."""
    home = expand(home or Path.home())
    counts = {s: sum(1 for r in rows if r.get("status") == s) for s in STATUSES}
    return {
        **(base if base is not None else git_snapshot(m, home)),
        "hygiene": {
            "counts": counts,
            # Details carry sizes and free space, which change every run; only
            # the per-check status goes into the hash.
            "checks": sorted([[r.get("check", ""), r.get("status", "")] for r in rows]),
            # Which path or branch each check flags (counts and sizes stripped).
            "items": hygiene_items(rows, home),
        },
    }


def snapshot_hash(snap: dict) -> str:
    blob = json.dumps(snap, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode()).hexdigest()


# ---------------------------------------------------------------- tree model

# Plain-English meaning of each `hygiene check` row, shown in the side panel.
CHECK_TEXT = {
    "home-not-a-repo": "Your home folder itself must not be a git repo.",
    "canonical-roots": "The folders the manifest names (checkouts, archive, scratch) exist.",
    "worktree-root": "The managed worktree folder exists.",
    "duplicate-clones": "The same repo is cloned more than once; extra clones drift and confuse agents.",
    "checkouts-outside-root": "This clone lives outside the checkout root. Move it there or list it as a manifest exception.",
    "worktree-placement": "This worktree sits outside the managed worktree root, so cleanup tools and other agents will not find it.",
    "worktree-age": "This worktree is older than the warning window; finish it, merge it or sweep it.",
    "worktree-count": "This repo has more than three linked worktrees open at once.",
    "local-only-work": "Commits or files here exist only on this Mac (not on any remote, no verified bundle). Losing the folder loses the work.",
    "skill-duplicates": "The same skill is installed in more than one harness root.",
    "skill-link": "Harness skill folders link to the canonical skill copy.",
    "cursor-rule": "The Cursor rule file matches the canonical one.",
    "instruction-block": "The shared instruction block in agent config files matches the manifest.",
    "harness-state": "A harness state folder is larger than its budget.",
    "disk-floor": "Free disk space is above the manifest floor.",
    "home-scratch": "Agent-looking leftovers sit directly in your home folder.",
    "prune-ready": "Past its retention window; `hygiene prune` can sweep it.",
    # Computed by this page from git state, not by `hygiene check`:
    "uncommitted-changes": "Files are modified or untracked and not committed.",
    "local-only-commits": "Commits on this branch are not on any remote (as of the last fetch).",
    "outside-root": "Not under the checkout or worktree root from the manifest.",
    "missing-folder": "git still lists this worktree but its folder is gone; `git worktree prune` cleans it up.",
    "stale-candidate": "Merged into observed main with no active PR or visible changes; recheck before cleanup.",
    "not-merged": "Not merged into main yet.",
}
RANK = {"fail": 4, "warn": 3, "stale": 2, "ok": 1}
ROW_LEVEL = {"FAIL": "fail", "WARN": "warn", "PASS": "ok", "UNKNOWN": "warn"}
STALE_CHECKS = {"prune-ready"}


def item_key(item: str, home: Path) -> str:
    """'/abs/path: 9 dirty file(s)…' or '/abs/path (53d)' -> '~/abs/path'; keeps 'path:branch'."""
    s = str(item).strip()
    if s.endswith(")") and " (" in s:
        s = s.rsplit(" (", 1)[0]
    s = s.split(": ", 1)[0]
    return tilde(s, home)


def hygiene_items(rows: list[dict], home: Path) -> list[list[str]]:
    keys = set()
    for r in rows:
        for it in r.get("items") or []:
            keys.add((str(r.get("check", "")), str(r.get("status", "")), item_key(it, home)))
    return sorted(list(k) for k in keys)


def worst(levels) -> str:
    return max(levels, key=lambda s: RANK.get(s, 0), default="ok")


def free_disk(rows: list[dict]) -> str | None:
    import re
    for r in rows:
        hit = re.search(r"([\d.]+\s*[KMGT]i?B?)\s+free", str(r.get("detail", "")))
        if hit:
            return hit.group(1)
    return None


def build_tree(snap: dict, rows: list[dict], home: Path, host: str) -> dict:
    """Mac Mini -> repos -> clone / worktrees / branches / extra clones, plus machine checks."""
    notes: dict[str, list[dict]] = {}
    for r in rows:
        for it in r.get("items") or []:
            notes.setdefault(item_key(it, home), []).append({
                "check": r.get("check", ""), "status": r.get("status", ""), "item": str(it)})
    used: set[str] = set()
    seq = iter(range(1, 1_000_000))
    live = snap.get('remote_observation') or {}

    def pr_note(branch: str, clone_path: str) -> str:
        if clone_path != snap.get('canonical') or live.get('active_prs') is None:
            return 'PR state unknown'
        pr = live['active_prs'].get(branch)
        return f"{pr['status']} PR #{pr['number']}" if pr else 'No open or draft PR observed'

    def can_retire(branch: str, clone_path: str) -> bool:
        return (clone_path == snap.get('canonical') and live.get('cached_main_matches') is True
                and isinstance(live.get('active_prs'), dict) and branch not in live['active_prs'])

    def node(kind: str, label: str, **kw) -> dict:
        n = {"id": f"n{next(seq)}", "kind": kind, "label": label, "children": [], "reasons": [], **kw}
        return n

    def attach(n: dict, key: str) -> None:
        for note in notes.get(key, []):
            used.add(key)
            st = note["status"]
            lvl = "stale" if note["check"] in STALE_CHECKS else ROW_LEVEL.get(st, "warn")
            n["reasons"].append({"check": note["check"], "level": lvl,
                                 "text": CHECK_TEXT.get(note["check"], ""), "item": note["item"]})

    def add(n: dict, check: str, level: str, item: str = "") -> None:
        n["reasons"].append({"check": check, "level": level, "text": CHECK_TEXT.get(check, ""), "item": item})

    def settle(n: dict, stale_ok: bool = False) -> dict:
        levels = [x["level"] for x in n["reasons"]]
        st = worst(levels) if levels else "ok"
        if stale_ok and st in ("ok", "warn") and not any(x["level"] == "fail" for x in n["reasons"]):
            add(n, "stale-candidate", "stale")
            st = "stale"
        n["status"] = st
        return n

    def wt_node(w: dict, clone_path: str) -> dict:
        n = node("worktree" if w["kind"] == "worktree" else "checkout",
                 w["path"].rsplit("/", 1)[-1] if w["kind"] == "worktree" else "main checkout",
                 path=w["path"], branch=w["branch"], head=w["head"], merged=w["in_main"], dirty=w["dirty"],
                 local_only=w["local_only"], age_days=w["age_days"], owner=w["owner"], place=w["place"],
                 note=pr_note(w['branch'], clone_path))
        attach(n, w["path"])
        if w['kind'] == 'worktree' and w['in_main'] is True and not can_retire(w['branch'], clone_path):
            n['reasons'].append({'check': 'cleanup-evidence', 'level': 'warn',
                                 'text': 'Open PR or live cleanup evidence unavailable; verify before removal.', 'item': ''})
        if w["dirty"]:
            add(n, "uncommitted-changes", "fail", f'{w["dirty"]} file(s)')
        if w["local_only"]:
            add(n, "local-only-commits", "fail", f'{w["local_only"]} commit(s)')
        if w["place"] == "missing":
            add(n, "missing-folder", "warn")
        elif w["place"] == "outside" and w["kind"] == "worktree" and not any(
                x["check"] == "worktree-placement" for x in n["reasons"]):
            add(n, "outside-root", "warn")
        stale = (w["kind"] == "worktree" and w["in_main"] is True and w["dirty"] == 0
                 and w["local_only"] == 0 and can_retire(w['branch'], clone_path))
        return settle(n, stale_ok=stale)

    def br_node(b: dict, clone: dict) -> dict:
        n = node("branch", b["name"], path=clone["path"], branch=b["name"], head=b["head"], merged=b["in_main"],
                 local_only=b["local_only"], age_days=b["age_days"], owner=b["owner"],
                 upstream=b["upstream"], checked_out=b["checked_out"], note=pr_note(b['name'], clone['path']))
        attach(n, f'{clone["path"]}:{b["name"]}')
        if b["local_only"]:
            add(n, "local-only-commits", "fail", f'{b["local_only"]} commit(s)')
        is_main = b["name"] in MAIN_NAMES or (clone["main_ref"] or "").endswith("/" + b["name"])
        if b['in_main'] is True and not is_main and not can_retire(b['name'], clone['path']):
            n['reasons'].append({'check': 'cleanup-evidence', 'level': 'warn',
                                 'text': 'Open PR or live cleanup evidence unavailable; verify before removal.', 'item': ''})
        stale = (b["in_main"] is True and not b["checked_out"] and not is_main
                 and b["local_only"] == 0 and can_retire(b['name'], clone['path']))
        return settle(n, stale_ok=stale)

    def clone_node(c: dict, label: str, primary: bool) -> dict:
        own = next((w for w in c["worktrees"] if w["kind"] == "clone"), None)
        n = node("clone", label, path=c["path"], remotes=c["remotes"], main_ref=c["main_ref"],
                 place=c["place"], role="primary clone" if primary else "extra clone",
                 branch=own["branch"] if own else None, dirty=own["dirty"] if own else None,
                 local_only=own["local_only"] if own else None, age_days=own["age_days"] if own else None,
                 owner=own["owner"] if own else None, merged=own["in_main"] if own else None)
        attach(n, c["path"])
        if own and own["dirty"]:
            add(n, "uncommitted-changes", "fail", f'{own["dirty"]} file(s)')
        if c["place"] == "outside" and not any(x["check"] == "checkouts-outside-root" for x in n["reasons"]):
            add(n, "outside-root", "warn")
        wts = [wt_node(w, c["path"]) for w in c["worktrees"] if w["kind"] == "worktree"]
        if wts:
            g = node("group", f"Worktrees ({len(wts)})")
            g["children"] = wts
            n["children"].append(settle(g))
        brs = [br_node(b, c) for b in c["branches"]]
        if brs:
            g = node("group", f"Branches ({len(brs)})")
            g["children"] = brs
            n["children"].append(settle(g))
        return settle(n)

    root = node("machine", f"Mac Mini ({host})" if host else "Mac Mini", path="~")
    by_repo: dict[str, list[dict]] = {}
    for c in snap.get("machine", []):
        by_repo.setdefault(c["repo"], []).append(c)
    for name in sorted(by_repo, key=str.lower):
        cl = by_repo[name]
        primary = min(cl, key=lambda c: (c["place"] != "ok", c["path"].count("/"), c["path"]))
        rn = clone_node(primary, name, True)
        rn["kind"] = "repo"
        extras = [c for c in cl if c is not primary]
        if extras:
            g = node("group", f"Extra clones ({len(extras)})")
            g["children"] = [clone_node(c, c["path"], False) for c in extras]
            rn["children"].append(settle(g))
        if name == REPO and snap.get("plugins"):
            g = node("group", f'Plugins vs Origin main ({len(snap["plugins"])})')
            for pl in snap["plugins"]:
                inst = {h: v for h, v in pl["installed"].items() if v}
                pn = node("plugin", pl["plugin"], main_version=pl["main"], installed=inst)
                for h, vs in inst.items():
                    if not pl["main"] or vs != [pl["main"]]:
                        pn["reasons"].append({"check": "plugin-version", "level": "warn",
                                              "text": "Installed version differs from Origin main.",
                                              "item": f'{h}: {", ".join(vs)} (main {pl["main"] or "none"})'})
                g["children"].append(settle(pn))
            rn["children"].append(settle(g))
        if name == REPO and snap.get("remote_branches"):
            g = node("group", f'Remote branches ({len(snap["remote_branches"])})')
            for b in snap["remote_branches"]:
                where = "Origin" if b["where"] == "origin" else "GitHub"
                origin_live, github_live = live.get('origin'), live.get('github')
                if origin_live is None or github_live is None:
                    mirror = 'Mirror status unknown'
                else:
                    a, ghead = origin_live.get(b['name']), github_live.get(b['name'])
                    mirror = 'Origin and GitHub match' if a and a == ghead else 'Origin and GitHub differ or branch missing'
                bn = node("remote-branch", f'{where}: {b["name"]}', branch=b["name"], head=b["head"],
                          merged=b["in_main"], owner=b["owner"],
                          note=f"{'Live observation' if b.get('live') else 'Cached ref'}; {mirror}; {pr_note(b['name'], snap.get('canonical'))}")
                g["children"].append(settle(bn))
            rn["children"].append(settle(g))
        root["children"].append(settle(rn))

    checks = node("group", "Machine checks")
    for r in sorted(rows, key=lambda r: (-RANK.get(ROW_LEVEL.get(r.get("status"), "ok"), 0), r.get("check", ""))):
        lvl = ROW_LEVEL.get(r.get("status"), "warn")
        cn = node("check", r.get("check", ""), detail=r.get("detail", ""), hygiene=r.get("status", ""))
        cn["reasons"].append({"check": r.get("check", ""), "level": lvl,
                              "text": CHECK_TEXT.get(r.get("check", ""), ""), "item": r.get("detail", "")})
        for it in r.get("items") or []:
            k = item_key(it, home)
            leaf = node("item", k, path=k, note="also shown on its tree node" if k in used else "")
            ilvl = "stale" if r.get("check") in STALE_CHECKS else lvl
            leaf["reasons"].append({"check": r.get("check", ""), "level": ilvl,
                                    "text": CHECK_TEXT.get(r.get("check", ""), ""), "item": str(it)})
            cn["children"].append(settle(leaf))
        settle(cn)
        if r.get("check") in STALE_CHECKS and cn["status"] == "warn":
            cn["status"] = "stale"
        checks["children"].append(cn)
    root["children"].append(settle(checks))
    return settle(root)


# ---------------------------------------------------------------- html

CSS = """
:root{--bg:#f7f7f9;--panel:#fff;--fg:#1d1d1f;--mut:#6e6e73;--line:#e3e3e8;--hov:#eef2f8;--sel:#dde8fb;
--ok:#1a7f37;--warn:#b26b00;--fail:#cf222e;--stale:#8c8c94;--acc:#0969da}
@media (prefers-color-scheme:dark){:root{--bg:#111214;--panel:#1a1b1f;--fg:#ececf1;--mut:#9a9aa3;--line:#2c2d33;
--hov:#22252c;--sel:#1f3251;--ok:#3fb950;--warn:#d29922;--fail:#f85149;--stale:#8b8b93;--acc:#58a6ff}}
*{box-sizing:border-box}html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;display:flex;flex-direction:column}
header{background:var(--panel);border-bottom:1px solid var(--line);padding:12px 18px;display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center}
h1{font-size:17px;margin:0}.sub{color:var(--mut);font-size:12px}
.stat{display:flex;gap:6px;align-items:center;font-weight:600}.stat span{font-weight:400;color:var(--mut);font-size:12px}.stat span.pill{color:#fff;font-weight:700;font-size:13px}
.pill{border-radius:999px;padding:1px 9px;font-size:12px;font-weight:600;color:#fff}
.p-ok{background:var(--ok)}.p-warn{background:var(--warn)}.p-fail{background:var(--fail)}.p-stale{background:var(--stale)}
.tools{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:8px 18px;border-bottom:1px solid var(--line);background:var(--panel)}
.tools input[type=search]{min-width:260px;padding:5px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg)}
button,.chip{font:inherit;font-size:12px;border:1px solid var(--line);background:var(--bg);color:var(--fg);border-radius:8px;padding:4px 10px;cursor:pointer}
.chip{display:inline-flex;gap:6px;align-items:center;user-select:none}.chip.off{opacity:.35;text-decoration:line-through}
label.cb{display:inline-flex;gap:5px;align-items:center;font-size:13px;cursor:pointer}
.wrap{flex:1;display:flex;min-height:0}
#tree{flex:1;overflow:auto;padding:10px 8px 40px 10px}
#side{width:420px;max-width:45vw;border-left:1px solid var(--line);background:var(--panel);overflow:auto;padding:16px 18px}
ul.t{list-style:none;margin:0;padding-left:18px;border-left:1px dashed var(--line);margin-left:9px}#tree>ul.t{border:0;padding-left:0;margin-left:0}
.row{display:flex;align-items:center;gap:7px;padding:3px 6px;border-radius:6px;cursor:pointer;white-space:nowrap}
.row:hover{background:var(--hov)}.row.sel{background:var(--sel)}.row:focus{outline:2px solid var(--acc);outline-offset:-2px}
.car{width:14px;color:var(--mut);font-size:10px;text-align:center;flex:none}
.dot{width:11px;height:11px;border-radius:50%;flex:none}
.d-ok{background:var(--ok)}.d-warn{background:var(--warn)}.d-fail{background:var(--fail)}.d-stale{background:var(--stale)}
.lbl{font-weight:500}.k-machine>.row .lbl{font-size:16px;font-weight:700}.k-repo>.row .lbl{font-weight:650}
.k-group>.row .lbl{color:var(--mut);font-weight:600}
.kind{font-size:11px;color:var(--mut);border:1px solid var(--line);border-radius:5px;padding:0 5px}
.meta{font-size:12px;color:var(--mut);overflow:hidden;text-overflow:ellipsis}
.cnt{font-size:11px;font-weight:600;border-radius:5px;padding:0 5px;color:#fff}
.hide{display:none}mark{background:#ffe58a;color:#000;border-radius:2px}
#side h2{font-size:16px;margin:0 0 2px;word-break:break-all}#side .st{margin:6px 0 12px}
dl{display:grid;grid-template-columns:120px 1fr;gap:5px 10px;margin:0 0 14px;font-size:13px}dt{color:var(--mut)}dd{margin:0;word-break:break-all}
code{font:12px ui-monospace,SFMono-Regular,Menlo,monospace}
.why{border:1px solid var(--line);border-left:4px solid var(--mut);border-radius:8px;padding:8px 10px;margin:8px 0;font-size:13px}
.why.l-fail{border-left-color:var(--fail)}.why.l-warn{border-left-color:var(--warn)}.why.l-stale{border-left-color:var(--stale)}.why.l-ok{border-left-color:var(--ok)}
.why b{font-family:ui-monospace,Menlo,monospace;font-size:12px}.why div{color:var(--mut);margin-top:3px;word-break:break-word}
.legend{display:flex;gap:12px;flex-wrap:wrap;font-size:12px;color:var(--mut)}.legend i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:4px;vertical-align:-1px}
details.all{margin-top:18px;font-size:13px}details.all table{border-collapse:collapse;width:100%}details.all td{border-top:1px solid var(--line);padding:4px 6px;vertical-align:top}
.hint{color:var(--mut);font-size:13px}
"""

JS = r"""
(function(){
var D=JSON.parse(document.getElementById('hm-data').textContent);
var RANK={fail:4,warn:3,stale:2,ok:1};
var KIND={machine:'Mac',repo:'repo',clone:'clone',checkout:'checkout',worktree:'worktree',branch:'branch',
'remote-branch':'remote',plugin:'plugin',group:'',check:'check',item:'path'};
var byId={},parent={},open={},sel=null,hidden={};
function walk(n,p){byId[n.id]=n;if(p)parent[n.id]=p.id;n.children.forEach(function(c){walk(c,n)});
 var w=n.status,cnt={fail:0,warn:0,stale:0,ok:0};
 n.children.forEach(function(c){if(RANK[c.roll]>RANK[w])w=c.roll;for(var k in cnt)cnt[k]+=c.cnt[k]+(c.status===k&&c.kind!=='group'?1:0)});
 n.roll=w;n.cnt=cnt;}
walk(D.tree,null);
open[D.tree.id]=true;
D.tree.children.forEach(function(c){if(c.kind==='group')return;});
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
function shown(n){return n.kind==='group'||n.kind==='machine'||n.kind==='repo'?n.roll:n.status}
function meta(n){var a=[];
 if(n.kind==='worktree'||n.kind==='checkout'||n.kind==='clone'||n.kind==='repo'){if(n.branch)a.push(n.branch);}
 if(n.kind==='clone'&&n.role==='extra clone'){}
 if(n.dirty)a.push(n.dirty+' dirty');if(n.local_only)a.push(n.local_only+' local-only');
 if(n.merged===true&&n.kind!=='repo'&&n.kind!=='clone')a.push('merged');
 if(n.age_days!=null&&n.kind!=='repo'&&n.kind!=='clone')a.push(n.age_days+'d');
 if(n.kind==='check'&&n.detail)a.push(n.detail);
 if(n.kind==='plugin')a.push('main '+(n.main_version||'none'));
 if(n.note&&n.kind==='remote-branch')a.push(n.note);
 return a.join(' · ');}
var q='',onlyProb=false;
function matches(n){if(!q)return true;var s=(n.label+' '+(n.path||'')+' '+(n.branch||'')+' '+(n.owner||'')).toLowerCase();return s.indexOf(q)>=0}
var vis={};
function computeVis(n,inh){var m=inh||matches(n),any=false;n.children.forEach(function(c){if(computeVis(c,m&&!!q))any=true});
 var self=m&&!hidden[n.status]&&(!onlyProb||n.status==='fail'||n.status==='warn');
 if(n.kind==='group'||n.kind==='machine')self=self&&!q&&!onlyProb&&!Object.keys(hidden).some(function(k){return hidden[k]});
 vis[n.id]=self||any||n.kind==='machine';return vis[n.id];}
function hl(s){s=esc(s);if(!q)return s;var i=s.toLowerCase().indexOf(esc(q));return i<0?s:s.slice(0,i)+'<mark>'+s.slice(i,i+q.length)+'</mark>'+s.slice(i+q.length)}
function render(){computeVis(D.tree);var filt=!!q||onlyProb||Object.keys(hidden).some(function(k){return hidden[k]});
 var h=[];(function r(n){if(!vis[n.id])return;var kids=n.children.filter(function(c){return vis[c.id]});
  var isOpen=filt?true:!!open[n.id];
  if(filt&&!kids.length)isOpen=false;
  var c=n.cnt,badges='';
  if(n.children.length){['fail','warn','stale'].forEach(function(k){if(c[k])badges+='<span class="cnt p-'+k+'" title="'+c[k]+' '+k+' below">'+c[k]+'</span>'})}
  h.push('<li class="k-'+n.kind+'"><div class="row'+(sel===n.id?' sel':'')+'" tabindex="0" data-id="'+n.id+'">'+
   '<span class="car">'+(n.children.length?(isOpen?'▼':'▶'):'')+'</span>'+
   '<span class="dot d-'+shown(n)+'" title="'+shown(n)+'"></span>'+
   '<span class="lbl">'+hl(n.label)+'</span>'+(KIND[n.kind]?'<span class="kind">'+KIND[n.kind]+'</span>':'')+badges+
   '<span class="meta">'+esc(meta(n))+'</span></div>');
  if(isOpen&&kids.length){h.push('<ul class="t">');kids.forEach(r);h.push('</ul>')}
  h.push('</li>');})(D.tree);
 document.getElementById('tree').innerHTML='<ul class="t">'+h.join('')+'</ul>';}
function yn(v,y,n){return v===true?y:v===false?n:'unknown'}
function panel(id){sel=id;var n=byId[id],h=[];
 var path=[];for(var p=id;p;p=parent[p])path.unshift(byId[p].label);
 h.push('<div class="sub">'+esc(path.slice(0,-1).join(' › '))+'</div><h2>'+esc(n.label)+'</h2>');
 h.push('<div class="st"><span class="pill p-'+shown(n)+'">'+({fail:'Fail',warn:'Warn',stale:'Review for cleanup',ok:'OK'}[shown(n)])+'</span> <span class="kind">'+esc(n.kind)+'</span></div>');
 var f=[];function add(k,v){if(v!==undefined&&v!==null&&v!=='')f.push('<dt>'+k+'</dt><dd>'+v+'</dd>')}
 add('Path',n.path?'<code>'+esc(n.path)+'</code>':'');add('Role',esc(n.role));add('Branch',n.branch?'<code>'+esc(n.branch)+'</code>':'');
 add('Head',n.head?'<code>'+esc(n.head)+'</code>':'');add('Upstream',n.upstream?'<code>'+esc(n.upstream)+'</code>':(n.kind==='branch'?'none':''));
 if('merged' in n)add('Merged into main',esc(yn(n.merged,'yes','no'))+(n.main_ref?' <span class="sub">(vs '+esc(n.main_ref)+')</span>':''));
 else if(n.main_ref)add('Main ref','<code>'+esc(n.main_ref)+'</code>');
 if('dirty' in n)add('Uncommitted files',n.dirty==null?'unknown':esc(n.dirty));
 if('local_only' in n)add('Local-only commits',n.local_only==null?'unknown (no remote)':esc(n.local_only));
 if('age_days' in n)add('Age',n.age_days==null?'unknown':esc(n.age_days)+' days');
 if(n.owner)add('Owner / harness',esc(n.owner)+(n.owner==='unknown'?' <span class="sub">(guessed from branch prefix or path; none matched)</span>':' <span class="sub">(guessed from branch prefix or path)</span>'));
 add('Placement',esc({ok:'under manifest root',outside:'outside manifest roots',harness:'harness-owned root',missing:'folder missing'}[n.place]||n.place||''));
 add('Checked out at',n.checked_out?'<code>'+esc(n.checked_out)+'</code>':'');
 if(n.remotes&&n.remotes.length)add('Remotes',n.remotes.map(function(r){return '<code>'+esc(r)+'</code>'}).join('<br>'));
 if(n.kind==='plugin'){add('Origin main',esc(n.main_version||'not on main'));var ins=[];for(var k in n.installed)ins.push(esc(k)+': '+esc(n.installed[k].join(', ')));add('Installed',ins.join('<br>')||'nowhere')}
 add('Note',esc(n.note));
 if(n.children.length){var c=n.cnt;add('Below',c.fail+' fail · '+c.warn+' warn · '+c.stale+' stale · '+c.ok+' ok')}
 h.push('<dl>'+f.join('')+'</dl>');
 if(n.reasons.length){h.push('<h3 style="font-size:14px;margin:0">Why this colour</h3>');
  n.reasons.slice().sort(function(a,b){return RANK[b.level]-RANK[a.level]}).forEach(function(r){
   h.push('<div class="why l-'+r.level+'"><b>'+esc(r.check)+'</b> <span class="pill p-'+r.level+'">'+r.level+'</span><div>'+esc(r.text)+'</div>'+(r.item?'<div><code>'+esc(r.item)+'</code></div>':'')+'</div>')})}
 else if(!n.children.length)h.push('<p class="hint">No warnings or failures on this node.</p>');
 else h.push('<p class="hint">Colour is the worst status below this node. Expand it or pick a child.</p>');
 document.getElementById('side').innerHTML=h.join('');render();}
document.getElementById('tree').addEventListener('click',function(e){var r=e.target.closest('.row');if(!r)return;var id=r.dataset.id;
 if(e.target.classList.contains('car')||sel===id){open[id]=!open[id]}else if(!open[id]&&byId[id].children.length)open[id]=true;panel(id)});
document.getElementById('tree').addEventListener('keydown',function(e){var r=e.target.closest('.row');if(!r)return;var id=r.dataset.id;
 if(e.key==='Enter'||e.key===' '){e.preventDefault();open[id]=!open[id];panel(id);focus(id)}
 else if(e.key==='ArrowRight'){open[id]=true;render();focus(id)}else if(e.key==='ArrowLeft'){if(open[id]){open[id]=false;render();focus(id)}else if(parent[id]){panel(parent[id]);focus(parent[id])}}
 else if(e.key==='ArrowDown'||e.key==='ArrowUp'){var rows=[].slice.call(document.querySelectorAll('#tree .row'));var i=rows.indexOf(r)+(e.key==='ArrowDown'?1:-1);if(rows[i]){rows[i].focus();e.preventDefault()}}});
function focus(id){var el=document.querySelector('#tree .row[data-id="'+id+'"]');if(el)el.focus()}
document.getElementById('q').addEventListener('input',function(e){q=e.target.value.trim().toLowerCase();render()});
document.getElementById('prob').addEventListener('change',function(e){onlyProb=e.target.checked;render()});
[].forEach.call(document.querySelectorAll('.chip[data-s]'),function(ch){ch.addEventListener('click',function(){var s=ch.dataset.s;hidden[s]=!hidden[s];ch.classList.toggle('off',hidden[s]);render()})});
function setAll(v){for(var id in byId)open[id]=v;open[D.tree.id]=true;render()}
document.getElementById('exp').addEventListener('click',function(){setAll(true)});
document.getElementById('col').addEventListener('click',function(){setAll(false)});
document.getElementById('probs').addEventListener('click',function(){setAll(false);for(var id in byId){var n=byId[id];if(n.roll==='fail'||n.roll==='warn')open[id]=true}render()});
render();panel(D.tree.id);
})();
"""


def esc(v) -> str:
    return html.escape("" if v is None else str(v))


def script_json(data) -> str:
    """JSON safe inside <script>: no '<', '>' or '&' survive literally."""
    blob = json.dumps(data, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return blob.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def mini_tab(m: dict, home: Path, rescan: bool = False) -> dict | None:
    """Mac Mini tab data from the sibling hygiene_mini module; None if unavailable."""
    try:
        import hygiene_mini  # noqa: PLC0415 - optional sibling, fail open
        return hygiene_mini.build(m, home, rescan=rescan)
    except Exception as exc:  # noqa: BLE001
        print(f"hygiene: Mac Mini tab skipped: {type(exc).__name__}: {exc}"[:300], file=sys.stderr)
        return None


def mini_html(tab: dict | None) -> tuple[str, str, str]:
    """(css, tab body, js) for the Mac Mini tab."""
    try:
        import hygiene_mini  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return "", '<p class="hint">hygiene_mini.py is not installed next to hygiene_map.py.</p>', ""
    body = hygiene_mini.render_tab(tab, esc) if tab else '<p class="hint">Mac Mini scan unavailable.</p>'
    return hygiene_mini.CSS, body, hygiene_mini.JS


def render_html(snap: dict, rows: list[dict], generated_at: datetime, home: Path | None = None,
                host: str | None = None, mini: dict | None = None) -> str:
    home = expand(home or Path.home())
    if host is None:
        import platform
        host = platform.node().split(".")[0]
    when = generated_at.astimezone(IST).strftime("%a %d %b %Y, %H:%M IST")
    c = snap["hygiene"]["counts"]
    observation = snap.get('remote_observation') or {}
    observed_at = observation.get('observed_at') or 'unknown'
    remote_status = ('available' if observation.get('origin') is not None else 'unknown')
    mirror_status = ('available' if observation.get('github') is not None else 'unknown')
    pr_status = ('available' if observation.get('active_prs') is not None else 'unknown')
    tree = build_tree(snap, rows, home, host)
    disk = free_disk(rows)
    mini_css, mini_body, mini_js = mini_html(mini)
    legend = [("ok", "OK: clean, merged"), ("warn", "Warn: old, too many worktrees, outside root"),
              ("fail", "Fail: misplaced, local-only commits, uncommitted"), ("stale", "Review for cleanup")]
    order = {"FAIL": 0, "WARN": 1, "UNKNOWN": 2, "PASS": 3}
    trs = []
    for r in sorted(rows, key=lambda r: (order.get(r.get("status"), 9), r.get("check", ""))):
        items = r.get("items") or []
        trs.append(f'<tr><td>{esc(r.get("status", ""))}</td><td><code>{esc(r.get("check", ""))}</code></td>'
                   f'<td>{esc(r.get("detail", ""))}{" (" + str(len(items)) + " items)" if items else ""}</td></tr>')
    out = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>Hygiene map: {esc(host or 'Mac Mini')} ({esc(when)})</title><style>{CSS}{mini_css}</style></head><body>",
        "<header>",
        f'<div><h1>Hygiene map: Mac Mini</h1><div class="sub">Last updated {esc(when)} · rewritten by '
        "<code>hygiene check</code> only when the snapshot changes</div></div>",
        f'<div class="stat"><span class="pill p-ok">{c["PASS"]}</span><span>pass</span>'
        f'<span class="pill p-warn">{c["WARN"]}</span><span>warn</span>'
        f'<span class="pill p-fail">{c["FAIL"]}</span><span>fail</span></div>',
        f'<div class="stat">{esc(disk + " free") if disk else "free disk unknown"}<span>disk</span></div>',
        f'<div class="stat">{esc(len(snap.get("machine", [])))}<span>clones scanned</span></div>',
        f'<div class="stat"><code>{esc(snap["origin_main"] or "unknown")}</code><span>cached Origin main (last fetch unknown)</span></div>',
        f'<div class="sub">Live observation: {esc(observed_at)} · Origin {remote_status} · GitHub {mirror_status} · PRs {pr_status}. Snapshot is advisory; recheck before cleanup.</div>',
        '<div class="legend">' + "".join(f'<span><i class="d-{k}"></i>{esc(t)}</span>' for k, t in legend) + "</div>",
        "</header>",
        '<nav class="tabs"><button class="on" data-t="tab-tree">Repos &amp; worktrees</button>'
        '<button data-t="tab-mini">Mac Mini</button></nav>',
        '<div class="tabp" id="tab-tree">',
        '<div class="tools"><input id="q" type="search" placeholder="Search repo, path, branch, owner…" autocomplete="off">',
        '<label class="cb"><input id="prob" type="checkbox"> Show only problems</label>',
        "".join(f'<span class="chip" data-s="{k}" title="Show or hide {k} nodes"><i class="dot d-{k}"></i>{k}</span>'
                for k in ("fail", "warn", "stale", "ok")),
        '<button id="probs">Open problems</button><button id="exp">Expand all</button><button id="col">Collapse all</button></div>',
        '<div class="wrap"><div id="tree"><noscript>Enable JavaScript to see the tree; the check list is in the panel.</noscript></div>',
        '<aside id="side"></aside></div>',
        '<details class="all" style="padding:0 18px 18px"><summary>All hygiene checks '
        f'({c["PASS"]} pass, {c["WARN"]} warn, {c["FAIL"]} fail, {c["UNKNOWN"]} unknown)</summary>'
        f'<table>{"".join(trs)}</table></details>',
        "</div>",
        f'<div class="tabp" id="tab-mini" hidden><div id="mini">{mini_body}</div></div>',
        f'<script type="application/json" id="hm-data">{script_json({"tree": tree})}</script>',
        f"<script>{JS}{mini_js}</script></body></html>\n",
    ]
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


def default_out(home: Path | None = None) -> Path:
    return expand(home or Path.home()) / ".agents" / "hygiene" / "map.html"


def publish(m: dict, rows: list[dict], out: Path | None = None, home: Path | None = None,
            now: datetime | None = None, force: bool = False, base: dict | None = None,
            mini_rescan: bool = False) -> str:
    """Write the map if the snapshot changed (or force). Returns 'written' or 'unchanged'."""
    home = expand(home or Path.home())
    out = Path(out) if out else home / ".agents" / "hygiene" / "map.html"
    hash_path = out.with_name(out.stem + ".sha256")
    snap = snapshot(m, rows, home, base=base)
    mini = mini_tab(m, home, rescan=mini_rescan)
    digest = snapshot_hash(snap)
    if mini and mini.get("scanned_at"):  # no scan yet: the git snapshot alone gates writes
        import hygiene_mini  # noqa: PLC0415
        digest = hashlib.sha256((digest + json.dumps(hygiene_mini.digest_part(mini), sort_keys=True))
                                .encode()).hexdigest()
    try:
        old = hash_path.read_text().strip()
    except OSError:
        old = ""
    if old == digest and out.is_file() and not force:
        return "unchanged"
    atomic_write(out, render_html(snap, rows, now or datetime.now(IST), home=home, mini=mini))
    atomic_write(hash_path, digest + "\n")
    return "written"


def publish_safe(m: dict, rows: list[dict], out: Path | None = None, home: Path | None = None,
                 force: bool = False, base: dict | None = None, mini_rescan: bool = False) -> str | None:
    """publish() that never raises; one stderr line on failure."""
    try:
        result = publish(m, rows, out=out, home=home, force=force, base=base, mini_rescan=mini_rescan)
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
