#!/usr/bin/env python3
"""Tests for scripts/hygiene_mini.py: the delete-candidate classifier and the tab.

Every path is a temporary directory; nothing touches ~/.agents.
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hygiene_mini as mini  # noqa: E402

DAY = 86400


class ClassifierTest(unittest.TestCase):
    def setUp(self) -> None:
        # short base dir: AF_UNIX socket paths are limited to ~104 bytes on macOS
        self._tmp = tempfile.TemporaryDirectory(dir="/tmp" if os.path.isdir("/tmp") else None)
        self.home = Path(self._tmp.name).resolve()
        self.now = time.time()
        self.m = {"protected": {"paths": [str(self.home / ".cache/macos-cua"),
                                          str(self.home / ".local/share/opencode")]}}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def tree(self, rel: str, age_days: float, files=("a.bin",)) -> Path:
        d = self.home / rel
        d.mkdir(parents=True)
        t = self.now - age_days * DAY
        for name in files:
            f = d / name
            f.write_bytes(b"x")
            os.utime(f, (t, t))
        os.utime(d, (t, t))
        return d

    def classify(self, d: Path) -> tuple[str, str]:
        return mini.classify(d, self.m, self.home, self.now, mini.probe(d, self.now))

    def test_idle_folder_is_candidate(self) -> None:
        d = self.tree("Library/Caches/com.old.app", 45)
        cls, why = self.classify(d)
        self.assertEqual(cls, "candidate")
        self.assertIn("idle 45 days", why)

    def test_recent_folder_is_not_candidate(self) -> None:
        cls, _ = self.classify(self.tree("Library/Caches/com.mid.app", 10))
        self.assertEqual(cls, "recent")

    def test_protected_path_never_candidate(self) -> None:
        d = self.tree(".local/share/opencode", 90)
        cls, why = self.classify(d)
        self.assertEqual(cls, "protected")
        self.assertIn("opencode", why)
        # a parent that holds a protected path is protected too
        self.assertEqual(self.classify(self.home / ".local/share")[0], "protected")

    def test_lock_file_is_live(self) -> None:
        d = self.tree("Library/Application Support/Busy", 60, files=("data.bin", "SingletonLock"))
        cls, why = self.classify(d)
        self.assertEqual(cls, "live")
        self.assertIn("SingletonLock", why)

    def test_socket_is_live(self) -> None:
        d = self.tree("s", 60)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.bind(str(d / "x.s"))
            t = self.now - 60 * DAY
            os.utime(d / "x.s", (t, t))
            cls, why = self.classify(d)
        finally:
            s.close()
        self.assertEqual(cls, "live")
        self.assertIn("socket", why)

    def test_recent_write_is_live(self) -> None:
        d = self.tree("Library/Caches/com.hot.app", 60)
        (d / "new.log").write_text("now")
        self.assertEqual(self.classify(d)[0], "live")

    def test_personal_data_never_candidate(self) -> None:
        for rel in ("Pictures/Old", "Library/Group Containers/group.net.whatsapp.WhatsApp.shared", "Personal"):
            d = self.tree(rel, 400)
            self.assertEqual(self.classify(d)[0], "personal", rel)
        docs = self.home / "Documents"
        docs.mkdir()
        self.assertEqual(self.classify(docs)[0], "personal")

    def test_macos_app_containers_never_entered(self) -> None:
        lib = self.home / "Library"
        for r in mini.MACOS_PRIVATE:
            (self.home / r / "com.other.app").mkdir(parents=True)
        (lib / "Caches").mkdir()
        self.assertNotIn("Library/Group Containers", mini.DRILL)
        for r in mini.MACOS_PRIVATE:
            for p in (self.home / r, self.home / r / "com.other.app"):
                self.assertTrue(mini.macos_private(p, self.home))
                self.assertEqual(mini.classify(p, self.m, self.home, self.now, None),
                                 ("macos-protected", mini.MACOS_PRIVATE_REASON))
        self.assertFalse(mini.macos_private(lib / "Caches", self.home))
        self.assertFalse(mini.macos_private(lib, self.home))
        real_scandir, real_lstat, real_run = os.scandir, os.lstat, mini.subprocess.run
        touched = []

        def guard(p):
            if mini.macos_private(p, self.home):
                touched.append(str(p))

        def scandir(p="."):
            guard(p)
            return real_scandir(p)

        def lstat(p, *a, **k):
            guard(p)
            return real_lstat(p, *a, **k)

        def run(cmd, *a, **k):
            for c in cmd[1:]:
                guard(c)
            return real_run(cmd, *a, **k)

        from unittest import mock
        with mock.patch.object(mini.os, "scandir", scandir), mock.patch.object(mini.os, "lstat", lstat), \
                mock.patch.object(mini.subprocess, "run", run):
            lib_rows = mini.du_children(lib, self.home)
            home_rows = mini.du_children(self.home, self.home)
            mini.probe(lib, self.now, home=self.home)
            for r in mini.MACOS_PRIVATE:
                self.assertEqual(mini.du_children(self.home / r, self.home), [])
        self.assertEqual(touched, [])
        by = {Path(e["path"]).name: e for e in lib_rows}
        self.assertIsNone(by["Containers"]["kb"])
        self.assertIsNone(by["Group Containers"]["kb"])
        self.assertIsNotNone(by["Caches"]["kb"])
        self.assertIsNotNone({Path(e["path"]).name: e for e in home_rows}["Library"]["kb"])

    def test_unprobed_is_unknown(self) -> None:
        self.assertEqual(mini.classify(self.home / "x", self.m, self.home, self.now, None)[0], "unknown")


class TabTest(unittest.TestCase):
    def test_build_from_cache_and_render(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            now = time.time()
            big = str(home / "Library/Caches/com.old.app")
            mini.save({"version": mini.SCAN_VERSION, "scanned_at": now, "home": str(home),
                       "sizes": {str(home / "Library/Caches"): [{"path": big, "kb": 500 * 1024},
                                                               {"path": big + "2", "kb": 10}]},
                       "probes": {big: {"newest": now - 40 * DAY, "live": None, "complete": True}},
                       "apps": [{"name": "Foo", "path": "/Applications/Foo.app", "kb": 2048,
                                 "last_used": "2026-01-02"}]}, mini.cache_path(home))
            agents = home / "Library/LaunchAgents"
            agents.mkdir(parents=True)
            (agents / "x.gone.plist").write_bytes(
                b'<?xml version="1.0"?><plist version="1.0"><dict><key>Label</key><string>x.gone</string>'
                b'<key>ProgramArguments</key><array><string>/nonexistent/bin/tool</string></array></dict></plist>')
            tab = mini.build({}, home, now=now)
            self.assertEqual(tab["state"], "fresh")
            self.assertEqual([c["path"] for c in tab["candidates"]], ["~/Library/Caches/com.old.app"])
            self.assertTrue(tab["agents"][0]["broken"])
            html = mini.render_tab(tab, lambda v: str(v))
            self.assertIn("broken leftover", html)
            self.assertIn("Delete candidates (1", html)


if __name__ == "__main__":
    unittest.main()
