"""Usage rows on response.done + reconnect backoff (fixtures only, no network)."""

from __future__ import annotations

import io
import json
import os
import random
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault(
    "VOICE_CUA_ACTIVITY_LOG",
    str(Path(tempfile.mkdtemp(prefix="voice-cua-test-")) / "activity.jsonl"),
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from voice_cua import activity_log  # noqa: E402
from voice_cua.activity_log import log_event, log_usage, usage_row  # noqa: E402
from voice_cua.island_copy import human_error  # noqa: E402
from voice_cua.realtime_session import RealtimeSession, retry_after_seconds  # noqa: E402
from test_phase1_reliability import feed, make_session  # noqa: E402

USAGE_FIELDS = [
    "input_tokens", "output_tokens", "cached_tokens", "input_text_tokens", "input_audio_tokens",
    "output_text_tokens", "output_audio_tokens", "total_tokens",
]

RESPONSE_DONE = {
    "type": "response.done",
    "response": {
        "id": "resp_fake123",
        "status": "completed",
        "output": [
            {"type": "message", "content": [{"type": "output_audio", "transcript": "SECRET TRANSCRIPT words"}]},
            {"type": "function_call", "call_id": "call_fake9", "name": "cua_state", "arguments": "{\"app\": \"SECRET\"}"},
        ],
        "usage": {
            "total_tokens": 1540,
            "input_tokens": 1200,
            "output_tokens": 340,
            "input_token_details": {
                "text_tokens": 1000,
                "audio_tokens": 200,
                "cached_tokens": 896,
                "cached_tokens_details": {"text_tokens": 896, "audio_tokens": 0},
            },
            "output_token_details": {"text_tokens": 60, "audio_tokens": 280},
        },
    },
}


class _LogCase(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.path = Path(self._td.name) / "activity.jsonl"
        env = patch.dict(os.environ, {"VOICE_CUA_ACTIVITY_LOG": str(self.path), "VOICE_CUA_STARTUP_ID": "abcd1234"})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self._td.cleanup)

    def rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]


