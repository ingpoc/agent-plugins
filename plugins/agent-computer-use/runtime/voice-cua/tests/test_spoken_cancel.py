from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from voice_cua import activity_log  # noqa: E402
from voice_cua.live_session import LiveSession  # noqa: E402
from voice_cua.providers import LiveProvider, XAIProvider  # noqa: E402
from test_phase1_reliability import FakeAudio, make_session  # noqa: E402


class FakeWS:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def send(self, raw: str) -> None:
        self.events.append(json.loads(raw))

    def close(self) -> None:
        self.events.append({"type": "__closed__"})


class SpokenStopTests(unittest.TestCase):
    @staticmethod
    def _redirect_session(provider):
        session = make_session()
        provider.bind(session._send_wire)
        if isinstance(provider, LiveProvider):
            provider._ready = True
        session.provider = provider
        session.ws = FakeWS()
        session._audio = FakeAudio()
        response_id = f"active-{provider.name}"
        with session._response_lock:
            session._current_response_id = response_id
            session._active_response_ids.add(response_id)
            session._response_task_run_ids[response_id] = "previous-request"
        return session

    def test_typed_redirect_waits_for_cancel_settlement_before_creating_response(self) -> None:
        for provider in (XAIProvider("grok-voice-latest"), LiveProvider()):
            with self.subTest(provider=provider.name), \
                    patch("voice_cua.workflow_store.begin_engagement"), \
                    patch("voice_cua.tools.note_user_turn"), \
                    patch("voice_cua.live_session.island_publish"), \
                    patch.object(activity_log, "_append_row"):
                session = self._redirect_session(provider)
                session.RESPONSE_CANCEL_TIMEOUT_S = 1.0
                audio = session._audio
                send = threading.Thread(target=session.send_text, args=("Actually, open Calendar instead.",))
                send.start()
                cancel_type = "response.cancel" if provider.name == "xai" else "session.instructions.append"
                deadline = time.monotonic() + 1.0
                while cancel_type not in [event["type"] for event in session.ws.events] and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertIn(cancel_type, [event["type"] for event in session.ws.events])
                self.assertTrue(send.is_alive())
                self.assertNotIn("response.create", [event["type"] for event in session.ws.events])
                self.assertNotIn("conversation.item.create", [event["type"] for event in session.ws.events])
                self.assertNotIn("response.item.create", [event["type"] for event in session.ws.events])
                session._audio = None
                session._dispatch_event({"type": "response.done", "response": {
                    "id": f"active-{provider.name}", "status": "cancelled",
                }})
                send.join(timeout=1.0)
                self.assertFalse(send.is_alive())
                request_id = session._active_task_run_id
                session._dispatch_event({"type": "response.created", "response": {
                    "id": "redirect-response", "metadata": {"voice_cua_request_id": request_id},
                }})
                self.assertNotIn(request_id, session._response_start_watchdogs)
            self.assertTrue(audio.cleared)
            types = [event["type"] for event in session.ws.events]
            self.assertIn("response.create", types)
            if provider.name == "xai":
                self.assertIn("response.cancel", types)
            else:
                self.assertIn("session.instructions.append", types)
                self.assertNotIn("response.cancel", types)

    def test_spoken_redirect_interrupts_both_provider_answers_and_audio(self) -> None:
        for provider in (XAIProvider("grok-voice-latest"), LiveProvider()):
            with self.subTest(provider=provider.name), \
                    patch("voice_cua.workflow_store.begin_engagement"), \
                    patch("voice_cua.tools.note_user_turn"), \
                    patch("voice_cua.live_session.island_publish"):
                session = self._redirect_session(provider)
                audio = session._audio
                session._dispatch_event({
                    "type": "conversation.item.input_audio_transcription.completed",
                    "transcript": "Actually open Calendar instead.", "item_id": "redirect",
                })
                request_id = session._active_task_run_id
                session._dispatch_event({"type": "response.created", "response": {"id": "spoken-redirect"}})
                self.assertNotIn(request_id, session._response_start_watchdogs)
            self.assertTrue(audio.cleared)
            types = [event["type"] for event in session.ws.events]
            if provider.name == "xai":
                self.assertIn("response.cancel", types)
            else:
                self.assertIn("session.instructions.append", types)
                self.assertNotIn("response.cancel", types)

    def test_typed_redirect_aborts_when_cancel_does_not_settle(self) -> None:
        for provider in (XAIProvider("grok-voice-latest"), LiveProvider()):
            with self.subTest(provider=provider.name), \
                    patch("voice_cua.workflow_store.begin_engagement"), \
                    patch("voice_cua.tools.note_user_turn"), \
                    patch("voice_cua.live_session.island_publish") as publish, \
                    patch.object(activity_log, "_append_row"):
                session = self._redirect_session(provider)
                session.RESPONSE_CANCEL_TIMEOUT_S = 0.01
                session.send_text("Actually, open Calendar instead.")
                types = [event["type"] for event in session.ws.events]
                self.assertNotIn("response.create", types)
                self.assertNotIn("conversation.item.create", types)
                self.assertNotIn("response.item.create", types)
                self.assertTrue(any(call.args[0] == "error"
                                    and call.kwargs.get("title") == "Couldn't stop"
                                    for call in publish.call_args_list))

    def test_direct_request_without_response_start_reconnects_and_reports_error(self) -> None:
        for provider in (XAIProvider("grok-voice-latest"), LiveProvider()):
            with self.subTest(provider=provider.name), \
                    patch("voice_cua.workflow_store.begin_engagement"), \
                    patch("voice_cua.tools.note_user_turn"), \
                    patch("voice_cua.live_session.island_publish") as publish, \
                    patch.object(activity_log, "_append_row"):
                session = make_session()
                provider.bind(session._send_wire)
                if isinstance(provider, LiveProvider):
                    provider._ready = True
                session.provider = provider
                session.ws = FakeWS()
                session._session_ready.set()
                session.RESPONSE_START_TIMEOUT_S = 0.01
                session.send_text("Open Calendar")
                deadline = time.monotonic() + 1.0
                while {"type": "__closed__"} not in session.ws.events and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertIn({"type": "__closed__"}, session.ws.events)
                self.assertFalse(session._session_ready.is_set())
                self.assertTrue(any(call.args[0] == "error"
                                    and call.kwargs.get("title") == "No response"
                                    and "try again" in call.kwargs.get("detail", "")
                                    for call in publish.call_args_list))

    def test_live_output_events_start_a_direct_request_response_watchdog(self) -> None:
        for output_type in ("session.output_audio.delta", "session.output_transcript.delta"):
            with self.subTest(output_type=output_type), \
                    patch("voice_cua.workflow_store.begin_engagement"), \
                    patch("voice_cua.tools.note_user_turn"), \
                    patch("voice_cua.live_session.island_publish"), \
                    patch.object(activity_log, "_append_row"):
                provider = LiveProvider()
                session = make_session()
                provider.bind(session._send_wire)
                provider._ready = True
                session.provider = provider
                session.ws = FakeWS()
                session.RESPONSE_START_TIMEOUT_S = 1.0
                session.send_text("Open Calendar")
                request_id = session._active_task_run_id
                self.assertIn(request_id, session._response_start_watchdogs)
                if output_type == "session.output_transcript.delta":
                    session._on_message(session.ws, json.dumps({"type": output_type, "delta": "Done."}))
                else:
                    event = {"type": "response.output_audio.delta", "response_id": "live_out_1", "delta": ""}
                    session._dispatch_event(event)
                self.assertNotIn(request_id, session._response_start_watchdogs)

    def test_spoken_request_without_response_start_reconnects_and_reports_error(self) -> None:
        for provider in (XAIProvider("grok-voice-latest"), None):
            with self.subTest(provider=provider.name if provider else "openai-live"), \
                    patch("voice_cua.workflow_store.begin_engagement"), \
                    patch("voice_cua.tools.note_user_turn"), \
                    patch("voice_cua.live_session.island_publish") as publish, \
                    patch("voice_cua.island_facade.island_publish"), \
                    patch.object(activity_log, "_append_row"):
                session = make_session()
                if provider is None:
                    provider = LiveProvider()
                    provider.bind(session._send_wire,
                                  on_spoken_request=session.provider._on_spoken_request)
                    session.provider = provider
                else:
                    provider.bind(session._send_wire)
                    session.provider = provider
                if isinstance(provider, LiveProvider):
                    provider._ready = True
                session.ws = FakeWS()
                session._session_ready.set()
                session.RESPONSE_START_TIMEOUT_S = 0.01
                if provider.name == "xai":
                    session._dispatch_event({
                        "type": "conversation.item.input_audio_transcription.completed",
                        "transcript": "Open Calendar", "item_id": "spoken-request",
                    })
                else:
                    session._on_message(session.ws, json.dumps({
                        "type": "session.input_transcript.delta", "delta": "Open Calendar",
                        "start_ms": 0, "end_ms": 900,
                    }))
                    provider.heard.flush()
                deadline = time.monotonic() + 1.0
                while {"type": "__closed__"} not in session.ws.events and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertIn({"type": "__closed__"}, session.ws.events)
                self.assertFalse(session._session_ready.is_set())
                self.assertTrue(any(call.args[0] == "error"
                                    and call.kwargs.get("title") == "No response"
                                    and "try again" in call.kwargs.get("detail", "")
                                    for call in publish.call_args_list))

    def test_grok_request_with_no_response_start_reconnects_and_returns_error(self) -> None:
        session = make_session()
        provider = XAIProvider("grok-voice-latest")
        provider.bind(session._send_wire)
        session.provider = provider
        session.ws = FakeWS()
        session._session_ready.set()
        session.RESPONSE_START_TIMEOUT_S = 0.01
        with patch("voice_cua.workflow_store.begin_engagement"), \
                patch("voice_cua.tools.note_user_turn"), \
                patch("voice_cua.live_session.island_publish"):
            result = session.send_text_and_wait("Tell me about the weather", timeout=None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "response_start_timeout")
        self.assertIn("Reconnecting", result["error"])
        self.assertIn({"type": "__closed__"}, session.ws.events)
        self.assertFalse(session._session_ready.is_set())

    def test_response_create_send_failure_reconnects_and_settles_typed_request(self) -> None:
        class FailResponseCreateWS(FakeWS):
            def send(self, raw: str) -> None:
                event = json.loads(raw)
                if event.get("type") == "response.create":
                    raise ConnectionError("socket closed")
                super().send(raw)

        session = make_session()
        provider = XAIProvider("grok-voice-latest")
        provider.bind(session._send_wire)
        session.provider = provider
        session.ws = FailResponseCreateWS()
        session._session_ready.set()
        with patch("voice_cua.workflow_store.begin_engagement"), \
                patch("voice_cua.tools.note_user_turn"), \
                patch("voice_cua.live_session.island_publish"):
            result = session.send_text_and_wait("Open Calculator", timeout=None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "send_failed")
        self.assertIn({"type": "__closed__"}, session.ws.events)
        self.assertFalse(session._session_ready.is_set())

    def test_typed_stop_uses_cancel_path_and_is_not_a_new_request(self) -> None:
        session = LiveSession(api_key="test", enable_audio=False)
        session.ws = FakeWS()
        calls = []
        session.island_control = lambda action, **kw: calls.append((action, kw)) or True  # type: ignore
        session._active_task_run_id = "run-typed"
        session.send_text("Uh, stop.")
        self.assertEqual(calls[0][0], "cancel")
        self.assertEqual(calls[0][1]["cancel_source"], "transcript")
        self.assertEqual(session.ws.events, [])

    def test_detector_matches_short_stop_intents_only(self) -> None:
        for phrase in (
            "stop", "please stop", "Wait, stop", "cancel that", "never mind",
            "leave it", "stop it", "you can stop", "that's enough",
        ):
            with self.subTest(phrase=phrase):
                self.assertTrue(LiveSession._spoken_task_cancel(phrase))
        for phrase in (
            "don't stop", "keep going", "stop by the store and open Safari",
            "please stop by the store", "the stop button is there",
        ):
            with self.subTest(phrase=phrase):
                self.assertFalse(LiveSession._spoken_task_cancel(phrase))

    def test_spoken_stop_between_response_start_and_dispatch_skips_action(self) -> None:
        session = LiveSession(api_key="test", enable_audio=False)
        session.ws = FakeWS()
        run_id = "run-spoken-stop"
        session._active_task_run_id = run_id
        session._task_runs[run_id] = []
        rows: list[dict] = []

        with patch.object(activity_log, "_append_row", side_effect=rows.append), \
                patch("voice_cua.live_session.dispatch") as dispatch, \
                patch("voice_cua.live_session.island_publish"):
            session._dispatch_event({"type": "response.created", "response": {
                "id": "response-before-stop", "metadata": {"voice_cua_request_id": run_id},
            }})
            session._dispatch_event({"type": "conversation.item.input_audio_transcription.completed",
                                     "transcript": "Wait, stop", "item_id": "stop-transcript"})
            session._handle_function_done({"call_id": "call-after-stop", "name": "cua_act",
                                           "arguments": "{}", "response_id": "response-before-stop"})
            deadline = time.monotonic() + 2
            while session._inflight_tools and time.monotonic() < deadline:
                time.sleep(0.01)

        dispatch.assert_not_called()
        cancelled = [row for row in rows if row.get("kind") == "tool_cancelled"]
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(cancelled[0]["classification"], "user_cancelled")
        cancel = next(row for row in rows if row.get("kind") == "cancel")
        self.assertEqual(cancel["cancel_source"], "transcript")
        self.assertEqual(cancel["caused_by"][-1], next(
            row["step_ref"] for row in rows
            if row.get("kind") == "user_transcript" and row.get("text") == "Wait, stop"
        ))


if __name__ == "__main__":
    unittest.main()


def test_detector_ignores_leading_filler_words() -> None:
    from voice_cua.live_session import LiveSession
    for phrase in ("Uh, you can stop", "Um, wait, stop", "Okay, please stop.", "Hey Samantha, cancel that"):
        assert LiveSession._spoken_task_cancel(phrase), phrase
    assert not LiveSession._spoken_task_cancel("Uh, don't stop")
