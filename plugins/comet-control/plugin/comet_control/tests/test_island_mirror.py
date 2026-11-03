#!/usr/bin/env python3
"""Deterministic contracts for the Comet Control fleet -> Dynamic Island mirror."""

from __future__ import annotations

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


NATIVE = Path(__file__).resolve().parents[1] / "native"

MIRROR_SPEC = importlib.util.spec_from_file_location(
    "comet_control_island_mirror_under_test", NATIVE / "island_mirror.py"
)
assert MIRROR_SPEC and MIRROR_SPEC.loader
mirror = importlib.util.module_from_spec(MIRROR_SPEC)
MIRROR_SPEC.loader.exec_module(mirror)

HOST_PATH = NATIVE / "broker.py"
HOST_SPEC = importlib.util.spec_from_file_location("comet_control_broker_for_island", HOST_PATH)
assert HOST_SPEC and HOST_SPEC.loader
host = importlib.util.module_from_spec(HOST_SPEC)
HOST_SPEC.loader.exec_module(host)


class Clock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class Recorder:
    """Publish double that records bodies and can refuse selected kinds."""

    def __init__(self, refuse=None) -> None:
        self.calls: list[dict] = []
        self.refuse = refuse or (lambda _body: False)

    def __call__(self, body: dict) -> bool:
        self.calls.append(dict(body))
        return not self.refuse(body)

    def kinds(self) -> list[str]:
        return [call["kind"] for call in self.calls]


class FakeSocketServer:
    """Threaded AF_UNIX server that answers one JSON reply per request."""

    def __init__(self, reply_for) -> None:
        self.directory = Path(tempfile.mkdtemp(dir="/tmp"))
        self.path = self.directory / "server.sock"
        self.requests: list[str] = []
        self.reply_for = reply_for
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
                    conn.sendall(json.dumps(self.reply_for(buffer)).encode() + b"\n")
                except OSError:
                    pass

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass
        shutil.rmtree(self.directory, ignore_errors=True)


class RowBodyTests(unittest.TestCase):
    def test_row_never_carries_the_lease_token(self) -> None:
        lease = {
            "session_id": "session-1",
            "agent_id": "codex",
            "agent_label": "Codex",
            "expires_at": 1_000_000,
            "lease_token": "secret-lease-token",
        }
        row = mirror.row_for_lease(lease, 900_000)
        self.assertIsNotNone(row)
        self.assertNotIn("lease_token", row)
        self.assertNotIn("secret-lease-token", json.dumps(row))

    def test_row_is_exactly_the_running_body(self) -> None:
        row = mirror.row_for_lease(
            {"session_id": "s1", "agent_label": "Codex", "expires_at": 1_060_000}, 1_000_000
        )
        self.assertEqual(
            row,
            {
                "source": "agent",
                "id": "comet-lease:s1",
                "kind": "running",
                "name": "Codex",
                "app": "Comet",
                "title": "Using Comet",
                "meta": "comet-control",
                "ttl_s": 75.0,
            },
        )

    def test_label_falls_back_from_label_to_id_to_agent(self) -> None:
        base = {"session_id": "s1", "expires_at": 1_000_000}
        cases = [
            ({"agent_label": "  Codex  ", "agent_id": "codex"}, "Codex"),
            ({"agent_label": "   ", "agent_id": "  claude-code "}, "claude-code"),
            ({"agent_label": "", "agent_id": ""}, "Agent"),
            ({}, "Agent"),
            ({"agent_label": 7, "agent_id": None}, "Agent"),
        ]
        for lease, expected in cases:
            with self.subTest(lease=lease):
                self.assertEqual(mirror.row_for_lease({**base, **lease}, 1_000_000)["name"], expected)

    def test_label_is_capped_at_48_chars(self) -> None:
        row = mirror.row_for_lease(
            {"session_id": "s1", "agent_label": "x" * 100, "expires_at": 1_000_000}, 1_000_000
        )
        self.assertEqual(len(row["name"]), 48)

    def test_ttl_is_clamped_at_both_ends(self) -> None:
        now_ms = 1_000_000
        far = mirror.row_for_lease(
            {"session_id": "s", "expires_at": now_ms + 10_000_000}, now_ms
        )
        past = mirror.row_for_lease({"session_id": "s", "expires_at": now_ms - 10_000_000}, now_ms)
        missing = mirror.row_for_lease({"session_id": "s"}, now_ms)
        self.assertEqual(far["ttl_s"], mirror.MAX_TTL)
        self.assertEqual(past["ttl_s"], mirror.MIN_TTL)
        self.assertEqual(missing["ttl_s"], mirror.MIN_TTL)

    def test_lease_without_a_usable_session_id_has_no_row(self) -> None:
        for session_id in (None, "", 7, True):
            with self.subTest(session_id=session_id):
                self.assertIsNone(mirror.row_for_lease({"session_id": session_id}, 1_000_000))