class UsageRowTests(_LogCase):
    def test_full_usage_row_shape(self) -> None:
        log_usage(RESPONSE_DONE["response"], latency_ms=812)
        raw = self.path.read_text()
        self.assertNotIn("SECRET", raw)
        self.assertEqual(raw.count("\n"), 1)
        row = json.loads(raw)
        self.assertEqual(
            list(row),
            ["kind", "ts", "session_id", "response_id", "call_id", "latency_ms", *USAGE_FIELDS],
        )
        self.assertEqual(row["kind"], "usage")
        self.assertEqual(row["session_id"], "abcd1234")
        self.assertEqual(row["response_id"], "resp_fake123")
        self.assertEqual(row["call_id"], "call_fake9")
        self.assertEqual(row["latency_ms"], 812)
        self.assertIsNotNone(datetime.fromisoformat(row["ts"]).utcoffset())
        self.assertEqual(
            {k: row[k] for k in USAGE_FIELDS},
            {
                "input_tokens": 1200, "output_tokens": 340, "cached_tokens": 896,
                "input_text_tokens": 1000, "input_audio_tokens": 200,
                "output_text_tokens": 60, "output_audio_tokens": 280, "total_tokens": 1540,
            },
        )
        for key in USAGE_FIELDS:
            self.assertIs(type(row[key]), int)

    def test_missing_and_non_numeric_fields_are_omitted(self) -> None:
        row = usage_row({
            "id": "resp_2",
            "usage": {
                "input_tokens": 10,
                "output_tokens": "lots",
                "total_tokens": True,
                "input_token_details": {"audio_tokens": None, "text_tokens": 10},
                "output_token_details": "nope",
            },
        })
        self.assertEqual(
            set(row) - {"ts"},
            {"kind", "session_id", "response_id", "input_tokens", "input_text_tokens"},
        )

    def test_no_usage_still_logs_ids_only(self) -> None:
        row = usage_row({"id": "resp_3"})
        self.assertEqual(set(row), {"kind", "ts", "session_id", "response_id"})
        self.assertEqual(set(usage_row(None)), {"kind", "ts", "session_id"})

    def test_response_done_appends_one_usage_row(self) -> None:
        session = make_session()
        with patch("voice_cua.realtime_session.island_publish"), \
                patch.object(session, "_run_tool"):
            feed(session, {"type": "response.created", "response": {"id": "resp_fake123"}})
            time.sleep(0.02)
            feed(session, RESPONSE_DONE)
        usage = [r for r in self.rows() if r.get("kind") == "usage"]
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0]["total_tokens"], 1540)
        self.assertGreaterEqual(usage[0]["latency_ms"], 15)
        self.assertNotIn("resp_fake123", session._response_started)
        self.assertNotIn("SECRET", json.dumps(usage[0]))

    def test_tool_result_rows_carry_ids_and_size(self) -> None:
        from voice_cua.activity_log import log_tool_result, tool_context

        result = {"ok": True, "text": "x" * 40}
        with tool_context(call_id="call_7", response_id="resp_7"):
            log_tool_result("cua_state", result)
        log_tool_result("cua_state", {"ok": False, "error": "nope"})
        first, second = self.rows()
        self.assertEqual((first["call_id"], first["response_id"]), ("call_7", "resp_7"))
        self.assertEqual(first["result_chars"], len(json.dumps(result)))
        self.assertNotIn("x" * 40, json.dumps(first))
        self.assertNotIn("call_id", second)  # context does not leak past the block

    def test_tool_thread_logs_call_and_response_ids(self) -> None:
        session = make_session()
        done = []
        with patch("voice_cua.realtime_session.island_publish"), \
                patch("voice_cua.tools._cua_state", return_value={"ok": True, "elements": []}), \
                patch.object(session, "_wait_for_responses_idle", return_value=True), \
                patch.object(session, "_send_response_create", side_effect=lambda **_: done.append(1)):
            feed(session, {"type": "response.output_item.done", "response_id": "resp_t",
                           "item": {"type": "function_call", "call_id": "call_t", "name": "cua_state", "arguments": "{}"}})
            deadline = time.monotonic() + 3
            while not done and time.monotonic() < deadline:
                time.sleep(0.01)
        tool_rows = [r for r in self.rows() if r.get("kind") == "tool_result"]
        self.assertEqual(len(tool_rows), 1)
        self.assertEqual((tool_rows[0]["call_id"], tool_rows[0]["response_id"]), ("call_t", "resp_t"))
        self.assertIn("result_chars", tool_rows[0])

    def test_session_update_bytes_logged(self) -> None:
        session = make_session()
        event = {"type": "session.update", "session": {"type": "realtime", "instructions": "hi"}}
        self.assertTrue(session._send(event))
        rows = [r for r in self.rows() if r.get("kind") == "session_update"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["bytes"], len(json.dumps(event).encode()))
        self.assertEqual(rows[0]["session_id"], "abcd1234")
        self.assertEqual(set(rows[0]), {"ts", "kind", "session_id", "bytes", "provider"})
        self.assertEqual(rows[0]["provider"], "test-loop")

    def test_every_row_carries_session_id(self) -> None:
        log_event("tool_start", tool="click")
        log_event("startup", phase="x", session_id="explicit")
        rows = self.rows()
        self.assertEqual(rows[0]["session_id"], "abcd1234")
        self.assertEqual(rows[1]["session_id"], "explicit")

    def test_session_id_falls_back_to_stable_process_id(self) -> None:
        with patch.dict(os.environ, {"VOICE_CUA_STARTUP_ID": ""}):
            log_event("a")
            log_event("b")
        first, second = self.rows()
        self.assertTrue(first["session_id"])
        self.assertEqual(first["session_id"], second["session_id"])

    def test_each_row_is_a_single_append_write(self) -> None:
        real_write = os.write
        writes: list[bytes] = []

        def spy(fd: int, data: bytes) -> int:
            writes.append(data)
            return real_write(fd, data)

        with patch.object(activity_log.os, "write", side_effect=spy):
            log_event("tool_start", tool="click")
            log_usage(RESPONSE_DONE["response"])
        self.assertEqual(len(writes), 2)
        for data in writes:
            self.assertTrue(data.endswith(b"\n"))
            self.assertEqual(data.count(b"\n"), 1)


class _Stop(Exception):
    pass


