#!/usr/bin/env python3
"""In-process tests for hygiene sweep (fixture under ~/.agents/tmp only)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hygiene as hy  # noqa: E402

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def sh(cwd: Path, *args: str, extra_env: dict | None = None) -> str:
    env = {**os.environ, **GIT_ENV, **(extra_env or {})}
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True,
    ).stdout.strip()


def old_env() -> dict:
    when = "Mon Jan 1 2024 00:00:00 +0000"
    return {"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}


class SweepFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "home"
        self.apps = self.home / "apps"
        self.wt_root = self.home / "code" / "worktrees"
        self.archive = self.home / "archive"
        self.scratch = self.home / ".agents" / "tmp"
        for p in (self.apps, self.wt_root, self.archive, self.scratch):
            p.mkdir(parents=True, exist_ok=True)

        seed = root / "seed"
        seed.mkdir()
        sh(seed, "init", "-q", "-b", "main")
        (seed / "a.txt").write_text("base\n")
        sh(seed, "add", "a.txt")
        sh(seed, "commit", "-q", "-m", "seed")

        self.origin = root / "origin.git"
        sh(root, "clone", "-q", "--bare", str(seed), str(self.origin))
        self.github = root / "github.git"
        sh(root, "clone", "-q", "--bare", str(self.origin), str(self.github))

        self.canonical = self.apps / "demo"
        sh(self.apps, "clone", "-q", "-o", "origin-cursor", str(self.origin), "demo")
        sh(
            self.canonical,
            "config",
            "url." + str(self.origin) + ".insteadOf",
            "https://origin.cursor.com/x/demo.git",
        )
        sh(
            self.canonical,
            "config",
            "url." + str(self.github) + ".insteadOf",
            "https://github.com/x/demo.git",
        )
        sh(self.canonical, "remote", "set-url", "origin-cursor", "https://origin.cursor.com/x/demo.git")
        sh(self.canonical, "remote", "add", "github", "https://github.com/x/demo.git")
        sh(self.canonical, "fetch", "-q", "github")

        # merged-ff: old branch fully merged into main
        sh(self.canonical, "branch", "merged-ff", extra_env=old_env())
        sh(self.canonical, "checkout", "-q", "merged-ff")
        (self.canonical / "ff.txt").write_text("ff\n")
        sh(self.canonical, "add", "ff.txt", extra_env=old_env())
        sh(self.canonical, "commit", "-q", "-m", "ff", extra_env=old_env())
        sh(self.canonical, "checkout", "-q", "main")
        sh(self.canonical, "merge", "-q", "--ff-only", "merged-ff")

        # cherry: old commit cherry-picked onto main
        sh(self.canonical, "checkout", "-q", "-b", "cherry-src", extra_env=old_env())
        (self.canonical / "cherry.txt").write_text("c\n")
        sh(self.canonical, "add", "cherry.txt", extra_env=old_env())
        sh(self.canonical, "commit", "-q", "-m", "cherry-src", extra_env=old_env())
        cherry_sha = sh(self.canonical, "rev-parse", "HEAD")
        sh(self.canonical, "checkout", "-q", "main")
        sh(self.canonical, "cherry-pick", cherry_sha)
        sh(self.canonical, "push", "-q", "origin-cursor", "main")

        # superseded-old: old change to a.txt, then main changes a.txt again
        old_main = sh(self.canonical, "rev-parse", "HEAD")
        sh(self.canonical, "checkout", "-q", "-b", "superseded-old", old_main, extra_env=old_env())
        (self.canonical / "a.txt").write_text("branch-a\n")
        sh(self.canonical, "add", "a.txt", extra_env=old_env())
        sh(self.canonical, "commit", "-q", "-m", "sup", extra_env=old_env())
        sh(self.canonical, "checkout", "-q", "main")
        (self.canonical / "a.txt").write_text("main-a\n")
        sh(self.canonical, "add", "a.txt")
        sh(self.canonical, "commit", "-q", "-m", "main moves a")
        sh(self.canonical, "push", "-q", "origin-cursor", "main")

        # unmerged-old
        sh(self.canonical, "checkout", "-q", "-b", "unmerged-old", extra_env=old_env())
        (self.canonical / "u.txt").write_text("u\n")
        sh(self.canonical, "add", "u.txt", extra_env=old_env())
        sh(self.canonical, "commit", "-q", "-m", "unmerged", extra_env=old_env())
        sh(self.canonical, "checkout", "-q", "main")

        # recent-work
        sh(self.canonical, "checkout", "-q", "-b", "recent-work")
        (self.canonical / "r.txt").write_text("r\n")
        sh(self.canonical, "add", "r.txt")
        sh(self.canonical, "commit", "-q", "-m", "recent")
        sh(self.canonical, "checkout", "-q", "main")

        # keep-me
        sh(self.canonical, "checkout", "-q", "-b", "keep-me", extra_env=old_env())
        (self.canonical / "k.txt").write_text("k\n")
        sh(self.canonical, "add", "k.txt", extra_env=old_env())
        sh(self.canonical, "commit", "-q", "-m", "keep", extra_env=old_env())
        self.keep_sha = sh(self.canonical, "rev-parse", "HEAD")
        sh(self.canonical, "checkout", "-q", "main")

        # pr-branch
        sh(self.canonical, "checkout", "-q", "-b", "pr-branch", extra_env=old_env())
        (self.canonical / "pr.txt").write_text("pr\n")
        sh(self.canonical, "add", "pr.txt", extra_env=old_env())
        sh(self.canonical, "commit", "-q", "-m", "pr", extra_env=old_env())
        sh(self.canonical, "push", "-q", "origin-cursor", "pr-branch")
        sh(self.canonical, "checkout", "-q", "main")

        # gone-merged: merged branch pushed then deleted locally
        sh(self.canonical, "checkout", "-q", "-b", "gone-merged", extra_env=old_env())
        (self.canonical / "gone.txt").write_text("g\n")
        sh(self.canonical, "add", "gone.txt", extra_env=old_env())
        sh(self.canonical, "commit", "-q", "-m", "gone", extra_env=old_env())
        sh(self.canonical, "checkout", "-q", "main")
        sh(self.canonical, "merge", "-q", "--ff-only", "gone-merged")
        sh(self.canonical, "push", "-q", "origin-cursor", "main", "gone-merged")
        sh(self.canonical, "branch", "-D", "gone-merged")

        # gh-old on github only
        sh(self.canonical, "checkout", "-q", "-b", "gh-old", extra_env=old_env())
        (self.canonical / "gh.txt").write_text("gh\n")
        sh(self.canonical, "add", "gh.txt", extra_env=old_env())
        sh(self.canonical, "commit", "-q", "-m", "gh", extra_env=old_env())
        sh(self.canonical, "push", "-q", "github", "gh-old")
        sh(self.canonical, "checkout", "-q", "main")
        sh(self.canonical, "branch", "-D", "gh-old")

        # worktrees
        self.merged_wt = self.wt_root / "demo" / "merged-wt"
        self.merged_wt.parent.mkdir(parents=True, exist_ok=True)
        sh(self.canonical, "branch", "merged-wt", extra_env=old_env())
        sh(self.canonical, "checkout", "-q", "merged-wt")
        (self.canonical / "mwt.txt").write_text("m\n")
        sh(self.canonical, "add", "mwt.txt", extra_env=old_env())
        sh(self.canonical, "commit", "-q", "-m", "mwt", extra_env=old_env())
        sh(self.canonical, "checkout", "-q", "main")
        sh(self.canonical, "merge", "-q", "--ff-only", "merged-wt")
        sh(self.canonical, "worktree", "add", "-q", str(self.merged_wt), "merged-wt")

        self.dirty_wt = self.wt_root / "demo" / "dirty-old"
        sh(self.canonical, "checkout", "-q", "-b", "dirty-old", extra_env=old_env())
        (self.canonical / "dwt.txt").write_text("d\n")
        sh(self.canonical, "add", "dwt.txt", extra_env=old_env())
        sh(self.canonical, "commit", "-q", "-m", "dwt", extra_env=old_env())
        sh(self.canonical, "checkout", "-q", "main")
        sh(self.canonical, "worktree", "add", "-q", str(self.dirty_wt), "dirty-old")
        (self.dirty_wt / "dirty-local.txt").write_text("dirty\n")
        old_ts = time.time() - 30 * 86400
        os.utime(self.dirty_wt, (old_ts, old_ts))
        os.utime(self.dirty_wt / "dirty-local.txt", (old_ts, old_ts))

        sh(self.canonical, "checkout", "-q", "main")

        # exception checkout
        exc_root = self.apps / "demo-origin-work"
        exc_root.mkdir(parents=True)
        self.ci_clone = exc_root / "ci-clone"
        sh(exc_root, "clone", "-q", str(self.origin), "ci-clone")

        # stray clone
        self.stray = self.home / "code" / "demo-copy"
        sh(self.home / "code", "clone", "-q", str(self.origin), "demo-copy")
        sh(self.stray, "remote", "set-url", "origin", "https://origin.cursor.com/x/demo.git")
        sh(self.stray, "config", "url." + str(self.origin) + ".insteadOf", "https://origin.cursor.com/x/demo.git")
        old_ts = time.time() - 30 * 86400
        os.utime(self.stray, (old_ts, old_ts))
        os.utime(self.stray / ".git" / "index", (old_ts, old_ts))

        self.trash = self.home / ".Trash"
        self.trash.mkdir(exist_ok=True)

        self.manifest_path = self.home / ".agents" / "machine.toml"
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        keep = self.keep_sha
        self.manifest_path.write_text(
            f"""
