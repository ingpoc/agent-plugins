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
        self.observation = mock.patch.object(hm, 'observe_remotes', return_value={
            'origin': hm.refs(self.fx.canonical, 'refs/remotes/origin-cursor'),
            'github': hm.refs(self.fx.canonical, 'refs/remotes/github'), 'active_prs': {},
            'observed_at': '2026-01-01T00:00:00+00:00'})
        self.observer = self.observation.start()
        self.addCleanup(self.observation.stop)

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
        for bad in ("http://", "https://", "<link", "src=", "@import", "fetch(", "XMLHttpRequest", "<iframe"):
            self.assertNotIn(bad, page)
        # Exactly one inline data block and one inline script, nothing loaded from elsewhere.
        self.assertEqual(page.count("<script"), 2)
        self.assertIn('<script type="application/json" id="hm-data">', page)
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


    # ------------------------------------------------------------ machine tree

    def tree(self, rows=ROWS) -> dict:
        snap = self.snap(rows)
        return hm.build_tree(snap, rows, self.fx.home, "mini")

    @staticmethod
    def find(node: dict, **want) -> dict | None:
        if all(node.get(k) == v for k, v in want.items()):
            return node
        for c in node["children"]:
            hit = HygieneMapTest.find(c, **want)
            if hit:
                return hit
        return None

    def add_other_repo(self) -> Path:
        """~/code/other: a second repo with a merged branch and an unpushed one."""
        bare = self.root / "remote" / "other.git"
        bare.parent.mkdir()
        sh(self.root, "init", "-q", "--bare", "-b", "main", str(bare))
        other = self.fx.home / "code" / "other"
        sh(self.root, "clone", "-q", str(bare), str(other))
        sh(other, "commit", "-q", "--allow-empty", "-m", "base")
        sh(other, "push", "-q", "origin", "main")
        sh(other, "branch", "done")
        sh(other, "checkout", "-q", "-b", "feature")
        sh(other, "commit", "-q", "--allow-empty", "-m", "unpushed")
        sh(other, "checkout", "-q", "main")
        return other

    def test_tree_root_is_the_mac_with_repos_worktrees_and_branches(self) -> None:
        self.add_other_repo()
        t = self.tree()
        self.assertEqual(t["kind"], "machine")
        self.assertEqual(t["label"], "Mac Mini (mini)")
        repos = [c for c in t["children"] if c["kind"] == "repo"]
        self.assertEqual([r["label"] for r in repos], ["agent-plugins", "other"])
        ap = repos[0]
        self.assertEqual(ap["path"], "~/apps/agent-plugins")
        groups = {c["label"].split(" (")[0]: c for c in ap["children"]}
        self.assertIn("Worktrees", groups)
        self.assertIn("Branches", groups)
        self.assertIn("Plugins vs Origin main", groups)
        self.assertEqual(t["children"][-1]["label"], "Machine checks")

    def test_status_colours_from_git_state(self) -> None:
        self.add_other_repo()
        t = self.tree(rows=[])
        task = self.find(t, kind="worktree", path="~/code/worktrees/agent-plugins/task")
        self.assertEqual(task["status"], "fail")
        self.assertEqual(task["dirty"], 2)
        self.assertEqual(task["local_only"], 1)
        self.assertEqual({r["check"] for r in task["reasons"]}, {"uncommitted-changes", "local-only-commits"})
        self.assertTrue(all(r["text"] for r in task["reasons"]))
        feature = self.find(t, kind="branch", label="feature")
        self.assertEqual((feature["status"], feature["merged"], feature["local_only"]), ("fail", False, 1))
        done = self.find(t, kind="branch", label="done")
        self.assertEqual((done["status"], done["merged"]), ("warn", True))
        main = self.find(t, kind="branch", path="~/code/other", label="main")
        self.assertEqual(main["status"], "ok")
        other = self.find(t, kind="repo", label="other")
        # ~/code is a scan root but not the checkout root: amber, with the reason.
        self.assertEqual(other["status"], "warn")
        self.assertEqual([r["check"] for r in other["reasons"]], ["outside-root"])

    def test_merged_branch_requires_current_origin_and_complete_pr_check(self) -> None:
        sh(self.fx.canonical, 'branch', 'finished', 'main')
        branch = self.find(self.tree(rows=[]), kind='branch', label='finished')
        self.assertEqual(branch['status'], 'stale')
        observed = self.observer.return_value
        observed['active_prs'] = {'finished': {'number': 8, 'status': 'open', 'head': branch['head']}}
        branch = self.find(self.tree(rows=[]), kind='branch', label='finished')
        self.assertEqual(branch['status'], 'warn')
        self.assertIn('open PR #8', branch['note'])
        observed['active_prs'] = None
        branch = self.find(self.tree(rows=[]), kind='branch', label='finished')
        self.assertEqual(branch['status'], 'warn')
        self.assertIn('unknown', branch['note'])
        observed['active_prs'] = {}
        observed['origin'] = {'main': '0' * 40}
        branch = self.find(self.tree(rows=[]), kind='branch', label='finished')
        self.assertEqual(branch['status'], 'warn')

    def test_remote_observation_fails_closed_on_incomplete_pr_listing(self) -> None:
        self.observation.stop()
        entries = [{'number': n, 'status': 'open', 'headRef': f'b{n}', 'headSha': 'a' * 40}
                   for n in range(200)]
        response = subprocess.CompletedProcess([], 0, json.dumps(entries), '')
        with mock.patch.object(hm, 'git', return_value='a' * 40 + '\trefs/heads/main\n'), \
             mock.patch.object(hm.subprocess, 'run', return_value=response):
            result = hm.observe_remotes(self.fx.canonical, 'origin', 'github')
        self.assertIsNone(result['active_prs'])
        self.assertEqual(result['origin']['main'], 'a' * 40)

    def test_hygiene_items_land_on_their_nodes_with_plain_reasons(self) -> None:
        home = str(self.fx.home)
        rows = [
            {"check": "worktree-age", "status": "WARN", "detail": "old",
             "items": [f"{home}/code/worktrees/agent-plugins/task (53d)"]},
            {"check": "local-only-work", "status": "FAIL", "detail": "x",
             "items": [f"{home}/apps/agent-plugins:codex/task: 1 local-only commit(s), head abc"]},
            {"check": "prune-ready", "status": "WARN", "detail": "1 path",
             "items": [f"{home}/.agents/tmp/old.txt (9d > 7d)"]},
        ]
        t = self.tree(rows)
        task = self.find(t, kind="worktree", path="~/code/worktrees/agent-plugins/task")
        age = next(r for r in task["reasons"] if r["check"] == "worktree-age")
        self.assertEqual(age["level"], "warn")
        self.assertIn("older than the warning window", age["text"])
        br = self.find(t, kind="branch", label="codex/task")
        self.assertIn("local-only-work", {r["check"] for r in br["reasons"]})
        checks = t["children"][-1]
        prune = next(c for c in checks["children"] if c["label"] == "prune-ready")
        self.assertEqual(prune["status"], "stale")
        self.assertEqual(prune["children"][0]["label"], "~/.agents/tmp/old.txt")
        self.assertEqual(prune["children"][0]["status"], "stale")

    def test_item_keys_ignore_counts_but_hash_tracks_paths(self) -> None:
        h = self.fx.home
        self.assertEqual(hm.item_key(f"{h}/x: 9 dirty file(s) remain", h), "~/x")
        self.assertEqual(hm.item_key(f"{h}/x (53d)", h), "~/x")
        self.assertEqual(hm.item_key(f"{h}/x:feat/y: 2 local-only commit(s)", h), "~/x:feat/y")
        rows = [{"check": "worktree-age", "status": "WARN", "detail": "", "items": [f"{h}/x (53d)"]}]
        a = hm.snapshot_hash(self.snap(rows))
        rows[0]["items"] = [f"{h}/x (54d)"]
        self.assertEqual(a, hm.snapshot_hash(self.snap(rows)))
        rows[0]["items"] = [f"{h}/y (54d)"]
        self.assertNotEqual(a, hm.snapshot_hash(self.snap(rows)))

    def test_embedded_data_cannot_close_the_script(self) -> None:
        blob = hm.script_json({"label": "</script><b>&"})
        self.assertNotIn("<", blob)
        self.assertNotIn("&", blob)
        self.assertEqual(json.loads(blob)["label"], "</script><b>&")
        rows = [{"check": "home-scratch", "status": "WARN", "detail": "</script>", "items": ["/tmp/</script>"]}]
        hm.publish(self.fx.manifest, rows, out=self.out, home=self.fx.home)
        page = self.out.read_text()
        self.assertEqual(page.count("</script>"), 2)

    def test_page_has_summary_filters_legend_and_panel(self) -> None:
        hm.publish(self.fx.manifest, ROWS, out=self.out, home=self.fx.home)
        page = self.out.read_text()
        for needle in ('id="q"', 'id="prob"', "Show only problems", 'id="side"', 'class="legend"',
                       "Review for cleanup", "pass</span>", "warn</span>", "fail</span>"):
            self.assertIn(needle, page)
        data = page.split('id="hm-data">', 1)[1].split("</script>", 1)[0]
        tree = json.loads(data)["tree"]
        self.assertTrue(tree["label"].startswith("Mac Mini"))


    def test_force_rewrites_and_precomputed_base_matches(self) -> None:
        hm.publish(self.fx.manifest, ROWS, out=self.out, home=self.fx.home)
        before = self.out.stat().st_mtime_ns
        self.assertEqual(hm.publish(self.fx.manifest, ROWS, out=self.out, home=self.fx.home), "unchanged")
        self.assertEqual(hm.publish(self.fx.manifest, ROWS, out=self.out, home=self.fx.home, force=True), "written")
        self.assertNotEqual(self.out.stat().st_mtime_ns, before)
        base = hm.git_snapshot(self.fx.manifest, self.fx.home)
        self.assertEqual(hm.snapshot_hash(hm.snapshot(self.fx.manifest, ROWS, self.fx.home, base=base)),
                         hm.snapshot_hash(self.snap()))


if __name__ == "__main__":
    unittest.main()