class RowIdTests(unittest.TestCase):
    def test_short_ids_stay_readable_and_long_ids_are_hashed(self) -> None:
        short = "a" * 40
        self.assertEqual(mirror.row_id(short), "comet-lease:" + short)

        long_session = "b" * 200
        hashed = mirror.row_id(long_session)
        expected = mirror.ROW_PREFIX + hashlib.sha256(long_session.encode()).hexdigest()[:24]
        self.assertEqual(hashed, expected)
        self.assertLessEqual(len(hashed), 80)
        self.assertNotIn(long_session, hashed)


class PlanTests(unittest.TestCase):
    def test_heartbeat_posts_only_when_due(self) -> None:
        now_s = 500.0
        leases = [{"session_id": "a", "expires_at": 500_000}]
        fresh = mirror.plan(leases, {}, 500_000, now_s)
        self.assertEqual([body["id"] for body in fresh[0]], ["comet-lease:a"])
        self.assertEqual(fresh[1], [])

        quiet = mirror.plan(leases, {"comet-lease:a": now_s - 19.0}, 500_000, now_s)
        self.assertEqual(quiet, ([], []))

        due = mirror.plan(leases, {"comet-lease:a": now_s - mirror.HEARTBEAT_SECONDS}, 500_000, now_s)
        self.assertEqual([body["id"] for body in due[0]], ["comet-lease:a"])

    def test_stale_posted_ids_are_cleared(self) -> None:
        now_s = 500.0
        posted = {"comet-lease:a": now_s, "comet-lease:b": now_s, "comet-lease:c": now_s}
        leases = [{"session_id": "a", "expires_at": 500_000}]
        posts, clears = mirror.plan(leases, posted, 500_000, now_s)
        self.assertEqual(posts, [])
        self.assertEqual(clears, ["comet-lease:b", "comet-lease:c"])


class MirrorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(dir="/tmp"))
        self.state = self.directory / "island-rows.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)