class BackoffTests(_LogCase):
    def _run(self, session: RealtimeSession, connect, *, max_waits: int = 50) -> tuple[list[float], list, str]:
        waits: list[float] = []

        def fake_wait(timeout: float | None = None) -> bool:
            waits.append(float(timeout or 0))
            return len(waits) >= max_waits

        out = io.StringIO()
        with patch.object(session, "connect", side_effect=connect), \
                patch.object(session._stop, "wait", side_effect=fake_wait), \
                patch.object(session, "_schedule_planned_reconnect"), \
                patch("voice_cua.realtime_session.island_publish") as pub, \
                redirect_stdout(out):
            session.run()
        return waits, pub.call_args_list, out.getvalue()

    def test_flapping_connection_increments_attempts(self) -> None:
        session = make_session()

        def flap() -> None:  # connects, then drops after ~1 s
            session._connected_at = time.monotonic() - 1.0

        self._run(session, flap, max_waits=4)
        attempts = [r["attempt"] for r in self.rows() if r.get("kind") == "reconnecting"]
        self.assertEqual(attempts, [1, 2, 3, 4])

    def test_stable_connection_resets_counter(self) -> None:
        session = make_session()
        session._reconnect_attempts = 6

        def stable() -> None:
            session._connected_at = time.monotonic() - (RealtimeSession.RECONNECT_STABLE_SECONDS + 1)

        self._run(session, stable, max_waits=3)
        attempts = [r["attempt"] for r in self.rows() if r.get("kind") == "reconnecting"]
        self.assertEqual(attempts, [1, 1, 1])

    def test_gives_up_after_max_attempts(self) -> None:
        session = make_session()

        def refuse() -> None:
            raise OSError("refused")

        waits, publishes, out = self._run(session, refuse)
        self.assertEqual(len(waits), RealtimeSession.RECONNECT_MAX_ATTEMPTS)
        rows = self.rows()
        self.assertEqual([r["attempt"] for r in rows if r.get("kind") == "reconnecting"], list(range(1, 9)))
        self.assertEqual(len([r for r in rows if r.get("kind") == "reconnect_gave_up"]), 1)
        last = publishes[-1]
        self.assertEqual(last.args, ("error",))
        self.assertEqual(last.kwargs["title"], "Couldn't reconnect voice")
        # The island copy contract keeps the title verbatim (not a generic fallback).
        self.assertEqual(human_error(last.kwargs["title"]), "Couldn't reconnect voice")
        self.assertIn("gave up after 8 failed reconnect attempts", out)
        self.assertNotIn(("idle",), [c.args for c in publishes])

    def test_full_jitter_draws_from_zero_to_capped_ceiling(self) -> None:
        calls: list[tuple[float, float]] = []

        class Rng:
            def uniform(self, lo: float, hi: float) -> float:
                calls.append((lo, hi))
                return hi

        for attempt in range(1, 10):
            RealtimeSession._reconnect_delay(attempt, rng=Rng())
        self.assertEqual(calls, [(0.0, c) for c in (0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0)])
        self.assertEqual(RealtimeSession._reconnect_delay(1, rng=random.Random(0), min_delay=12.0), 12.0)

    def test_rate_limit_error_sets_long_minimum_delay(self) -> None:
        session = make_session()
        with patch("voice_cua.realtime_session.island_publish"):
            feed(session, {"type": "error", "error": {"type": "rate_limit_error", "code": "rate_limit_exceeded",
                                                      "message": "Rate limit reached for tokens per min"}})
        self.assertEqual(session._classify_error({"code": "rate_limit_exceeded"}), "rate_limit")
        min_delay, reason = session._take_retry_hint()
        self.assertGreater(min_delay, RealtimeSession.RATE_LIMIT_MIN_DELAY - 1)
        self.assertTrue(reason.startswith("rate_limit"))
        self.assertGreaterEqual(RealtimeSession._reconnect_delay(1, min_delay=min_delay), min_delay)

    def test_rate_limit_honours_longer_server_retry_after(self) -> None:
        session = make_session()
        with patch("voice_cua.realtime_session.island_publish"):
            feed(session, {"type": "error", "error": {"code": "rate_limit_exceeded",
                                                      "message": "Please try again in 45s."}})
        min_delay, _ = session._take_retry_hint()
        self.assertGreater(min_delay, 44)

    def test_hint_feeds_the_next_reconnect_delay(self) -> None:
        session = make_session()

        def rate_limited() -> None:
            if not any(r.get("kind") == "reconnecting" for r in self.rows()):
                session._note_retry_hint(min_delay=25.0, reason="rate_limit:test")

        waits, _, _ = self._run(session, rate_limited, max_waits=2)
        self.assertGreater(waits[0], 24)
        self.assertLessEqual(waits[1], 1.0)  # hint is consumed once

    def test_handshake_429_uses_retry_after_header(self) -> None:
        session = make_session()

        class BadStatus(Exception):
            status_code = 429
            resp_headers = {"Retry-After": "40"}

        session._on_error(None, BadStatus("Handshake status 429 Too Many Requests"))
        min_delay, reason = session._take_retry_hint()
        self.assertGreater(min_delay, 39)
        self.assertIn("http_429", reason)

    def test_retry_after_parsing(self) -> None:
        self.assertEqual(retry_after_seconds(None, {"retry-after": "12"}), 12.0)
        self.assertEqual(retry_after_seconds(None, {"Retry-After-Ms": "1500"}), 1.5)
        self.assertEqual(retry_after_seconds({"message": "Please try again in 350ms"}), 0.35)
        self.assertEqual(retry_after_seconds({"message": "try again in 2m"}), 120.0)
        self.assertEqual(retry_after_seconds({"retry_after": 7}), 7.0)
        self.assertIsNone(retry_after_seconds({"message": "boom"}, {}))
        self.assertIsNone(retry_after_seconds({"retry_after": True}))

    def test_retry_after_is_capped(self) -> None:
        session = make_session()
        session._note_retry_hint(min_delay=10_000, reason="retry_after:test")
        min_delay, _ = session._take_retry_hint()
        self.assertLessEqual(min_delay, RealtimeSession.RETRY_AFTER_MAX)


if __name__ == "__main__":
    unittest.main()
