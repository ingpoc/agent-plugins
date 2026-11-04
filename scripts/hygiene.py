#!/usr/bin/env python3
"""Machine hygiene for agent-created state.

Commands:
  check     report violations, warnings and prune-ready paths (never deletes);
            also refreshes ~/.agents/hygiene/map.html when its snapshot changed
            (--no-map skips; a map failure is one stderr line, never the result)
  map       run the checks, rewrite the map page now and open it (--no-open)
  prune     remove only manifest [prune].paths entries past retention (needs --yes)
  render    regenerate the AGENTS.md block and the Cursor rule from the manifest
  selftest  prove the helpers and the render path on temporary fixtures

Policy owner: ~/.agents/machine.toml
Rules:        ~/.agents/HYGIENE.md

Statuses: PASS (check ran and held), FAIL (actionable violation),
WARN (drift or risk), UNKNOWN (could not verify).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    print("hygiene: python3.11+ required (tomllib)", file=sys.stderr)
    raise SystemExit(2)

HOME = Path.home()
MANIFEST_PATH = HOME / ".agents" / "machine.toml"
SKIP_DIRS = {
    ".git", "node_modules", "Library", ".Trash", ".cache", "venv", ".venv",
    "dist", "build", "target", ".next", ".turbo", "__pycache__",
}


# ---------------------------------------------------------------- manifest

def load_manifest() -> dict:
    with open(MANIFEST_PATH, "rb") as fh:
        return tomllib.load(fh)


def expand(value: str) -> Path:
    return Path(os.path.expanduser(value)).resolve()


def paths(m: dict) -> dict[str, Path]:
    return {k: expand(v) for k, v in m["paths"].items()}


# ---------------------------------------------------------------- helpers

def run(cmd: list[str], cwd: Path | None = None, timeout: int = 30) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            cmd, cwd=str(cwd) if cwd else None, capture_output=True,
            text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def git(path: Path, *args: str, timeout: int = 30) -> str | None:
    proc = run(["git", "-C", str(path), *args], timeout=timeout)
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout


def is_clone(path: Path) -> bool:
    """A standalone clone has a .git directory; a linked worktree has .git file."""
    return (path / ".git").is_dir()


def is_worktree(path: Path) -> bool:
    return (path / ".git").is_file()


def remote_url(path: Path) -> str | None:
    out = git(path, "config", "--get", "remote.origin.url")
    return out.strip() if out and out.strip() else None


def age_days(path: Path) -> float:
    try:
        return (time.time() - path.stat().st_mtime) / 86400
    except OSError:
        return 0.0


def human_gib(path: Path) -> float | None:
    proc = run(["du", "-sk", str(path)], timeout=120)
    if proc is None or proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.split()[0]) / 1024 / 1024
    except (ValueError, IndexError):
        return None


def sha256_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def walk_repos(roots: list[Path], depth: int) -> list[Path]:
    """Find standalone clones and linked worktrees, bounded by depth."""
    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        base_parts = len(root.parts)
        for base, dirnames, _filenames in os.walk(root):
            here = Path(base)
            if len(here.parts) - base_parts >= depth:
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            if (here / ".git").exists():
                found.append(here)
                # Do not descend into a repo's own tree; nested clones are rare
                # and would double-report the parent.
                dirnames[:] = []
    return found


def scan_clones(m: dict) -> list[Path]:
    scan = m.get("scan", {})
    roots = [expand(x) for x in scan.get("roots", [])]
    depth = int(scan.get("depth", 4))
    return sorted({repo for repo in walk_repos(roots, depth) if is_clone(repo)}, key=str)


def norm_remote(url: str) -> str:
    url = url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    # Treat common transport aliases as the same project.
    url = re.sub(r"^git@([^:]+):", r"https://\1/", url)
    url = re.sub(r"^ssh://git@([^/]+)/", r"https://\1/", url)
    return url.lower()


# ---------------------------------------------------------------- checks

class Report:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, name: str, status: str, detail: str = "", items: list[str] | None = None) -> None:
        self.rows.append({
            "check": name,
            "status": status,
            "detail": detail,
            "items": items or [],
        })

    def count(self, status: str) -> int:
        return sum(1 for r in self.rows if r["status"] == status)


def check_home_repo(m: dict, r: Report) -> None:
    if (HOME / ".git").exists():
        remotes = git(HOME, "remote", "-v") or ""
        state = "no remotes" if not remotes.strip() else "has remotes"
        r.add("home-not-a-repo", "FAIL",
              f"$HOME is a git repository ({state}); move tracked content out and remove ~/.git")
    else:
        r.add("home-not-a-repo", "PASS")


def check_roots(m: dict, r: Report) -> None:
    p = paths(m)
    missing = [k for k in ("checkout_root", "archive", "scratch", "artifacts") if not p[k].is_dir()]
    if missing:
        r.add("canonical-roots", "WARN", "missing directories", [str(p[k]) for k in missing])
    else:
        r.add("canonical-roots", "PASS")
    if not p["worktree_root"].is_dir():
        r.add("worktree-root", "WARN",
              f"not created yet: {p['worktree_root']} (new-worktree will create it)")
    else:
        r.add("worktree-root", "PASS")


def check_duplicate_clones(m: dict, r: Report) -> None:
    exceptions = {expand(x) for x in m.get("exceptions", {}).get("checkouts", [])}
    by_remote: dict[str, list[str]] = {}
    for repo in scan_clones(m):
        if repo in exceptions:
            continue
        url = remote_url(repo)
        if url:
            by_remote.setdefault(norm_remote(url), []).append(str(repo))
    dupes = {u: ps for u, ps in by_remote.items() if len(ps) > 1}
    if dupes:
        items = [f"{u} -> {', '.join(ps)}" for u, ps in sorted(dupes.items())]
        r.add("duplicate-clones", "FAIL",
              f"{len(dupes)} remote(s) cloned more than once", items)
    else:
        r.add("duplicate-clones", "PASS", f"{len(by_remote)} unique clone(s)")
    # Repos outside the canonical root.
    canonical = paths(m)["checkout_root"]
    outside = [
        str(p) for ps in by_remote.values() for p in map(Path, ps)
        if canonical not in p.parents and p != canonical and p not in exceptions
        and not any(part in ("checkouts", "vendor", ".build") for part in p.parts)
    ]
    if outside:
        r.add("checkouts-outside-root", "WARN",
              "clones outside checkout_root (manifest exception or move)", sorted(set(outside)))


def check_worktrees(m: dict, r: Report) -> None:
    p = paths(m)
    wt_root = p["worktree_root"]
    warn_days = float(m.get("retention", {}).get("worktree_warn_days", 14))
    repos = scan_clones(m)
    misplaced: list[str] = []
    harness_wt: list[str] = []
    old: list[str] = []
    crowded: list[str] = []
    total = 0
    harness_roots = [expand(x) for x in m.get("harness_owned", {}).get("roots", [])]
    wt_exceptions = {expand(x) for x in m.get("exceptions", {}).get("worktrees", [])}
    wt_exception_roots = [expand(x) for x in m.get("exceptions", {}).get("worktree_roots", [])]
    allowed_roots = [wt_root, *wt_exception_roots]
    for repo in repos:
        out = git(repo, "worktree", "list", "--porcelain")
        if not out:
            continue
        entries = [line.split(" ", 1)[1] for line in out.splitlines() if line.startswith("worktree ")]
        linked = [Path(e) for e in entries if Path(e) != repo and Path(e) not in wt_exceptions]
        if len(linked) > 3:
            crowded.append(f"{repo}: {len(linked)} linked worktrees")
        for wt in linked:
            total += 1
            if any(root == wt or root in wt.parents for root in allowed_roots):
                pass
            elif any(root in wt.parents for root in harness_roots):
                harness_wt.append(str(wt))
            else:
                misplaced.append(str(wt))
            if age_days(wt) > warn_days:
                old.append(f"{wt} ({age_days(wt):.0f}d)")
    if total == 0:
        r.add("worktrees", "PASS", "no linked worktrees")
        return
    if misplaced:
        r.add("worktree-placement", "FAIL",
              "linked worktrees outside configured or approved roots", misplaced)
    elif harness_wt:
        r.add("worktree-placement", "UNKNOWN",
              "under harness-owned roots; verify each harness honors worktree_root",
              harness_wt)
    else:
        r.add("worktree-placement", "PASS", f"{total} linked worktree(s)")
    r.add("worktree-age", "WARN" if old else "PASS", "older than warning window", old)
    r.add("worktree-count", "WARN" if crowded else "PASS", "more than three per repo", crowded)


def verified_bundle_heads(repo: Path, archive_root: Path, entry: dict) -> tuple[dict[str, str] | None, str | None]:
    bundle_value = entry.get("bundle")
    expected_sha = entry.get("bundle_sha256")
    if not isinstance(bundle_value, str) or not isinstance(expected_sha, str):
        return None, "bundle path or sha256 missing"
    bundle = expand(bundle_value)
    if archive_root not in bundle.parents:
        return None, "bundle is outside the configured archive"
    if not bundle.is_file():
        return None, "bundle is missing"
    if sha256_file(bundle) != expected_sha:
        return None, "bundle sha256 mismatch"
    if git(repo, "bundle", "verify", str(bundle), timeout=120) is None:
        return None, "git bundle verify failed"
    listed = git(repo, "bundle", "list-heads", str(bundle))
    if listed is None:
        return None, "git bundle list-heads failed"
    heads: dict[str, str] = {}
    for line in listed.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            heads[parts[1]] = parts[0]
    return heads, None


def check_local_only_work(m: dict, r: Report) -> None:
    p = paths(m)
    archive_root = p["archive"]
    excepted = {str(expand(x)) for x in m.get("exceptions", {}).get("checkouts", [])}
    repos = [x for x in scan_clones(m) if str(x) not in excepted]
    approved_entries: dict[Path, dict] = {}
    failures: list[str] = []
    unknown: list[str] = []
    for entry in m.get("exceptions", {}).get("local_only", []):
        if not isinstance(entry, dict):
            failures.append("local-only exception must be a table")
            continue
        repo_value = entry.get("repo")
        heads = entry.get("heads")
        if not isinstance(repo_value, str) or not isinstance(heads, dict) or not heads:
            failures.append("local-only exception needs repo and exact branch heads")
            continue
        repo = expand(repo_value)
        if repo in approved_entries:
            failures.append(f"duplicate local-only exception for {repo}")
            continue
        if entry.get("owner_approved") is not True or not entry.get("approved_at") or not entry.get("reason"):
            failures.append(f"local-only exception lacks approval and reason: {repo}")
            continue
        if any(not isinstance(branch, str) or not isinstance(head, str)
               or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head)
               for branch, head in heads.items()):
            failures.append(f"local-only exception has an invalid branch head: {repo}")
            continue
        approved_entries[repo] = entry

    accepted: list[str] = []
    notes: list[str] = []
    active_local: set[tuple[Path, str]] = set()
    bundle_cache: dict[Path, tuple[dict[str, str] | None, str | None]] = {}
    for repo in repos:
        refs = git(repo, "for-each-ref", "--format=%(refname:short)%09%(objectname)", "refs/heads")
        status = git(repo, "status", "--porcelain")
        if refs is None or status is None:
            unknown.append(f"{repo}: couldn't read branches or working-tree status")
            continue
        n_dirty = len([line for line in status.splitlines() if line.strip()])
        local_branches: list[tuple[str, str, int]] = []
        for line in refs.splitlines():
            parts = line.split("\t", 1)
            if len(parts) != 2:
                unknown.append(f"{repo}: malformed branch inventory")
                continue
            branch, head = parts
            unique = git(repo, "rev-list", "--count", f"refs/heads/{branch}", "--not", "--remotes")
            if unique is None:
                unknown.append(f"{repo}:{branch}: couldn't compare against remote-tracking refs")
                continue
            count = int(unique.strip() or 0)
            if count:
                local_branches.append((branch, head, count))
                active_local.add((repo, branch))
        if not local_branches:
            continue
        entry = approved_entries.get(repo)
        if entry is None:
            failures.extend(
                f"{repo}:{branch}: {count} local-only commit(s), head {head[:12]}; "
                "review, then publish to a verified destination, retire via the deletion gate, "
                "or record this exact branch/head with a verified bundle"
                for branch, head, count in local_branches
            )
            if n_dirty:
                notes.append(f"{repo}: {n_dirty} dirty file(s) remain in place")
            continue
        bundle = bundle_cache.get(repo)
        if bundle is None:
            bundle = verified_bundle_heads(repo, archive_root, entry)
            bundle_cache[repo] = bundle
        bundled_heads, bundle_error = bundle
        for branch, head, count in local_branches:
            expected_head = entry["heads"].get(branch)
            if expected_head != head:
                failures.append(
                    f"{repo}:{branch}: exact branch/head is not approved; review, then update its "
                    "disposition with a verified bundle, publish to a verified destination, "
                    "or retire via the deletion gate"
                )
            elif bundle_error:
                failures.append(f"{repo}:{branch}: {bundle_error}")
            elif bundled_heads is None or bundled_heads.get(f"refs/heads/{branch}") != head:
                failures.append(f"{repo}:{branch}: verified bundle lacks the recorded head")
            else:
                accepted.append(f"{repo}:{branch}: {count} commit(s), head {head[:12]}, bundle verified")
        if n_dirty:
            notes.append(f"{repo}: {n_dirty} dirty file(s) remain in place; bundle covers committed refs only")

    for repo, entry in approved_entries.items():
        if repo not in repos:
            notes.append(f"{repo}: approved local-only record no longer matches a discovered checkout")
            continue
        for branch in entry["heads"]:
            if (repo, branch) not in active_local:
                notes.append(f"{repo}:{branch}: approved local-only record is stale")

    if failures:
        r.add("local-only-work", "FAIL", "unreviewed or unrecoverable local-only commits", failures + notes)
    elif unknown:
        r.add("local-only-work", "UNKNOWN", "could not verify local-only work", unknown + notes)
    elif accepted or notes:
        r.add("local-only-work", "WARN",
              "approved local-only refs remain unpublished; verified bundles are retained",
              accepted + notes)
    else:
        r.add("local-only-work", "PASS", f"{len(repos)} scanned repo(s)")


def check_skill_dupes(m: dict, r: Report) -> None:
    managed = paths(m)["skills"]
    harness = [
        HOME / ".codex" / "skills",
        HOME / ".grok" / "skills",
        HOME / ".config" / "opencode" / "skills",
    ]
    seen: dict[str, list[str]] = {}
    for root in [managed, *harness]:
        if not root.is_dir():
            continue
        for entry in root.iterdir():
            # A symlink is the sanctioned shape; only real copies collide.
            if entry.is_dir() and not entry.is_symlink():
                seen.setdefault(entry.name, []).append(str(entry))
    managed_prefix = str(managed) + "/"
    managed_dupes: dict[str, list[str]] = {}
    harness_dupes: dict[str, list[str]] = {}
    for name, copies in seen.items():
        if len(copies) < 2:
            continue
        in_managed = [c for c in copies if c.startswith(managed_prefix)]
        if len(in_managed) > 1:
            managed_dupes[name] = copies
        else:
            harness_dupes[name] = copies
    if managed_dupes:
        r.add("skill-duplicates", "FAIL",
              f"{len(managed_dupes)} skill(s) exist as real copies in more than one managed root",
              [f"{n}: {', '.join(ps)}" for n, ps in sorted(managed_dupes.items())])
    elif harness_dupes:
        r.add("skill-duplicates", "WARN",
              f"{len(harness_dupes)} skill(s) also exist in harness roots (report-only)",
              [f"{n}: {', '.join(ps)}" for n, ps in sorted(harness_dupes.items())])
    else:
        r.add("skill-duplicates", "PASS", f"{len(seen)} skill(s), one real copy each")


def check_skill_links(m: dict, r: Report) -> None:
    canonical = paths(m)["skills"] / "machine-hygiene"
    if not canonical.exists():
        r.add("skill-link", "FAIL", f"missing canonical skill {canonical}")
        return
    targets = [
        HOME / ".codex" / "skills" / "machine-hygiene",
        HOME / ".claude" / "skills" / "machine-hygiene",
        HOME / ".grok" / "skills" / "machine-hygiene",
    ]
    bad = [f"{t} -> {t.resolve()}" for t in targets
           if t.exists() and t.resolve() != canonical.resolve()]
    missing = [str(t) for t in targets if not t.exists()]
    if bad:
        r.add("skill-link", "FAIL", "harness skill link points elsewhere", bad)
    elif missing:
        r.add("skill-link", "WARN", f"{len(missing)} harness root(s) lack the skill", missing)
    else:
        r.add("skill-link", "PASS", f"{len(targets)} harness root(s) link the canonical skill")


def block_sha(block: str) -> str:
    return hashlib.sha256(block.encode("utf-8")).hexdigest()


def render_block(m: dict) -> str:
    begin = m["render"]["begin_marker"]
    end = m["render"]["end_marker"]
    body = "\n".join([
        "## Machine hygiene",
        "- Owner `~/.agents/machine.toml`; rules `~/.agents/HYGIENE.md`; skill `machine-hygiene`.",
        "- Before repo edits/resumes, verify the client-bound worktree, branch/HEAD, base/target, dirty/untracked paths, and linked worktrees; cwd/name alone is not proof.",
        "- One owner and integrator per task; isolate only disjoint tasks. Handoffs name source/target SHAs and dirty paths; verify remote/PR state before claiming merged.",
        "- Create checkouts/worktrees only via `new-clone` / `new-worktree`; never in `$HOME`, Desktop, Downloads or caches.",
        "- Link skills from `~/.agents/skills`; never copy a skill into a harness root.",
        "- Delete a copy of work only via `safe-delete`; worktrees use `git worktree remove`, never `rm -rf`.",
        "- Harness state is report-only, never consolidated. Unchecked = `UNKNOWN`, never `PASS`.",
    ])
    return f"{begin}\n{body}\n{end}"


def check_instruction_drift(m: dict, r: Report) -> None:
    render = m["render"]
    agents = expand(render["agents_file"])
    rule = expand(render["cursor_rule"])
    begin, end = render["begin_marker"], render["end_marker"]
    if not agents.exists():
        r.add("instruction-block", "FAIL", f"missing {agents}")
        return
    text = agents.read_text()
    if begin not in text or end not in text:
        r.add("instruction-block", "FAIL", f"markers absent in {agents}; run hygiene.py render")
        return
    block = text.split(begin, 1)[1].split(end, 1)[0]
    digest = block_sha(block)
    if not rule.exists():
        r.add("cursor-rule", "FAIL", f"missing {rule}; run hygiene.py render")
    else:
        rule_text = rule.read_text()
        match = re.search(r"source-sha256:\s*([0-9a-f]{64})", rule_text)
        if not match:
            r.add("cursor-rule", "FAIL", "no source-sha256 header; regenerate")
        elif match.group(1) != digest:
            r.add("cursor-rule", "FAIL", "generated rule is stale; run hygiene.py render")
        else:
            r.add("cursor-rule", "PASS")
    r.add("instruction-block", "PASS", f"sha256 {digest[:12]}")


def check_harness_sizes(m: dict, r: Report) -> None:
    rows = []
    for root in m.get("harness_owned", {}).get("roots", []):
        path = expand(root)
        if not path.exists():
            continue
        gib = human_gib(path)
        if gib is None:
            r.add("harness-state", "UNKNOWN", f"could not size {path}")
            return
        rows.append((str(path), gib))
    if not rows:
        r.add("harness-state", "UNKNOWN", "no harness roots found")
        return
    detail = ", ".join(f"{Path(p).name}={g:.1f}Gi" for p, g in rows)
    big = [f"{p} ({g:.1f} Gi)" for p, g in rows if g >= 5]
    r.add("harness-state", "WARN" if big else "PASS", detail, big)


def check_disk(m: dict, r: Report) -> None:
    floor = float(m.get("retention", {}).get("disk_floor_gi", 30))
    st = os.statvfs("/")
    free = st.f_bavail * st.f_frsize / 1024 ** 3
    status = "FAIL" if free < floor else "PASS"
    r.add("disk-floor", status, f"{free:.1f} Gi free (floor {floor:.0f} Gi)")


def check_home_scratch(r: Report) -> None:
    patterns = re.compile(r"^(acu-|agents-md-review|ZCodeProject|.*\.tgz$|.*backup.*)")
    hits = []
    for entry in HOME.iterdir():
        if entry.name.startswith("."):
            continue
        if patterns.match(entry.name):
            hits.append(str(entry))
    if hits:
        r.add("home-scratch", "WARN", "agent-looking leftovers in $HOME root", sorted(hits))
    else:
        r.add("home-scratch", "PASS")


def check_retention(m: dict, r: Report) -> None:
    p = paths(m)
    ret = m.get("retention", {})
    windows = {
        p["scratch"]: float(ret.get("scratch_days", 7)),
        p["artifacts"]: float(ret.get("artifact_days", 30)),
    }
    ready: list[str] = []
    for root, days in windows.items():
        if not root.is_dir():
            continue
        for entry in root.iterdir():
            if entry.name.startswith("."):
                continue
            if age_days(entry) > days:
                ready.append(f"{entry} ({age_days(entry):.0f}d > {days:.0f}d)")
    r.add("prune-ready", "WARN" if ready else "PASS",
          f"{len(ready)} path(s) past retention", ready)


def run_checks(m: dict) -> Report:
    r = Report()
    check_home_repo(m, r)
    check_roots(m, r)
    check_duplicate_clones(m, r)
    check_worktrees(m, r)
    check_local_only_work(m, r)
    check_skill_dupes(m, r)
    check_skill_links(m, r)
    check_instruction_drift(m, r)
    check_harness_sizes(m, r)
    check_disk(m, r)
    check_home_scratch(r)
    check_retention(m, r)
    return r


def print_report(report: Report) -> None:
    icons = {"PASS": "PASS", "FAIL": "FAIL", "WARN": "WARN", "UNKNOWN": "UNKN"}
    print(f"Machine hygiene — {time.strftime('%Y-%m-%d %H:%M:%S')} — {MANIFEST_PATH}")
    for row in report.rows:
        print(f"{icons[row['status']]}  {row['check']}: {row['detail']}")
        for item in row["items"]:
            print(f"      {item}")
    print("summary: " + ", ".join(
        f"{report.count(s)} {s.lower()}" for s in ("PASS", "FAIL", "WARN", "UNKNOWN")
    ))


# ---------------------------------------------------------------- prune

def select_prunable(m: dict) -> list[Path]:
    ret = m.get("retention", {})
    out: list[Path] = []
    for entry in m.get("prune", {}).get("paths", []):
        root = expand(entry)
        days = float(ret.get("scratch_days", 7) if "tmp" in root.name else ret.get("artifact_days", 30))
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if child.name.startswith("."):
                continue
            if age_days(child) > days:
                out.append(child)
    return out


def prune(m: dict, yes: bool) -> int:
    targets = select_prunable(m)
    if not targets:
        print("prune: nothing past retention")
        return 0
    for path in targets:
        print(f"{'remove' if yes else 'would remove'}  {path}")
    if not yes:
        print("prune: dry run; pass --yes to delete")
        return 0
    for path in targets:
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
            else:
                shutil.rmtree(path)
        except OSError as exc:
            print(f"prune: failed {path}: {exc}", file=sys.stderr)
            return 1
    print(f"prune: removed {len(targets)} path(s)")
    return 0


# ---------------------------------------------------------------- render

def render(m: dict) -> int:
    settings = m["render"]
    agents = expand(settings["agents_file"])
    rule = expand(settings["cursor_rule"])
    begin, end = settings["begin_marker"], settings["end_marker"]
    block = render_block(m)
    digest = block_sha(block.split(begin, 1)[1].split(end, 1)[0])

    if not agents.exists():
        print(f"render: missing {agents}", file=sys.stderr)
        return 1
    text = agents.read_text()
    if begin in text and end in text:
        head, rest = text.split(begin, 1)
        _, tail = rest.split(end, 1)
        text = f"{head}{block}{tail}"
    else:
        marker = "\n## BEFORE"
        if marker not in text:
            print("render: no markers and no '## BEFORE' anchor; insert markers manually", file=sys.stderr)
            return 1
        text = text.replace(marker, f"\n{block}\n{marker}", 1)
    agents.write_text(text)
    print(f"render: updated {agents}")

    rule_text = (
        "---\n"
        "description: Machine hygiene — canonical checkout, worktree, scratch and deletion rules\n"
        "alwaysApply: true\n"
        "---\n\n"
        f"<!-- GENERATED by ~/.agents/bin/hygiene.py render; edit ~/.agents/machine.toml "
        f"or ~/.agents/HYGIENE.md, not this file. source-sha256: {digest} -->\n\n"
        f"{block.split(begin, 1)[1].split(end, 1)[0].strip()}\n"
    )
    rule.parent.mkdir(parents=True, exist_ok=True)
    rule.write_text(rule_text)
    print(f"render: updated {rule}")

    state = expand(m["paths"]["state"])
    state.mkdir(parents=True, exist_ok=True)
    (state / "render.json").write_text(json.dumps(
        {"block_sha256": digest, "rendered_at": time.strftime("%Y-%m-%dT%H:%M:%S")},
        indent=2,
    ) + "\n")
    return 0


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    failures: list[str] = []

    def expect(name: str, condition: bool) -> None:
        print(f"{'ok  ' if condition else 'FAIL'}  {name}")
        if not condition:
            failures.append(name)

    m = load_manifest()
    expect("manifest loads", isinstance(m, dict) and "paths" in m)
    expect("checkout_root expanded", paths(m)["checkout_root"].is_absolute())
    expect("block has markers",
           m["render"]["begin_marker"] in render_block(m)
           and m["render"]["end_marker"] in render_block(m))
    expect("block hash stable", block_sha(render_block(m)) == block_sha(render_block(m)))
    expect("remote normalisation",
           norm_remote("git@github.com:a/b.git") == norm_remote("https://github.com/a/b"))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        checkout_root = root / "checkouts"
        repo = checkout_root / "repo"
        repo.mkdir(parents=True)
        run(["git", "init", "-q", "-b", "main"], cwd=repo)
        run(["git", "config", "user.email", "t@t"], cwd=repo)
        run(["git", "config", "user.name", "t"], cwd=repo)
        (repo / "a").write_text("a\n")
        run(["git", "add", "a"], cwd=repo)
        run(["git", "commit", "-qm", "init"], cwd=repo)
        expect("clone detected", is_clone(repo))
        t3_root = root / "t3" / "worktrees"
        wt = t3_root / "repo" / "sample"
        wt.parent.mkdir(parents=True)
        run(["git", "worktree", "add", "-q", "-b", "side", str(wt)], cwd=repo)
        expect("worktree detected", is_worktree(wt))
        expect("walk finds both", len(walk_repos([root], 4)) == 2)
        expect("age is finite", age_days(repo) >= 0)
        worktree_manifest = {
            "paths": {"checkout_root": str(checkout_root), "worktree_root": str(root / "managed")},
            "scan": {"roots": [str(checkout_root)], "depth": 4},
            "exceptions": {"worktrees": [], "worktree_roots": [str(t3_root)]},
            "retention": {},
        }
        report = Report()
        check_worktrees(worktree_manifest, report)
        placement = next(x for x in report.rows if x["check"] == "worktree-placement")
        expect("registered harness worktree root passes", placement["status"] == "PASS")
        worktree_manifest["exceptions"]["worktree_roots"] = []
        report = Report()
        check_worktrees(worktree_manifest, report)
        placement = next(x for x in report.rows if x["check"] == "worktree-placement")
        expect("unregistered external worktree fails", placement["status"] == "FAIL")

        external_root = root / "external-checkouts"
        external_repo = external_root / "nested" / "external"
        external_repo.mkdir(parents=True)
        run(["git", "init", "-q", "-b", "main"], cwd=external_repo)
        run(["git", "config", "user.email", "t@t"], cwd=external_repo)
        run(["git", "config", "user.name", "t"], cwd=external_repo)
        (external_repo / "a").write_text("a\n")
        run(["git", "add", "a"], cwd=external_repo)
        run(["git", "commit", "-qm", "init"], cwd=external_repo)
        run(["git", "remote", "add", "origin", "https://example.test/shared.git"], cwd=repo)
        run(["git", "remote", "add", "origin", "https://example.test/shared.git"], cwd=external_repo)
        clone_manifest = {
            "paths": {"checkout_root": str(checkout_root)},
            "scan": {"roots": [str(checkout_root), str(external_root)], "depth": 4},
            "exceptions": {"checkouts": []},
        }
        report = Report()
        check_duplicate_clones(clone_manifest, report)
        duplicate = next(x for x in report.rows if x["check"] == "duplicate-clones")
        expect("duplicate clone check sees external checkout", duplicate["status"] == "FAIL")
        clone_manifest["exceptions"]["checkouts"] = [str(external_repo.resolve())]
        report = Report()
        check_duplicate_clones(clone_manifest, report)
        duplicate = next(x for x in report.rows if x["check"] == "duplicate-clones")
        expect("checkout exception suppresses duplicate clone", duplicate["status"] == "PASS")
        expect("configured depth excludes deeper clone",
               external_repo.resolve() not in scan_clones({
                   "scan": {"roots": [str(external_root)], "depth": 1},
               }))
        expect("configured depth includes clone at limit",
               external_repo.resolve() in scan_clones({
                   "scan": {"roots": [str(external_root)], "depth": 2},
               }))
        external_wt = root / "unregistered" / "external"
        external_wt.parent.mkdir(parents=True)
        run(["git", "worktree", "add", "-q", "-b", "external-side", str(external_wt)], cwd=external_repo)
        worktree_manifest["scan"]["roots"].append(str(external_root))
        worktree_manifest["exceptions"]["worktree_roots"] = [str(t3_root)]
        report = Report()
        check_worktrees(worktree_manifest, report)
        placement = next(x for x in report.rows if x["check"] == "worktree-placement")
        expect("external clone worktree is reported", placement["status"] == "FAIL"
               and str(external_wt.resolve()) in placement["items"])
        worktree_manifest["exceptions"]["worktrees"] = [str(external_wt.resolve())]
        report = Report()
        check_worktrees(worktree_manifest, report)
        placement = next(x for x in report.rows if x["check"] == "worktree-placement")
        expect("exact worktree exception suppresses placement failure", placement["status"] == "PASS")
        overlapping_scan = {"scan": {"roots": [str(root), str(checkout_root), str(external_root)], "depth": 4}}
        expect("overlapping scan roots deduplicate clones",
               scan_clones(overlapping_scan) == sorted([repo.resolve(), external_repo.resolve()], key=str))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        checkout_root = root / "apps"
        repo = checkout_root / "repo"
        archive = root / "archive"
        repo.mkdir(parents=True)
        archive.mkdir()
        run(["git", "init", "-q", "-b", "main"], cwd=repo)
        run(["git", "config", "user.email", "t@t"], cwd=repo)
        run(["git", "config", "user.name", "t"], cwd=repo)
        (repo / "a").write_text("a\n")
        run(["git", "add", "a"], cwd=repo)
        run(["git", "commit", "-qm", "init"], cwd=repo)
        head = git(repo, "rev-parse", "refs/heads/main").strip()
        bundle = archive / "repo.bundle"
        created = run(["git", "bundle", "create", str(bundle), "--all"], cwd=repo)
        verified = run(["git", "bundle", "verify", str(bundle)], cwd=repo)
        expect("local-only bundle verifies", created is not None and created.returncode == 0
               and verified is not None and verified.returncode == 0)
        local_manifest = {
            "paths": {"checkout_root": str(checkout_root), "archive": str(archive)},
            "scan": {"roots": [str(checkout_root)], "depth": 4},
            "exceptions": {"checkouts": [], "local_only": [{
                "repo": str(repo), "heads": {"main": head}, "bundle": str(bundle),
                "bundle_sha256": sha256_file(bundle), "owner_approved": True,
                "approved_at": "selftest", "reason": "selftest fixture",
            }]},
        }
        report = Report()
        check_local_only_work(local_manifest, report)
        local_status = next(x for x in report.rows if x["check"] == "local-only-work")
        expect("approved exact local-only head warns", local_status["status"] == "WARN")
        run(["git", "branch", "same-head-alias"], cwd=repo)
        created = run(["git", "bundle", "create", str(bundle), "--all"], cwd=repo)
        verified = run(["git", "bundle", "verify", str(bundle)], cwd=repo)
        local_entry = local_manifest["exceptions"]["local_only"][0]
        local_entry["bundle_sha256"] = sha256_file(bundle)
        report = Report()
        check_local_only_work(local_manifest, report)
        local_status = next(x for x in report.rows if x["check"] == "local-only-work")
        expect("same-head alias needs its exact branch entry", local_status["status"] == "FAIL")
        expect("alias failure gives a recovery path", any(
            "same-head-alias" in item and "exact branch/head" in item
            for item in local_status["items"]
        ))
        expect("alias bundle verifies", created is not None and created.returncode == 0
               and verified is not None and verified.returncode == 0)
        local_entry["heads"]["same-head-alias"] = head
        report = Report()
        check_local_only_work(local_manifest, report)
        local_status = next(x for x in report.rows if x["check"] == "local-only-work")
        expect("approved same-head alias warns", local_status["status"] == "WARN")
        (repo / "b").write_text("b\n")
        run(["git", "add", "b"], cwd=repo)
        run(["git", "commit", "-qm", "unexpected"], cwd=repo)
        report = Report()
        check_local_only_work(local_manifest, report)
        local_status = next(x for x in report.rows if x["check"] == "local-only-work")
        expect("changed local-only head still fails", local_status["status"] == "FAIL")

        external_root = root / "external-checkouts"
        external_repo = external_root / "external"
        external_repo.mkdir(parents=True)
        run(["git", "init", "-q", "-b", "main"], cwd=external_repo)
        run(["git", "config", "user.email", "t@t"], cwd=external_repo)
        run(["git", "config", "user.name", "t"], cwd=external_repo)
        (external_repo / "a").write_text("a\n")
        run(["git", "add", "a"], cwd=external_repo)
        run(["git", "commit", "-qm", "init"], cwd=external_repo)
        external_head = git(external_repo, "rev-parse", "HEAD").strip()
        run(["git", "update-ref", "refs/remotes/origin/main", external_head], cwd=external_repo)
        run(["git", "checkout", "-q", "-b", "outside-only"], cwd=external_repo)
        (external_repo / "b").write_text("b\n")
        run(["git", "add", "b"], cwd=external_repo)
        run(["git", "commit", "-qm", "local-only"], cwd=external_repo)
        local_manifest["scan"]["roots"].append(str(external_root))
        report = Report()
        check_local_only_work(local_manifest, report)
        local_status = next(x for x in report.rows if x["check"] == "local-only-work")
        expect("external clone local-only branch is reported", local_status["status"] == "FAIL"
               and any(f"{external_repo.resolve()}:outside-only:" in item for item in local_status["items"]))
        local_manifest["exceptions"]["checkouts"] = [str(external_repo.resolve())]
        report = Report()
        check_local_only_work(local_manifest, report)
        local_status = next(x for x in report.rows if x["check"] == "local-only-work")
        expect("checkout exception suppresses external local-only finding",
               not any(f"{external_repo.resolve()}:" in item for item in local_status["items"]))

    print(f"selftest: {len(failures)} failure(s)")
    return 1 if failures else 0


# ---------------------------------------------------------------- map page

MAP_JOIN_SECONDS = 120


def map_warn(msg: str) -> None:
    print(f"hygiene: map not updated: {msg}"[:300], file=sys.stderr)


def start_map(m: dict) -> dict | None:
    """Import hygiene_map (installed beside this file) and start its git scan on a
    thread, so it overlaps run_checks(). Never raises."""
    try:
        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        import hygiene_map  # noqa: PLC0415 - optional sibling, fail open
    except Exception as exc:  # noqa: BLE001
        map_warn(f"{type(exc).__name__}: {exc}")
        return None
    job: dict = {"hm": hygiene_map}

    def scan() -> None:
        try:
            job["base"] = hygiene_map.git_snapshot(m)
        except Exception as exc:  # noqa: BLE001
            job["error"] = exc

    job["thread"] = threading.Thread(target=scan, name="hygiene-map", daemon=True)
    job["thread"].start()
    return job


def finish_map(job: dict | None, m: dict, rows: list[dict], force: bool = False) -> str | None:
    """Publish the map from the scan plus the check rows. Returns 'written',
    'unchanged' or None (failed, already reported on stderr). Never raises."""
    if job is None:
        return None
    try:
        job["thread"].join(MAP_JOIN_SECONDS)
        if job["thread"].is_alive():
            map_warn(f"git scan still running after {MAP_JOIN_SECONDS}s")
            return None
        if "error" in job:
            exc = job["error"]
            map_warn(f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}")
            return None
        return job["hm"].publish_safe(m, rows, force=force, base=job["base"],
                                      mini_rescan=job.get("mini_rescan", False))
    except Exception as exc:  # noqa: BLE001
        map_warn(f"{type(exc).__name__}: {exc}")
        return None


def open_map(path: Path) -> None:
    if sys.platform != "darwin":
        print(f"hygiene: map at {path} (open it in a browser)", file=sys.stderr)
        return
    try:
        subprocess.run(["open", str(path)], check=False, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"hygiene: could not open {path}: {exc}", file=sys.stderr)


# ---------------------------------------------------------------- cli

def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="hygiene")
    sub = parser.add_subparsers(dest="cmd", required=True)
    check_p = sub.add_parser("check", help="report only")
    check_p.add_argument("--json", action="store_true")
    check_p.add_argument("--strict", action="store_true", help="exit 1 on FAIL")
    check_p.add_argument("--no-map", action="store_true", help="do not refresh ~/.agents/hygiene/map.html")
    check_p.add_argument("--mini-rescan", action="store_true",
                         help="rescan Mac Mini folder sizes now instead of using the 6 h cache")
    map_p = sub.add_parser("map", help="run the checks, rewrite the map page and open it")
    map_p.add_argument("--no-open", action="store_true", help="write the page but do not open it")
    prune_p = sub.add_parser("prune", help="remove manifest-disposable paths")
    prune_p.add_argument("--yes", action="store_true")
    sub.add_parser("render", help="regenerate instruction block and Cursor rule")
    sub.add_parser("selftest")
    args = parser.parse_args(argv)

    if args.cmd == "selftest":
        return selftest()
    m = load_manifest()
    if args.cmd == "render":
        return render(m)
    if args.cmd == "prune":
        return prune(m, args.yes)
    if args.cmd == "map":
        job = start_map(m)
        report = run_checks(m)
        if finish_map(job, m, report.rows, force=True) is None:
            return 1
        path = job["hm"].default_out()
        print(f"map: {path}")
        if not args.no_open:
            open_map(path)
        return 0
    job = None if args.no_map else start_map(m)
    if job is not None:
        job["mini_rescan"] = args.mini_rescan
    report = run_checks(m)
    if args.json:
        print(json.dumps(report.rows, indent=2))
    else:
        print_report(report)
    sys.stdout.flush()
    finish_map(job, m, report.rows)
    return 1 if (args.strict and report.count("FAIL")) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