class TickTests(MirrorTestCase):
    def test_open_posts_then_label_change_reposts_then_close_clears(self) -> None:
        clock = Clock()
        leases = [{"session_id": "s1", "agent_label": "Codex", "expires_at": 1_060_000}]
        recorded = Recorder()
        instance = mirror.IslandMirror(lambda: leases, recorded, self.state, clock)

        first = instance.tick()
        self.assertEqual(first, {"active": 1, "posted": 1, "cleared": 0, "error": False})
        self.assertEqual(recorded.calls[-1]["kind"], "running")
        self.assertEqual(recorded.calls[-1]["name"], "Codex")

        quiet = instance.tick()
        self.assertEqual(quiet, {"active": 1, "posted": 0, "cleared": 0, "error": False})

        leases[0]["agent_label"] = "Claude Code"
        changed = instance.tick()
        self.assertEqual(changed, {"active": 1, "posted": 1, "cleared": 0, "error": False})
        self.assertEqual(recorded.calls[-1]["name"], "Claude Code")

        leases.clear()
        closed = instance.tick()
        self.assertEqual(closed, {"active": 0, "posted": 0, "cleared": 1, "error": False})
        self.assertEqual(
            recorded.calls[-1], {"source": "agent", "id": "comet-lease:s1", "kind": "clear"}
        )
        self.assertEqual(instance.posted, {})

    def test_heartbeat_reposts_an_unchanged_body(self) -> None:
        clock = Clock()
        leases = [{"session_id": "s1", "expires_at": 1_060_000}]
        recorded = Recorder()
        instance = mirror.IslandMirror(lambda: leases, recorded, self.state, clock)
        instance.tick()
        clock.value += mirror.HEARTBEAT_SECONDS
        self.assertEqual(instance.tick()["posted"], 1)
        self.assertEqual(recorded.kinds(), ["running", "running"])

    def test_ttl_drift_alone_does_not_repost_before_heartbeat(self) -> None:
        clock = Clock()
        leases = [{"session_id": "s1", "expires_at": 1_060_000}]
        recorded = Recorder()
        instance = mirror.IslandMirror(lambda: leases, recorded, self.state, clock)
        instance.tick()
        clock.value += 5.0
        leases[0]["expires_at"] += 40_000  # lease renewed: ttl_s changes, visible fields do not
        self.assertEqual(instance.tick()["posted"], 0)
        self.assertEqual(recorded.kinds(), ["running"])

    def test_fetch_errors_keep_rows_until_miss_limit(self) -> None:
        clock = Clock()
        leases = [{"session_id": "s1", "expires_at": 1_060_000}]
        failing = {"on": False}

        def fetch() -> list[dict]:
            if failing["on"]:
                raise RuntimeError("broker down")
            return leases

        recorded = Recorder()
        instance = mirror.IslandMirror(fetch, recorded, self.state, clock)
        instance.tick()

        failing["on"] = True
        for _ in range(mirror.MISS_LIMIT - 1):
            self.assertEqual(
                instance.tick(), {"active": 1, "posted": 0, "cleared": 0, "error": True}
            )
            self.assertIn("comet-lease:s1", instance.posted)
        self.assertEqual(recorded.kinds(), ["running"])

        final = instance.tick()
        self.assertEqual(final, {"active": 0, "posted": 0, "cleared": 1, "error": False})
        self.assertEqual(instance.posted, {})
        self.assertEqual(recorded.calls[-1]["kind"], "clear")

    def test_a_failed_clear_is_retried_on_the_next_tick(self) -> None:
        clock = Clock()
        leases = [{"session_id": "s1", "expires_at": 1_060_000}]
        recorded = Recorder(refuse=lambda body: body["kind"] == "clear")
        instance = mirror.IslandMirror(lambda: leases, recorded, self.state, clock)
        instance.tick()

        leases.clear()
        failed = instance.tick()
        self.assertEqual(failed, {"active": 0, "posted": 0, "cleared": 0, "error": False})
        self.assertIn("comet-lease:s1", instance.posted)

        recorded.refuse = lambda _body: False
        retried = instance.tick()
        self.assertEqual(retried, {"active": 0, "posted": 0, "cleared": 1, "error": False})
        self.assertEqual(instance.posted, {})
        self.assertEqual(recorded.kinds(), ["running", "clear", "clear"])


class StateTests(MirrorTestCase):
    def test_saved_state_lets_a_new_mirror_clear_orphan_rows(self) -> None:
        clock = Clock()
        first_recorder = Recorder()
        leases = [{"session_id": "s1", "expires_at": 1_060_000}]
        first = mirror.IslandMirror(lambda: leases, first_recorder, self.state, clock)
        first.tick()

        self.assertTrue(self.state.exists())
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(self.state.read_text()), {"rows": ["comet-lease:s1"]})

        second_recorder = Recorder()
        second = mirror.IslandMirror(lambda: [], second_recorder, self.state, clock)
        self.assertIn("comet-lease:s1", second.posted)
        self.assertEqual(second.tick()["cleared"], 1)
        self.assertEqual(second_recorder.calls[0]["id"], "comet-lease:s1")
        self.assertEqual(second_recorder.calls[0]["kind"], "clear")
        self.assertEqual(second.posted, {})
        self.assertEqual(json.loads(self.state.read_text()), {"rows": []})
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)

    def test_bad_state_file_is_ignored(self) -> None:
        self.state.write_text("{not json")
        instance = mirror.IslandMirror(lambda: [], Recorder(), self.state)
        self.assertEqual(instance.posted, {})

    def test_shutdown_clears_every_posted_row(self) -> None:
        clock = Clock()
        leases = [
            {"session_id": "s1", "expires_at": 1_060_000},
            {"session_id": "s2", "expires_at": 1_060_000},
        ]
        recorded = Recorder()
        instance = mirror.IslandMirror(lambda: leases, recorded, self.state, clock)
        instance.tick()
        instance.shutdown()

        self.assertEqual(instance.posted, {})
        self.assertEqual(recorded.kinds()[-2:], ["clear", "clear"])
        self.assertEqual(
            {call["id"] for call in recorded.calls[-2:]}, {"comet-lease:s1", "comet-lease:s2"}
        )
        self.assertEqual(json.loads(self.state.read_text()), {"rows": []})


