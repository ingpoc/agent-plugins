#!/usr/bin/env python3
"""Tests for scripts/hygiene.py's map integration and scripts/install-hygiene.sh.

`hygiene check` (plain, --strict, --json) refreshes the map without changing its
stdout or exit code; `hygiene map` forces a rewrite and opens it. HOME points at
a temporary fixture, so nothing touches the real ~/.agents.
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hygiene as hy  # noqa: E402
import hygiene_map as hm  # noqa: E402
from test_hygiene_map import Fixture  # noqa: E402

ROWS = [
    {"check": "worktree-placement", "status": "FAIL", "detail": "1 misplaced", "items": []},
    {"check": "disk-floor", "status": "PASS", "detail": "50.0 Gi free (floor 30 Gi)", "items": []},
]


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.fx = Fixture(self.root)
        self.map = self.fx.home / ".agents" / "hygiene" / "map.html"
        report = hy.Report()
        for r in ROWS:
            report.add(r["check"], r["status"], r["detail"], r["items"])
        self.patches = [
            mock.patch.dict(os.environ, {"HOME": str(self.fx.home), "HYGIENE_MINI_BACKGROUND": "0"}),
            mock.patch.object(hy, "load_manifest", return_value=self.fx.manifest),
            mock.patch.object(hy, "run_checks", return_value=report),
            # The plain report header carries the wall clock; pin it so outputs compare.
            mock.patch.object(hy.time, "strftime", return_value="2026-11-03 12:00:00"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        self._tmp.cleanup()

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = hy.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_every_check_mode_refreshes_the_map_once(self) -> None:
        code, out, err = self.run_cli("check", "--json")
        self.assertEqual(code, 0)
        self.assertTrue(out.lstrip().startswith("["))
        self.assertTrue(self.map.is_file())
        self.assertIn("hygiene: map updated", err)
        before = self.map.stat().st_mtime_ns
        for argv, want in ((["check"], 0), (["check", "--strict"], 1), (["check", "--json"], 0)):
            code, _out, err = self.run_cli(*argv)
            self.assertEqual(code, want, argv)
            self.assertNotIn("map updated", err, argv)  # same snapshot: no rewrite
        self.assertEqual(self.map.stat().st_mtime_ns, before)
        (self.fx.wt / "new-file.txt").write_text("z")
        self.run_cli("check")
        self.assertNotEqual(self.map.stat().st_mtime_ns, before)

    def test_map_failure_never_changes_output_or_exit_code(self) -> None:
        clean = {tuple(a): self.run_cli(*a)[:2] for a in (["check", "--no-map"], ["check", "--strict", "--no-map"],
                                                          ["check", "--json", "--no-map"])}
        self.assertFalse(self.map.exists())
        breakers = [
            mock.patch.object(hm, "git_snapshot", side_effect=RuntimeError("scan blew up")),
            mock.patch.object(hm, "atomic_write", side_effect=OSError("disk full")),
            mock.patch.dict(sys.modules, {"hygiene_map": None}),  # not installed
        ]
        for breaker in breakers:
            with breaker:
                for argv, (code, out) in clean.items():
                    got = self.run_cli(*[a for a in argv if a != "--no-map"])
                    self.assertEqual(got[:2], (code, out), argv)
                    self.assertEqual(got[2].count("hygiene: map not updated"), 1, got[2])
                    self.assertEqual(got[2].count("\n"), 1, got[2])

    def test_map_command_forces_rewrite_and_opens(self) -> None:
        self.run_cli("check")
        before = self.map.stat().st_mtime_ns
        with mock.patch.object(hy, "open_map") as opener:
            code, out, err = self.run_cli("map", "--no-open")
            opener.assert_not_called()
        self.assertEqual(code, 0)
        self.assertIn(f"map: {self.map}", out)
        self.assertIn("map updated", err)
        self.assertNotEqual(self.map.stat().st_mtime_ns, before)
        with mock.patch.object(hy, "open_map") as opener:
            self.assertEqual(self.run_cli("map")[0], 0)
            opener.assert_called_once_with(self.map)

    def test_open_map_uses_macos_open_only_on_darwin(self) -> None:
        with mock.patch.object(hy.subprocess, "run") as run, mock.patch.object(hy.sys, "platform", "darwin"):
            hy.open_map(self.map)
        self.assertEqual(run.call_args.args[0], ["open", str(self.map)])
        with mock.patch.object(hy.subprocess, "run") as run, mock.patch.object(hy.sys, "platform", "linux"), \
                contextlib.redirect_stderr(io.StringIO()):
            hy.open_map(self.map)
        run.assert_not_called()

    def test_map_command_reports_failure_in_exit_code(self) -> None:
        with mock.patch.object(hm, "atomic_write", side_effect=OSError("disk full")):
            code, out, err = self.run_cli("map", "--no-open")
        self.assertEqual(code, 1)
        self.assertIn("map not updated: OSError: disk full", err)


class InstallTest(unittest.TestCase):
    def test_installs_both_files_backs_up_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            bin_dir, link_dir = t / "bin", t / "links"
            bin_dir.mkdir()
            (bin_dir / "hygiene.py").write_text("#!/usr/bin/env python3\nprint('old')\n")
            env = {**os.environ, "AGENTS_BIN": str(bin_dir), "HYGIENE_LINK_DIR": str(link_dir)}
            script = HERE / "install-hygiene.sh"
            for _ in range(2):
                proc = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, check=False)
                self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            for name in ("hygiene.py", "hygiene_map.py", "hygiene_mini.py"):
                self.assertEqual((bin_dir / name).read_bytes(), (HERE / name).read_bytes())
                self.assertTrue(os.access(bin_dir / name, os.X_OK))
            backups = sorted(p.name for p in bin_dir.glob("hygiene.py.bak-*"))
            self.assertEqual(len(backups), 1)
            self.assertIn("print('old')", (bin_dir / backups[0]).read_text())
            self.assertEqual((link_dir / "hygiene").resolve(), (bin_dir / "hygiene.py").resolve())
            self.assertEqual([p.name for p in bin_dir.iterdir() if p.name.startswith(".")], [])


if __name__ == "__main__":
    unittest.main()
