#!/usr/bin/env python3
"""Contracts for the shared plugin-activity island signal. No real sockets."""

from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT.parents[1] / "bin" / "island_activity.py"
SPEC = importlib.util.spec_from_file_location("island_activity_under_test", SCRIPT)
assert SPEC and SPEC.loader
activity = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(activity)


class FakeSocketServer:
    """Threaded AF_UNIX server: answers one status per request and records bodies."""

    def __init__(self, status: int = 200) -> None:
        self.directory = Path(tempfile.mkdtemp(dir="/tmp"))
        self.path = self.directory / "island.sock"
        self.status = status
        self.requests: list[str] = []
        self.bodies: list[dict] = []
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(self.path))
        self.sock.listen(8)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(2.0)
                buffer = b""
                while True:
                    try:
                        chunk = conn.recv(65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    buffer += chunk
                    if buffer.endswith(b"\n"):
                        break
                self.requests.append(buffer.decode("utf-8"))
                try:
                    self.bodies.append(json.loads(buffer)["body"])
                except (ValueError, KeyError, TypeError):
                    pass
                try:
                    conn.sendall(json.dumps({"status": self.status, "body": {}}).encode() + b"\n")
                except OSError:
                    pass

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass
        shutil.rmtree(self.directory, ignore_errors=True)


def wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class ActivityTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(dir="/tmp"))
        self.state = self.directory / "state"
        self.server = FakeSocketServer()
        self.env = mock.patch.dict(
            os.environ,
            {
                "ACU_ISLAND_SOCKET": str(self.server.path),
                "ACU_ISLAND_ACTIVITY_DIR": str(self.state),
            },
        )
        self.env.start()
        os.environ.pop("ACU_ISLAND_ACTIVITY", None)
        activity._RECENT.clear()

    def tearDown(self) -> None:
        self.env.stop()
        self.server.close()
        shutil.rmtree(self.directory, ignore_errors=True)

    def hold_path(self, rid: str) -> Path:
        return activity._hold_path(rid)

    def write_hold(self, rid: str, until: float) -> Path:
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.hold_path(rid)
        path.write_text(json.dumps({"id": rid, "until": until}), encoding="utf-8")
        return path


class RowTests(ActivityTestCase):
    def test_row_shape_and_app_for_each_plugin(self) -> None:
        for plugin, (app, label) in activity.MARKS.items():
            with self.subTest(plugin=plugin):
                self.assertEqual(
                    activity.row(plugin, "  Codex  ", "k1", 75),
                    {
                        "source": "agent",
                        "id": f"plugin:{plugin}:k1",
                        "kind": "running",
                        "name": "Codex",
                        "app": app,
                        "title": f"Using {label}",
                        "meta": plugin,
                        "plugin": plugin,
                        "ttl_s": 75.0,
                    },
                )

    def test_name_ttl_and_unknown_plugin(self) -> None:
        self.assertEqual(activity.row("acu", "   ", "k", 1)["name"], "Agent")
        self.assertEqual(len(activity.row("acu", "x" * 100, "k", 1)["name"]), 48)
        self.assertEqual(activity.row("acu", "a", "k", 1)["ttl_s"], activity.MIN_TTL)
        self.assertEqual(activity.row("acu", "a", "k", 999_999)["ttl_s"], activity.MAX_TTL)
        with self.assertRaises(ValueError):
            activity.row("unknown-plugin", "a", "k", 10)

    def test_ids_stay_readable_or_hash_long_keys(self) -> None:
        self.assertEqual(activity.row_id("acu", "k1"), "plugin:acu:k1")
        long_key = "b" * 200
        expected = "plugin:acu:" + hashlib.sha256(long_key.encode()).hexdigest()[:24]
        hashed = activity.row_id("acu", long_key)
        self.assertEqual(hashed, expected)
        self.assertLessEqual(len(hashed), 80)
        self.assertNotIn(long_key, hashed)


