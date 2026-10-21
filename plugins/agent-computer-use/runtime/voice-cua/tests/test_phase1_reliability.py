"""Phase 1 reliability tests (P1-1..P1-6): GA events, reconnect, barge-in, errors.

All tests use fake WS messages — no live sockets, no mic, no network.
"""

from __future__ import annotations

import base64
import io
import json
import os
import random
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

# Keep test events out of the real ~/.cache/macos-cua activity log.
# Never read the machine's ~/.config/voice-cua/settings.json: a user who picked GPT-Live
# there switched every session in these tests to the Live provider.
os.environ["VOICE_CUA_SETTINGS"] = str(Path(tempfile.mkdtemp(prefix="voice-cua-test-settings-")) / "settings.json")
os.environ.setdefault(
    "VOICE_CUA_ACTIVITY_LOG",
    str(Path(tempfile.mkdtemp(prefix="voice-cua-test-")) / "activity.jsonl"),
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from voice_cua.activity_log import log_event, tail_events  # noqa: E402
from voice_cua.gateway import mint_client_secret  # noqa: E402
from voice_cua.realtime_session import RealtimeSession  # noqa: E402

CLIENT_SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"


class _FakeResp:
    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def read(self) -> bytes:
        return json.dumps({"value": "reference"}).encode("utf-8")


class FakeWS:
    """Records outgoing events; close is recorded as a __closed__ marker."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def send(self, raw: str) -> None:
        self.events.append(json.loads(raw))

    def close(self) -> None:
        self.events.append({"type": "__closed__"})


class FakeAudio:
    """RealtimeAudio substitute: has playback, returns canned interrupt stats."""

    def __init__(self, played_ms: int = 700, item_id: str = "item_1") -> None:
        self.played_ms_value = played_ms
        self.item_id = item_id
        self.cleared = False
        self.received: list[str] = []

    def has_playback(self) -> bool:
        return True

    def interrupt(self) -> tuple[int, str]:
        self.cleared = True
        return self.played_ms_value, self.item_id

    def on_output_delta(self, audio_b64: str, *, item_id: str = "") -> None:
        self.received.append(audio_b64)

    def clear_playback(self) -> None:
        self.cleared = True

    def stop(self) -> None:
        pass


def make_session(eagerness: str = "balanced") -> RealtimeSession:
    # Patch voice_settings.load_settings (apply_env + turn_detection_config both
    # resolve it there) so the machine's settings.json can't leak into tests.
    settings = {
        "realtime_model": "gpt-realtime-2",
        "realtime_voice": "alloy",
        "eagerness": eagerness,
        "mic_profile": "near_field",
    }
    with patch("voice_cua.voice_settings.load_settings", return_value=settings):
        session = RealtimeSession(api_key="test", enable_audio=False)
    session.ws = FakeWS()
    return session


def feed(session: RealtimeSession, event: dict) -> None:
    session._on_message(None, json.dumps(event))


def event_types(session: RealtimeSession) -> list[str]:
    return [e.get("type", "") for e in session.ws.events]


class MintClientSecretTests(unittest.TestCase):
    """P1-1: GA client_secrets mint only — Beta /v1/realtime/sessions is gone."""

    def test_mint_body_sends_expiry_and_full_session_config(self) -> None:
        with (
            patch("voice_cua.gateway.resolve_openai_api_key", return_value="test-key"),
            patch("voice_cua.gateway.urllib.request.urlopen", return_value=_FakeResp()) as urlopen,
            patch.dict(os.environ, {"VOICE_CUA_EAGERNESS": "balanced"}),
        ):
            mint_client_secret()
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, CLIENT_SECRETS_URL)
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["expires_after"], {"anchor": "created_at", "seconds": 600})
        self.assertEqual(body["session"]["type"], "realtime")
        self.assertTrue(body["session"]["model"])

    def test_http_error_surfaces_status_and_body_without_fallback(self) -> None:
        err = urllib.error.HTTPError(
            CLIENT_SECRETS_URL, 401, "Unauthorized", {}, io.BytesIO(b'{"error":"bad key"}')
        )
        urlopen = Mock(side_effect=err)
        with (
            patch("voice_cua.gateway.resolve_openai_api_key", return_value="test-key"),
            patch("voice_cua.gateway.urllib.request.urlopen", urlopen),
        ):
            with self.assertRaises(RuntimeError) as raised:
                mint_client_secret()
        message = str(raised.exception)
        self.assertIn("HTTP 401", message)
        self.assertIn("bad key", message)
        # Exactly one outbound POST — no /v1/realtime/sessions fallback call.
        self.assertEqual(urlopen.call_count, 1)
        self.assertNotIn("sessions", urlopen.call_args.args[0].full_url)

    def test_404_raises_instead_of_falling_back(self) -> None:
        err = urllib.error.HTTPError(CLIENT_SECRETS_URL, 404, "Not Found", {}, io.BytesIO(b"nope"))
        urlopen = Mock(side_effect=err)
        with (
            patch("voice_cua.gateway.resolve_openai_api_key", return_value="test-key"),
            patch("voice_cua.gateway.urllib.request.urlopen", urlopen),
        ):
            with self.assertRaises(RuntimeError) as raised:
                mint_client_secret()
        self.assertEqual(urlopen.call_count, 1)
        self.assertIn("HTTP 404", str(raised.exception))


class GAEventTests(unittest.TestCase):
    """P1-2: speaking triggers on first GA output_audio.delta; Beta aliases gone."""

    def test_speaking_on_first_delta_per_response_id(self) -> None:
        session = make_session()
        published: list[str] = []
        with patch(
            "voice_cua.realtime_session.island_publish",
            side_effect=lambda kind, **kw: published.append(kind),
        ):
            feed(session, {
                "type": "response.output_audio.delta",
                "response_id": "r1",
                "item_id": "i1",
                "delta": base64.b64encode(b"\x00\x01").decode("ascii"),
            })
            self.assertIn("speaking", published)
            # Same response_id again → no duplicate speaking publish.
            feed(session, {
                "type": "response.output_audio.delta",
                "response_id": "r1",
                "item_id": "i1",
                "delta": base64.b64encode(b"\x00\x01").decode("ascii"),
            })
            self.assertEqual(published.count("speaking"), 1)
            # Beta alias must not publish speaking.
            feed(session, {"type": "response.audio.delta", "response_id": "r2", "delta": "AAAA"})
            self.assertEqual(published.count("speaking"), 1)
            # response.done clears the tracker → a new response speaks again.
            feed(session, {"type": "response.done", "response": {"id": "r1"}})
            feed(session, {
                "type": "response.output_audio.delta",
                "response_id": "r3",
                "item_id": "i3",
                "delta": "AAAA",
            })
            self.assertEqual(published.count("speaking"), 2)

    def test_beta_started_events_are_not_speaking_triggers(self) -> None:
        session = make_session()
        published: list[str] = []
        with patch(
            "voice_cua.realtime_session.island_publish",
            side_effect=lambda kind, **kw: published.append(kind),
        ):
            feed(session, {"type": "response.audio.started"})
            feed(session, {"type": "response.output_audio.started"})
            feed(session, {"type": "response.audio_transcript.done", "transcript": "hi"})
            self.assertNotIn("speaking", published)
        # GA transcript alias must not record transcripts either.
        self.assertEqual(session._response_transcripts, {})

    def test_content_part_added_output_audio_publishes_once(self) -> None:
        session = make_session()
        published: list[str] = []
        with patch(
            "voice_cua.realtime_session.island_publish",
            side_effect=lambda kind, **kw: published.append(kind),
        ):
            feed(session, {
                "type": "response.content_part.added",
                "response_id": "r1",
                "part": {"type": "output_audio"},
            })
            feed(session, {
                "type": "response.content_part.added",
                "response_id": "r1",
                "part": {"type": "output_audio"},
            })
            feed(session, {
                "type": "response.content_part.added",
                "response_id": "r1",
                "part": {"type": "output_text"},
            })
        self.assertEqual(published.count("speaking"), 1)


class ResponseDonePartsTests(unittest.TestCase):
    """P1-3: output_text not double-added; output_audio transcript accepted."""

    def _assistant_reply(self, session: RealtimeSession, response: dict) -> str:
        replies: list[str] = []
        with patch(
            "voice_cua.realtime_session.island_publish",
        ), patch(
            "voice_cua.realtime_session.log_event",
            side_effect=lambda kind, **kw: replies.append(str(kw.get("text") or "")),
        ):
            feed(session, {"type": "response.done", "response": response})
        return max((r for r in replies if r), key=len, default="")

    def test_output_text_scanned_once(self) -> None:
        session = make_session()
        reply = self._assistant_reply(session, {
            "id": "r1",
            "status": "completed",
            "output": [{
                "type": "message",
                "content": [{"type": "output_text", "text": "hi"}],
            }],
        })
        self.assertEqual(reply, "hi")

    def test_output_audio_uses_transcript(self) -> None:
        session = make_session()
        reply = self._assistant_reply(session, {
            "id": "r1",
            "status": "completed",
            "output": [{
                "type": "message",
                "content": [
                    {"type": "output_audio", "transcript": "hello there"},
                    {"type": "output_text", "text": "friend"},
                ],
            }],
        })
        self.assertIn("hello there", reply)
        self.assertIn("friend", reply)

    def test_legacy_audio_part_type_ignored(self) -> None:
        session = make_session()
        reply = self._assistant_reply(session, {
            "id": "r1",
            "status": "completed",
            "output": [{
                "type": "message",
                "content": [{"type": "audio", "transcript": "old beta shape"}],
            }],
        })
        self.assertEqual(reply, "")

    def test_function_call_items_skipped(self) -> None:
        session = make_session()
        reply = self._assistant_reply(session, {
            "id": "r1",
            "status": "completed",
            "output": [{
                "type": "function_call",
                "call_id": "c1",
                "name": "cua_state",
                "arguments": "{}",
            }],
        })
        self.assertEqual(reply, "")


class ReconnectTests(unittest.TestCase):
    """P1-4: ping timeout constants, jittered backoff, planned reconnect."""

    def test_keepalive_and_planned_reconnect_constants(self) -> None:
        self.assertEqual(RealtimeSession.REALTIME_PING_INTERVAL, 20)
        self.assertIn(RealtimeSession.REALTIME_PING_TIMEOUT, range(15, 21))
        self.assertEqual(RealtimeSession.PLANNED_RECONNECT_SECONDS, 3300.0)

    def test_connect_uses_ping_interval_and_timeout(self) -> None:
        session = make_session()
        app = Mock()
        with patch("voice_cua.realtime_session.websocket") as ws_module:
            ws_module.WebSocketApp.return_value = app
            session.connect()
        app.run_forever.assert_called_once_with(
            ping_interval=RealtimeSession.REALTIME_PING_INTERVAL,
            ping_timeout=RealtimeSession.REALTIME_PING_TIMEOUT,
            suppress_origin=True,
        )

    def test_backoff_bounds_and_sequence(self) -> None:
        # Full jitter: attempt n draws from U(0, min(30, 0.5 * 2**(n-1))).
        for attempt, ceiling in enumerate([0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0], start=1):
            for seed in range(20):
                delay = RealtimeSession._reconnect_delay(attempt, rng=random.Random(seed))
                self.assertGreaterEqual(delay, RealtimeSession.RECONNECT_DELAY_FLOOR)
                self.assertLessEqual(delay, ceiling)

    def test_session_updated_marks_connected_without_resetting_backoff(self) -> None:
        session = make_session()
        session._reconnect_attempts = 5
        with (
            patch("voice_cua.realtime_session.island_publish"),
            patch.object(RealtimeSession, "_schedule_planned_reconnect") as schedule,
        ):
            feed(session, {"type": "session.updated"})
        # A flapping socket must not reset the counter — only 30 s of stability does.
        self.assertEqual(session._reconnect_attempts, 5)
        self.assertGreater(session._connected_at, 0)
        schedule.assert_called_once_with()

    def test_close_triggers_reconnect_loop(self) -> None:
        session = make_session()
        connects = []
        with (
            patch.object(session, "connect", side_effect=lambda: connects.append(1)),
            patch.object(RealtimeSession, "_reconnect_delay", return_value=0.01),
            patch("voice_cua.realtime_session.island_publish"),
        ):
            thread = threading.Thread(target=session.run, daemon=True)
            thread.start()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and len(connects) < 3:
                time.sleep(0.01)
            session.shutdown()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertGreaterEqual(len(connects), 3)
        # Backoff grows across failed attempts until a stable connection resets it.
        self.assertGreaterEqual(session._reconnect_attempts, 3)

    def test_backoff_grows_across_consecutive_failed_attempts(self) -> None:
        session = make_session()
        delays: list[float] = []
        calls = {"n": 0}

        def failing_connect() -> None:
            calls["n"] += 1
            raise OSError("refused")

        def fake_wait(timeout: float | None = None) -> bool:
            delays.append(float(timeout or 0))
            return len(delays) >= 5  # stop after 5 retries

        with patch.object(session, "connect", side_effect=failing_connect), \
                patch.object(session._stop, "wait", side_effect=fake_wait), \
                patch.object(session, "_schedule_planned_reconnect"), \
                patch("voice_cua.realtime_session.island_publish"), \
                patch("voice_cua.realtime_session.random.uniform", side_effect=lambda _lo, hi: hi):
            session.run()
        self.assertEqual(delays, [0.5, 1.0, 2.0, 4.0, 8.0])
        self.assertEqual(session._reconnect_attempts, 5)
        self.assertEqual(calls["n"], 5)

    def test_prepare_reconnect_fails_text_waiters_with_reconnected(self) -> None:
        session = make_session()
        result: dict = {}

        def waiter() -> None:
            result.update(session.send_text_and_wait("ping", timeout=5))

        thread = threading.Thread(target=waiter, daemon=True)
        thread.start()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not session._text_waiters:
            time.sleep(0.01)
        self.assertTrue(session._text_waiters)
        session._prepare_reconnect()
        thread.join(timeout=2)
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "reconnected")

    def test_prepare_reconnect_clears_session_state(self) -> None:
        session = make_session()
        audio = FakeAudio()
        session._audio = audio  # type: ignore[assignment]
        with session._response_condition:
            session._active_response_ids.add("r1")
            session._speaking_response_ids.add("r1")
            session._response_transcripts["r1"] = "partial"
            session._current_response_id = "r1"
            session._session_ready.set()
        session._pending_calls["c1"] = {"name": "cua_state"}
        session._prepare_reconnect()
        with session._response_condition:
            self.assertEqual(session._active_response_ids, set())
            self.assertEqual(session._speaking_response_ids, set())
            self.assertEqual(session._response_transcripts, {})
            self.assertEqual(session._current_response_id, "")
        self.assertEqual(session._pending_calls, {})
        self.assertFalse(session._session_ready.is_set())
        self.assertTrue(audio.cleared)

    def test_planned_reconnect_busy_reschedules(self) -> None:
        session = make_session()
        with session._response_condition:
            session._active_response_ids.add("r1")
        with (
            patch.object(session, "_schedule_planned_reconnect") as schedule,
            patch.object(session, "_request_reconnect") as request,
        ):
            session._planned_reconnect()
        request.assert_not_called()
        schedule.assert_called_once_with(delay=RealtimeSession.PLANNED_RECONNECT_RETRY_SECONDS)

    def test_planned_reconnect_idle_requests_reconnect(self) -> None:
        session = make_session()
        with patch.object(session, "_request_reconnect") as request:
            session._planned_reconnect()
        request.assert_called_once()
        self.assertNotIn("__closed__", event_types(session))


class BargeInTests(unittest.TestCase):
    """P1-5: truncate with actually-played ms; cancel when interrupt_response off."""

    def test_truncate_payload_uses_played_ms(self) -> None:
        session = make_session()
        session._audio = FakeAudio(played_ms=700, item_id="item_1")  # type: ignore[assignment]
        with session._response_condition:
            session._active_response_ids.add("resp1")
            session._current_response_id = "resp1"
        with patch("voice_cua.realtime_session.island_publish"):
            feed(session, {"type": "input_audio_buffer.speech_started"})
        types = event_types(session)
        self.assertNotIn("response.cancel", types)  # balanced: server interrupts itself
        self.assertIn("conversation.item.truncate", types)
        truncate = session.ws.events[types.index("conversation.item.truncate")]
        self.assertEqual(truncate["item_id"], "item_1")
        self.assertEqual(truncate["content_index"], 0)
        self.assertEqual(truncate["audio_end_ms"], 700)

    def test_cancel_sent_when_interrupt_response_false(self) -> None:
        session = make_session(eagerness="polite")  # turn_detection.interrupt_response: False
        audio = FakeAudio(played_ms=450, item_id="item_2")
        session._audio = audio  # type: ignore[assignment]
        with session._response_condition:
            session._active_response_ids.add("resp1")
            session._current_response_id = "resp1"
        with patch("voice_cua.realtime_session.island_publish"):
            feed(session, {"type": "input_audio_buffer.speech_started"})
            feed(session, {
                "type": "response.output_audio.delta",
                "response_id": "resp1",
                "item_id": "item_2",
                "delta": "AAAA",
            })
        types = event_types(session)
        self.assertIn("response.cancel", types)
        truncate = session.ws.events[types.index("conversation.item.truncate")]
        self.assertEqual(truncate["audio_end_ms"], 450)
        self.assertEqual(truncate["item_id"], "item_2")
        # Deltas for the cancelled response are dropped, not pushed to playback.
        self.assertEqual(audio.received, [])
        self.assertIn("resp1", session._cancelled_response_ids)

    def test_no_truncate_without_playback(self) -> None:
        session = make_session()

        class IdleAudio(FakeAudio):
            def has_playback(self) -> bool:
                return False

        session._audio = IdleAudio()  # type: ignore[assignment]
        with patch("voice_cua.realtime_session.island_publish"):
            feed(session, {"type": "input_audio_buffer.speech_started"})
        self.assertEqual(session.ws.events, [])

    def test_truncated_event_handled(self) -> None:
        session = make_session()
        feed(session, {"type": "conversation.item.truncated", "item_id": "item_1", "audio_end_ms": 700})

    def test_speaker_played_ms(self) -> None:
        import numpy as np

        from voice_cua.audio_io import SpeakerPlayer

        player = SpeakerPlayer()
        silence = b"\x00\x01" * 480  # 960 bytes = 480 samples = 20 ms @ 24 kHz
        player.push_b64(base64.b64encode(silence).decode("ascii"))
        outdata = np.zeros((480, 1), dtype=np.int16)
        player._output_callback(outdata, 480, None, None)
        self.assertEqual(player.played_ms(), 20)
        player.clear()
        self.assertEqual(player.played_ms(), 0)


class ErrorHandlingTests(unittest.TestCase):
    """P1-6: structured classification + rate-limit threshold logging."""

    def test_classify_recoverable(self) -> None:
        cases = [
            {"code": "response_cancel_not_active", "message": "Cannot cancel an inactive response"},
            {"code": "input_audio_buffer_truncate_past_end", "message": "..."},
            {"code": "conversation_item_not_found", "param": "item_id"},
        ]
        for case in cases:
            self.assertEqual(RealtimeSession._classify_error(case), "recoverable", case)

    def test_classify_config_and_session_and_client(self) -> None:
        self.assertEqual(
            RealtimeSession._classify_error({
                "type": "invalid_request_error",
                "message": "Unknown parameter: session.audio.x",
            }),
            "config",
        )
        self.assertEqual(
            RealtimeSession._classify_error({"message": "Session expired"}),
            "session",
        )
        self.assertEqual(
            RealtimeSession._classify_error({"type": "server_error", "message": "internal"}),
            "client",
        )

    def test_recoverable_errors_do_not_publish_island_error(self) -> None:
        session = make_session()
        published: list[str] = []
        with patch(
            "voice_cua.realtime_session.island_publish",
            side_effect=lambda kind, **kw: published.append(kind),
        ):
            feed(session, {
                "type": "error",
                "error": {
                    "code": "response_cancel_not_active",
                    "param": "",
                    "event_id": "evt_1",
                },
            })
            feed(session, {
                "type": "error",
                "error": {"code": "conversation_item_not_found", "param": "item_id"},
            })
        self.assertNotIn("error", published)

    def test_config_error_published_once_with_code_and_param(self) -> None:
        session = make_session()
        published: list[str] = []
        logged: list[dict] = []
        with patch(
            "voice_cua.realtime_session.island_publish",
            side_effect=lambda kind, **kw: published.append(kind),
        ), patch(
            "voice_cua.realtime_session.log_event",
            side_effect=lambda kind, **kw: logged.append(kw),
        ):
            for _ in range(2):
                feed(session, {
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "code": "invalid_session_config",
                        "param": "session.audio.output.voice",
                        "message": "Unknown voice",
                        "event_id": "evt_2",
                    },
                })
        self.assertEqual(published.count("error"), 1)
        self.assertTrue(any(
            entry.get("code") == "invalid_session_config"
            and entry.get("param") == "session.audio.output.voice"
            and entry.get("event_id") == "evt_2"
            for entry in logged
        ))

    def test_session_error_triggers_reconnect(self) -> None:
        session = make_session()
        with (
            patch("voice_cua.realtime_session.island_publish"),
            patch.object(session, "_request_reconnect") as request,
        ):
            feed(session, {"type": "error", "error": {"message": "Session expired"}})
        request.assert_called_once()

    def test_rate_limit_low_logged_once_below_10_percent(self) -> None:
        session = make_session()
        low_events: list[dict] = []
        calls = log_event

        def capture(kind: str, **kwargs: object) -> None:
            if kind == "rate_limit_low":
                low_events.append(dict(kwargs))
            calls(kind, **kwargs)

        with patch("voice_cua.realtime_session.log_event", side_effect=capture):
            feed(session, {"type": "rate_limits.updated", "rate_limits": [
                {"name": "requests", "limit": 100, "remaining": 99},
                {"name": "tokens", "limit": 10000, "remaining": 500},
            ]})
            feed(session, {"type": "rate_limits.updated", "rate_limits": [
                {"name": "tokens", "limit": 10000, "remaining": 200},
            ]})
            # Back above 10% resets the warning latch.
            feed(session, {"type": "rate_limits.updated", "rate_limits": [
                {"name": "tokens", "limit": 10000, "remaining": 9000},
            ]})
            feed(session, {"type": "rate_limits.updated", "rate_limits": [
                {"name": "tokens", "limit": 10000, "remaining": 100},
            ]})
        self.assertEqual(len(low_events), 2)
        self.assertEqual(low_events[0]["remaining"], 500)
        self.assertEqual(low_events[0]["limit"], 10000)

    def test_rate_limit_not_logged_above_10_percent(self) -> None:
        session = make_session()
        with (
            tempfile.TemporaryDirectory() as td,
            patch.dict(os.environ, {"VOICE_CUA_ACTIVITY_LOG": str(Path(td) / "activity.jsonl")}),
        ):
            feed(session, {"type": "rate_limits.updated", "rate_limits": [
                {"name": "tokens", "limit": 10000, "remaining": 5000},
            ]})
            lows = [e for e in tail_events(limit=100) if e.get("kind") == "rate_limit_low"]
            self.assertEqual(lows, [])


if __name__ == "__main__":
    unittest.main()
