"""Agent-friendly CLI contract for durable_lease_controller (bookmark #124/#138).

JSON on every path (including usage errors), ``hint`` on known errors, fixed
exit codes, ``--dry-run`` that changes nothing, and repeats that never mint a
second session.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = PLUGIN_ROOT / "skills" / "comet-control" / "scripts"
CTRL_PATH = SCRIPTS / "durable_lease_controller.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _run_cli(*argv: str) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, str(CTRL_PATH), *argv],
        capture_output=True,
        text=True,
        timeout=30,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    return proc.returncode, json.loads("\n".join(lines))


def _call(fn, args) -> tuple[int, dict]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = fn(args)
    return rc, json.loads(buf.getvalue())


class AgentCliHelperTests(unittest.TestCase):
    def setUp(self):
        self.cli = _load("agent_cli", SCRIPTS / "agent_cli.py")

    def test_exit_codes_are_distinct(self):
        codes = [self.cli.EXIT_OK, self.cli.EXIT_FAIL, self.cli.EXIT_LEASE, self.cli.EXIT_USAGE]
        self.assertEqual(len(set(codes)), 4)
        self.assertEqual(self.cli.EXIT_USAGE, 64)

    def test_error_key_strips_detail(self):
        self.assertEqual(self.cli.error_key("invalid_payload_json: Expecting value"), "invalid_payload_json")

    def test_with_hint_keeps_existing_hint(self):
        out = self.cli.with_hint({"error": "controller_died", "hint": "mine"})
        self.assertEqual(out["hint"], "mine")

    def test_unknown_error_gets_no_hint(self):
        self.assertNotIn("hint", self.cli.with_hint({"error": "novel"}))

    def test_no_hint_suggests_remint(self):
        for key, hint in self.cli.HINTS.items():
            low = hint.lower()
            self.assertNotIn("remint", low, key)
            self.assertNotIn("new session", low, key)
            if key in {"controller_not_alive", "controller_died", "timeout_waiting_response",
                       "timeout_waiting_request_slot", "closeout_unverified", "status_unhealthy"}:
                self.assertIn("do not start a second session", low, key)

    def test_summarize_payload_masks_values(self):
        body = {"actions": [{"type": "fill", "selector": "#pw", "text": "hunter2"}, {"type": "page_context"}]}
        out = self.cli.summarize_payload(body)
        self.assertEqual(out, {"action_count": 2, "action_types": ["fill", "page_context"]})
        self.assertNotIn("hunter2", json.dumps(out))


class ControllerCliContractTests(unittest.TestCase):
    def setUp(self):
        self.ctrl = _load("durable_lease_controller", CTRL_PATH)

    def test_usage_error_is_json_exit_64(self):
        rc, out = _run_cli("send")  # missing --workdir
        self.assertEqual(rc, 64)
        self.assertEqual(out["error"], "usage_error")
        self.assertIn("hint", out)
        rc, out = _run_cli("frobnicate")
        self.assertEqual(rc, 64)

    def test_json_flag_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, out = _run_cli("status", "--workdir", tmp, "--json")
            self.assertEqual(rc, 1)
            self.assertFalse(out["ok"])
            self.assertIn("hint", out)

    def test_send_empty_and_invalid_payload_exit_64(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "controller.alive").write_text("1")
            for payload, err in (("", "empty_payload"), ("{nope", "invalid_payload_json"), ("[1]", "payload_not_object")):
                args = SimpleNamespace(workdir=tmp, payload=payload, payload_file=None, timeout=5, dry_run=False)
                rc, out = _call(self.ctrl.cmd_send, args)
                self.assertEqual(rc, 64, payload)
                self.assertTrue(out["error"].startswith(err))
                self.assertIn("hint", out)
            self.assertFalse(Path(tmp, "request.json").exists())
            self.assertFalse(Path(tmp, "seq").exists())

    def test_send_dry_run_writes_nothing_and_masks(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "controller.alive").write_text("1")
            before = sorted(os.listdir(tmp))
            payload = json.dumps({"actions": [{"type": "fill", "text": "s3cret"}]})
            args = SimpleNamespace(workdir=tmp, payload=payload, payload_file=None, timeout=60, dry_run=True)
            rc, out = _call(self.ctrl.cmd_send, args)
            self.assertEqual(rc, 0)
            self.assertTrue(out["dry_run"])
            self.assertEqual(out["payload"]["action_types"], ["fill"])
            self.assertEqual(out["timeoutSeconds"], 60)
            self.assertNotIn("s3cret", json.dumps(out))
            self.assertEqual(sorted(os.listdir(tmp)), before)

    def test_send_dry_run_flags_dead_controller_and_empty_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(workdir=tmp, payload='{"actions":[{"type":"page_context"}]}',
                                   payload_file=None, timeout=60, dry_run=True)
            rc, out = _call(self.ctrl.cmd_send, args)
            self.assertEqual(rc, 0)
            self.assertFalse(out["controller_alive"])
            self.assertIn("second session", out["hint"])
            args.payload = '{"actions":[]}'
            rc, out = _call(self.ctrl.cmd_send, args)
            self.assertEqual((rc, out["error"]), (64, "actions_missing"))

    def test_send_dead_controller_has_hint_exit_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(workdir=tmp, payload='{"actions":[{"type":"page_context"}]}',
                                   payload_file=None, timeout=1, dry_run=False)
            with patch.object(self.ctrl, "_live_controller_pid", return_value=None):
                rc, out = _call(self.ctrl.cmd_send, args)
            self.assertEqual((rc, out["error"]), (1, "controller_not_alive"))
            self.assertIn("escalate to ACU", out["hint"])

    def test_start_dry_run_spawn_creates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "w"
            args = SimpleNamespace(workdir=str(work), session_id="s1", dry_run=True)
            with patch.object(self.ctrl.subprocess, "Popen") as popen:
                rc, out = _call(self.ctrl.cmd_start, args)
            popen.assert_not_called()
            self.assertEqual((rc, out["would"]), (0, "spawn_controller"))
            self.assertIn("never after", out["hint"])
            self.assertFalse(work.exists())

    def test_start_dry_run_reuse_flags_session_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "ready.json").write_text(json.dumps({"ok": True, "session_id": "old"}))
            args = SimpleNamespace(workdir=tmp, session_id="new", dry_run=True)
            with patch.object(self.ctrl, "_live_controller_pid", return_value=4242):
                rc, out = _call(self.ctrl.cmd_start, args)
            self.assertEqual(out["would"], "reuse_live_controller")
            self.assertTrue(out["session_mismatch"])
            self.assertEqual(rc, 0)

    def test_start_repeat_reuses_live_controller_never_spawns(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "ready.json").write_text(json.dumps({"ok": True, "session_id": "s1"}))
            args = SimpleNamespace(workdir=tmp, session_id="s1", socket="/nonexistent.sock", dry_run=False)
            with patch.object(self.ctrl, "_live_controller_pid", return_value=4242), \
                 patch.object(self.ctrl, "_repair_heartbeat", return_value={"ok": True, "session_id": "s1"}), \
                 patch.object(self.ctrl.subprocess, "Popen") as popen:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = self.ctrl.cmd_start(args)
            popen.assert_not_called()
            out = json.loads(buf.getvalue())
            self.assertEqual(rc, 0)
            self.assertTrue(out["reused"])
            self.assertNotIn("session_mismatch", out)

    def test_closeout_dry_run_sends_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "controller.alive").write_text("1")
            args = SimpleNamespace(workdir=tmp, timeout=5, dry_run=True)
            with patch.object(self.ctrl, "cmd_send") as send:
                rc, out = _call(self.ctrl.cmd_closeout, args)
            send.assert_not_called()
            self.assertEqual((rc, out["would"]), (0, "send_closeout"))
            self.assertFalse(Path(tmp, "response.json").exists())

    def test_help_marks_start_here(self):
        proc = subprocess.run([sys.executable, str(CTRL_PATH), "--help"], capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("(start here)", proc.stdout)
        self.assertIn("64 usage", proc.stdout)


if __name__ == "__main__":
    unittest.main()