class EnableAndPublishTests(ActivityTestCase):
    def test_enabled_reads_only_documented_disable_values(self) -> None:
        for value in ("0", "false", "OFF", " no ", "False"):
            with self.subTest(value=value), mock.patch.dict(
                os.environ, {"ACU_ISLAND_ACTIVITY": value}
            ):
                self.assertFalse(activity.enabled())
                self.assertFalse(activity.publish(activity.row("acu", "a", "k", 10)))
        for value in ("1", "true", "yes", "on", ""):
            with self.subTest(value=value), mock.patch.dict(
                os.environ, {"ACU_ISLAND_ACTIVITY": value}
            ):
                self.assertTrue(activity.enabled())
        self.assertEqual(self.server.requests, [])

    def test_wire_request_is_one_exact_publish_line(self) -> None:
        body = activity.row("acu", "Codex", "k", 10)
        self.assertTrue(activity.publish(body))
        wire = self.server.requests[0]
        self.assertTrue(wire.endswith("\n"))
        self.assertEqual(wire.count("\n"), 1)
        self.assertEqual(
            json.loads(wire),
            {"method": "POST", "path": "/api/island/publish", "query": {}, "body": body},
        )

    def test_non_200_and_non_socket_paths_are_false(self) -> None:
        self.server.status = 401
        self.assertFalse(activity.publish(activity.row("acu", "a", "k", 10)))

        regular_file = self.directory / "regular.sock"
        regular_file.write_text("not a socket")
        self.assertFalse(
            activity.publish(activity.row("acu", "a", "k", 10), path=regular_file)
        )
        self.assertFalse(
            activity.publish(activity.row("acu", "a", "k", 10), path=self.directory / "missing.sock")
        )

    def test_socket_path_prefers_the_environment_override(self) -> None:
        with mock.patch.dict(
            os.environ, {"ACU_ISLAND_SOCKET": "~/custom-island.sock"}
        ):
            self.assertEqual(
                activity.socket_path(), Path("~/custom-island.sock").expanduser()
            )


class PulseAndEndTests(ActivityTestCase):
    def test_end_clears_the_row_and_removes_the_hold_file(self) -> None:
        with mock.patch.object(activity, "ensure_reaper"):
            self.assertTrue(activity.pulse("acu", "Codex", "k", hold_s=10.0))
        rid = activity.row_id("acu", "k")
        self.assertTrue(self.hold_path(rid).exists())

        self.assertTrue(activity.end("acu", "k"))
        self.assertFalse(self.hold_path(rid).exists())
        self.assertEqual(self.server.bodies[-1], {"source": "agent", "id": rid, "kind": "clear"})

    def test_pulse_writes_a_private_hold_file_and_throttles_the_socket(self) -> None:
        with mock.patch.object(activity, "ensure_reaper"):
            self.assertTrue(activity.pulse("acu", "Codex", "k", hold_s=10.0))
        rid = activity.row_id("acu", "k")
        path = self.hold_path(rid)
        self.assertTrue(path.exists())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["id"], rid)
        self.assertGreater(payload["until"], time.time())
        posted = len(self.server.bodies)

        path.write_text(json.dumps({"id": rid, "until": 0}), encoding="utf-8")
        with mock.patch.object(activity, "ensure_reaper"):
            self.assertTrue(activity.pulse("acu", "Codex", "k", hold_s=10.0))
        self.assertEqual(len(self.server.bodies), posted)
        self.assertGreater(
            json.loads(path.read_text(encoding="utf-8"))["until"], time.time()
        )

        with mock.patch.object(activity, "ensure_reaper"):
            self.assertTrue(activity.pulse("acu", "Claude", "k", hold_s=10.0))
        self.assertEqual(len(self.server.bodies), posted + 1)
        self.assertEqual(self.server.bodies[-1]["name"], "Claude")


class ReapTests(ActivityTestCase):
    def test_reap_clears_expired_holds_and_keeps_unexpired(self) -> None:
        expired = activity.row_id("acu", "expired")
        live = activity.row_id("acu", "live")
        self.write_hold(expired, time.time() - 1.0)
        live_path = self.write_hold(live, time.time() + 600.0)

        results: list[int] = []
        thread = threading.Thread(
            target=lambda: results.append(activity.reap(poll=0.01, idle_exit=0.05)),
            daemon=True,
        )
        thread.start()
        try:
            self.assertTrue(
                wait_for(
                    lambda: any(
                        body.get("id") == expired and body.get("kind") == "clear"
                        for body in self.server.bodies
                    )
                )
            )
            self.assertFalse(self.hold_path(expired).exists())
            self.assertTrue(live_path.exists())
            self.assertFalse(
                any(
                    body.get("id") == live and body.get("kind") == "clear"
                    for body in self.server.bodies
                )
            )
        finally:
            for path in self.state.glob("*.json"):
                path.unlink()
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [0])

    def test_reap_exits_when_another_reaper_holds_the_lock(self) -> None:
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = self.state / "reaper.lock"
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            self.assertEqual(activity.reap(poll=0.01, idle_exit=0.01), 0)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        self.assertEqual(self.server.bodies, [])


class ReaperSpawnTests(ActivityTestCase):
    def test_locked_reaper_blocks_a_second_spawn(self) -> None:
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = self.state / "reaper.lock"
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with mock.patch("subprocess.Popen") as popen:
                activity.ensure_reaper()
            popen.assert_not_called()
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_free_lock_spawns_one_detached_reaper(self) -> None:
        with mock.patch("subprocess.Popen") as popen:
            activity.ensure_reaper()
        popen.assert_called_once()
        command = popen.call_args.args[0]
        self.assertEqual(Path(command[1]).name, "island_activity.py")
        self.assertEqual(command[2], "reap")
        self.assertTrue(popen.call_args.kwargs["start_new_session"])


if __name__ == "__main__":
    unittest.main()
