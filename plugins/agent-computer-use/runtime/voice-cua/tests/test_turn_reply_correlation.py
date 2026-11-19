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

from voice_cua import activity_log, providers  # noqa: E402
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


class PlaybackAudio(FakeAudio):
    def has_playback(self) -> bool:
        return bool(self.received) and not self.cleared

    def on_output_delta(self, audio_b64: str, *, item_id: str = "") -> None:
        self.cleared = False
        super().on_output_delta(audio_b64, item_id=item_id)

    def pause_input(self) -> None:
        pass

    def resume_input(self) -> None:
        pass


class TypedTurnReplayTests(unittest.TestCase):
    STORY = "Tell me a detailed, long story about the history of Pune, at least ten sentences."
    REDIRECT = "Actually, stop that. Just tell me in one short sentence what Pune is famous for."
    RECALL = "Quick one: what did I say my favourite colour was?"
    MULTI_STEP = ("Please open the Calculator app, then open https://example.com in your isolated Comet browser, "
                  "tell me the page heading, and close that Comet test window.")
    EMPATHY = "Honestly I've had a rough day. My project slipped again and I feel like I'm letting everyone down."

    def _make(self, provider):
        session = make_session()
        provider.bind(session._send_wire)
        if isinstance(provider, LiveProvider):
            provider._ready = True
        session.provider = provider
        session.ws = FakeWS()
        session._audio = PlaybackAudio()
        session._session_ready.set()
        return session

    @staticmethod
    def _wait_for(predicate, message: str) -> None:
        deadline = time.monotonic() + 2.0
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.005)
        if not predicate():
            raise AssertionError(message)

    @staticmethod
    def _create_count(session) -> int:
        return sum(event.get("type") == "response.create" for event in session.ws.events)

    def _launch_turn(self, session, text: str):
        count = self._create_count(session)
        result: dict = {}
        thread = threading.Thread(target=lambda: result.update(session.send_text_and_wait(text, timeout=3)))
        thread.daemon = True
        thread.start()
        return thread, result, count

    def _start_turn(self, session, text: str):
        thread, result, count = self._launch_turn(session, text)
        self._wait_for(lambda: self._create_count(session) > count, f"response.create missing for {text[:24]}")
        return thread, result, session._active_task_run_id

    def _event_id(self, session, request_id: str) -> str:
        return next((str(event.get("event_id") or "") for event in reversed(session.ws.events)
                     if event.get("type") == "response.create"
                     and str(event.get("event_id") or "").startswith(f"voice_text_{request_id}_")), "")

    def _response_created(self, session, provider, delegation: str, response_id: str) -> None:
        if isinstance(provider, LiveProvider):
            session._on_message(None, json.dumps({"type": "session.delegation.created",
                                                 "delegation": {"id": delegation, "target": "responses"}}))
            self._live_event(session, delegation, {"type": "response.created", "response": {"id": response_id}})
        else:
            session._on_message(None, json.dumps({"type": "response.created", "response": {"id": response_id}}))

    @staticmethod
    def _live_event(session, delegation: str, inner: dict) -> None:
        session._on_message(None, json.dumps({"type": "response.event", "delegation_id": delegation,
                                              "event": inner}))

    def _response_done(self, session, provider, delegation: str, response_id: str,
                       text: str, *, status: str = "completed", tool_calls: list[dict] | None = None) -> None:
        output = tool_calls or [{"type": "message", "content": [{"type": "output_text", "text": text}]}]
        if isinstance(provider, LiveProvider):
            if not tool_calls:
                self._live_event(session, delegation, {"type": "response.output_item.done", "response_id": response_id,
                                   "item": output[0]})
            self._live_event(session, delegation, {"type": "response.completed", "response": {
                "id": response_id, "status": status, "output": output,
            }})
        else:
            session._on_message(None, json.dumps({"type": "response.done", "response": {
                "id": response_id, "status": status, "output": output,
            }}))

    def _feed_late_output(self, session, provider, response_id: str, event_id: str) -> None:
        if isinstance(provider, LiveProvider):
            session._on_message(None, json.dumps({"type": "session.output_transcript.delta",
                "client_event_id": event_id, "start_ms": 3000, "end_ms": 3500,
                "delta": "The cancelled story kept going."}))
            session._on_message(None, json.dumps({"type": "session.output_audio.delta",
                "client_event_id": event_id, "delta": "QQ=="}))
        else:
            session._on_message(None, json.dumps({"type": "response.output_audio.delta",
                "response_id": response_id, "item_id": "story-audio", "delta": "QQ=="}))
            session._on_message(None, json.dumps({"type": "response.output_audio_transcript.done",
                "response_id": response_id, "transcript": "The cancelled story kept going."}))

    def _replay(self, provider) -> None:
        session = self._make(provider)
        rows: list[dict] = []
        dispatches: list[tuple[str, dict]] = []
        fake_result = lambda name, args: dispatches.append((name, dict(args))) or {
            "ok": True, "verified": True, "summary": f"{name} done"}
        with patch("voice_cua.workflow_store.begin_engagement"), \
                patch("voice_cua.tools.note_user_turn"), \
                patch("voice_cua.live_session.island_publish"), \
                patch("voice_cua.live_session.dispatch", side_effect=fake_result), \
                patch("voice_cua.live_session.plugin_island.tool_started"), \
                patch("voice_cua.live_session.plugin_island.island_app", side_effect=lambda _name, app: app), \
                patch.object(session, "_wait_for_tools_update_ack"), \
                patch.object(session, "_sync_tool_groups"), \
                patch.object(session, "_on_comet_change"), \
                patch.object(activity_log, "_append_row", side_effect=rows.append):
            story_thread, story_result, story_request = self._start_turn(session, self.STORY)
            self._response_created(session, provider, "story-delegation", "story-response")
            story_event_id = self._event_id(session, story_request)
            if isinstance(provider, LiveProvider):
                session._on_message(None, json.dumps({"type": "session.output_audio.delta",
                    "client_event_id": story_event_id, "delta": "QQ=="}))
            else:
                session._on_message(None, json.dumps({"type": "response.output_audio.delta",
                    "response_id": "story-response", "item_id": "story-audio", "delta": "QQ=="}))
            story_audio_chunks = len(session._audio.received)
            time.sleep(0.03)  # scaled 3s scenario gap; keep the offline replay fast
            if isinstance(provider, LiveProvider):
                self._live_event(session, "story-delegation", {"type": "response.output_item.done",
                    "response_id": "story-response", "item": {"type": "message", "content": [
                        {"type": "output_text", "text": "Pune began as a town on the Mutha River."}]}})
            else:
                session._on_message(None, json.dumps({"type": "response.output_audio_transcript.done",
                    "response_id": "story-response", "transcript": "Pune began as a town on the Mutha River."}))

            redirect_thread, redirect_result, redirect_create_count = self._launch_turn(session, self.REDIRECT)
            cancel_type = "session.instructions.append" if isinstance(provider, LiveProvider) else "response.cancel"
            self._wait_for(lambda: any(e.get("type") == cancel_type for e in session.ws.events),
                           "redirect did not interrupt the active story")
            self.assertEqual(self._create_count(session), 1, "redirect must wait for cancel settlement")
            self._feed_late_output(session, provider, "story-response", story_event_id)
            self.assertEqual(len(session._audio.received), story_audio_chunks,
                             "late audio from the cancelled story must be dropped")
            # A cancelled story response carries only the partial text already produced.
            self._response_done(session, provider, "story-delegation", "story-response",
                                "Pune began as a town on the Mutha River.", status="cancelled")
            self._wait_for(lambda: self._create_count(session) > redirect_create_count,
                           "redirect response.create missing after cancellation")
            redirect_request = session._active_task_run_id
            self._response_created(session, provider, "redirect-delegation", "redirect-response")
            self._response_done(session, provider, "redirect-delegation", "redirect-response",
                                "Pune is famous for its Maratha heritage and universities.")
            redirect_thread.join(timeout=2)
            story_thread.join(timeout=2)
            self.assertFalse(redirect_thread.is_alive())
            self.assertFalse(story_thread.is_alive())
            self.assertEqual(story_result.get("reply"), "Pune began as a town on the Mutha River.")
            self.assertFalse(story_result.get("ok"), "the interrupted story should settle as cancelled")
            self.assertEqual(redirect_result.get("reply"), "Pune is famous for its Maratha heritage and universities.")

            redirect_event_id = self._event_id(session, redirect_request)
            recall_thread, recall_result, _recall_request = self._start_turn(session, self.RECALL)
            self._feed_late_output(session, provider, "redirect-response", redirect_event_id)
            self._response_created(session, provider, "recall-delegation", "recall-response")
            self._response_done(session, provider, "recall-delegation", "recall-response", "You said teal.")
            recall_thread.join(timeout=2)
            self.assertFalse(recall_thread.is_alive())
            self.assertEqual(recall_result.get("reply"), "You said teal.")

            before_multi = self._create_count(session)
            multi_thread, multi_result, multi_request = self._start_turn(session, self.MULTI_STEP)
            self._response_created(session, provider, "multi-delegation", "multi-tools-response")
            tool_calls = [
                {"type": "function_call", "call_id": "call-calc", "name": "cua_act", "arguments": "{\"app\":\"Calculator\"}"},
                {"type": "function_call", "call_id": "call-comet-begin", "name": "comet_begin", "arguments": "{\"url\":\"https://example.com\"}"},
                {"type": "function_call", "call_id": "call-comet-read", "name": "comet_act", "arguments": "{\"action\":\"snapshot\"}"},
                {"type": "function_call", "call_id": "call-comet-end", "name": "comet_end", "arguments": "{}"},
            ]
            for item in tool_calls:
                if isinstance(provider, LiveProvider):
                    self._live_event(session, "multi-delegation", {"type": "response.output_item.done",
                        "response_id": "multi-tools-response", "item": item})
                else:
                    session._on_message(None, json.dumps({"type": "response.output_item.done",
                        "response_id": "multi-tools-response", "item": item}))
            # Providers may redeliver a function-call event; the same call ID must execute once.
            if isinstance(provider, LiveProvider):
                self._live_event(session, "multi-delegation", {"type": "response.output_item.done",
                    "response_id": "multi-tools-response", "item": tool_calls[0]})
            else:
                session._on_message(None, json.dumps({"type": "response.output_item.done",
                    "response_id": "multi-tools-response", "item": tool_calls[0]}))
            self._response_done(session, provider, "multi-delegation", "multi-tools-response", "",
                                tool_calls=tool_calls)
            self._wait_for(lambda: len(dispatches) == 4 and not session._pending_calls,
                           "delegated Calculator and Comet tools did not finish")
            self._wait_for(lambda: self._create_count(session) > before_multi + 1,
                           "tool continuation response.create missing")
            self._response_created(session, provider, "multi-delegation", "multi-final-response")
            self._response_done(session, provider, "multi-delegation", "multi-final-response",
                                "Calculator is open; Example Domain was read and the Comet window is closed.")
            multi_thread.join(timeout=2)
            self.assertFalse(multi_thread.is_alive())
            self.assertEqual(multi_result.get("reply"),
                             "Calculator is open; Example Domain was read and the Comet window is closed.")
            self.assertEqual([name for name, _ in dispatches],
                             ["cua_act", "comet_begin", "comet_act", "comet_end"])

            empathy_thread, empathy_result, _empathy_request = self._start_turn(session, self.EMPATHY)
            self._response_created(session, provider, "empathy-delegation", "empathy-response")
            empathy = "I'm sorry; that sounds heavy, and a setback doesn't mean you've let everyone down."
            self._response_done(session, provider, "empathy-delegation", "empathy-response", empathy)
            empathy_thread.join(timeout=2)
            self.assertFalse(empathy_thread.is_alive())
            self.assertEqual(empathy_result.get("reply"), empathy)

            live_replies = [r for r in rows if r.get("kind") == "assistant_reply" and r.get("source") == "live_voice"]
            self.assertFalse(any("cancelled story kept going" in r.get("text", "") for r in live_replies))
            self.assertEqual(len([r for r in rows if r.get("kind") == "assistant_reply"
                                  and r.get("source") == "backend"]), 5)

    def test_grok_voice_replays_five_turns_with_own_replies_and_tools_once(self) -> None:
        self._replay(XAIProvider("grok-voice-latest"))

    def test_gpt_live_replays_five_turns_with_own_replies_and_tools_once(self) -> None:
        self._replay(LiveProvider())

    def test_gpt_live_transcript_event_id_keeps_its_request_and_drops_retired_output(self) -> None:
        p = LiveProvider()
        rows: list[tuple[str, dict]] = []
        p._ready = True
        p.outbound({"type": "response.create", "event_id": "turn-a-event", "response": {
            "metadata": {"voice_cua_request_id": "turn-a"}}})
        p.outbound({"type": "response.create", "event_id": "turn-b-event", "response": {
            "metadata": {"voice_cua_request_id": "turn-b"}}})
        def capture(kind, **fields):
            if kind == "assistant_reply":
                rows.append((fields["text"], {"run_id": fields.get("run_id", "")}))

        with patch.object(providers, "log_event", side_effect=capture), patch("builtins.print"):
            self.assertEqual(p.inbound({"type": "session.output_transcript.delta", "client_event_id": "turn-a-event",
                                        "delta": "stale"}), [])
            output = p.inbound({"type": "session.output_transcript.delta", "client_event_id": "turn-b-event",
                                "delta": "current"})
            p.said.flush()
        self.assertEqual(output[0]["response"]["metadata"]["voice_cua_request_id"], "turn-b")
        self.assertEqual(rows, [("current", {"run_id": "turn-b"})])
        self.assertIn(("client_event_id", "turn-a-event"), p._retired_live_output_ids)

    def test_gpt_live_untagged_reply_settles_its_typed_waiter(self) -> None:
        provider = LiveProvider()
        session = self._make(provider)
        provider.said = providers.TranscriptBurst(provider._on_said, gap_s=60)
        with patch("voice_cua.workflow_store.begin_engagement"), \
                patch("voice_cua.tools.note_user_turn"), \
                patch("voice_cua.live_session.island_publish"), \
                patch.object(activity_log, "_append_row"), patch("builtins.print"):
            thread, result, request_id = self._start_turn(session, "Tell me what this turn is about.")
            session._on_message(None, json.dumps({"type": "response.created", "response": {"id": "typed-live-response"}}))
            session._on_message(None, json.dumps({"type": "session.output_transcript.delta",
                "response_id": "typed-live-response", "item_id": "typed-live-item", "delta": "This turn is distinct."}))
            session._on_message(None, json.dumps({"type": "response.done", "response": {
                "id": "typed-live-response", "status": "completed", "output": [{"type": "message",
                    "content": [{"type": "output_text", "text": "This turn is distinct."}]}]}}))
            provider.said.flush()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result.get("reply"), "This turn is distinct.")
        self.assertTrue(request_id)

    def test_gpt_live_untagged_retired_output_cannot_settle_next_turn(self) -> None:
        provider = LiveProvider()
        session = self._make(provider)
        provider.said = providers.TranscriptBurst(provider._on_said, gap_s=60)
        with patch("voice_cua.workflow_store.begin_engagement"), \
                patch("voice_cua.tools.note_user_turn"), \
                patch("voice_cua.live_session.island_publish"), \
                patch.object(activity_log, "_append_row"), patch("builtins.print"):
            first_thread, first_result, _ = self._start_turn(session, "First typed turn")
            session._on_message(None, json.dumps({"type": "response.created", "response": {"id": "old-live-response"}}))
            session._on_message(None, json.dumps({"type": "session.output_transcript.delta",
                "response_id": "old-live-response", "item_id": "old-live-item", "delta": "First reply."}))
            self.assertTrue(session._interrupt_answer_for_new_request("typed_request"))
            session._on_message(None, json.dumps({"type": "response.done", "response": {
                "id": "old-live-response", "status": "cancelled", "output": [{"type": "message",
                    "content": [{"type": "output_text", "text": "First reply."}]}]}}))
            first_thread.join(timeout=2)
            self.assertFalse(first_thread.is_alive())
            self.assertEqual(first_result.get("reply"), "First reply.")
            self.assertFalse(first_result.get("ok"))

            next_thread, next_result, _ = self._start_turn(session, "Next typed turn")
            session._on_message(None, json.dumps({"type": "response.created", "response": {"id": "new-live-response"}}))
            self.assertEqual(provider.inbound({"type": "session.output_transcript.delta",
                "item_id": "old-live-item", "delta": "Late first reply."}), [])
            self.assertEqual(provider.inbound({"type": "session.output_audio.delta",
                "response_id": "old-live-response", "item_id": "old-live-item", "delta": "QQ=="}), [])
            session._on_message(None, json.dumps({"type": "response.done", "response": {
                "id": "old-live-response", "status": "completed", "output": [{"type": "message",
                    "content": [{"type": "output_text", "text": "Late first reply."}]}]}}))
            next_thread.join(timeout=0.02)
            self.assertTrue(next_thread.is_alive())
            self.assertEqual(next_result, {})
            session._on_message(None, json.dumps({"type": "session.output_transcript.delta",
                "response_id": "new-live-response", "item_id": "new-live-item", "delta": "Second reply."}))
            session._on_message(None, json.dumps({"type": "response.done", "response": {
                "id": "new-live-response", "status": "completed", "output": [{"type": "message",
                    "content": [{"type": "output_text", "text": "Second reply."}]}]}}))
            provider.said.flush()
            next_thread.join(timeout=2)
        self.assertFalse(next_thread.is_alive())
        self.assertEqual(next_result.get("reply"), "Second reply.")

    def test_gpt_live_spoken_turn_without_typed_request_still_plays_audio(self) -> None:
        provider = LiveProvider()
        session = self._make(provider)
        provider.heard = providers.TranscriptBurst(provider._on_heard, gap_s=60)
        out = provider.inbound({"type": "session.input_transcript.delta", "delta": "Hello Samantha",
                                "start_ms": 100, "end_ms": 600})
        self.assertEqual(out, [{"type": "input_audio_buffer.speech_started"}])
        spoken_request_id = provider._pending_spoken_request[0]
        [audio_event] = provider.inbound({"type": "session.output_audio.delta",
            "response_id": "spoken-live-response", "item_id": "spoken-live-item", "delta": "QQ=="})
        self.assertEqual(audio_event["response"]["metadata"]["voice_cua_request_id"],
                         spoken_request_id)
        with patch("voice_cua.live_session.island_publish"), patch.object(activity_log, "_append_row"):
            session._dispatch_event(audio_event)
        self.assertEqual(session._audio.received, ["QQ=="])
        provider.heard.clear()


if __name__ == "__main__":
    unittest.main()
