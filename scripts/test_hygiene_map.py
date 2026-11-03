#!/usr/bin/env python3
"""Tests for scripts/hygiene_map.py: snapshot, hash gate, atomic write, fail-open.

Every path is a temporary directory; nothing touches ~/.agents or the network.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hygiene_map as hm  # noqa: E402

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}
ROWS = [
    {"check": "worktree-placement", "status": "FAIL", "detail": "1 misplaced <x>", "items": ["/a/b"]},
    {"check": "disk", "status": "PASS", "detail": "123.4 GiB free", "items": []},
    {"check": "skill-dupes", "status": "WARN", "detail": "2 dupes", "items": []},
]


def sh(cwd: Path, *args: str) -> str:
    env = {**os.environ, **GIT_ENV}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


class Fixture:
    """home/ with a bare 'Origin' repo, a canonical clone, a worktree and installs."""

    def __init__(self, root: Path) -> None:
        self.home = root / "home"
        self.apps = self.home / "apps"
        self.wt_root = self.home / "code" / "worktrees"
        seed = root / "seed"
        seed.mkdir(parents=True)
        sh(seed, "init", "-q", "-b", "main")
        (seed / "plugins" / "demo").mkdir(parents=True)
        (seed / "plugins" / "demo" / "plugin.json").write_text(json.dumps({"name": "demo", "version": "1.2.0"}))
        sh(seed, "add", ".")
        sh(seed, "commit", "-q", "-m", "seed")
        self.origin = root / "origin" / "agent-plugins.git"
        self.origin.parent.mkdir()
        sh(root, "clone", "-q", "--bare", str(seed), str(self.origin))
        self.github = root / "github" / "agent-plugins.git"
        self.github.parent.mkdir()
        sh(root, "clone", "-q", "--bare", str(seed), str(self.github))
        sh(self.github, "branch", "old-gh-only")
        self.apps.mkdir(parents=True)
        self.canonical = self.apps / "agent-plugins"
        sh(self.apps, "clone", "-q", "-o", "origin-cursor", str(self.origin), "agent-plugins")
        # The real remotes are origin.cursor.com and github.com; fake that by URL rewrite.
        sh(self.canonical, "config", "url." + str(self.origin) + ".insteadOf", "https://origin.cursor.com/x/agent-plugins.git")
        sh(self.canonical, "config", "url." + str(self.github) + ".insteadOf", "https://github.com/x/agent-plugins.git")
        sh(self.canonical, "remote", "set-url", "origin-cursor", "https://origin.cursor.com/x/agent-plugins.git")
        sh(self.canonical, "remote", "add", "github", "https://github.com/x/agent-plugins.git")
        sh(self.canonical, "fetch", "-q", "github")
        self.wt = self.wt_root / "agent-plugins" / "task"
        sh(self.canonical, "worktree", "add", "-q", "-b", "codex/task", str(self.wt), "origin-cursor/main")
        (self.wt / "new.txt").write_text("x")
        sh(self.wt, "commit", "-q", "--allow-empty", "-m", "wip")
        (self.wt / "dirty.txt").write_text("dirty")
        inst = self.home / ".agents" / "plugins" / "demo"
        inst.mkdir(parents=True)
        (inst / "plugin.json").write_text(json.dumps({"name": "demo", "version": "1.1.0"}))
        cache = self.home / ".codex" / "plugins" / "cache" / "personal" / "demo" / "1.2.0"
        cache.mkdir(parents=True)
        (cache / "plugin.json").write_text(json.dumps({"name": "demo", "version": "1.2.0"}))
        self.manifest = {
            "paths": {"checkout_root": str(self.apps), "worktree_root": str(self.wt_root)},
            "scan": {"roots": [str(self.apps), str(self.home / "code")], "depth": 4},
            "exceptions": {"worktree_roots": []},
        }


class HygieneMapTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.fx = Fixture(self.root)
        self.out = self.root / "out" / "map.html"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def snap(self, rows=ROWS) -> dict:
        return hm.snapshot(self.fx.manifest, rows, home=self.fx.home)

    def test_snapshot_contents(self) -> None:
        s = self.snap()
        self.assertEqual(s["canonical"], "~/apps/agent-plugins")
        self.assertEqual(len(s["clones"]), 1)
        paths = {w["path"]: w for w in s["worktrees"]}
        task = paths["~/code/worktrees/agent-plugins/task"]
        self.assertEqual(task["branch"], "codex/task")
        self.assertEqual(task["dirty"], 2)  # new.txt untracked + dirty.txt
        self.assertEqual(task["owner"], "Codex")
        self.assertEqual(task["place"], "ok")
        self.assertFalse(task["in_main"])
        self.assertEqual(paths["~/apps/agent-plugins"]["dirty"], 0)
        names = {(b["where"], b["name"]) for b in s["remote_branches"]}
        self.assertIn(("origin", "main"), names)
        self.assertIn(("github", "old-gh-only"), names)
        gh_only = next(b for b in s["remote_branches"] if b["name"] == "old-gh-only")
        self.assertFalse(gh_only["on_both"])
        self.assertTrue(gh_only["in_main"])
        local = {b["name"] for b in s["local_branches"]}
        self.assertEqual(local, {"main", "codex/task"})
        demo = s["plugins"][0]
        self.assertEqual(demo["main"], "1.2.0")
        self.assertEqual(demo["installed"]["agents (~/.agents/plugins)"], ["1.1.0"])
        self.assertEqual(demo["installed"]["codex"], ["1.2.0"])
        self.assertEqual(demo["installed"]["claude"], [])
        self.assertEqual(s["hygiene"]["counts"], {"PASS": 1, "WARN": 1, "FAIL": 1, "UNKNOWN": 0})

    def test_hash_is_stable_and_ignores_volatile_detail(self) -> None:
        a = hm.snapshot_hash(self.snap())
        rows = [dict(r) for r in reversed(ROWS)]
        rows[1]["detail"] = "99.9 GiB free"
        self.assertEqual(a, hm.snapshot_hash(self.snap(rows)))
        rows[0]["status"] = "PASS"
        self.assertNotEqual(a, hm.snapshot_hash(self.snap(rows)))

    def test_writes_only_when_snapshot_changes(self) -> None:
        now = datetime(2026, 11, 3, 12, 0, tzinfo=hm.IST)
        r1 = hm.publish(self.fx.manifest, ROWS, out=self.out, home=self.fx.home, now=now)
        self.assertEqual(r1, "written")
        hash_file = self.out.with_name("map.sha256")
        self.assertEqual(hash_file.read_text().strip(), hm.snapshot_hash(self.snap()))
        before = self.out.stat().st_mtime_ns
        r2 = hm.publish(self.fx.manifest, ROWS, out=self.out, home=self.fx.home)
        self.assertEqual(r2, "unchanged")
        self.assertEqual(self.out.stat().st_mtime_ns, before)
        (self.fx.wt / "more.txt").write_text("y")
        r3 = hm.publish(self.fx.manifest, ROWS, out=self.out, home=self.fx.home)
        self.assertEqual(r3, "written")
        self.assertEqual([p.name for p in self.out.parent.iterdir() if p.name.endswith(".tmp")], [])

    def test_missing_html_is_rewritten_even_if_hash_matches(self) -> None:
        hm.publish(self.fx.manifest, ROWS, out=self.out, home=self.fx.home)
        self.out.unlink()
        self.assertEqual(hm.publish(self.fx.manifest, ROWS, out=self.out, home=self.fx.home), "written")

    def test_html_is_self_contained_escaped_and_dated_in_ist(self) -> None:
        now = datetime(2026, 11, 3, 12, 7, tzinfo=hm.IST)
        hm.publish(self.fx.manifest, ROWS, out=self.out, home=self.fx.home, now=now)
        page = self.out.read_text()
        self.assertIn("Last updated Tue 03 Nov 2026, 12:07 IST", page)
        for bad in ("http://", "https://", "<script", "<link", "src=", "@import"):
            self.assertNotIn(bad, page)
        self.assertIn("1 misplaced &lt;x&gt;", page)
        self.assertIn("codex/task", page)
        self.assertIn("123.4 GiB free", page)

    def test_failed_write_keeps_old_map_and_hash(self) -> None:
        hm.publish(self.fx.manifest, ROWS, out=self.out, home=self.fx.home)
        old_page, old_hash = self.out.read_text(), self.out.with_name("map.sha256").read_text()
        (self.fx.wt / "more.txt").write_text("y")
        with mock.patch.object(hm.os, "replace", side_effect=OSError("disk full")):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertIsNone(hm.publish_safe(self.fx.manifest, ROWS, out=self.out, home=self.fx.home))
        self.assertEqual(err.getvalue().count("\n"), 1)
        self.assertIn("map not updated: OSError: disk full", err.getvalue())
        self.assertEqual(self.out.read_text(), old_page)
        self.assertEqual(self.out.with_name("map.sha256").read_text(), old_hash)
        self.assertEqual([p.name for p in self.out.parent.iterdir() if p.name.endswith(".tmp")], [])

    def test_fail_open_on_unwritable_target(self) -> None:
        blocker = self.root / "blocker"
        blocker.write_text("file, not a dir")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertIsNone(hm.publish_safe(self.fx.manifest, ROWS, out=blocker / "map.html", home=self.fx.home))
        self.assertTrue(err.getvalue().startswith("hygiene: map not updated: "))

    def test_remote_urls_shown_without_credentials(self) -> None:
        self.assertEqual(hm.norm_remote("https://user:secret@github.com/X/agent-plugins.git"), "github.com/x/agent-plugins")
        self.assertEqual(hm.norm_remote("git@github.com:x/agent-plugins.git"), "github.com:x/agent-plugins")
        sh(self.fx.canonical, "remote", "set-url", "github", "https://tok@github.com/x/agent-plugins.git")
        hm.publish(self.fx.manifest, ROWS, out=self.out, home=self.fx.home)
        self.assertNotIn("tok", self.out.read_text())

    def test_no_canonical_clone_still_renders(self) -> None:
        m = {"paths": {"checkout_root": str(self.root / "nowhere")}, "scan": {"roots": [], "depth": 2}}
        self.assertEqual(hm.publish(m, [], out=self.out, home=self.fx.home), "written")
        self.assertIn("Origin main", self.out.read_text())


if __name__ == "__main__":
    unittest.main()