[paths]
checkout_root = "{self.apps}"
worktree_root = "{self.wt_root}"
archive = "{self.archive}"
scratch = "{self.scratch}"

[scan]
roots = ["{self.apps}", "{self.home / 'code'}"]
depth = 4

[exceptions]
checkouts = ["{self.ci_clone}"]

[[exceptions.local_only]]
repo = "{self.canonical}"
owner_approved = true
approved_at = "2026-01-01"
reason = "test"
heads = {{ keep-me = "{keep}" }}
""".strip()
            + "\n",
            encoding="utf-8",
        )

        self.pr_created = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat().replace("+00:00", "Z")


class SweepTest(unittest.TestCase):
    def setUp(self) -> None:
        agents_tmp = Path.home() / ".agents" / "tmp"
        agents_tmp.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=str(agents_tmp))
        self.root = Path(self._tmp.name).resolve()
        self.fx = SweepFixture(self.root)
        self.pr_iso = self.fx.pr_created
        self.patches = [
            mock.patch.dict(os.environ, {"HOME": str(self.fx.home)}),
            mock.patch.object(hy, "MANIFEST_PATH", self.fx.manifest_path),
            mock.patch.object(hy, "HOME", self.fx.home),
            mock.patch.object(
                hy,
                "open_prs",
                side_effect=self._mock_open_prs,
            ),
            mock.patch.object(hy, "run_checks", return_value=hy.Report()),
            mock.patch.object(hy, "start_map", return_value=None),
            mock.patch.object(hy, "finish_map", return_value=None),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        self._tmp.cleanup()

    def _mock_open_prs(self, repo_path: Path, forge: str, slug: str) -> dict | None:
        if forge == "origin":
            return {
                "pr-branch": {
                    "url": "https://origin.cursor.com/x/demo/pull/1",
                    "created": self.pr_iso,
                    "ci": "success",
                },
            }
        if forge == "github":
            return {}
        return {}

    def run_sweep(self, *argv: str) -> tuple[int, dict]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = hy.main(["sweep", *argv])
        payload = json.loads(out.getvalue())
        return code, payload

    def snapshot(self) -> str:
        parts: list[str] = []
        for repo in (self.fx.canonical, self.fx.origin, self.fx.github):
            parts.append(sh(repo, "for-each-ref", "--format=%(refname) %(objectname)"))
        code_tree = sorted(str(p) for p in self.fx.home.joinpath("code").rglob("*") if p.is_dir())
        parts.append("\n".join(code_tree))
        trash = sorted(p.name for p in self.fx.trash.iterdir()) if self.fx.trash.is_dir() else []
        parts.append("\n".join(trash))
        archive = self.fx.home / "archive"
        parts.append("\n".join(sorted(str(p) for p in archive.rglob("*"))) if archive.is_dir() else "")
        return "\n---\n".join(parts)

    def classes(self, payload: dict) -> dict[str, str]:
        return {r["item"]: r["class"] for r in payload["rows"]}

    def test_slug_from_url(self) -> None:
        self.assertEqual(hy._slug_from_norm_url(hy.norm_remote("https://github.com/ingpoc/agent-plugins.git")), "ingpoc/agent-plugins")
        self.assertEqual(hy._slug_from_norm_url(hy.norm_remote("git@github.com:o/r.git")), "o/r")
        self.assertEqual(hy._slug_from_norm_url(hy.norm_remote("https://origin.cursor.com/gurusharan/x.git")), "gurusharan/x")

    def test_sweep_flow(self) -> None:
        snap0 = self.snapshot()
        code, payload = self.run_sweep("--json")
        self.assertEqual(code, 2)
        by = self.classes(payload)
        self.assertEqual(by["main"], "protected")
        self.assertEqual(by["keep-me"], "protected")
        self.assertEqual(by["merged-ff"], "merged")
        self.assertEqual(by["cherry-src"], "merged")
        self.assertEqual(by[str(self.fx.merged_wt)], "merged")
        self.assertEqual(by[str(self.fx.dirty_wt)], "old-unmerged")
        dirty_row = next(r for r in payload["rows"] if r["item"] == str(self.fx.dirty_wt))
        self.assertEqual(dirty_row["detail"], "dirty")
        self.assertEqual(by["unmerged-old"], "old-unmerged")
        self.assertEqual(by["recent-work"], "recent")
        self.assertEqual(by["pr-branch"], "merge-candidate")
        self.assertEqual(by["superseded-old"], "superseded")
        self.assertEqual(by[str(self.fx.ci_clone)], "protected")
        self.assertEqual(by[str(self.fx.stray)], "merged")
        pr_rows = [r for r in payload["rows"] if r.get("item") == "pr-branch" and r["kind"] == "branch"]
        self.assertTrue(pr_rows[0].get("pr_url"))
        self.assertEqual(self.snapshot(), snap0)

        code, payload = self.run_sweep("--json", "--apply")
        self.assertEqual(code, 2)
        for branch in ("merged-ff", "cherry-src", "superseded-old"):
            proc = subprocess.run(
                ["git", "-C", str(self.fx.canonical), "show-ref", "--verify", f"refs/heads/{branch}"],
                capture_output=True,
            )
            self.assertNotEqual(proc.returncode, 0, branch)
        self.assertFalse(self.fx.merged_wt.exists())
        self.assertTrue(any("merged-wt" in p.name for p in self.fx.trash.iterdir()))
        proc = subprocess.run(
            ["git", "--git-dir", str(self.fx.origin), "show-ref", "--verify", "refs/heads/gone-merged"],
            capture_output=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(self.fx.stray.exists())
        bundles = list((self.fx.archive / "git-bundles").rglob("*.bundle"))
        self.assertTrue(bundles)
        for b in bundles:
            proc = subprocess.run(
                ["git", "-C", str(self.fx.canonical), "bundle", "verify", str(b)],
                capture_output=True,
            )
            self.assertEqual(proc.returncode, 0, b)
        by2 = self.classes(payload)
        self.assertEqual(by2.get("unmerged-old", payload), "old-unmerged")

        snap1 = self.snapshot()
        code, payload = self.run_sweep("--json", "--apply")
        self.assertEqual(code, 2)
        for r in payload["rows"]:
            self.assertIn(r["action"], ("keep", "report", "skip"))
        self.assertEqual(self.snapshot(), snap1)

        code, payload = self.run_sweep("--json", "--apply", "--include-unmerged")
        self.assertEqual(code, 2)
        proc = subprocess.run(
            ["git", "-C", str(self.fx.canonical), "show-ref", "--verify", "refs/heads/unmerged-old"],
            capture_output=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(self.fx.dirty_wt.exists())
        patch_files = list((self.fx.archive / "git-bundles").rglob("*.patch"))
        self.assertTrue(patch_files)
        tgz = list((self.fx.archive / "git-bundles").rglob("*.untracked.tgz"))
        self.assertTrue(tgz)

        snap2 = self.snapshot()
        code, payload = self.run_sweep("--json", "--apply", "--include-unmerged")
        self.assertEqual(code, 2)
        for r in payload["rows"]:
            self.assertIn(r["action"], ("keep", "report", "skip"))
        self.assertEqual(self.snapshot(), snap2)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            hy.main(["sweep"])
        plain = out.getvalue()
        self.assertTrue(plain.strip().splitlines()[-1].startswith("sweep: "))


if __name__ == "__main__":
    unittest.main()
