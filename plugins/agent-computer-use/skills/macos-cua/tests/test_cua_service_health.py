#!/usr/bin/env python3
"""CUAService socket health: refusing socket -> one guarded relaunch or a loud
error that names the exact recovery command."""
from __future__ import annotations

import fcntl
import os
import signal
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))
import cua_client  # noqa: E402


class CUAServiceHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cuah-", dir="/tmp"))
        self.sock_path = self.tmp / "s.sock"
        # A socket file with no listener: connect() -> ECONNREFUSED (the
        # wedged-service symptom).
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead.bind(str(self.sock_path))
        dead.close()
        app = self.tmp / "CUAService.app"
        app.mkdir()
        self.patches = [
            mock.patch.object(cua_client, "DEFAULT_SOCKET", self.sock_path),
            mock.patch.object(cua_client, "SERVICE_APP", app),
            mock.patch.object(cua_client, "SERVICE_BIN", app / "missing-bin"),
            mock.patch.object(cua_client, "RELAUNCH_LOCK", self.tmp / "relaunch.lock"),
            mock.patch.object(cua_client, "RELAUNCH_STAMP", self.tmp / "relaunch.stamp"),
        ]
        for p in self.patches:
            p.start()
        self.listener = None

    def tearDown(self):
        for p in self.patches:
            p.stop()
        if self.listener is not None:
            self.listener.close()

    def _listen(self):
        """Simulate the relaunched service binding a fresh listening socket."""
        os.unlink(self.sock_path)
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.sock_path))
        self.listener.listen(4)

    def test_refusing_socket_with_live_service_relaunches_once_then_connects(self):
        killed = []
        alive = {4242}

        def fake_kill(pid, sig):
            killed.append((pid, sig))
            alive.discard(pid)

        def fake_run(cmd, **kwargs):
            self.assertEqual(cmd, ["open", str(cua_client.SERVICE_APP)])
            self.assertTrue(kwargs.get("check"))
            self._listen()
            alive.add(5151)
            return mock.Mock(returncode=0)

        with (
            mock.patch.dict(os.environ, {"MACOS_CUA_NO_RELAUNCH": ""}),
            mock.patch.object(cua_client, "service_pids", side_effect=lambda: sorted(alive)),
            mock.patch.object(cua_client.os, "kill", side_effect=fake_kill),
            mock.patch.object(cua_client.subprocess, "run", side_effect=fake_run) as run,
            mock.patch.object(cua_client.CUAClient, "_ensure_service") as spawn,
        ):
            client = cua_client.CUAClient(self.sock_path)
            client.connect(timeout=3)
        client.close()
        run.assert_called_once()
        spawn.assert_not_called()  # never a second instance beside a wedged one
        self.assertEqual(killed, [(4242, signal.SIGTERM)])
        self.assertEqual(client.relaunch, {"relaunched": True, "killed": [4242]})

    def test_refusing_socket_without_relaunch_fails_loud_with_recovery(self):
        with (
            mock.patch.dict(os.environ, {"MACOS_CUA_NO_RELAUNCH": "1"}),
            mock.patch.object(cua_client, "service_pids", return_value=[4242]),
            mock.patch.object(cua_client.os, "kill") as kill,
            mock.patch.object(cua_client.CUAClient, "_ensure_service") as spawn,
        ):
            started = time.monotonic()
            with self.assertRaises(cua_client.CUAServiceUnavailable) as ctx:
                cua_client.CUAClient(self.sock_path).connect(timeout=1.5)
        self.assertLess(time.monotonic() - started, 4)
        message = str(ctx.exception)
        self.assertIn("refusing connections", message)
        self.assertIn(cua_client.RECOVERY_COMMAND, message)
        self.assertIn("MACOS_CUA_NO_RELAUNCH=1", message)
        kill.assert_not_called()
        spawn.assert_not_called()

    def test_stale_socket_without_service_spawns_instead_of_killing(self):
        with (
            mock.patch.object(cua_client, "service_pids", return_value=[]),
            mock.patch.object(cua_client.os, "kill") as kill,
            mock.patch.object(
                cua_client.CUAClient, "_ensure_service", side_effect=self._listen
            ) as spawn,
        ):
            client = cua_client.CUAClient(self.sock_path)
            client.connect(timeout=3)
        client.close()
        spawn.assert_called_once()
        kill.assert_not_called()
        self.assertIsNone(client.relaunch)

    def test_relaunch_respects_cooldown_and_concurrent_lock(self):
        client = cua_client.CUAClient(self.sock_path)
        with (
            mock.patch.dict(os.environ, {"MACOS_CUA_NO_RELAUNCH": ""}),
            mock.patch.object(cua_client, "service_pids", return_value=[4242]),
            mock.patch.object(cua_client.os, "kill") as kill,
            mock.patch.object(cua_client.subprocess, "run") as run,
        ):
            cua_client.RELAUNCH_STAMP.touch()
            cooled = client._guarded_relaunch()
            cua_client.RELAUNCH_STAMP.unlink()
            with open(cua_client.RELAUNCH_LOCK, "w") as held:
                fcntl.flock(held, fcntl.LOCK_EX)
                result = {}
                t = threading.Thread(target=lambda: result.update(client._guarded_relaunch()))
                t.start()
                t.join(5)
        self.assertFalse(cooled["relaunched"])
        self.assertIn("ago", cooled["reason"])
        self.assertEqual(result, {"relaunched": False, "reason": "another agent is relaunching"})
        kill.assert_not_called()
        run.assert_not_called()

    def test_health_requires_a_real_list_apps_round_trip(self):
        fake = mock.Mock()
        fake.call.return_value = []
        fake.relaunch = None
        fake.socket_path = str(self.sock_path)
        with mock.patch.object(cua_client, "CUAClient", return_value=fake):
            with self.assertRaises(cua_client.CUAServiceUnavailable) as ctx:
                cua_client.health()
        self.assertIn(cua_client.RECOVERY_COMMAND, str(ctx.exception))
        fake.close.assert_called_once()
        fake.call.return_value = [{"name": "Finder"}]
        with (
            mock.patch.object(cua_client, "CUAClient", return_value=fake),
            mock.patch.object(cua_client, "service_pids", return_value=[7]),
        ):
            packet = cua_client.health()
        self.assertEqual(packet["apps"], 1)
        self.assertTrue(packet["ok"])


if __name__ == "__main__":
    unittest.main()
