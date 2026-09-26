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


class SettleRespawn(unittest.TestCase):
    ENV = {"BU_NAME": "comet-test0000", "BU_CDP_WS": "ws://x"}

    def test_classify_causes(self):
        self.assertEqual(sp.classify("", "fatal: received 1008 (policy violation) lease already has a Browser Use client", False), "slot_busy")
        self.assertEqual(sp.classify("", "attached x\nenable Page on session-1: \nlistening on s", True), "cdp_slow")
        self.assertEqual(sp.classify("", "connecting to ws://127.0.0.1:1\nlistening on s", True), "probe_slow")
        self.assertEqual(sp.classify("", "connecting to ws://127.0.0.1:1", True), "attach_slow")
        self.assertEqual(sp.classify("", "", True), "spawn_slow")

    def test_slot_busy_backs_off_then_proves(self):
        good = 'SETTLE {"js": 2, "url": "https://www.linkedin.com/feed/", "dialog": null}'
        runs = iter([(1, "browser-harness: ConnectionError: WebSocket connection closed"), (0, good)])
        sleeps, reaps = [], []
        with mock.patch.object(sp, "read_tail", return_value="fatal: received 1008 lease already has a Browser Use client"):
            proof = sp.respawn_and_prove(self.ENV, 60, "linkedin.com", reap=lambda: reaps.append(1) or [9],
                                         probe=lambda *a: next(runs), sleep=sleeps.append)
        self.assertEqual(proof["js"], 2)
        self.assertEqual([a["cause"] for a in proof["attempts"]], ["slot_busy", "ok"])
        self.assertEqual(sleeps, [1.0])
        self.assertEqual(len(reaps), 1)

    def test_slot_busy_is_bounded(self):
        reaps = []
        with mock.patch.object(sp, "read_tail", return_value="lease already has a Browser Use client"):
            with self.assertRaises(sp.GateFail) as ctx:
                sp.respawn_and_prove(self.ENV, 60, "", reap=lambda: reaps.append(1) or [],
                                     probe=lambda *a: (1, "ConnectionError"), sleep=lambda s: None)
        p = ctx.exception.payload
        self.assertEqual(p["cause"], "slot_busy")
        self.assertEqual(len(p["attempts"]), len(sp.SLOT_BUSY_BACKOFF_S) + 1)
        self.assertEqual(len(reaps), len(p["attempts"]))

    def test_timeout_reaps_and_reports_cause_without_retry(self):
        reaps, calls = [], []
        with mock.patch.object(sp, "read_tail", return_value="connecting to ws://127.0.0.1:1\nenable DOM on session-1: "):
            with self.assertRaises(sp.GateFail) as ctx:
                sp.respawn_and_prove(self.ENV, 60, "", reap=lambda: reaps.append(1) or [4242],
                                     probe=lambda *a: calls.append(a[2]) or (None, ""), sleep=lambda s: None)
        p = ctx.exception.payload
        self.assertEqual(p["reason"], "respawn+probe exceeded 60s")
        self.assertEqual(p["cause"], "cdp_slow")
        self.assertEqual(p["reaped"], [4242])
        self.assertEqual(len(calls), 1)

    def test_probe_timeout_kills_the_process_group(self):
        import sys
        child = ("import subprocess,sys,time\n"
                 "g=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
                 "print('GRAND', g.pid, flush=True)\ntime.sleep(60)\n")
        rc, out = sp.run_probe([sys.executable, "-c", child], dict(os.environ), 2)
        self.assertIsNone(rc)
        grand = int(out.split("GRAND", 1)[1].split()[0])
        import time as _t
        deadline = _t.monotonic() + 5
        while _t.monotonic() < deadline and sp.pid_alive(grand):
            try:
                os.waitpid(grand, os.WNOHANG)
            except ChildProcessError:
                pass
            _t.sleep(0.1)
        self.assertFalse(sp.pid_alive(grand))

    def test_run_logs_timings_on_pass_and_fail(self):
        work = Path(tempfile.mkdtemp())
        (work / "browser-use.env").write_text("export BU_CDP_WS=ws://x\nexport BU_NAME=comet-test0000\n")
        rt = Path(tempfile.mkdtemp())
        with mock.patch.dict(os.environ, {"BH_RUNTIME_DIR": str(rt)}), \
             mock.patch.object(sp, "lease_daemons", return_value=[]), \
             mock.patch.object(sp, "respawn_and_prove", return_value={"js": 2, "url": "u", "seconds": 0.8, "attempts": [{"s": 0.8, "rc": 0, "cause": "ok"}]}):
            out = sp.run(work, 60, "", False, None)
        self.assertIn("prove_s", out["timings"])
        self.assertIn("total_s", out["timings"])
        fail = sp.GateFail("prove", "respawn+probe exceeded 60s", cause="cdp_slow")
        with mock.patch.dict(os.environ, {"BH_RUNTIME_DIR": str(rt)}), \
             mock.patch.object(sp, "lease_daemons", return_value=[]), \
             mock.patch.object(sp, "respawn_and_prove", side_effect=fail):
            with self.assertRaises(sp.GateFail) as ctx:
                sp.run(work, 60, "", False, None)
        self.assertIn("total_s", ctx.exception.payload["timings"])
        rows = [json.loads(l) for l in (work / "settle-preflight.jsonl").read_text().splitlines()]
        self.assertEqual([r["ok"] for r in rows], [True, False])
        self.assertEqual(rows[1]["cause"], "cdp_slow")
        self.assertNotIn("ws://", json.dumps(rows))

    def test_kill_waits_for_bridge_slot_and_keeps_daemon_log(self):
        work = Path(tempfile.mkdtemp())
        (work / "browser-use.env").write_text("export BU_CDP_WS=ws://x\nexport BU_NAME=comet-test0000\n")
        rt, tmp = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        (tmp / "bu.log").write_text("connecting to ws://127.0.0.1:1\nReceived duplicate response for request 5\n")
        slept = []
        with mock.patch.dict(os.environ, {"BH_RUNTIME_DIR": str(rt), "BH_TMP_DIR": str(tmp)}), \
             mock.patch.object(sp, "lease_daemons", return_value=[4242]), \
             mock.patch.object(sp, "kill_all", return_value=[4242]), \
             mock.patch.object(sp.time, "sleep", side_effect=slept.append), \
             mock.patch.object(sp, "respawn_and_prove", return_value={"js": 2, "url": "u", "attempts": []}):
            out = sp.run(work, 60, "", False, None)
        self.assertEqual(slept, [sp.SLOT_WAIT_S])
        self.assertIn("slot_wait_s", out["timings"])
        self.assertIn("duplicate response", (work / "settle-daemon-pre-kill.log").read_text())

    def test_default_budget_matches_harness_startup_window(self):
        self.assertEqual(sp.DEFAULT_TIMEOUT_S, 60.0)


if __name__ == "__main__":
    unittest.main()
