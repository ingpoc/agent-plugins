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
            out = sp.run(work, 75, "", False, None)
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

    def test_default_budget_leaves_harness_startup_window_for_probe(self):
        # one overall deadline: the probe still gets about the harness's 60 s startup window
        self.assertEqual(sp.DEFAULT_TIMEOUT_S, 75.0)
        self.assertGreaterEqual(sp.DEFAULT_TIMEOUT_S - sp.CLEANUP_RESERVE_S, 60.0)


def _rt() -> dict[str, str]:
    """Private harness home so sweep tests never touch ~/.config/browser-harness/runtime."""
    return {"BH_HOME": tempfile.mkdtemp()}


def _orphan_daemon(name: str) -> int:
    """A fake browser_harness daemon in its own session, reparented away from the test (like the
    real harness daemon), carrying BU_NAME=<name>. Returns its pid."""
    import subprocess, sys
    launcher = ("import subprocess,sys\n"
                "g=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)','browser_harness.daemon'],"
                "start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\nprint(g.pid, flush=True)\n")
    out = subprocess.run([sys.executable, "-c", launcher], capture_output=True, text=True,
                         env={**os.environ, "BU_NAME": name}, timeout=10).stdout
    return int(out.split()[0])


class SettleCleanupAndDeadline(unittest.TestCase):
    ENV = {"BU_NAME": "comet-test0000", "BU_CDP_WS": "ws://x"}

    def work(self, body="export BU_CDP_WS=ws://x\nexport BU_NAME=comet-test0000\n") -> Path:
        d = Path(tempfile.mkdtemp())
        (d / "browser-use.env").write_text(body)
        return d

    def test_reap_kills_own_session_daemon_and_verifies(self):
        import time as _t
        name = f"comet-reap{os.getpid()}"
        pid = _orphan_daemon(name)
        try:
            _t.sleep(0.3)
            killed = sp.reap_lease(name, Path("/nonexistent/bu.pid"), _t.monotonic() + 20)
            self.assertEqual(killed, [pid])
            self.assertFalse(sp.pid_alive(pid))
        finally:
            if sp.pid_alive(pid):
                os.kill(pid, 9)

    def test_reap_survivor_is_cleanup_failed(self):
        with mock.patch.object(sp, "lease_daemons", side_effect=[[4242], [4242]]), \
             mock.patch.object(sp, "kill_all", return_value=[4242]):
            with self.assertRaises(sp.GateFail) as ctx:
                sp.reap_lease("comet-test0000", Path("/x"), 1e12)
        p = ctx.exception.payload
        self.assertEqual((p["stage"], p["cause"], p["survivors"]), ("cleanup", "cleanup_failed", [4242]))
        with mock.patch.object(sp, "lease_daemons", return_value=[4242]), \
             mock.patch.object(sp, "kill_all", side_effect=sp.GateFail("kill", "daemon survived SIGKILL", survivors=[4242])):
            with self.assertRaises(sp.GateFail) as ctx:
                sp.reap_lease("comet-test0000", Path("/x"), 1e12)
        self.assertEqual(ctx.exception.payload["cause"], "cleanup_failed")

    def test_cleanup_failure_is_classified_and_never_retried(self):
        calls = []
        def bad_reap():
            raise sp.cleanup_fail("lease daemon still present after kill", survivors=[4242])
        with mock.patch.object(sp, "read_tail", return_value="lease already has a Browser Use client"):
            with self.assertRaises(sp.GateFail) as ctx:
                sp.respawn_and_prove(self.ENV, 60, "", reap=bad_reap,
                                     probe=lambda *a: calls.append(1) or (1, "ConnectionError"), sleep=lambda s: None)
        p = ctx.exception.payload
        self.assertEqual((p["stage"], p["cause"], p["probe_cause"]), ("cleanup", "cleanup_failed", "slot_busy"))
        self.assertEqual(len(calls), 1)  # slot_busy retry only after a verified reap

    def test_deadline_running_out_during_cleanup(self):
        with mock.patch.object(sp, "kill_all", return_value=[]):
            with self.assertRaises(sp.GateFail) as ctx:
                sp.reap_lease("comet-test0000", Path("/x"), 0.0)  # monotonic deadline already passed
        self.assertEqual(ctx.exception.payload["cause"], "cleanup_failed")
        self.assertIn("Exhausted", ctx.exception.payload["detail"])

    def test_discovery_timeouts_map_to_exhausted(self):
        import subprocess
        def slow(timeout):
            raise subprocess.TimeoutExpired("ps", timeout)
        with self.assertRaises(sp.Exhausted):
            sp.lease_daemons("n", None, deadline=1e12, lister=slow)
        seen = []
        sp.lease_daemons("n", None, procs=[(1, "python -m browser_harness.daemon")],
                         env_has=lambda *a, **k: seen.append(k["timeout"]) or False, deadline=1e12)
        self.assertEqual(seen, [sp.DISCOVERY_CAP_S])

    def test_discovery_deadline_fails_discovery_slow_and_logs(self):
        work = self.work()
        with mock.patch.object(sp, "lease_daemons", side_effect=sp.Exhausted("deadline passed")), \
             mock.patch.object(sp, "respawn_and_prove") as prove:
            with self.assertRaises(sp.GateFail) as ctx:
                sp.run(work, 75, "", False, None)
        p = ctx.exception.payload
        self.assertEqual((p["stage"], p["cause"]), ("discovery", "discovery_slow"))
        prove.assert_not_called()
        rows = [json.loads(l) for l in (work / "settle-preflight.jsonl").read_text().splitlines()]
        self.assertEqual(rows[-1]["cause"], "discovery_slow")

    def test_probe_budget_is_what_the_overall_deadline_leaves(self):
        work, rt = self.work(), Path(tempfile.mkdtemp())
        seen = {}
        def prove(env, budget, url, **kw):
            seen["budget"] = budget
            return {"js": 2, "url": "u", "attempts": []}
        with mock.patch.dict(os.environ, {"BH_RUNTIME_DIR": str(rt)}), \
             mock.patch.object(sp, "lease_daemons", return_value=[]), \
             mock.patch.object(sp, "respawn_and_prove", side_effect=prove):
            out = sp.run(work, 75, "", False, None)
        self.assertTrue(60 <= seen["budget"] <= 75 - sp.CLEANUP_RESERVE_S)
        self.assertIn("discovery_s", out["timings"])

    def test_probe_pid_file_exists_only_while_probe_runs(self):
        import sys
        d = Path(tempfile.mkdtemp())
        pf = d / sp.PROBE_PID_FILE
        child = f"import time; time.sleep(0.5); print('PF', open({str(pf)!r}).read())"
        rc, out = sp.run_probe([sys.executable, "-c", child], dict(os.environ), 10, pid_path=pf)
        self.assertEqual(rc, 0)
        self.assertIn('"pgid"', out)
        self.assertFalse(pf.exists())

    def test_reap_only_kills_recorded_probe_group_but_not_a_reused_pid(self):
        import subprocess, sys
        work = self.work()
        probe = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "browser-use"], start_new_session=True)
        (work / sp.PROBE_PID_FILE).write_text(json.dumps({"pgid": probe.pid, "cmd": "uvx"}))
        with mock.patch.object(sp, "lease_daemons", return_value=[]):
            out = sp.reap_only(work)
        self.assertTrue(out["probe"]["killed"])
        self.assertEqual(probe.wait(timeout=5), -9)
        self.assertFalse((work / sp.PROBE_PID_FILE).exists())
        other = subprocess.Popen(["sleep", "30"], start_new_session=True)
        try:
            (work / sp.PROBE_PID_FILE).write_text(json.dumps({"pgid": other.pid, "cmd": "uvx"}))
            with mock.patch.object(sp, "lease_daemons", return_value=[]):
                out = sp.reap_only(work)
            self.assertFalse(out["probe"]["killed"])
            self.assertIsNone(other.poll())
        finally:
            other.kill()
            other.wait()

    def test_redacts_jsonl_payload_and_daemon_log_copies(self):
        secret_ws = "ws://127.0.0.1:9222/devtools/browser/SECRET-abc123"
        work = self.work(f"export BU_CDP_WS='{secret_ws}'\nexport BU_NAME=comet-test0000\n")
        rt, tmp = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        (tmp / "bu.log").write_text(f"connecting to {secret_ws}\nattach wss://bridge.local/x?token=tok999\n")
        fail = sp.GateFail("prove", "browser-use exited 1", cause="attach_failed",
                           tail=f"error {secret_ws} api_key=k777", daemon_log="wss://h/y?token=tok999")
        with mock.patch.dict(os.environ, {"BH_RUNTIME_DIR": str(rt), "BH_TMP_DIR": str(tmp)}), \
             mock.patch.object(sp, "lease_daemons", return_value=[4242]), \
             mock.patch.object(sp, "kill_all", return_value=[4242]), \
             mock.patch.object(sp.time, "sleep"), \
             mock.patch.object(sp, "respawn_and_prove", side_effect=fail):
            with self.assertRaises(sp.GateFail):
                sp.run(work, 75, "", False, None)
        blob = (work / "settle-preflight.jsonl").read_text() + (work / "settle-daemon-pre-kill.log").read_text()
        for leak in ("SECRET-abc123", "tok999", "k777", "127.0.0.1:9222"):
            self.assertNotIn(leak, blob)
        self.assertIn("[redacted]", blob)
        self.assertEqual(sp.redact("see wss://a/b?token=1 and key=2"), "see wss://[redacted] and key=[redacted]")


