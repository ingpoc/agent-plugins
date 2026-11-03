#!/usr/bin/env python3
"""Contracts for the shared plugin-activity island signal. No real sockets."""

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


class ActivityTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(dir="/tmp"))
        self.server = FakeSocketServer()
        self.env = mock.patch.dict(
            os.environ,
            {
                "ACU_ISLAND_SOCKET": str(self.server.path),
                "ACU_ISLAND_ACTIVITY": "1",
            },
        )
        self.env.start()
        activity._RECENT.clear()

    def tearDown(self) -> None:
        self.env.stop()
        self.server.close()
        shutil.rmtree(self.directory, ignore_errors=True)


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

    def test_acu_rows_carry_the_operated_app_others_never(self) -> None:
        self.assertEqual(activity.row("acu", "a", "k", 5, target_app="  Calculator ")["target_app"], "Calculator")
        self.assertNotIn("target_app", activity.row("acu", "a", "k", 5))
        self.assertNotIn("target_app", activity.row("acu", "a", "k", 5, target_app="   "))
        self.assertNotIn("target_app", activity.row("comet-control", "a", "k", 5, target_app="Comet"))
        self.assertEqual(len(activity.row("acu", "a", "k", 5, target_app="x" * 300)["target_app"]), 128)

    def test_app_key_gives_each_app_its_own_row(self) -> None:
        self.assertEqual(activity.app_key("Grok Bot:1", "TextEdit"), "Grok Bot:1:app:TextEdit")
        self.assertEqual(activity.app_key("Grok Bot:1", ""), "Grok Bot:1")
        self.assertEqual(activity.app_key("Grok Bot:1", None), "Grok Bot:1")

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
    def test_end_publishes_only_the_clear(self) -> None:
        self.assertTrue(activity.pulse("acu", "Codex", "k", hold_s=10.0))
        rid = activity.row_id("acu", "k")
        before = len(self.server.bodies)

        self.assertTrue(activity.end("acu", "k"))
        self.assertEqual(len(self.server.bodies), before + 1)
        self.assertEqual(self.server.bodies[-1], {"source": "agent", "id": rid, "kind": "clear"})

    def test_pulse_posts_a_running_row_with_the_hold_as_its_ttl(self) -> None:
        with mock.patch.object(activity, "publish", wraps=activity.publish) as published:
            self.assertTrue(activity.pulse("acu", "Codex", "k", hold_s=8.0))
        body = published.call_args.args[0]
        self.assertEqual(body["kind"], "running")
        self.assertEqual(body["plugin"], "acu")
        self.assertEqual(body["id"], "plugin:acu:k")
        self.assertEqual(body["ttl_s"], 8.0)
        self.assertEqual(body["name"], "Codex")
        self.assertEqual(self.server.bodies[-1]["ttl_s"], 8.0)

    def test_pulse_throttles_the_socket_inside_the_window(self) -> None:
        self.assertTrue(activity.pulse("acu", "Codex", "k", hold_s=10.0))  # window = min(5, 5) = 5 s
        posted = len(self.server.bodies)
        self.assertTrue(activity.pulse("acu", "Codex", "k", hold_s=10.0))
        self.assertEqual(len(self.server.bodies), posted)

        # A different agent on the same row is never throttled.
        self.assertTrue(activity.pulse("acu", "Claude", "k", hold_s=10.0))
        self.assertEqual(len(self.server.bodies), posted + 1)
        self.assertEqual(self.server.bodies[-1]["name"], "Claude")

    def test_throttle_window_is_half_the_hold_below_five_seconds(self) -> None:
        clock = [1000.0]
        with mock.patch.object(activity.time, "time", side_effect=lambda: clock[0]):
            self.assertTrue(activity.pulse("acu", "Codex", "k", hold_s=4.0))  # window = 2 s
            posted = len(self.server.bodies)

            clock[0] += 1.9
            self.assertTrue(activity.pulse("acu", "Codex", "k", hold_s=4.0))
            self.assertEqual(len(self.server.bodies), posted, "inside the 2 s window")

            clock[0] += 0.1
            self.assertTrue(activity.pulse("acu", "Codex", "k", hold_s=4.0))
            self.assertEqual(len(self.server.bodies), posted + 1, "the window elapsed")

    def test_unknown_plugin_never_posts(self) -> None:
        self.assertFalse(activity.pulse("unknown-plugin", "Codex", "k", hold_s=10.0))
        self.assertEqual(self.server.bodies, [])


class NameTests(unittest.TestCase):
    def test_one_shared_normaliser_maps_spellings_to_one_name(self) -> None:
        cases = {
            "grok-bot": "Grok Bot", "Grok Bot": "Grok Bot", "  grok_bot ": "Grok Bot",
            "grok.bot": "Grok Bot", "smallcase-manager": "Smallcase Manager", "codex": "Codex",
            "GPT-5": "GPT 5", "Claude Code": "Claude Code", "": "Agent", "   ": "Agent", None: "Agent",
        }
        for raw, shown in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(activity.normalize_agent(raw), shown)
        self.assertEqual(len(activity.normalize_agent("x" * 100)), activity.NAME_MAX_CHARS)

    def test_rows_use_the_normaliser_and_title_case_labels(self) -> None:
        self.assertEqual(activity.row("acu", "grok-bot", "k", 10)["name"], "Grok Bot")
        self.assertEqual(activity.row("acu", "a", "k", 10)["title"], "Using Agent Computer Use")
        self.assertEqual(activity.row("comet-control", "a", "k", 10)["title"], "Using Comet Control")
        self.assertEqual(activity.row("context-ledger", "a", "k", 10)["title"], "Using Context Ledger")


class CallTests(ActivityTestCase):
    def test_started_holds_and_finished_drops_two_seconds_later(self) -> None:
        self.assertTrue(activity.call_started("acu", "grok-bot", "k"))
        self.assertTrue(activity.call_finished("acu", "grok-bot", "k"))
        self.assertEqual([b["ttl_s"] for b in self.server.bodies], [activity.IN_FLIGHT_S, 2.0])
        self.assertEqual({b["name"] for b in self.server.bodies}, {"Grok Bot"})
        self.assertEqual(activity.CLEAR_AFTER_S, 2.0)

    def test_finished_is_never_throttled_after_started(self) -> None:
        activity.call_started("context-ledger", "Codex", "k", in_flight_s=60.0)
        activity.call_finished("context-ledger", "Codex", "k")
        activity.call_started("context-ledger", "Codex", "k", in_flight_s=60.0)
        activity.call_finished("context-ledger", "Codex", "k")
        self.assertEqual([b["ttl_s"] for b in self.server.bodies], [60.0, 2.0, 60.0, 2.0])

    def test_a_pulse_inside_the_window_never_reposts_a_longer_hold(self) -> None:
        activity.call_finished("acu", "Codex", "k")
        activity.pulse("acu", "Codex", "k", hold_s=30.0)
        self.assertEqual([b["ttl_s"] for b in self.server.bodies], [2.0])

    def test_unknown_plugin_and_linger(self) -> None:
        self.assertFalse(activity.call_started("nope", "a", "k"))
        self.assertFalse(activity.call_finished("nope", "a", "k"))
        self.assertEqual(self.server.bodies, [])
        body = activity.row("comet-control", "a", "s1", 600)
        lingered = activity.linger(body)
        self.assertEqual(lingered["ttl_s"], 2.0)
        self.assertEqual(body["ttl_s"], 600.0)
        self.assertEqual({k: v for k, v in lingered.items() if k != "ttl_s"},
                         {k: v for k, v in body.items() if k != "ttl_s"})


if __name__ == "__main__":
    unittest.main()
