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
            mock.patch.object(cua_client, "owner_pids", side_effect=lambda: sorted(alive)),
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
            mock.patch.object(cua_client, "owner_pids", return_value=[4242]),
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
            mock.patch.object(cua_client, "owner_pids", return_value=[]),
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
        self.assertEqual(client.relaunch, {"spawned": True, "probe": "ConnectionRefusedError"})

    def test_relaunch_cooldown_blocks_a_second_kill(self):
        client = cua_client.CUAClient(self.sock_path)
        with (
            mock.patch.dict(os.environ, {"MACOS_CUA_NO_RELAUNCH": ""}),
            mock.patch.object(cua_client, "owner_pids", return_value=[4242]),
            mock.patch.object(cua_client.os, "kill") as kill,
            mock.patch.object(cua_client.subprocess, "run") as run,
        ):
            cua_client.RELAUNCH_STAMP.touch()
            cooled = client._recover()
        self.assertFalse(cooled["relaunched"])
        self.assertIn("ago", cooled["reason"])
        kill.assert_not_called()
        run.assert_not_called()

    def test_waiter_rechecks_under_lock_and_never_spawns_beside_a_peer(self):
        """Codex: a lock loser must not spawn during the winner's TERM->open gap."""
        client = cua_client.CUAClient(self.sock_path)
        with (
            mock.patch.object(cua_client, "owner_pids", return_value=[]),
            mock.patch.object(cua_client.os, "kill") as kill,
            mock.patch.object(cua_client.CUAClient, "_ensure_service") as spawn,
        ):
            with open(cua_client.RELAUNCH_LOCK, "w") as held:
                fcntl.flock(held, fcntl.LOCK_EX)
                result = {}
                t = threading.Thread(target=lambda: result.update(client._recover()))
                t.start()
                time.sleep(0.4)  # waiter is blocked on the lock, socket still dead
                self.assertTrue(t.is_alive())
                self._listen()  # the peer heals, then releases
            t.join(5)
        self.assertEqual(result, {"recovered": True, "by": "peer"})
        spawn.assert_not_called()
        kill.assert_not_called()

    def test_custom_socket_never_kills_any_instance(self):
        other = self.tmp / "other.sock"
        with (
            mock.patch.object(cua_client, "owner_pids", return_value=[4242]),
            mock.patch.object(cua_client.os, "kill") as kill,
            mock.patch.object(cua_client.CUAClient, "_ensure_service") as spawn,
        ):
            result = cua_client.CUAClient(other)._recover()
        kill.assert_not_called()
        spawn.assert_called_once()
        self.assertTrue(result["spawned"])

    def test_owner_pids_only_matches_the_default_socket_install(self):
        bin_ = str(cua_client.SERVICE_BIN)
        out = (
            f"  101 {bin_}\n"
            f"  102 {bin_} --socket-path {self.sock_path}\n"
            f"  103 {bin_} --socket-path /tmp/elsewhere.sock\n"
            "  104 /Applications/Other/CUAService\n"
            f"  105 {bin_}-other\n"
            f"  106 {bin_} --socket-path=/tmp/custom.sock\n"
            f"  107 {bin_} --socket-path={self.sock_path}\n"
        )
        ok = mock.Mock(returncode=0, stdout=out, stderr="")
        with mock.patch.object(cua_client.subprocess, "run", return_value=ok):
            self.assertEqual(cua_client.owner_pids(), [101, 102, 107])
        bad = mock.Mock(returncode=1, stdout="", stderr="ps: denied")
        with mock.patch.object(cua_client.subprocess, "run", return_value=bad):
            with self.assertRaises(cua_client.CUAServiceUnavailable):
                cua_client.owner_pids()

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
            mock.patch.object(cua_client, "owner_pids", return_value=[7]),
        ):
            packet = cua_client.health()
        self.assertEqual(packet["apps"], 1)
        self.assertTrue(packet["ok"])


if __name__ == "__main__":
    unittest.main()