class LeaseCloseoutAndSweep(unittest.TestCase):
    def test_close_lease_daemons_kills_real_daemon_verifies_and_logs(self):
        import time as _t
        name = f"comet-close{os.getpid()}"
        d = Path(tempfile.mkdtemp())
        (d / "browser-use.env").write_text(f"export BU_CDP_WS=ws://127.0.0.1:1/x\nexport BU_NAME={name}\n")
        pid = _orphan_daemon(name)
        try:
            _t.sleep(0.3)
            res = sp.close_lease_daemons(d)
            self.assertEqual(res["killed"], [pid])
            self.assertTrue(res["verified_absent"])
            self.assertFalse(sp.pid_alive(pid))
            rows = [json.loads(x) for x in (d / "settle-preflight.jsonl").read_text().splitlines()]
            self.assertTrue(rows[-1]["closeout"])
        finally:
            if sp.pid_alive(pid):
                os.kill(pid, 9)

    def test_close_lease_daemons_removes_killed_daemons_runtime_pair(self):
        import time as _t
        name = f"comet-rt{os.getpid()}"
        home = Path(tempfile.mkdtemp())
        d = Path(tempfile.mkdtemp())
        (d / "browser-use.env").write_text(
            f"export BU_CDP_WS=ws://127.0.0.1:1/x\nexport BU_NAME={name}\nexport BH_HOME={home}\n")
        pid = _orphan_daemon(name)
        pid_file, sock_file = sp.runtime_files(name, {"BH_HOME": str(home)})
        pid_file.parent.mkdir(parents=True)
        pid_file.write_text(f"{pid}\n")
        sock_file.write_text("")
        try:
            _t.sleep(0.3)
            res = sp.close_lease_daemons(d)
            self.assertEqual(res["killed"], [pid])
            self.assertEqual(sorted(res["cleared"]), sorted([str(pid_file), str(sock_file)]))
            self.assertFalse(pid_file.exists() or sock_file.exists())
        finally:
            if sp.pid_alive(pid):
                os.kill(pid, 9)

    def test_clear_runtime_files_never_touches_a_live_owner_it_did_not_kill(self):
        home = Path(tempfile.mkdtemp())
        env = {"BH_HOME": str(home)}
        pid_file, sock_file = sp.runtime_files("comet-abcdef0123", env)
        pid_file.parent.mkdir(parents=True)
        pid_file.write_text(f"{os.getpid()}\n")  # alive, not killed: pid reuse / someone else's
        sock_file.write_text("")
        self.assertEqual(sp.clear_runtime_files("comet-abcdef0123", env), [])
        self.assertTrue(pid_file.exists() and sock_file.exists())
        # Shared (non lease-scoped) stem with no pid file: never guess.
        shared = {"BH_RUNTIME_DIR": str(home / "shared")}
        spid, ssock = sp.runtime_files("comet-abcdef0123", shared)
        spid.parent.mkdir(parents=True)
        ssock.write_text("")
        self.assertEqual(sp.clear_runtime_files("comet-abcdef0123", shared), [])
        self.assertTrue(ssock.exists())

    def test_sweep_stale_runtime_files_clears_only_dead_pairs_of_gone_leases(self):
        import subprocess, sys
        home = Path(tempfile.mkdtemp())
        env = {"BH_HOME": str(home)}
        dead = subprocess.Popen([sys.executable, "-c", "pass"]); dead.wait()
        rows = {"comet-" + "a1" * 5: dead.pid,            # gone lease, dead pid -> clear
                sp.lease_name("live-session"): dead.pid,  # live lease -> keep
                "comet-" + "b2" * 5: os.getpid()}         # live pid -> keep
        for name, pid in rows.items():
            pf, sf = sp.runtime_files(name, env)
            pf.parent.mkdir(parents=True, exist_ok=True)
            pf.write_text(f"{pid}\n")
            sf.write_text("")
        live = {sp.lease_name("live-session")}
        dry = sp.sweep_stale_runtime_files(live, dry_run=True, environ=env)
        self.assertEqual([x["bu_name"] for x in dry], ["comet-" + "a1" * 5])
        self.assertTrue(sp.runtime_files("comet-" + "a1" * 5, env)[0].exists())
        res = sp.sweep_stale_runtime_files(live, environ=env)
        self.assertEqual([x["bu_name"] for x in res], ["comet-" + "a1" * 5])
        self.assertEqual(len(res[0]["cleared"]), 2)
        left = sorted(f.name for f in (home / "runtime").iterdir())
        self.assertEqual(len(left), 4)
        self.assertNotIn("bu-comet-" + "a1" * 5 + ".pid", left)

    def test_sweep_kills_only_comet_daemons_of_gone_leases(self):
        import time as _t
        dead = "comet-" + "ab" * 5
        live = sp.lease_name("live-session")
        other = f"notcomet{os.getpid()}"
        pids = {n: _orphan_daemon(n) for n in (dead, live, other)}
        mine = set(pids.values())
        try:
            _t.sleep(0.3)
            lister = lambda t: [row for row in sp.list_processes(t) if row[0] in mine]
            inv = lambda: [{"session_id": "live-session"}]
            dry = sp.sweep_stale(inv, dry_run=True, lister=lister, listening=lambda ws: False, runtime_environ=_rt())
            self.assertEqual([x["pid"] for x in dry["stale"]], [pids[dead]])
            self.assertTrue(all(sp.pid_alive(p) for p in mine))  # dry run kills nothing
            res = sp.sweep_stale(inv, lister=lister, listening=lambda ws: False, runtime_environ=_rt())
            self.assertEqual(res["killed"], [pids[dead]])
            self.assertEqual(res["kept"], 2)
            self.assertFalse(sp.pid_alive(pids[dead]))
            self.assertTrue(sp.pid_alive(pids[live]) and sp.pid_alive(pids[other]))
        finally:
            for p in mine:
                if sp.pid_alive(p):
                    os.kill(p, 9)

    def test_sweep_keeps_daemon_whose_bridge_still_listens_and_fails_closed(self):
        env = {"BU_NAME": "comet-" + "cd" * 5, "BU_CDP_WS": "ws://127.0.0.1:1/x"}
        with mock.patch.object(sp, "kill_all") as kill:
            res = sp.sweep_stale(lambda: [], lister=lambda t: [(4242, "python -m browser_harness.daemon")],
                                 env_of=lambda pid, timeout: env, listening=lambda ws: True,
                                 runtime_environ=_rt())
            self.assertEqual(res["stale"], [])
            def boom():
                raise OSError("socket gone")
            with self.assertRaises(SystemExit) as ctx:
                sp.sweep_stale(boom, lister=lambda t: [(4242, "browser_harness.daemon")], env_of=lambda p, timeout: env,
                               listening=lambda ws: False, runtime_environ=_rt())
            self.assertIn("killed nothing", ctx.exception.payload["reason"])
            kill.assert_not_called()

    def test_bridge_listening_detects_closed_port_and_doubts_as_alive(self):
        import socket
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        self.assertTrue(sp.bridge_listening(f"ws://127.0.0.1:{port}/devtools/browser/x"))
        srv.close()
        self.assertFalse(sp.bridge_listening(f"ws://127.0.0.1:{port}/devtools/browser/x"))
        self.assertTrue(sp.bridge_listening(None))

    def test_lease_name_mirrors_bridge(self):
        import hashlib
        self.assertEqual(sp.lease_name("s1"), "comet-" + hashlib.sha256(b"s1").hexdigest()[:10])
        src = (PLUGIN_ROOT / "skills/comet-control/scripts/browser_use_cdp_bridge.py").read_text()
        self.assertIn('self.name = f"comet-{digest[:10]}"', src)


if __name__ == "__main__":
    unittest.main()