class EnvironmentTests(unittest.TestCase):
    def test_enabled_reads_only_the_documented_disable_values(self) -> None:
        for value in ("0", "false", "OFF", " no ", "False"):
            with self.subTest(value=value), mock.patch.dict(
                os.environ, {"COMET_CONTROL_ISLAND": value}
            ):
                self.assertFalse(mirror.enabled())
        for value in ("1", "true", "yes", "on", ""):
            with self.subTest(value=value), mock.patch.dict(
                os.environ, {"COMET_CONTROL_ISLAND": value}
            ):
                self.assertTrue(mirror.enabled())
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(mirror.enabled())

    def test_socket_path_prefers_the_environment_override(self) -> None:
        with mock.patch.dict(os.environ, {"COMET_CONTROL_ISLAND_SOCKET": "~/custom-island.sock"}):
            self.assertEqual(
                mirror.island_socket_path(), Path("~/custom-island.sock").expanduser()
            )
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                mirror.island_socket_path(),
                Path.home() / "Library/Application Support/CUAService/island.sock",
            )


class IslandPublishTests(MirrorTestCase):
    BODY = {
        "source": "agent",
        "id": "comet-lease:s1",
        "kind": "running",
        "name": "Codex",
        "app": "Comet",
        "title": "Using Comet",
        "meta": "comet-control",
        "ttl_s": 75.0,
    }

    def test_wire_request_is_one_exact_publish_line_and_200_is_true(self) -> None:
        server = FakeSocketServer(lambda _raw: {"status": 200, "body": {"id": "comet-lease:s1"}})
        try:
            self.assertTrue(mirror.island_publish(self.BODY, path=server.path))
        finally:
            server.close()
        wire = server.requests[0]
        self.assertTrue(wire.endswith("\n"))
        self.assertEqual(wire.count("\n"), 1)
        self.assertEqual(
            json.loads(wire),
            {
                "method": "POST",
                "path": "/api/island/publish",
                "query": {},
                "body": self.BODY,
            },
        )

    def test_non_200_and_invalid_socket_are_false(self) -> None:
        server = FakeSocketServer(lambda _raw: {"status": 401, "body": {}})
        try:
            self.assertFalse(mirror.island_publish(self.BODY, path=server.path))
        finally:
            server.close()

        regular_file = self.directory / "island.sock"
        regular_file.write_text("not a socket")
        self.assertFalse(mirror.island_publish(self.BODY, path=regular_file))
        self.assertFalse(mirror.island_publish(self.BODY, path=self.directory / "missing.sock"))


class BrokerSessionsTests(MirrorTestCase):
    def test_sessions_are_returned_without_lease_tokens(self) -> None:
        server = FakeSocketServer(
            lambda _raw: {
                "success": True,
                "sessions": [
                    {
                        "session_id": "s1",
                        "agent_label": "Codex",
                        "expires_at": 1_000,
                        "lease_token": "secret-lease-token",
                    },
                    "not-a-dict",
                ],
            }
        )
        try:
            sessions = mirror.broker_sessions(server.path)
        finally:
            server.close()
        self.assertEqual(len(sessions), 1)
        self.assertNotIn("lease_token", sessions[0])
        self.assertNotIn("secret-lease-token", json.dumps(sessions))
        self.assertEqual(
            json.loads(server.requests[0]), {"type": "sessions", "timeoutSeconds": 5}
        )

    def test_unsuccessful_reply_raises(self) -> None:
        server = FakeSocketServer(lambda _raw: {"success": False, "error": "nope"})
        try:
            with self.assertRaises(RuntimeError):
                mirror.broker_sessions(server.path)
        finally:
            server.close()


class BrokerWiringTests(unittest.TestCase):
    def test_island_mirror_is_disabled_by_environment(self) -> None:
        with mock.patch.dict(host.os.environ, {"COMET_CONTROL_ISLAND": "0"}):
            self.assertIsNone(host._start_island_mirror())


if __name__ == "__main__":
    unittest.main()
