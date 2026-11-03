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
  sweep     classify stale git worktrees, branches, remotes and stray clones;
            dry-run by default (--apply executes; --include-unmerged for risky deletes)

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

def run(
    cmd: list[str], cwd: Path | None = None, timeout: int = 30,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess | None:
    try:
        merged_env = {**os.environ, **env} if env else None
        return subprocess.run(
            cmd, cwd=str(cwd) if cwd else None, capture_output=True,
            text=True, timeout=timeout, check=False, env=merged_env,
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


# ---------------------------------------------------------------- sweep

_REMOTE_PRIORITY = ("origin-cursor", "origin", "github")
_PR_LOOKUP_CACHE: dict[tuple[str, str], dict | None] = {}
_GH_TOKEN_ONCE: str | None | bool = False  # False = unset; None = lookup failed


def _parse_older_than(raw: str) -> float:
    m = re.fullmatch(r"(\d+)([dhw])", raw.strip())
    if not m:
        raise argparse.ArgumentTypeError(f"invalid --older-than {raw!r}; use <int>d|h|w")
    n, unit = int(m.group(1)), m.group(2)
    if unit == "d":
        return float(n)
    if unit == "h":
        return n / 24.0
    return n * 7.0


def _remote_sort_key(name: str) -> tuple[int, int | str]:
    if name in _REMOTE_PRIORITY:
        return (0, _REMOTE_PRIORITY.index(name))
    return (1, name)


def _slug_from_norm_url(norm: str) -> str | None:
    if norm.startswith("https://"):
        path = norm[len("https://"):]
    elif norm.startswith("http://"):
        path = norm[len("http://"):]
    else:
        return None
    parts = [p for p in path.split("/") if p]
    # parts[0] is the host (github.com / origin.cursor.com).
    if len(parts) >= 3:
        return f"{parts[1]}/{parts[2]}"
    return None


def _forge_for_url(raw: str) -> str:
    low = raw.lower()
    if "origin.cursor.com" in low:
        return "origin"
    if "github.com" in low:
        return "github"
    return "other"


def _gh_token_for_sweep() -> str | None:
    global _GH_TOKEN_ONCE
    if _GH_TOKEN_ONCE is not False:
        return _GH_TOKEN_ONCE  # type: ignore[return-value]
    proc = run(["gh", "auth", "token", "-u", "ingpoc"], timeout=30)
    if proc is None or proc.returncode != 0:
        _GH_TOKEN_ONCE = None
        return None
    _GH_TOKEN_ONCE = proc.stdout.strip()
    return _GH_TOKEN_ONCE


def _github_ci_from_rollup(rollup: list | None) -> str:
    if not rollup:
        return "none"
    fail = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT"}
    pend = {"PENDING", "IN_PROGRESS", "QUEUED", "EXPECTED"}
    saw_fail = saw_pend = saw_ok = False
    for check in rollup:
        if not isinstance(check, dict):
            continue
        state = str(check.get("state") or "").upper()
        concl = str(check.get("conclusion") or "").upper()
        if concl in fail or state in fail:
            saw_fail = True
        elif concl in pend or state in pend or not concl:
            saw_pend = True
        elif concl:
            saw_ok = True
    if saw_fail:
        return "failure"
    if saw_pend:
        return "pending"
    if saw_ok:
        return "success"
    return "none"


def open_prs(repo_path: Path, forge: str, slug: str) -> dict | None:  # noqa: ARG001
    """Return branch -> {url, created, ci} or None if lookup failed."""
    key = (forge, slug)
    if key in _PR_LOOKUP_CACHE:
        return _PR_LOOKUP_CACHE[key]
    if forge == "other":
        _PR_LOOKUP_CACHE[key] = {}
        return {}
    out: dict[str, dict] = {}
    if forge == "github":
        token = _gh_token_for_sweep()
        if not token:
            _PR_LOOKUP_CACHE[key] = None
            return None
        proc = run(
            ["gh", "pr", "list", "-R", slug, "--state", "open",
             "--json", "number,headRefName,createdAt,url,statusCheckRollup", "-L", "500"],
            timeout=120,
            env={"GH_TOKEN": token},
        )
        if proc is None or proc.returncode != 0:
            _PR_LOOKUP_CACHE[key] = None
            return None
        try:
            rows = json.loads(proc.stdout or "[]")
        except json.JSONDecodeError:
            _PR_LOOKUP_CACHE[key] = None
            return None
        if not isinstance(rows, list):
            _PR_LOOKUP_CACHE[key] = None
            return None
        for row in rows:
            if not isinstance(row, dict):
                continue
            branch = row.get("headRefName")
            if not isinstance(branch, str):
                continue
            out[branch] = {
                "url": str(row.get("url") or ""),
                "created": str(row.get("createdAt") or ""),
                "ci": _github_ci_from_rollup(row.get("statusCheckRollup")),
            }
        _PR_LOOKUP_CACHE[key] = out
        return out
    if forge == "origin":
        for state in ("open", "draft"):
            proc = run(
                ["origin", "pr", "list", "-R", slug, f"--state={state}",
                 "--json", "number,headRef,headRefName,createdAt,url,ciState", "-L", "200"],
                timeout=120,
            )
            if proc is None or proc.returncode != 0:
                _PR_LOOKUP_CACHE[key] = None
                return None
            try:
                rows = json.loads(proc.stdout or "[]")
            except json.JSONDecodeError:
                _PR_LOOKUP_CACHE[key] = None
                return None
            if not isinstance(rows, list):
                _PR_LOOKUP_CACHE[key] = None
                return None
            for row in rows:
                if not isinstance(row, dict):
                    continue
                branch = row.get("headRef") or row.get("headRefName")
                if not isinstance(branch, str):
                    continue
                ci_raw = row.get("ciState")
                ci = _origin_ci_summary(ci_raw)
                out[branch] = {
                    "url": str(row.get("url") or ""),
                    "created": str(row.get("createdAt") or ""),
                    "ci": ci or "none",
                }
        _PR_LOOKUP_CACHE[key] = out
        return out
    _PR_LOOKUP_CACHE[key] = {}
    return {}


def _sweep_trash() -> Path:
    return Path(os.path.expanduser("~")) / ".Trash"


def _sweep_backup_root(m: dict) -> Path:
    day = time.strftime("%Y%m%d")
    return paths(m)["archive"] / "git-bundles" / day


def _sanitize_stem(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", text)


def _remote_names(repo: Path) -> list[str]:
    out = git(repo, "remote")
    if not out:
        return []
    return [x.strip() for x in out.splitlines() if x.strip()]


def _remote_raw_url(repo: Path, name: str) -> str | None:
    proc = run(["git", "-C", str(repo), "config", "--get", f"remote.{name}.url"], timeout=30)
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _committer_unix(repo: Path, ref: str) -> float | None:
    out = git(repo, "log", "-1", "--format=%ct", ref)
    if not out:
        out = git(repo, "for-each-ref", "--format=%(committerdate:unix)", ref)
    if not out:
        return None
    try:
        return float(out.strip().splitlines()[0])
    except (ValueError, IndexError):
        return None


def _merged(repo: Path, tip: str, base: str) -> bool | None:
    if git(repo, "merge-base", "--is-ancestor", tip, base) is not None:
        return True
    cherry = git(repo, "cherry", base, tip)
    if cherry is not None:
        lines = [ln for ln in cherry.splitlines() if ln.strip()]
        if lines and all(ln.startswith("-") for ln in lines):
            return True
    proc = run(["git", "-C", str(repo), "diff", "--quiet", f"{base}...{tip}"], timeout=60)
    if proc is not None and proc.returncode == 0:
        return True
    if proc is not None and proc.returncode == 1:
        return False
    return None


def _resolve_base(repo: Path) -> tuple[str | None, str | None]:
    local = None
    for name in ("main", "master"):
        ref = f"refs/heads/{name}"
        if git(repo, "show-ref", "--verify", ref) is not None:
            local = ref
            break
    remotes = sorted(_remote_names(repo), key=_remote_sort_key)
    if local:
        for rname in remotes:
            bname = local.split("/", 2)[-1]
            rref = f"refs/remotes/{rname}/{bname}"
            if git(repo, "show-ref", "--verify", rref) is None:
                continue
            head = git(repo, "rev-parse", local)
            if head and git(repo, "merge-base", "--is-ancestor", head.strip(), rref) is not None:
                return rref, None
        return local, None
    for rname in remotes:
        for bname in ("main", "master"):
            rref = f"refs/remotes/{rname}/{bname}"
            if git(repo, "show-ref", "--verify", rref) is not None:
                return rref, None
    return None, "no main"


def _path_set_diff(repo: Path, mb: str, tip: str) -> set[str] | None:
    out = git(repo, "diff", "--name-only", "--no-renames", f"{mb}..{tip}")
    if out is None:
        return None
    return {ln for ln in out.splitlines() if ln.strip()}


def _is_superseded(
    repo: Path, base: str, tip: str, extra_paths: set[str] | None = None,
) -> bool | None:
    mb = git(repo, "merge-base", base, tip)
    if not mb:
        return None
    mb = mb.strip()
    branch_paths = _path_set_diff(repo, mb, tip)
    if branch_paths is None:
        return None
    if extra_paths:
        branch_paths = set(branch_paths) | extra_paths
    main_paths = _path_set_diff(repo, mb, base)
    if main_paths is None:
        return None
    if not branch_paths:
        return False
    return branch_paths <= main_paths


def _pr_age_days(created_iso: str) -> int:
    from datetime import datetime, timezone  # noqa: PLC0415

    try:
        ts = created_iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(dt.tzinfo) - dt
        return max(0, int(delta.total_seconds() // 86400))
    except (ValueError, TypeError):
        return 0


def _move_to_trash(src: Path, stem: str) -> Path | None:
    trash = _sweep_trash()
    trash.mkdir(exist_ok=True)
    day = time.strftime("%Y%m%d")
    base = trash / f"{stem}-{day}"
    dst = base
    n = 2
    while dst.exists():
        dst = Path(f"{base}-{n}")
        n += 1
    proc = run(["mv", str(src), str(dst)], timeout=120)
    if proc is None or proc.returncode != 0:
        return None
    return dst


def _untracked_list(repo: Path, cwd: Path | None = None) -> list[str]:
    root = cwd or repo
    proc = run(
        ["git", "-C", str(root), "ls-files", "--others", "--exclude-standard", "-z"],
        timeout=60,
    )
    if proc is None or proc.returncode != 0:
        return []
    raw = proc.stdout or ""
    return [p for p in raw.split("\0") if p]


def _backup_bundle(repo: Path, bundle: Path, ref: str, cwd: Path | None = None) -> bool:
    root = cwd or repo
    proc = run(["git", "-C", str(root), "bundle", "create", str(bundle), ref], timeout=300)
    if proc is None or proc.returncode != 0:
        return False
    return git(root, "bundle", "verify", str(bundle), timeout=120) is not None


def _backup_patch(repo: Path, patch: Path, base: str, tip: str, cwd: Path | None = None) -> bool:
    root = cwd or repo
    proc = run(
        ["git", "-C", str(root), "format-patch", "--stdout", f"{base}..{tip}"],
        timeout=120,
    )
    if proc is None or proc.returncode != 0:
        return False
    try:
        patch.write_bytes(proc.stdout.encode("utf-8"))
        return True
    except OSError:
        return False


def _backup_wt_patch(wt: Path, patch: Path) -> bool:
    proc = run(["git", "-C", str(wt), "diff", "HEAD", "--binary"], timeout=120)
    if proc is None or proc.returncode != 0:
        return False
    try:
        patch.write_bytes(proc.stdout.encode("utf-8"))
        return True
    except OSError:
        return False


def _backup_untracked_tar(wt: Path, archive: Path, rel_paths: list[str]) -> bool:
    if not rel_paths:
        return True
    listfile = archive.parent / f".{archive.name}.list"
    try:
        listfile.write_text("\0".join(rel_paths) + "\0", encoding="utf-8")
        proc = run(
            ["tar", "-czf", str(archive), "--null", "-T", str(listfile), "-C", str(wt)],
            timeout=300,
        )
        return proc is not None and proc.returncode == 0
    except OSError:
        return False
    finally:
        try:
            listfile.unlink(missing_ok=True)
        except OSError:
            pass


SWEEP_REMOTES = ("origin-cursor", "origin", "github")


def _origin_ci_cache_roots() -> list[Path]:
    """The Origin CI cache is always protected (bin/agent-plugins-origin-ci)."""
    return [Path(os.path.expanduser("~/Library/Caches/agent-plugins-origin-ci")).resolve()]


def _origin_ci_summary(ci_raw: object) -> str:
    """Collapse Origin's ciState (string or {checkRunGroups:[{checkRuns:[...]}]})."""
    if ci_raw is None:
        return "none"
    if isinstance(ci_raw, str):
        return ci_raw.lower() or "none"
    if not isinstance(ci_raw, dict):
        return "none"
    runs = [
        cr for grp in ci_raw.get("checkRunGroups") or [] if isinstance(grp, dict)
        for cr in grp.get("checkRuns") or [] if isinstance(cr, dict)
    ]
    if not runs:
        return "none"
    concl = [str(cr.get("conclusion") or "").lower() for cr in runs]
    if any(c in ("failure", "error", "cancelled", "timed_out", "action_required") for c in concl):
        return "failure"
    if any(str(cr.get("status") or "").lower() != "completed" or not c for cr, c in zip(runs, concl)):
        return "pending"
    return "success"


def _git_porcelain_status(path: Path) -> str | None:
    proc = run(
        ["git", "--no-optional-locks", "-C", str(path), "status", "--porcelain", "--untracked-files=all"],
        timeout=60,
        env={"GIT_OPTIONAL_LOCKS": "0"},
    )
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout or ""


def _format_sweep_action(action: str, apply: bool) -> str:
    if apply or action in ("keep", "report", "skip"):
        return action
    if action.startswith("failed:"):
        return action
    return f"would-{action}"


def sweep(
    m: dict,
    older_than_days: float,
    apply: bool,
    include_unmerged: bool,
    as_json: bool,
) -> int:
    global _PR_LOOKUP_CACHE, _GH_TOKEN_ONCE
    _PR_LOOKUP_CACHE = {}
    _GH_TOKEN_ONCE = False

    checkout_root = paths(m)["checkout_root"]
    exception_checkouts = {expand(x) for x in m.get("exceptions", {}).get("checkouts", [])}
    wt_exceptions = {expand(x) for x in m.get("exceptions", {}).get("worktrees", [])}
    wt_exception_roots = [expand(x) for x in m.get("exceptions", {}).get("worktree_roots", [])]
    harness_roots = [expand(x) for x in m.get("harness_owned", {}).get("roots", [])]
    local_only_by_repo: dict[Path, set[str]] = {}
    for entry in m.get("exceptions", {}).get("local_only", []):
        if isinstance(entry, dict) and isinstance(entry.get("repo"), str) and isinstance(entry.get("heads"), dict):
            local_only_by_repo[expand(entry["repo"])] = set(entry["heads"])

    clones = scan_clones(m)
    canonical_remotes: set[str] = set()
    for clone in clones:
        if clone.parent != checkout_root:
            continue
        for rname in _remote_names(clone):
            raw = _remote_raw_url(clone, rname)
            if raw:
                canonical_remotes.add(norm_remote(raw))

    scanned_repos: list[Path] = []
    stray_clones: list[Path] = []
    for clone in clones:
        if clone in exception_checkouts:
            continue
        if clone.parent == checkout_root:
            scanned_repos.append(clone)
            continue
        urls = set()
        for rname in _remote_names(clone):
            raw = _remote_raw_url(clone, rname)
            if raw:
                urls.add(norm_remote(raw))
        if urls & canonical_remotes:
            stray_clones.append(clone)
        else:
            scanned_repos.append(clone)

    scanned_repos = sorted(set(scanned_repos), key=str)
    stray_clones = sorted(set(stray_clones), key=str)

    rows: list[dict] = []
    gate_fail = False
    state_changed = False
    now = time.time()
    global_pr_cache: dict[tuple[str, str], dict | None] = {}
    seen_remote_norm: set[str] = set()

    def push_row(row: dict) -> None:
        rows.append(row)

    for clone in clones:
        if clone in exception_checkouts:
            push_row({
                "repo": clone.name, "item": str(clone), "kind": "clone", "class": "protected",
                "action": "keep", "backup": "", "detail": "exception checkout",
            })

    # --- stray clones ---
    for clone in stray_clones:
        repo_label = clone.name
        age_ts = clone.stat().st_mtime
        idx = clone / ".git" / "index"
        if idx.is_file():
            age_ts = max(age_ts, idx.stat().st_mtime)
        heads_out = git(clone, "for-each-ref", "--format=%(objectname)", "refs/heads")
        if heads_out:
            for sha in heads_out.splitlines():
                ts = _committer_unix(clone, sha.strip())
                if ts:
                    age_ts = max(age_ts, ts)
        age_d = (now - age_ts) / 86400.0
        status = _git_porcelain_status(clone)
        stash = git(clone, "stash", "list")
        clean = status is not None and stash is not None and not status.strip() and not stash.strip()
        all_contained = True
        if heads_out:
            for sha in heads_out.splitlines():
                sha = sha.strip()
                if not git(clone, "branch", "-r", "--contains", sha):
                    all_contained = False
                    break
        if age_d < older_than_days:
            cls = "recent"
        elif clean and all_contained:
            cls = "merged"
        else:
            cls = "old-unmerged"
        action = "keep"
        if cls == "merged":
            action = "delete"
        elif cls == "old-unmerged":
            action = "delete" if include_unmerged else "report"
        push_row({
            "repo": repo_label, "item": str(clone), "kind": "clone", "class": cls,
            "action": action, "backup": "", "detail": "",
        })

    def wt_removal_planned(cls: str) -> bool:
        if cls == "merged":
            return True
        if cls == "superseded":
            return True
        return cls == "old-unmerged" and include_unmerged

    def classify_tip(
        repo: Path, tip: str, age_d: float, dirty: bool, dirty_paths: set[str] | None,
        branch_name: str, prs_any: set[str], pr_data: dict | None,
    ) -> tuple[str, str, str, dict]:
        extra: dict = {}
        if branch_name in prs_any and pr_data and branch_name in pr_data:
            pr = pr_data[branch_name]
            age_pr = _pr_age_days(pr.get("created", ""))
            detail = f"PR {pr.get('url', '')} age {age_pr}d ci {pr.get('ci', 'none')}"
            extra = {"pr_url": pr.get("url", ""), "pr_age_days": age_pr, "ci": pr.get("ci", "none")}
            return "merge-candidate", "report", detail, extra
        mval = _merged(repo, tip, base_sha)
        if mval is None:
            return "unknown", "skip", "git failed", extra
        if mval and not dirty:
            return "merged", "delete", "", extra
        if age_d < older_than_days:
            return "recent", "keep", "", extra
        if mval and dirty:
            return "old-unmerged", "delete" if include_unmerged else "report", "dirty", extra
        sup = _is_superseded(repo, base_sha, tip, dirty_paths)
        if sup is True:
            return "superseded", "delete", "", extra
        if sup is False:
            return "old-unmerged", "delete" if include_unmerged else "report", "", extra
        return "unknown", "skip", "git failed", extra

    # --- per scanned repo ---
    for repo in scanned_repos:
        repo_label = repo.name
        base, base_err = _resolve_base(repo)
        pr_data_by_forge: dict[str, dict | None] = {}
        prs_any: set[str] = set()

        remote_groups: dict[str, list[tuple[str, str]]] = {}
        for rname in _remote_names(repo):
            raw = _remote_raw_url(repo, rname)
            if not raw:
                continue
            remote_groups.setdefault(norm_remote(raw), []).append((rname, raw))
        # Only our own remotes (origin-cursor, origin, github and their URL aliases);
        # third-party remotes such as `upstream` are never swept.
        remote_groups = {
            k: v for k, v in remote_groups.items()
            if any(n in SWEEP_REMOTES for n, _ in v)
            and _forge_for_url(v[0][1]) in ("origin", "github")
        }

        for _norm, pairs in remote_groups.items():
            pairs_sorted = sorted(pairs, key=lambda x: _remote_sort_key(x[0]))
            raw = pairs_sorted[0][1]
            forge = _forge_for_url(raw)
            slug = _slug_from_norm_url(_norm)
            if slug and forge in ("origin", "github"):
                key = (forge, slug)
                if key not in global_pr_cache:
                    global_pr_cache[key] = open_prs(repo, forge, slug)
                pr_data_by_forge[forge] = global_pr_cache[key]
                if global_pr_cache[key]:
                    prs_any |= set(global_pr_cache[key])

        if base_err:
            for kind in ("worktree", "branch", "remote"):
                push_row({
                    "repo": repo_label, "item": "*", "kind": kind, "class": "unknown",
                    "action": "skip", "backup": "", "detail": base_err,
                })
            continue

        assert base is not None
        base_sha = git(repo, "rev-parse", base)
        if not base_sha:
            push_row({
                "repo": repo_label, "item": "*", "kind": "branch", "class": "unknown",
                "action": "skip", "backup": "", "detail": "no main",
            })
            continue
        base_sha = base_sha.strip()

        wt_out = git(repo, "worktree", "list", "--porcelain") or ""
        wt_blocks: list[dict] = []
        block: dict = {}
        for line in wt_out.splitlines():
            if line.startswith("worktree "):
                if block:
                    wt_blocks.append(block)
                block = {"path": line.split(" ", 1)[1].strip()}
            elif line.startswith("branch "):
                block["branch"] = line.split(" ", 1)[1].strip()
            elif line == "locked":
                block["locked"] = True
        if block:
            wt_blocks.append(block)

        wt_meta: list[dict] = []
        for wb in wt_blocks:
            wt_path = Path(wb["path"]).resolve()
            if wt_path == repo.resolve():
                continue
            branch_ref = wb.get("branch", "")
            branch_name = branch_ref.replace("refs/heads/", "") if branch_ref else ""
            head = git(wt_path, "rev-parse", "HEAD") if wt_path.is_dir() else None
            missing = not wt_path.is_dir()
            protected = (
                wt_path in wt_exceptions
                or any(wt_path == r or r in wt_path.parents for r in _origin_ci_cache_roots())
                or any(wt_path == r or r in wt_path.parents for r in wt_exception_roots)
                or any(wt_path == r or r in wt_path.parents for r in harness_roots)
                or wb.get("locked")
            )
            dirty_paths: set[str] = set()
            status = _git_porcelain_status(wt_path) if not missing else ""
            status_failed = status is None
            dirty = status_failed or (bool(status and status.strip()) if not missing else False)
            if dirty and status:
                for ln in status.splitlines():
                    if len(ln) >= 4:
                        path_part = ln[3:].strip()
                        if " -> " in path_part:
                            path_part = path_part.split(" -> ", 1)[1]
                        dirty_paths.add(path_part)
            age_ts = wt_path.stat().st_mtime if wt_path.exists() else 0
            if head:
                ts = _committer_unix(wt_path, head.strip())
                if ts:
                    age_ts = max(age_ts, ts)
            for dp in dirty_paths:
                p = wt_path / dp
                if p.exists():
                    age_ts = max(age_ts, p.stat().st_mtime)
            age_d = (now - age_ts) / 86400.0 if age_ts else 0
            cls, detail, action, extra = "unknown", "", "skip", {}
            if missing:
                cls, detail, action = "merged", "missing dir", "worktree-prune"
            elif protected:
                cls, action = "protected", "keep"
            elif status_failed:
                cls, detail, action = "unknown", "status failed", "skip"
            elif head:
                pr_union = {}
                for pdata in pr_data_by_forge.values():
                    if pdata:
                        pr_union.update(pdata)
                cls, action, detail, extra = classify_tip(
                    repo, head.strip(), age_d, dirty, dirty_paths, branch_name, prs_any, pr_union,
                )
                if dirty and cls == "old-unmerged":
                    detail = "dirty"
            row = {
                "repo": repo_label, "item": str(wt_path), "kind": "worktree",
                "class": cls, "action": action, "backup": "", "detail": detail, **extra,
                "_path": wt_path, "_branch": branch_name, "_head": head, "_dirty": dirty,
            }
            wt_meta.append(row)
            push_row({k: v for k, v in row.items() if not k.startswith("_")})

        main_head = (git(repo, "symbolic-ref", "--short", "-q", "HEAD") or "").strip()
        kept_wt_branches = {main_head} - {""}
        kept_wt_branches |= {
            r["_branch"] for r in wt_meta
            if r["_branch"] and not wt_removal_planned(r["class"])
        }
        local_only = local_only_by_repo.get(repo, set())

        heads_out = git(repo, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads")
        if heads_out:
            for line in heads_out.splitlines():
                parts = line.split(maxsplit=1)
                if len(parts) != 2:
                    continue
                bname, tip = parts
                if bname in ("main", "master") or bname in local_only or bname in kept_wt_branches:
                    push_row({
                        "repo": repo_label, "item": bname, "kind": "branch", "class": "protected",
                        "action": "keep", "backup": "", "detail": "",
                    })
                    continue
                ts = _committer_unix(repo, tip)
                age_d = (now - ts) / 86400.0 if ts else 0
                pr_union = {}
                for pdata in pr_data_by_forge.values():
                    if pdata:
                        pr_union.update(pdata)
                cls, action, detail, extra = classify_tip(
                    repo, tip, age_d, False, None, bname, prs_any, pr_union,
                )
                push_row({
                    "repo": repo_label, "item": bname, "kind": "branch",
                    "class": cls, "action": action, "backup": "", "detail": detail,
                    "_tip": tip, **extra,
                })

        for norm, pairs in remote_groups.items():
            if norm in seen_remote_norm:
                continue
            seen_remote_norm.add(norm)
            pairs_sorted = sorted(pairs, key=lambda x: _remote_sort_key(x[0]))
            primary = pairs_sorted[0][0]
            raw = pairs_sorted[0][1]
            forge = _forge_for_url(raw)
            slug = _slug_from_norm_url(norm)
            pr_heads: dict | None = {}
            if slug and forge in ("origin", "github"):
                pr_heads = pr_data_by_forge.get(forge)
                if forge not in pr_data_by_forge and slug:
                    pr_heads = open_prs(repo, forge, slug)
            refs = git(
                repo, "for-each-ref",
                "--format=%(refname:strip=3) %(objectname) %(committerdate:unix)",
                f"refs/remotes/{primary}",
            )
            if refs is None:
                continue
            for line in refs.splitlines():
                parts = line.split()
                if len(parts) < 2 or parts[0] == "HEAD":
                    continue
                bname, tip = parts[0], parts[1]
                if bname in ("main", "master") or bname in local_only or bname in kept_wt_branches:
                    push_row({
                        "repo": repo_label, "item": f"{primary}/{bname}", "kind": "remote",
                        "class": "protected", "action": "keep", "backup": "", "detail": "",
                    })
                    continue
                if pr_heads is None and forge in ("origin", "github"):
                    detail = "origin-pr-unknown" if forge == "origin" else "github-pr-unknown"
                    push_row({
                        "repo": repo_label, "item": f"{primary}/{bname}", "kind": "remote",
                        "class": "unknown", "action": "skip", "backup": "", "detail": detail,
                    })
                    continue
                try:
                    age_d = (now - float(parts[2])) / 86400.0 if len(parts) > 2 else 0
                except ValueError:
                    age_d = 0
                # An open PR for this head on any of the repo's forges keeps every copy.
                pr_any_forge = {
                    b: v for pdata in pr_data_by_forge.values() if pdata for b, v in pdata.items()
                }
                if (pr_heads and bname in pr_heads) or bname in pr_any_forge:
                    pr = (pr_heads or {}).get(bname) or pr_any_forge[bname]
                    age_pr = _pr_age_days(pr.get("created", ""))
                    detail = f"PR {pr.get('url', '')} age {age_pr}d ci {pr.get('ci', 'none')}"
                    push_row({
                        "repo": repo_label, "item": f"{primary}/{bname}", "kind": "remote",
                        "class": "merge-candidate", "action": "report", "backup": "", "detail": detail,
                        "pr_url": pr.get("url", ""), "pr_age_days": age_pr, "ci": pr.get("ci", "none"),
                    })
                    continue
                cls, action, detail, extra = classify_tip(
                    repo, tip, age_d, False, None, bname, set(), None,
                )
                push_row({
                    "repo": repo_label, "item": f"{primary}/{bname}", "kind": "remote",
                    "class": cls, "action": action, "backup": "", "detail": detail, **extra,
                    "_primary": primary, "_norm": norm, "_groups": pairs_sorted,
                    "_forge": forge, "_slug": slug, "_tip": tip,
                })

    # normalize row keys for output (drop internal keys)
    def public_row(r: dict) -> dict:
        return {k: v for k, v in r.items() if not str(k).startswith("_")}

    def needs_backup(r: dict) -> bool:
        return r["class"] in ("old-unmerged", "superseded") or (
            r["kind"] == "clone" and r["class"] in ("merged", "old-unmerged")
        )

    backup_root = _sweep_backup_root(m)

    def do_backup(repo: Path, stem: str, ref: str, base_ref: str, wt: Path | None = None) -> str:
        backup_root.mkdir(parents=True, exist_ok=True)
        bundle = backup_root / f"{stem}.bundle"
        if not _backup_bundle(repo, bundle, ref, cwd=wt):
            return ""
        if wt:
            patch = backup_root / f"{stem}.patch"
            if not _backup_wt_patch(wt, patch):
                return ""
            untracked = _untracked_list(repo, wt)
            if untracked:
                tgz = backup_root / f"{stem}.untracked.tgz"
                if not _backup_untracked_tar(wt, tgz, untracked):
                    return ""
        else:
            patch = backup_root / f"{stem}.patch"
            if not _backup_patch(repo, patch, base_ref, ref):
                return ""
        return str(bundle)

    def do_clone_backup(clone: Path, stem: str) -> str:
        backup_root.mkdir(parents=True, exist_ok=True)
        bundle = backup_root / f"{stem}.bundle"
        proc = run(["git", "-C", str(clone), "bundle", "create", str(bundle), "--all"], timeout=300)
        if proc is None or proc.returncode != 0 or git(clone, "bundle", "verify", str(bundle)) is None:
            return ""
        patch = backup_root / f"{stem}.patch"
        proc = run(["git", "-C", str(clone), "diff", "HEAD", "--binary"], timeout=120)
        if proc is None or proc.returncode != 0:
            return ""
        try:
            patch.write_bytes(proc.stdout.encode("utf-8"))
        except OSError:
            return ""
        untracked = _untracked_list(clone)
        if untracked:
            tgz = backup_root / f"{stem}.untracked.tgz"
            if not _backup_untracked_tar(clone, tgz, untracked):
                return ""
        return str(bundle)

    if apply:
        by_repo: dict[str, list[dict]] = {}
        for r in rows:
            by_repo.setdefault(r["repo"], []).append(r)

        for r in rows:
            if r["kind"] != "clone" or r["class"] not in ("merged", "old-unmerged"):
                continue
            if r["action"] != "delete":
                continue
            clone = Path(r["item"])
            stem = _sanitize_stem(f"{clone.name}-clone-{clone.name}")
            bundle = do_clone_backup(clone, stem)
            if not bundle:
                r["action"] = "failed: backup"
                gate_fail = True
                continue
            r["backup"] = bundle
            dst = _move_to_trash(clone, f"{clone.name}-{clone.name}")
            if dst is None:
                r["action"] = "failed: move"
                gate_fail = True
            else:
                r["action"] = "delete"
                state_changed = True

        for repo in scanned_repos:
            repo_label = repo.name
            repo_rows = [r for r in rows if r["repo"] == repo_label and r["kind"] == "worktree"]
            missing_prune = any(r.get("detail") == "missing dir" for r in repo_rows)
            base, _ = _resolve_base(repo)
            base_sha = git(repo, "rev-parse", base).strip() if base else ""
            for r in repo_rows:
                if r["action"] != "delete" and r["action"] != "worktree-prune":
                    continue
                wt = Path(r["item"])
                if r["action"] == "worktree-prune":
                    continue
                if not wt.is_dir():
                    continue
                stem = _sanitize_stem(f"{repo_label}-worktree-{wt.name}")
                if needs_backup(r):
                    bpath = do_backup(repo, stem, "HEAD", base_sha, wt=wt)
                    if not bpath:
                        r["action"] = "failed: backup"
                        gate_fail = True
                        continue
                    r["backup"] = bpath
                if _move_to_trash(wt, f"{repo_label}-{wt.name}") is None:
                    r["action"] = "failed: move"
                    gate_fail = True
                else:
                    r["action"] = "delete"
                    state_changed = True
            if missing_prune or any(
                r["kind"] == "worktree" and r["action"] == "delete" for r in repo_rows
            ):
                if git(repo, "worktree", "prune") is not None:
                    for r in repo_rows:
                        if r["action"] == "worktree-prune":
                            r["action"] = "worktree-prune"
                            state_changed = True

            for r in rows:
                if r["repo"] != repo_label or r["kind"] != "branch":
                    continue
                if r["action"] != "delete":
                    continue
                bname = r["item"]
                tip = r.get("_tip") or git(repo, "rev-parse", f"refs/heads/{bname}")
                if not tip:
                    r["action"] = "failed: delete"
                    gate_fail = True
                    continue
                stem = _sanitize_stem(f"{repo_label}-branch-{bname}")
                if needs_backup(r):
                    bpath = do_backup(repo, stem, f"refs/heads/{bname}", base_sha)
                    if not bpath:
                        r["action"] = "failed: backup"
                        gate_fail = True
                        continue
                    r["backup"] = bpath
                if git(repo, "branch", "-D", bname) is None:
                    r["action"] = "failed: delete"
                    gate_fail = True
                else:
                    r["action"] = "delete"
                    state_changed = True

            for r in rows:
                if r["repo"] != repo_label or r["kind"] != "remote":
                    continue
                if r["action"] != "delete":
                    continue
                item = r["item"]
                primary, bname = item.split("/", 1)
                tip = r.get("_tip", "")
                groups = r.get("_groups") or []
                forge = r.get("_forge", "other")
                slug = r.get("_slug")
                remote = primary
                ls = run(["git", "-C", str(repo), "ls-remote", remote, f"refs/heads/{bname}"], timeout=60)
                if ls is not None and ls.returncode == 0 and not (ls.stdout or "").strip():
                    for rname, _ in groups:
                        ref = f"refs/remotes/{rname}/{bname}"
                        if git(repo, "show-ref", "--verify", ref) is not None:
                            run(["git", "-C", str(repo), "update-ref", "-d", ref], timeout=30)
                    r["action"] = "pruned-tracking"
                    state_changed = True
                    continue
                if ls is None or ls.returncode != 0:
                    r["action"] = "failed: delete"
                    gate_fail = True
                    continue
                remote_sha = (ls.stdout or "").split()[0] if (ls.stdout or "").strip() else ""
                local_sha = tip
                if remote_sha and local_sha and remote_sha != local_sha:
                    r["class"] = "unknown"
                    r["detail"] = "remote moved"
                    r["action"] = "skip"
                    continue
                stem = _sanitize_stem(f"{repo_label}-remote-{bname}")
                if needs_backup(r):
                    bpath = do_backup(repo, stem, f"refs/remotes/{primary}/{bname}", base_sha)
                    if not bpath:
                        r["action"] = "failed: backup"
                        gate_fail = True
                        continue
                    r["backup"] = bpath
                pushed = git(repo, "push", remote, "--delete", bname) is not None
                if not pushed and forge == "github" and slug:
                    token = _gh_token_for_sweep()
                    if token:
                        proc = run(
                            ["gh", "api", "-X", "DELETE", f"repos/{slug}/git/refs/heads/{bname}"],
                            timeout=60,
                            env={"GH_TOKEN": token},
                        )
                        pushed = proc is not None and proc.returncode == 0
                if not pushed:
                    r["action"] = "failed: delete"
                    gate_fail = True
                    continue
                for rname, _ in groups:
                    ref = f"refs/remotes/{rname}/{bname}"
                    if git(repo, "show-ref", "--verify", ref) is not None:
                        run(["git", "-C", str(repo), "update-ref", "-d", ref], timeout=30)
                r["action"] = "delete"
                state_changed = True

    out_rows = sorted(
        [public_row(r) for r in rows],
        key=lambda x: (x["repo"], x["kind"], x["item"]),
    )
    for r in out_rows:
        r["action"] = _format_sweep_action(r["action"], apply)

    class_counts: dict[str, int] = {}
    action_counts: dict[str, int] = {}
    for r in out_rows:
        class_counts[r["class"]] = class_counts.get(r["class"], 0) + 1
        action_counts[r["action"]] = action_counts.get(r["action"], 0) + 1

    check_report = run_checks(m)
    check_fail = check_report.count("FAIL")
    if check_fail:
        names = ",".join(x["check"] for x in check_report.rows if x["status"] == "FAIL")
        print(f"sweep: final check FAIL: {names}", file=sys.stderr)

    exit_code = 0
    if gate_fail or check_fail:
        exit_code = 3
    elif any(
        r["class"] in ("merge-candidate", "unknown")
        or (r["class"] == "old-unmerged" and r["action"] in ("report", "would-report"))
        for r in out_rows
    ):
        exit_code = 2

    if state_changed:
        job = start_map(m)
        finish_map(job, m, check_report.rows)
    else:
        pass

    if as_json:
        print(json.dumps({
            "rows": out_rows,
            "summary": {
                "classes": class_counts,
                "actions": action_counts,
                "check_fail": check_fail,
                "exit": exit_code,
            },
        }))
    else:
        for r in out_rows:
            print("\t".join([
                r["repo"], r["item"], r["class"], r["action"], r.get("backup", ""),
                r.get("detail", ""),
            ]).rstrip("\t"))
        cc = " ".join(f"{k}={v}" for k, v in sorted(class_counts.items()))
        ac = " ".join(f"{k}={v}" for k, v in sorted(action_counts.items()))
        print(
            f"sweep: {len(out_rows)} items; class counts {cc}; actions {ac}; "
            f"check FAIL={check_fail}; exit {exit_code}"
        )
    return exit_code


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
        return job["hm"].publish_safe(m, rows, force=force, base=job["base"])
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
    map_p = sub.add_parser("map", help="run the checks, rewrite the map page and open it")
    map_p.add_argument("--no-open", action="store_true", help="write the page but do not open it")
    prune_p = sub.add_parser("prune", help="remove manifest-disposable paths")
    prune_p.add_argument("--yes", action="store_true")
    sub.add_parser("render", help="regenerate instruction block and Cursor rule")
    sub.add_parser("selftest")
    sweep_p = sub.add_parser("sweep", help="classify stale worktrees, branches, remotes and clones")
    sweep_p.add_argument("--older-than", type=_parse_older_than, default=7.0,
                         help="age threshold as <int>d|h|w (default 7d)")
    sweep_p.add_argument("--apply", action="store_true", help="execute deletions and backups")
    sweep_p.add_argument("--include-unmerged", action="store_true",
                         help="backup and delete old-unmerged items")
    sweep_p.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.cmd == "selftest":
        return selftest()
    if args.cmd == "sweep":
        m = load_manifest()
        older = args.older_than if isinstance(args.older_than, float) else float(args.older_than)
        return sweep(m, older, args.apply, args.include_unmerged, args.json)
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
