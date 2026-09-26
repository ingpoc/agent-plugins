#!/usr/bin/env python3
"""Offline tests for settle_preflight.py (no Comet, no daemon, no browser)."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = PLUGIN_ROOT / "skills/comet-control/scripts/settle_preflight.py"


def load():
    spec = importlib.util.spec_from_file_location("settle_preflight", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sp = load()


class SettlePreflight(unittest.TestCase):
    def work(self, body: str) -> Path:
        d = Path(tempfile.mkdtemp())
        (d / "browser-use.env").write_text(body)
        return d

    def test_reads_bu_name_without_exposing_ws(self):
        env = sp.read_lease_env(self.work("export BU_CDP_WS='ws://127.0.0.1:1/devtools/browser/x'\nexport BU_NAME=comet-70ab542fd7\n"))
        self.assertEqual(env["BU_NAME"], "comet-70ab542fd7")

    def test_missing_name_fails_closed(self):
        with self.assertRaises(sp.GateFail) as ctx:
            sp.read_lease_env(self.work("export BU_CDP_WS=ws://x\n"))
        self.assertEqual(ctx.exception.payload["gate"], "li_settle_preflight")
        self.assertEqual(ctx.exception.payload["stage"], "lease_env")

    def test_runtime_files_match_browser_harness_layout(self):
        pid, sock = sp.runtime_files("comet-70ab542fd7", {})
        self.assertEqual(pid, Path.home() / ".config/browser-harness/runtime/bu-comet-70ab542fd7.pid")
        self.assertEqual(sock.name, "bu-comet-70ab542fd7.sock")
        pid, _ = sp.runtime_files("n", {"BH_RUNTIME_DIR": "/tmp/rt"})
        self.assertEqual(pid, Path("/tmp/rt/bu.pid"))

    def test_pid_file_json_or_plain(self):
        d = Path(tempfile.mkdtemp())
        (d / "a").write_text(json.dumps({"pid": 56885, "started": "x"}))
        (d / "b").write_text("23032\n")
        self.assertEqual(sp.pid_from_file(d / "a"), 56885)
        self.assertEqual(sp.pid_from_file(d / "b"), 23032)
        self.assertIsNone(sp.pid_from_file(d / "missing"))

    def test_only_this_leases_daemons_are_selected(self):
        procs = [
            (56885, "/x/bin/python -m browser_harness.daemon"),
            (60001, "/x/bin/python -m browser_harness.daemon"),
            (60002, "/x/bin/python -m browser_harness.daemon"),
            (700, "/Applications/Comet.app/Contents/MacOS/Comet"),
            (701, "python durable_lease_controller.py --browser-use"),
        ]
        envs = {56885: "comet-70ab542fd7", 60001: "comet-other", 60002: "comet-70ab542fd7"}
        has = lambda pid, k, v: envs.get(pid) == v
        self.assertEqual(sp.lease_daemons("comet-70ab542fd7", None, procs, has), [56885, 60002])
        # pid-file pid counts only when it is a browser_harness daemon
        self.assertEqual(sp.lease_daemons("comet-70ab542fd7", 700, procs, lambda *_: False), [])
        self.assertEqual(sp.lease_daemons("comet-70ab542fd7", 60001, procs, lambda *_: False), [60001])

    def test_probe_requires_two_and_url(self):
        ok = sp.parse_probe('noise\nSETTLE {"js": 2, "url": "https://www.linkedin.com/article/new/", "title": "t", "dialog": null}', "linkedin.com")
        self.assertEqual(ok["js"], 2)
        for bad in ['SETTLE {"js": 3, "url": "u"}', 'SETTLE {"js": 2, "url": ""}',
                    'SETTLE {"js": 2, "url": "u", "dialog": {"type": "alert"}}', "no line"]:
            with self.assertRaises(sp.GateFail):
                sp.parse_probe(bad)

    def test_dry_run_kills_nothing(self):
        work = self.work("export BU_CDP_WS=ws://x\nexport BU_NAME=comet-test0000\n")
        with mock.patch.object(sp, "lease_daemons", return_value=[4242]), \
             mock.patch.object(sp, "kill_all") as kill, \
             mock.patch.object(sp, "respawn_and_prove") as prove:
            out = sp.run(work, 5, "", True, None)
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["daemons"], [4242])
        kill.assert_not_called()
        prove.assert_not_called()

    def test_clears_dead_runtime_files_then_proves(self):
        rt = Path(tempfile.mkdtemp())
        work = self.work("export BU_CDP_WS=ws://x\nexport BU_NAME=comet-test0000\n")
        (rt / "bu.pid").write_text("999999")
        (rt / "bu.sock").write_text("")
        with mock.patch.dict(os.environ, {"BH_RUNTIME_DIR": str(rt)}), \
             mock.patch.object(sp, "lease_daemons", return_value=[]), \
             mock.patch.object(sp, "pid_alive", return_value=False), \
             mock.patch.object(sp, "respawn_and_prove", return_value={"js": 2, "url": "u"}):
            out = sp.run(work, 5, "", False, None)
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["cleared"]), 2)
        self.assertFalse((rt / "bu.pid").exists())

    def test_live_non_daemon_pid_file_fails_closed(self):
        rt = Path(tempfile.mkdtemp())
        work = self.work("export BU_CDP_WS=ws://x\nexport BU_NAME=comet-test0000\n")
        (rt / "bu.pid").write_text(str(os.getpid()))
        with mock.patch.dict(os.environ, {"BH_RUNTIME_DIR": str(rt)}), \
             mock.patch.object(sp, "lease_daemons", return_value=[]), \
             mock.patch.object(sp, "respawn_and_prove") as prove:
            with self.assertRaises(sp.GateFail) as ctx:
                sp.run(work, 5, "", False, None)
        self.assertEqual(ctx.exception.payload["stage"], "runtime_files")
        prove.assert_not_called()


if __name__ == "__main__":
    unittest.main()
