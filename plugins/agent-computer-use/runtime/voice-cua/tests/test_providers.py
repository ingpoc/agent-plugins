"""Provider adapter layer: Grok Voice (xAI), GPT-Live-1 (Responses delegation).

Offline only: fake sockets, no network, no API keys.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

_TMP = tempfile.TemporaryDirectory(prefix="voice-cua-test-")
os.environ.setdefault("VOICE_CUA_ACTIVITY_LOG", str(Path(_TMP.name) / "activity.jsonl"))
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from voice_cua import auth, providers, tools  # noqa: E402
from voice_cua import session_instructions  # noqa: E402
from voice_cua.activity_log import usage_row  # noqa: E402
from voice_cua.providers import (  # noqa: E402
    ESCALATE_TOOL, LiveProvider, XAIProvider, make_provider, provider_for,
)
from voice_cua.live_session import LiveSession, build_session_update  # noqa: E402
from test_phase1_reliability import StrictGPTLiveWS, feed  # noqa: E402

TOOLS = [{"type": "function", "name": "cua_act", "description": "d", "parameters": {"type": "object"}}]
UPDATE = {"type": "session.update", "session": {
    "type": "realtime", "output_modalities": ["audio"], "instructions": "be nice", "tools": TOOLS,
    "tool_choice": "auto", "parallel_tool_calls": False, "reasoning": {"effort": "low"},
    "audio": {"input": {"format": {"type": "audio/pcm", "rate": 24000},
                        "turn_detection": {"type": "semantic_vad", "eagerness": "low"},
                        "noise_reduction": {"type": "near_field"}},
              "output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": "alloy"}}}}


class _Rows(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.path = Path(self._td.name) / "activity.jsonl"
        env = patch.dict(os.environ, {"VOICE_CUA_ACTIVITY_LOG": str(self.path), "VOICE_CUA_STARTUP_ID": "abcd1234"})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self._td.cleanup)

    def rows(self, kind: str) -> list[dict]:
        if not self.path.exists():
            return []
        return [r for r in map(json.loads, self.path.read_text().splitlines()) if r.get("kind") == kind]


class SelectionTests(unittest.TestCase):
    def test_model_picks_provider(self) -> None:
        self.assertEqual(provider_for("gpt-realtime-2"), "openai-live")  # retired -> Live
        self.assertEqual(provider_for("gpt-realtime-2.1"), "openai-live")
        self.assertEqual(provider_for("gpt-live-1"), "openai-live")
        self.assertEqual(provider_for("grok-voice-latest"), "xai")
        self.assertIsInstance(make_provider("gpt-realtime-2.1"), LiveProvider)
        self.assertEqual(make_provider("gpt-realtime-2.1").model, "gpt-live-1")

    def test_unsupported_live_model_fails_before_provider_or_key_resolution(self) -> None:
        with self.assertRaisesRegex(ValueError, "Voice model unavailable: acu01-unknown-model"):
            make_provider("acu01-unknown-model")

    def test_live_tier_defaults_and_bad_ids_fall_back(self) -> None:
        p = make_provider("gpt-live-1", {"live_bulk_model": "nope", "live_intelligence_model": "gpt-6-astra"})
        with patch.dict(os.environ, {"VOICE_CUA_LIVE_BULK_MODEL": "", "VOICE_CUA_LIVE_INTELLIGENCE_MODEL": ""}):
            p = make_provider("gpt-live-1", {"live_bulk_model": "nope", "live_intelligence_model": "gpt-6-astra"})
        self.assertEqual((p.bulk, p.intelligence), ("gpt-6-luna", "gpt-6-astra"))
        self.assertEqual(LiveProvider().intelligence, "gpt-6.1-sol")


class XAITests(unittest.TestCase):
    def test_grok_zero_usage_is_unknown_and_positive_usage_is_preserved(self) -> None:
        p = XAIProvider("grok-voice-latest")
        (unknown,) = p.inbound({"type": "response.done", "response": {
            "id": "r-zero", "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        }})
        row = usage_row(unknown["response"], labels=p.usage_labels(unknown["response"]))
        self.assertIsNone(row["input_tokens"])
        self.assertIsNone(row["total_tokens"])
        (measured,) = p.inbound({"type": "response.done", "response": {
            "id": "r-used", "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
        }})
        self.assertEqual(measured["response"]["usage"]["total_tokens"], 6)

    def test_session_update_is_flattened(self) -> None:
        p = XAIProvider("grok-voice-latest")
        self.assertEqual(p.url(), "wss://api.x.ai/v1/realtime?model=grok-voice-latest")
        (out,) = p.outbound(UPDATE)
        s = out["session"]
        self.assertEqual(s["voice"], "eve")
        self.assertEqual(s["turn_detection"], {"type": "server_vad"})
        self.assertEqual(s["tools"], TOOLS)
        self.assertEqual(s["audio"]["input"]["format"], {"type": "audio/pcm", "rate": 24000})
        for gone in ("type", "output_modalities", "tool_choice", "reasoning"):
            self.assertNotIn(gone, s)

    def test_typed_request_id_survives_xai_round_trip(self) -> None:
        p = XAIProvider("grok-voice-latest")
        meta = {"voice_cua_request_id": "req1"}
        self.assertEqual(p.outbound({"type": "response.create", "response": {"metadata": meta}}),
                         [{"type": "response.create"}])
        (created,) = p.inbound({"type": "response.created", "response": {"id": "r1"}})
        self.assertEqual(created["response"]["metadata"], meta)
        (done,) = p.inbound({"type": "response.done", "response": {"id": "r1", "status": "completed"}})
        self.assertEqual(done["response"]["metadata"], meta)
        p.outbound({"type": "response.create"})  # untagged create keeps the order aligned
        (plain,) = p.inbound({"type": "response.created", "response": {"id": "r2"}})
        self.assertNotIn("metadata", plain["response"])

    def test_system_note_becomes_tagged_user_item(self) -> None:
        (out,) = XAIProvider("grok-voice-latest").outbound({"type": "conversation.item.create", "item": {
            "type": "message", "role": "system", "content": [{"type": "input_text", "text": "reconnected"}]}})
        self.assertEqual(out["item"]["role"], "user")
        self.assertIn("[system note] reconnected", out["item"]["content"][0]["text"])

    def test_session_resumption_reuses_the_server_conversation(self) -> None:
        p = XAIProvider("grok-voice-latest")
        (update,) = p.outbound(UPDATE)
        self.assertEqual(update["session"]["resumption"], {"enabled": True})
        p.inbound({"type": "conversation.created", "conversation": {"id": "conversation/1"}})
        p.reset()
        self.assertEqual(p.url(), "wss://api.x.ai/v1/realtime?model=grok-voice-latest&conversation_id=conversation%2F1")

    def test_live_prompt_uses_completed_transcripts_and_reports_app_misses(self) -> None:
        self.assertIn("Treat a completed user transcript as authoritative", providers.LIVE_INSTRUCTIONS)
        self.assertIn("ask only to confirm its path or installation", providers.LIVE_INSTRUCTIONS)
        self.assertIn("Conversational recall from this conversation and empathy also stay in Live", providers.LIVE_INSTRUCTIONS)
        self.assertIn("a casual preference mention is not a request to save it", providers.LIVE_INSTRUCTIONS)

    def test_preference_and_result_rules_reach_both_voice_providers(self) -> None:
        with patch("voice_cua.workflow_store.index_context", return_value=""):
            instructions = session_instructions()
        (xai_update,) = XAIProvider("grok-voice-latest").outbound({"type": "session.update", "session": {
            "instructions": instructions, "tools": []}})
        live_start = LiveProvider()._start({"instructions": instructions, "tools": []})
        prompts = (xai_update["session"]["instructions"], live_start["session"]["delegation"]["responses"]["instructions"])
        for prompt in prompts:
            self.assertIn("A passing mention such as \"my favourite colour is teal\" is conversation", prompt)
            self.assertIn("Use operation=preference for user preferences", prompt)
            self.assertIn('An immediate follow-up such as "Please remember that" applies to the most recent quoted preference.', prompt)
            self.assertIn("before confirming cleanup or closure, state every fact", prompt)

    def test_missing_key_is_reported_not_read(self) -> None:
        with patch.dict(os.environ, {"XAI_API_KEY": ""}), \
                patch("voice_cua.auth.keychain_exists", return_value=False), \
                patch("voice_cua.auth.keychain_get") as get:
            self.assertFalse(auth.xai_key_available())
            with self.assertRaises(auth.MissingApiKey) as cm:
                XAIProvider("grok-voice-latest").resolve_key()
        get.assert_not_called()
        self.assertIn("xai-api", str(cm.exception))


def _live(**kw) -> tuple[LiveProvider, list[dict]]:
    sent: list[dict] = []
    p = LiveProvider(**kw)
    p.bind(lambda e: sent.append(e) or True)
    p._ready = True  # past session.started (held-until-started is covered in test_live_transcripts)
    return p, sent


def _fc(delegation: str, call_id: str, name: str = "cua_act", args: str = "{}") -> dict:
    return {"type": "response.event", "delegation_id": delegation, "event": {
        "type": "response.output_item.done", "output_index": 0,
        "item": {"type": "function_call", "call_id": call_id, "name": name, "arguments": args}}}


class LiveTests(_Rows):
    def test_strict_stand_in_rejects_unknown_event_type_and_nested_field(self) -> None:
        ws = StrictGPTLiveWS()
        for event, param in (
            ({"type": "session.teleport"}, "type"),
            ({"type": "response.create", "event_id": "turn-1",
              "response": {"instructions": "For this response only, answer directly."}}, "response"),
            ({"type": "response.item.create", "item": {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "hi"}], "unexpected": True}}, "item.unexpected"),
        ):
            with self.assertRaisesRegex(ValueError, f"^{event['type']}: {param}$"):
                ws.send(json.dumps(event))
        self.assertEqual([error["error"]["param"] for error in ws.errors],
                         ["type", "response", "item.unexpected"])
        self.assertEqual([error["event_type"] for error in ws.errors],
                         ["session.teleport", "response.create", "response.item.create"])
        self.assertEqual(ws.events, [])

    def test_gpt_live_strict_fake_rejects_unsupported_response_parameter(self) -> None:
        ws = StrictGPTLiveWS()
        with self.assertRaisesRegex(ValueError, "response.create: response"):
            ws.send(json.dumps({"type": "response.create", "event_id": "turn-1",
                                "response": {"instructions": "For this response only, answer directly."}}))
        self.assertEqual(ws.errors, [{"type": "error", "event_type": "response.create", "error": {
            "type": "invalid_request_error", "code": "unknown_parameter", "param": "response",
            "message": "response.create: unknown or invalid parameter 'response'.",
        }}])
        self.assertEqual(ws.events, [])

    def test_gpt_live_strict_fake_rejects_unknown_fields_on_all_prompt_events(self) -> None:
        ws = StrictGPTLiveWS()
        for event in (
            {"type": "response.item.create", "item": {"type": "message", "role": "user",
             "content": [], "unexpected": True}},
            {"type": "session.update", "session": {"delegation": {"type": "responses"}},
             "unexpected": True},
            {"type": "session.update", "session": {"delegation": {"type": "responses",
             "responses": {"model": "gpt-6-luna", "unexpected": True}}}},
        ):
            with self.assertRaises(ValueError):
                ws.send(json.dumps(event))
        self.assertEqual([error["error"]["param"] for error in ws.errors], [
            "item.unexpected", "unexpected", "session.delegation.responses.unexpected",
        ])

    def test_gpt_live_per_turn_route_is_a_part_of_the_typed_item(self) -> None:
        p, _ = _live()
        ws = StrictGPTLiveWS()
        for request, route in (
            ("Hi, how are you?", "direct"),
            ("What did I just say?", "direct"),
            ("I had a rough day.", "direct"),
            ("Open example.com and read the heading.", "delegate"),
        ):
            (user_item,) = p.outbound({"type": "conversation.item.create", "item": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": request}]}})
            output = p.outbound({"type": "response.create", "event_id": "turn"})
            for event in [user_item, *output]:
                ws.send(json.dumps(event))
            self.assertEqual(ws.errors, [])
            self.assertEqual(user_item["item"]["content"], [
                {"type": "input_text", "text": request},
                {"type": "input_text", "text": providers.ROUTE_TEXT[route]},
            ])
            self.assertEqual(output, [{"type": "response.create", "event_id": "turn"}])

    def test_first_update_starts_session_with_bulk_delegation(self) -> None:
        p, _ = _live()
        self.assertEqual(p.url(), "wss://api.openai.com/v1/live/sessions")
        (start,) = p.outbound(UPDATE)
        self.assertEqual(start["type"], "session.start")
        s = start["session"]
        self.assertEqual(s["model"], "gpt-live-1")
        self.assertIn("Do not delegate to the backend", s["instructions"])
        self.assertIn("simple arithmetic (for example, 12 + 12 = 24)", s["instructions"])
        self.assertIn("Delegate to the backend when", s["instructions"])
        self.assertIn("never generate <|SpawnThinking|> or create a Responses delegation", s["instructions"])
        self.assertIn("without adding a greeting or acknowledgment", s["instructions"])
        d = s["delegation"]
        self.assertEqual(d["type"], "responses")
        self.assertEqual(d["responses"]["model"], "gpt-6-luna")
        self.assertEqual([t["name"] for t in d["responses"]["tools"]], ["cua_act", ESCALATE_TOOL])
        self.assertFalse(d["responses"]["parallel_tool_calls"])
        # Later tool changes (lazy groups) only touch the backend tool list.
        (upd,) = p.outbound({"type": "session.update", "session": {"type": "realtime", "tools": TOOLS + TOOLS[:0]}})
        self.assertEqual(upd["type"], "session.update")
        self.assertIn("tools", upd["session"]["delegation"]["responses"])
        self.assertNotIn("model", upd["session"]["delegation"]["responses"])

    def test_typed_turn_routing_covers_conversation_and_tasks(self) -> None:
        cases = {
            "Hey Samantha, how's your afternoon going? By the way, my favourite colour is teal.": "direct",
            "Quick one: what did I say my favourite colour was?": "direct",
            "I had a rough day.": "direct",
            "Hi, check my calendar.": "delegate",
            "I had a rough day, but please check my calendar.": "delegate",
            "My favourite colour is teal. Also, check my calendar.": "delegate",
            "Please remember that.": "delegate",
            "Open Calculator": "delegate",
            "Please open example.com and say the heading.": "delegate",
            "Hey, could you open Calculator?": "delegate",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(providers.typed_turn_route(text), expected)

    def test_typed_turn_route_is_turn_scoped_and_keeps_turn_correlation(self) -> None:
        for text, expected in (
            ("Hey, how are you?", "direct"),
            ("Open Calculator", "delegate"),
        ):
            p, _ = _live()
            (user_item,) = p.outbound({"type": "conversation.item.create", "item": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}})
            (created,) = p.outbound({"type": "response.create", "event_id": "response-event", "response": {
                "metadata": {"voice_cua_request_id": "req-1", "voice_cua_request_step_ref": "step-1"}}})
            self.assertEqual(user_item["item"]["content"][-1],
                             {"type": "input_text", "text": providers.ROUTE_TEXT[expected]})
            self.assertEqual(created, {"type": "response.create", "event_id": "response-event"})
            self.assertEqual((p._turn_seq, p._turn_request), (1, "req-1"))

    def test_silent_output_does_not_merge_audible_replies(self) -> None:
        p, _ = _live()
        def audio(delta):
            return p.inbound({"type": "session.output_audio.delta", "delta": delta})[0]["item_id"]
        with patch.object(providers.time, "monotonic", return_value=10.0):
            first = audio("QQ==")
        for now in (10.5, 11.0, 11.5, 12.0):
            with patch.object(providers.time, "monotonic", return_value=now):
                self.assertEqual(audio("AAA="), first)
        with patch.object(providers.time, "monotonic", return_value=12.1):
            second = audio("QQ==")
        self.assertNotEqual(first, second)
        p.reset()
        with patch.object(providers.time, "monotonic", return_value=14.0):
            self.assertNotEqual(audio("QQ=="), second)

    def test_spoken_update_reaches_busy_backend_without_starting_another_response(self) -> None:
        p, sent = _live()
        p.inbound({"type": "session.started", "session": {"id": "s"}})
        p.inbound({"type": "response.event", "delegation_id": "d", "event": {
            "type": "response.created", "response": {"id": "r"}}})
        with patch("voice_cua.island_facade.island_publish"), patch.object(providers, "log_event"):
            p.inbound({"type": "session.input_transcript.delta", "delta": "Dil Chahta Hai"})
            p.heard.flush()
        self.assertFalse(sent, "speech must not write backend input during generation")
        self.assertEqual(p.outbound({"type": "response.create"}), [{"type": "response.create"}])
        self.assertFalse(any(e.get("type") == "response.create" for e in sent))
        p.inbound({"type": "response.event", "delegation_id": "d", "event": {
            "type": "response.output_item.done", "response_id": "r", "item": {
                "type": "function_call", "name": "probe", "call_id": "call", "arguments": "{}"}}})
        p.inbound({"type": "response.event", "delegation_id": "d", "event": {
            "type": "response.completed", "response": {"id": "r", "status": "completed"}}})
        p.outbound({"type": "conversation.item.create", "item": {
            "type": "function_call_output", "call_id": "call", "output": "{}"}})
        update, continuation = p.outbound({"type": "response.create"})
        self.assertEqual(update["type"], "response.item.create")
        self.assertIn("Dil Chahta Hai", update["item"]["content"][0]["text"])
        self.assertEqual(continuation, {"type": "response.create"})
        self.assertEqual(p.outbound({"type": "response.create"}), [{"type": "response.create"}])
        before = len(sent)
        with patch("voice_cua.island_facade.island_publish"), patch.object(providers, "log_event"):
            p._on_heard("Hello")
        self.assertEqual(len(sent), before, "idle conversation must not start backend work")

    def test_new_typed_request_supersedes_queued_speech(self) -> None:
        p, _ = _live()
        p.inbound({"type": "session.started", "session": {"id": "s"}})
        p._pending_spoken_update = "old request"
        p.outbound({"type": "conversation.item.create", "item": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": "new request"}]}})
        self.assertEqual(p.outbound({"type": "response.create"}), [{"type": "response.create"}])

    def test_final_answer_does_not_leak_queued_speech_into_later_task(self) -> None:
        p, _ = _live()
        p.inbound({"type": "session.started", "session": {"id": "s"}})
        p.inbound({"type": "response.event", "delegation_id": "old", "event": {
            "type": "response.created", "response": {"id": "r"}}})
        with patch("voice_cua.island_facade.island_publish"), patch.object(providers, "log_event"):
            p._on_heard("old correction")
        p.inbound({"type": "response.event", "delegation_id": "old", "event": {
            "type": "response.completed", "response": {"id": "r", "status": "completed"}}})
        self.assertEqual(p._pending_spoken_update, "")
        p._pending_spoken_update = "another old correction"
        p.inbound(_dlg("new"))
        self.assertEqual(p._pending_spoken_update, "")
        self.assertEqual(p.outbound({"type": "response.create"}), [{"type": "response.create"}])

    def test_audio_and_turn_events_translate(self) -> None:
        p, _ = _live()
        self.assertEqual(p.outbound({"type": "input_audio_buffer.append", "audio": "AAA="}),
                         [{"type": "session.input_audio.append", "audio": "AAA="}])
        self.assertEqual(p.outbound({"type": "response.cancel"}), [])
        self.assertEqual(p.inbound({"type": "session.started", "session": {"id": "s"}})[0]["type"], "session.updated")
        (audio,) = p.inbound({"type": "session.output_audio.delta", "delta": "QQ=="})
        self.assertEqual(audio["type"], "response.output_audio.delta")
        self.assertEqual(audio["delta"], "QQ==")
        note = p.outbound({"type": "conversation.item.create", "item": {
            "type": "message", "role": "system", "content": [{"type": "input_text", "text": "x" * 5000}]}})
        self.assertEqual(note[0]["type"], "session.instructions.append")
        self.assertLessEqual(len(note[0]["content"]), providers.LIVE_APPEND_MAX_CHARS)
        self.assertIsNone(note[0]["delegation_id"])

    def test_backend_function_call_reaches_loop_and_result_returns(self) -> None:
        p, _ = _live()
        (fwd,) = p.inbound(_fc("dlg1", "call_1"))
        self.assertEqual(fwd["type"], "response.output_item.done")
        self.assertEqual(fwd["item"]["call_id"], "call_1")
        out = p.outbound({"type": "conversation.item.create", "item": {
            "type": "function_call_output", "call_id": "call_1", "output": json.dumps({"ok": True})}})
        self.assertEqual(out, [{"type": "response.item.create", "item": {
            "type": "function_call_output", "call_id": "call_1", "output": json.dumps({"ok": True})}}])
        self.assertEqual(p.outbound({"type": "response.create", "response": {"metadata": {}}}), [{"type": "response.create"}])

    def test_backend_requested_escalation_switches_model_once(self) -> None:
        p, sent = _live()
        self.assertEqual(p.inbound(_fc("dlg1", "call_e", name=ESCALATE_TOOL, args='{"reason":"plan"}')), [])
        self.assertEqual([e["type"] for e in sent], ["session.update", "response.item.create", "response.create"])
        self.assertEqual(sent[0]["session"]["delegation"]["responses"]["model"], "gpt-6.1-sol")
        self.assertEqual(p.tier, "intelligence")
        sent.clear()
        p.inbound(_fc("dlg1", "call_e2", name=ESCALATE_TOOL))
        self.assertEqual([e["type"] for e in sent], ["response.item.create", "response.create"], "escalate once")
        self.assertEqual(self.rows("live_escalate")[0]["to_model"], "gpt-6.1-sol")

    def test_bulk_failure_after_two_tries_escalates_before_continuing(self) -> None:
        p, _ = _live()
        p.inbound(_fc("dlg2", "call_2"))
        failed = json.dumps({"ok": False, "error_type": "target_missing", "retryable": False, "tries": 2})
        out = p.outbound({"type": "conversation.item.create", "item": {
            "type": "function_call_output", "call_id": "call_2", "output": failed}})
        self.assertEqual([e["type"] for e in out], ["session.update", "response.item.create"])
        self.assertEqual(out[0]["session"]["delegation"]["responses"]["model"], "gpt-6.1-sol")

    def test_first_failure_and_confirmation_refusal_do_not_escalate(self) -> None:
        p, _ = _live()
        for i, payload in enumerate(({"ok": False, "retryable": True, "tries": 1},
                                     {"ok": False, "needs_confirmation": True, "retryable": True})):
            p.inbound(_fc("dlg3", f"c{i}"))
            out = p.outbound({"type": "conversation.item.create", "item": {
                "type": "function_call_output", "call_id": f"c{i}", "output": json.dumps(payload)}})
            self.assertEqual([e["type"] for e in out], ["response.item.create"])
        self.assertEqual(p.tier, "bulk")

    def test_completed_delegation_returns_to_bulk_and_logs_tiered_usage(self) -> None:
        p, sent = _live()
        p.inbound({"type": "response.event", "delegation_id": "dlg4",
                   "event": {"type": "response.created", "response": {"id": "resp_bulk"}}})
        p.inbound(_fc("dlg4", "call_e", name=ESCALATE_TOOL))
        p.inbound({"type": "response.event", "delegation_id": "dlg4",
                   "event": {"type": "response.created", "response": {"id": "resp_int"}}})
        sent.clear()
        (done,) = p.inbound({"type": "response.event", "delegation_id": "dlg4", "event": {
            "type": "response.completed", "response": {"id": "resp_int", "status": "completed", "output": [],
                                                       "usage": {"input_tokens": 900, "output_tokens": 40,
                                                                 "input_tokens_details": {"cached_tokens": 512},
                                                                 "output_tokens_details": {"reasoning_tokens": 12}}}}})
        self.assertEqual(done["type"], "response.done")
        self.assertEqual(p.usage_labels(done["response"])["tier"], "intelligence")
        self.assertEqual(p.usage_labels(done["response"])["model"], "gpt-6.1-sol")
        self.assertEqual(p.usage_labels({"id": "resp_bulk"})["model"], "gpt-6-luna")
        self.assertEqual(sent[0]["session"]["delegation"]["responses"]["model"], "gpt-6-luna")
        self.assertEqual(p.tier, "bulk")

    def test_typed_request_maps_to_final_backend_text(self) -> None:
        p, _ = _live()
        p.outbound({"type": "response.create", "response": {"metadata": {"voice_cua_request_id": "req1"}}})
        (created,) = p.inbound({"type": "response.event", "delegation_id": "d7",
                                "event": {"type": "response.created", "response": {"id": "r1"}}})
        self.assertEqual(created["response"]["metadata"]["voice_cua_request_id"], "req1")
        p.inbound(_fc("d7", "c7"))
        (mid,) = p.inbound({"type": "response.event", "delegation_id": "d7",
                            "event": {"type": "response.completed", "response": {"id": "r1", "status": "completed"}}})
        self.assertEqual(mid["response"]["output"], [{"type": "function_call"}], "not final while a call ran")
        p.outbound({"type": "conversation.item.create", "item": {"type": "function_call_output", "call_id": "c7", "output": "{}"}})
        p.inbound({"type": "response.event", "delegation_id": "d7",
                   "event": {"type": "response.created", "response": {"id": "r2"}}})
        p.inbound({"type": "response.event", "delegation_id": "d7",
                   "event": {"type": "response.output_text.done", "text": "Opened Notes."}})
        (final,) = p.inbound({"type": "response.event", "delegation_id": "d7",
                              "event": {"type": "response.completed", "response": {"id": "r2", "status": "completed"}}})
        self.assertEqual(final["response"]["metadata"]["voice_cua_request_id"], "req1")
        self.assertEqual(final["response"]["output"][0]["content"][0]["text"], "Opened Notes.")

    def test_spoken_turn_maps_to_live_delegation_by_timeline(self) -> None:
        p, _ = _live()
        p.heard._on_flush = lambda _text: None
        p.inbound({"type": "session.input_transcript.delta", "delta": "Open Calculator",
                   "start_ms": 1000, "end_ms": 1400})
        request_id, _ = p._pending_spoken_request
        p.inbound({"type": "session.delegation.created", "offset_ms": 1300,
                   "delegation": {"id": "d_spoken", "target": "responses"}})
        (created,) = p._backend_event("d_spoken", {
            "type": "response.created", "response": {"id": "r_spoken"}
        })
        p.heard.flush()
        self.assertEqual(created["response"]["metadata"]["voice_cua_request_id"], request_id)
        self.assertEqual(p._current_request("d_spoken"), request_id)

    def test_delayed_transcript_links_unique_unstarted_delegation(self) -> None:
        p, _ = _live()
        p.inbound({"type": "session.delegation.created", "offset_ms": 1300,
                   "delegation": {"id": "d_spoken", "target": "responses"}})
        p.inbound({"type": "session.input_transcript.delta", "delta": "Open Calculator",
                   "start_ms": 1000, "end_ms": 1400})
        (created,) = p._backend_event("d_spoken", {"type": "response.created", "response": {"id": "r_spoken"}})
        self.assertRegex(created["response"]["metadata"]["voice_cua_request_id"], r"^[0-9a-f]{32}$")

    def test_delayed_transcript_does_not_link_ambiguous_delegations(self) -> None:
        p, _ = _live()
        for delegation in ("d1", "d2"):
            p.inbound({"type": "session.delegation.created", "offset_ms": 1300,
                       "delegation": {"id": delegation, "target": "responses"}})
        p.inbound({"type": "session.input_transcript.delta", "delta": "Open Calculator",
                   "start_ms": 1000, "end_ms": 1400})
        (created,) = p._backend_event("d1", {"type": "response.created", "response": {"id": "r1"}})
        self.assertNotIn("metadata", created["response"])

    def test_delegation_before_spoken_timeline_keeps_old_turn_then_matches_new_turn(self) -> None:
        p, _ = _live()
        p.outbound({"type": "response.create", "response": {"metadata": {"voice_cua_request_id": "a" * 32}}})
        p.inbound({"type": "session.delegation.created", "offset_ms": 900,
                   "delegation": {"id": "d_old", "target": "responses"}})
        p.heard._on_flush = lambda _text: None
        p.inbound({"type": "session.input_transcript.delta", "delta": "Open Calculator",
                   "start_ms": 1000, "end_ms": 1400})
        p.inbound({"type": "session.delegation.created", "offset_ms": 950,
                   "delegation": {"id": "d_unmatched", "target": "responses"}})
        (created,) = p._backend_event("d_unmatched", {
            "type": "response.created", "response": {"id": "r_unmatched"}
        })
        old = "a" * 32
        new_delegation = p.inbound({"type": "session.delegation.created", "offset_ms": 1500,
                                    "delegation": {"id": "d_spoken", "target": "responses"}})
        (spoken,) = p._backend_event("d_spoken", {
            "type": "response.created", "response": {"id": "r_spoken"}
        })
        p.heard.flush()
        self.assertEqual(created["response"]["metadata"]["voice_cua_request_id"], old)
        self.assertEqual(new_delegation, [])
        self.assertEqual(spoken["response"]["metadata"]["voice_cua_request_id"],
                         p._delegation_request["d_spoken"])
        self.assertNotEqual(p._delegation_request["d_spoken"], old)

    def test_stale_result_after_reconnect_becomes_context_note(self) -> None:
        p, _ = _live()
        p.inbound(_fc("d8", "c8"))
        p.reset()  # reconnect: the new Live session has no such call
        p._ready = True
        self.assertEqual(p.outbound({"type": "conversation.item.create", "item": {"type": "function_call", "call_id": "c8"}}), [])
        (note,) = p.outbound({"type": "conversation.item.create", "item": {
            "type": "function_call_output", "call_id": "c8", "output": json.dumps({"ok": True, "opened": "Notes"})}})
        self.assertEqual(note["type"], "session.thinking.append")
        self.assertIsNone(note["delegation_id"])
        self.assertIn("Notes", note["content"])
        # Drop recovery: the pre-drop result continues the task once on the new session.
        self.assertEqual(p.outbound({"type": "response.create"}), [{"type": "response.create"}])

    def test_large_ax_dump_is_compacted_under_cap(self) -> None:
        from voice_cua import providers
        p, _ = _live()
        p.inbound(_fc("d9", "c9"))
        lines = "\n".join(f"{'  ' * (i % 6)}[{i}] AXButton \"Item {i}\" [pressable] {{10,{i} 80x20}}" for i in range(200))
        raw = json.dumps({"ok": True, "verified": True, "app": "Reminders", "text": lines})
        (wire,) = p.outbound({"type": "conversation.item.create", "item": {
            "type": "function_call_output", "call_id": "c9", "output": raw}})
        out = wire["item"]["output"]
        self.assertLessEqual(len(out), providers.LIVE_TOOL_OUTPUT_MAX)
        data = json.loads(out)
        self.assertTrue(data["ok"] and data["verified"])
        self.assertIn("[0] AXButton", data["text"])
        self.assertIn("more lines cut", data["text"])
        self.assertEqual(providers.compact_tool_output('{"ok": true}'), '{"ok": true}')

    def test_missing_delegation_id_still_escalates_once(self) -> None:
        p, sent = _live()
        p.inbound(_fc("", "e1", name=ESCALATE_TOOL))
        p.inbound(_fc("", "e2", name=ESCALATE_TOOL))
        self.assertEqual([e["type"] for e in sent].count("session.update"), 1)

    def test_final_text_from_message_item(self) -> None:
        p, _ = _live()
        p.inbound({"type": "response.event", "delegation_id": "d9",
                   "event": {"type": "response.created", "response": {"id": "r9"}}})
        p.inbound({"type": "response.event", "delegation_id": "d9", "event": {
            "type": "response.output_item.done", "item": {"type": "message", "content": [{"type": "output_text", "text": "Done."}]}}})
        (done,) = p.inbound({"type": "response.event", "delegation_id": "d9",
                             "event": {"type": "response.completed", "response": {"id": "r9", "status": "completed"}}})
        self.assertEqual(done["response"]["output"][0]["content"][0]["text"], "Done.")

    def test_live_session_usage_keeps_tokens_and_seconds_and_leaves_missing_tokens_unknown(self) -> None:
        p, _ = _live()
        self.assertEqual(p.inbound({"type": "session.usage.updated", "usage": {
            "seconds": 12, "input_tokens": 24, "output_tokens": 11, "cached_tokens": 7,
        },
                                    "context_window": {"usage_ratio": 0.4}}), [])
        p.inbound({"type": "session.closed", "usage": {"seconds": 15}, "reason": "close_requested"})
        rows = self.rows("live_usage")
        self.assertEqual([r["voice_seconds"] for r in rows], [12, 15])
        self.assertTrue(rows[1]["final"])
        self.assertNotIn("input_tokens", rows[0])
        usage = self.rows("usage")
        self.assertEqual([row["voice_seconds"] for row in usage], [12, 15])
        self.assertTrue(all(row["provider"] == "openai-live" for row in usage))
        self.assertTrue(all(row["live_model"] == "gpt-live-1" for row in usage))
        self.assertEqual((usage[0]["input_tokens"], usage[0]["output_tokens"], usage[0]["cached_tokens"]),
                         (24, 11, 7))
        self.assertTrue(all(row["input_tokens"] is None and row["output_tokens"] is None
                            and row["cached_tokens"] is None and row["total_tokens"] is None
                            for row in usage[1:]))


def live_session() -> LiveSession:
    settings = {"live_model": "gpt-live-1", "live_voice": "alloy", "eagerness": "balanced",
                "mic_profile": "near_field", "live_bulk_model": "gpt-6-luna",
                "live_intelligence_model": "gpt-6.1-sol"}
    with patch("voice_cua.voice_settings.load_settings", return_value=settings), \
            patch("voice_cua.live_session.load_settings", return_value=settings):
        session = LiveSession(api_key="test", enable_audio=False)
    session.ws = StrictGPTLiveWS()
    return session


class LiveSessionLoopTests(_Rows):
    """The shared loop (tools, RiskGate, usage) runs unchanged behind the Live adapter."""

    def setUp(self) -> None:
        super().setUp()
        tools.reset_session_state()
        self.backend = MagicMock()
        p = patch("voice_cua.tools.get_backend", return_value=self.backend)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(tools.reset_session_state)

    def test_session_uses_live_adapter(self) -> None:
        s = live_session()
        self.assertIsInstance(s.provider, LiveProvider)
        s._send(build_session_update(text_only=True))
        self.assertEqual(s.ws.events[0]["type"], "session.start")
        self.assertEqual(self.rows("session_update")[0]["provider"], "openai-live")

    def test_reconnect_summary_keeps_last_completed_task_and_result(self) -> None:
        s = live_session()
        s._last_task_run_id = "completed-run"
        s._last_user_text = "Open Calendar"
        s._last_assistant_reply = "Calendar is open."
        s._prepare_reconnect()
        self.assertEqual(s._reconnect_context["repo"], os.path.basename(os.getcwd()))
        self.assertTrue(s._reconnect_context["branch"])
        self.assertEqual(s._reconnect_context["task"], "Open Calendar")
        self.assertEqual(s._reconnect_context["last_result"], "Calendar is open.")
        self.assertIn("do not ask the user to restate it", s.reconnect_note(comet_active=False))

    def test_reconnect_replays_accumulated_turns_without_server_history(self) -> None:
        s = live_session()
        with patch("voice_cua.tools.note_user_turn"):
            first = s._note_user_turn("Remember marker EARLY-TASK-110", request_id="1" * 32)
            s._note_response_done({
                "id": "response-1", "status": "completed",
                "metadata": {"voice_cua_request_id": first},
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "Marker recorded."}]}],
            })
            second = s._note_user_turn("Check the current branch", request_id="2" * 32)
            s._note_response_done({
                "id": "response-2", "status": "completed",
                "metadata": {"voice_cua_request_id": second},
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "Branch checked."}]}],
            })

        self.assertEqual(s.provider.history, [])
        s._prepare_reconnect()
        s._conn_epoch = 1
        notes = []
        with patch.object(s, "_send"), patch.object(s, "_send_system_note", side_effect=notes.append), \
                patch("voice_cua.live_session.comet_is_active", return_value=False), \
                patch("voice_cua.live_session.island_publish"):
            s._on_open(s.ws)

        self.assertEqual(len(notes), 1)
        self.assertIn("EARLY-TASK-110", notes[0])
        self.assertIn("Check the current branch => Branch checked.", notes[0])

    def test_late_retired_turn_response_cannot_bind_or_settle_new_turn(self) -> None:
        s = live_session()
        old_waiter = {"event": threading.Event(), "reply": "", "error": "", "typed_turn_id": "typed-old"}
        with s._response_lock:
            s._text_waiters["old-request"] = old_waiter
            s._typed_turn_ids_by_request["old-request"] = "typed-old"
            s._response_request_ids["old-response"] = "old-request"
            s._response_task_run_ids["old-response"] = "old-request"
        s._finish_response("old-response")
        s._retire_text_waiter("old-request")

        new_waiter = {"event": threading.Event(), "reply": "", "error": "", "typed_turn_id": "typed-new"}
        with s._response_lock:
            s._text_waiters["new-request"] = new_waiter
            s._typed_turn_ids_by_request["new-request"] = "typed-new"
        self.assertIn("old-request", s._retired_request_ids)
        self.assertIn("old-response", s._retired_response_ids)

        s._on_message(s.ws, json.dumps({"type": "response.created", "response": {
            "id": "late-response", "metadata": {"voice_cua_request_id": "old-request"},
        }}))
        s._on_message(s.ws, json.dumps({"type": "response.done", "response": {
            "id": "old-response", "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "Old reply."}]}],
        }}))

        self.assertNotIn("late-response", s._response_typed_turn_ids)
        self.assertFalse(new_waiter["event"].is_set())
        self.assertEqual(new_waiter["reply"], "")

    def test_broken_request_id_does_not_bind_or_settle_a_typed_waiter(self) -> None:
        from contextlib import nullcontext

        s = live_session()
        s.provider = XAIProvider("grok-voice-latest")
        s.provider.bind(s._send_wire)
        waiter = {"event": threading.Event(), "reply": "", "error": "", "typed_turn_id": "typed-1",
                  "last_progress": time.monotonic(), "unanswered_confirms": 0}
        request_id = "a" * 32
        with s._response_lock:
            s._text_waiters[request_id] = waiter
            s._typed_turn_ids_by_request[request_id] = "typed-1"
            s._task_runs[request_id] = []
            s._task_expected_outcomes[request_id] = "Confirm build"
            s._task_started_at[request_id] = time.monotonic()
        s._on_message(s.ws, json.dumps({"type": "response.created", "response": {
            "id": "response-1", "metadata": {"voice_cua_request_id": "b" * 32},
        }}))
        self.assertNotIn("response-1", s._response_typed_turn_ids)

        contexts = []
        dispatched = threading.Event()

        def capture_context(**kwargs):
            contexts.append(kwargs)
            return nullcontext()

        def fake_dispatch(_name, _arguments):
            dispatched.set()
            return {"ok": True}

        with patch("voice_cua.live_session.tool_context", side_effect=capture_context), \
                patch("voice_cua.live_session.dispatch", side_effect=fake_dispatch), \
                patch("voice_cua.live_session.plugin_island.tool_started"), \
                patch("voice_cua.live_session.island_publish"), \
                patch.object(s, "_wait_for_responses_idle", return_value=True):
            s._run_tool("confirm-call", "confirm_risky", "{}", response_id="response-1")
            self.assertTrue(dispatched.wait(1))

        self.assertEqual(contexts[0]["typed_turn_id"], "")
        s._on_message(s.ws, json.dumps({"type": "response.done", "response": {
            "id": "response-1", "status": "completed",
            "metadata": {"voice_cua_request_id": "b" * 32},
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "That confirm timed out too."}]}],
        }}))
        self.assertFalse(waiter["event"].is_set())
        self.assertEqual(waiter["reply"], "")

    def test_uncorrelated_final_response_never_falls_back_to_a_typed_waiter(self) -> None:
        s = live_session()
        waiter = {"event": threading.Event(), "reply": "", "error": "", "typed_turn_id": "typed-1"}
        with s._response_lock:
            s._text_waiters["typed-request"] = waiter
        s._note_response_done({
            "id": "orphaned-response", "status": "completed",
            "metadata": {"voice_cua_request_id": "missing-request"},
            "output": [{"type": "message", "content": [
                {"type": "output_text", "text": "The typed turn is settled."},
            ]}],
        })
        self.assertFalse(waiter["event"].is_set())
        self.assertEqual(waiter["reply"], "")

        ambiguous = live_session()
        waiters = [{"event": threading.Event(), "reply": "", "error": "", "typed_turn_id": turn_id}
                   for turn_id in ("typed-2", "typed-3")]
        with ambiguous._response_lock:
            ambiguous._text_waiters.update({f"request-{i}": item for i, item in enumerate(waiters)})
        ambiguous._note_response_done({
            "id": "ambiguous-response", "status": "completed",
            "metadata": {"voice_cua_request_id": "missing-request"},
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "Ambiguous."}]}],
        })
        self.assertTrue(all(not item["event"].is_set() for item in waiters))

    def test_first_unanswered_confirm_prompts_and_releases_typed_turn(self) -> None:
        from contextlib import ExitStack, nullcontext

        s = live_session()
        sent = threading.Event()
        output = {}
        timed_out = {
            "ok": False, "timed_out": True, "error_type": "confirm_timed_out",
            "hint": "A timeout is not a denial; nothing was done. Reassess and retry work that is safe.",
        }
        with ExitStack() as stack:
            stack.enter_context(patch("voice_cua.live_session.CONFIRM_TIMEOUT_SETTLE_S", 0.25))
            stack.enter_context(patch.object(s, "send_text", side_effect=lambda *_args, **_kwargs: sent.set()))
            dispatch = stack.enter_context(patch("voice_cua.live_session.dispatch", return_value=timed_out))
            deliver = stack.enter_context(patch.object(
                s, "_deliver_tool_result", side_effect=lambda _call, _name, result, **_kw: result,
            ))
            stack.enter_context(patch("voice_cua.live_session.tool_context",
                                      side_effect=lambda **_kwargs: nullcontext()))
            stack.enter_context(patch("voice_cua.live_session.plugin_island.tool_started"))
            stack.enter_context(patch("voice_cua.live_session.island_publish"))
            stack.enter_context(patch.object(s, "_wait_for_responses_idle", return_value=True))
            turn = threading.Thread(target=lambda: output.setdefault(
                "result", s.send_text_and_wait("Run it", timeout=None, typed_turn_id="typed-timeout"),
            ), daemon=True)
            turn.start()
            self.assertTrue(sent.wait(1))
            with s._response_lock:
                s._response_typed_turn_ids["confirm-first"] = "typed-timeout"

            s._run_tool("confirm-first", "confirm_risky", "{}", response_id="confirm-first")
            turn.join(1)
            self.assertFalse(turn.is_alive())

        self.assertIn("unanswered confirmation", output["result"]["error"])
        self.assertEqual(dispatch.call_count, 1)
        self.assertIn("Reassess and retry work that is safe", deliver.call_args.args[2]["hint"])
        self.assertFalse(s._text_request_lock.locked())

    def test_second_unanswered_confirm_releases_typed_text_lock(self) -> None:
        from contextlib import nullcontext

        s = live_session()
        sent = threading.Event()
        output = {}
        timed_out = {"ok": False, "timed_out": True, "error_type": "confirm_timed_out"}

        with patch("voice_cua.live_session.CONFIRM_TIMEOUT_SETTLE_S", 0.25), \
                patch.object(s, "send_text", side_effect=lambda *_args, **_kwargs: sent.set()), \
                patch("voice_cua.live_session.dispatch", return_value=timed_out) as dispatch, \
                patch.object(s, "_deliver_tool_result", side_effect=lambda _call, _name, result, **_kw: result) as deliver, \
                patch("voice_cua.live_session.tool_context", side_effect=lambda **_kwargs: nullcontext()), \
                patch("voice_cua.live_session.plugin_island.tool_started"), \
                patch("voice_cua.live_session.island_publish"), \
                patch.object(s, "_wait_for_responses_idle", return_value=True):
            turn = threading.Thread(target=lambda: output.setdefault(
                "result", s.send_text_and_wait("Yes", timeout=None, typed_turn_id="typed-2"),
            ), daemon=True)
            turn.start()
            self.assertTrue(sent.wait(1))
            with s._response_lock:
                request_id = next(iter(s._text_waiters))
                s._response_typed_turn_ids["confirm-1"] = "typed-2"
                s._response_typed_turn_ids["confirm-2"] = "typed-2"

            s._run_tool("confirm-1", "confirm_risky", "{}", response_id="confirm-1")
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                with s._response_lock:
                    if s._text_waiters[request_id]["unanswered_confirms"] == 1:
                        break
                time.sleep(0.01)
            with s._response_lock:
                self.assertEqual(s._text_waiters[request_id]["unanswered_confirms"], 1)
            s._run_tool("confirm-2", "confirm_risky", "{}", response_id="confirm-2")
            turn.join(1)
            self.assertFalse(turn.is_alive())

        self.assertIn("unanswered confirmation", output["result"]["error"])
        self.assertEqual(dispatch.call_count, 1)
        self.assertIn("A timeout is not a denial", deliver.call_args.args[2]["hint"])
        self.assertFalse(s._text_request_lock.locked())

    def test_risky_backend_call_is_gated_and_loads_confirm(self) -> None:
        s = live_session()
        s._send(build_session_update(text_only=True))
        s.provider._ready = True
        done: list[int] = []
        risky = json.dumps({"app": "Mail", "op": "click", "label": "Send"})
        with patch("voice_cua.live_session.island_publish"), \
                patch("voice_cua.tools.island_publish"), \
                patch.object(s, "_wait_for_responses_idle", return_value=True), \
                patch.object(s, "_send_response_create", side_effect=lambda **_: done.append(1)):
            feed(s, _fc("dlg9", "call_r", args=risky))
            deadline = time.monotonic() + 3
            while not done and time.monotonic() < deadline:
                time.sleep(0.01)
        self.backend.act.assert_not_called()
        results = [e for e in s.ws.events if e["type"] == "response.item.create"]
        self.assertEqual(len(results), 1)
        self.assertTrue(json.loads(results[0]["item"]["output"])["needs_confirmation"])
        updates = [e for e in s.ws.events if e["type"] == "session.update"]
        names = [t["name"] for t in updates[-1]["session"]["delegation"]["responses"]["tools"]]
        self.assertIn("confirm_risky", names)
        self.assertEqual(names[-1], ESCALATE_TOOL)

    def test_backend_usage_row_has_provider_model_tier(self) -> None:
        s = live_session()
        feed(s, {"type": "response.event", "delegation_id": "dlg5",
                 "event": {"type": "response.created", "response": {"id": "resp_u"}}})
        with patch("voice_cua.live_session.island_publish"):
            feed(s, {"type": "response.event", "delegation_id": "dlg5", "event": {
                "type": "response.completed", "response": {"id": "resp_u", "status": "completed",
                                                           "usage": {"input_tokens": 700, "output_tokens": 30}}}})
        (row,) = self.rows("usage")
        self.assertEqual((row["provider"], row["model"], row["live_model"], row["tier"]),
                         ("openai-live", "gpt-6-luna", "gpt-live-1", "bulk"))
        self.assertEqual((row["input_tokens"], row["output_tokens"], row["delegation_id"]), (700, 30, "dlg5"))
        self.assertIn("latency_ms", row)


def _dlg(d: str) -> dict:
    return {"type": "session.delegation.created", "delegation": {"id": d, "target": "responses"}}


def _ev(d: str, inner: dict) -> dict:
    return {"type": "response.event", "delegation_id": d, "event": inner}


def _answer(d: str, rid: str, text: str) -> list[dict]:
    return [_ev(d, {"type": "response.created", "response": {"id": rid}}),
            _ev(d, {"type": "response.output_text.done", "text": text}),
            _ev(d, {"type": "response.completed", "response": {"id": rid, "status": "completed"}})]


def _create(rid: str) -> dict:
    return {"type": "response.create", "response": {"metadata": {"voice_cua_request_id": rid}}}


def _meta(events: list[dict]) -> str:
    return str(((events[0].get("response") or {}).get("metadata") or {}).get("voice_cua_request_id") or "")


def _step_meta(events: list[dict]) -> str:
    return str(((events[0].get("response") or {}).get("metadata") or {}).get("voice_cua_request_step_ref") or "")


class LiveTurnIdTests(_Rows):
    """Each typed request is a turn; only delegations created in that turn answer it."""

    def test_unmatched_new_delegation_does_not_reuse_completed_spoken_turn(self) -> None:
        p, _ = _live()
        p.heard._on_flush = lambda _text: None
        p.inbound({'type': 'session.input_transcript.delta', 'delta': 'First task', 'start_ms': 1000, 'end_ms': 1400})
        p.inbound({'type': 'session.delegation.created', 'offset_ms': 1300, 'delegation': {'id': 'first'}})
        initial = [p.inbound(event) for event in _answer('first', 'r1', 'Finished.')]
        self.assertTrue(_meta(initial[0]))
        self.assertEqual(_meta(initial[0]), _meta(initial[2]))
        p.heard.flush()
        p.inbound({'type': 'session.delegation.created', 'delegation': {'id': 'unmatched'}})
        subsequent = [p.inbound(event) for event in _answer('unmatched', 'r2', 'Different task.')]
        self.assertEqual(_meta(subsequent[0]), '')
        self.assertEqual(_meta(subsequent[2]), '')

    def test_spoken_tool_continuation_keeps_its_turn(self) -> None:
        p, _ = _live()
        p.heard._on_flush = lambda _text: None
        p.inbound({'type': 'session.input_transcript.delta', 'delta': 'Open app', 'start_ms': 1000, 'end_ms': 1400})
        p.inbound({'type': 'session.delegation.created', 'offset_ms': 1300, 'delegation': {'id': 'first'}})
        first = p.inbound(_ev('first', {'type': 'response.created', 'response': {'id': 'r1'}}))
        request = _meta(first)
        request_step_ref = _step_meta(first)
        p.inbound(_fc('first', 'c1'))
        p.outbound({'type': 'conversation.item.create', 'item': {'type': 'function_call_output', 'call_id': 'c1', 'output': '{}'}})
        p.inbound(_ev('first', {'type': 'response.completed', 'response': {'id': 'r1', 'status': 'completed'}}))
        continuation = [p.inbound(event) for event in _answer('first', 'r2', 'Done.')]
        self.assertTrue(request)
        self.assertTrue(request_step_ref)
        self.assertEqual(_meta(continuation[0]), request)
        self.assertEqual(_meta(continuation[2]), request)
        self.assertEqual(_step_meta(continuation[0]), request_step_ref)

    def test_typed_request_step_ref_crosses_the_provider_boundary(self) -> None:
        p, _ = _live()
        request_id = "a" * 32
        outbound = p.outbound({"type": "response.create", "response": {"metadata": {
            "voice_cua_request_id": request_id,
            "voice_cua_request_step_ref": "request-step-1",
        }}})
        self.assertTrue(outbound)
        created = p.inbound(_ev("typed", {"type": "response.created", "response": {"id": "r-typed"}}))
        self.assertEqual(_meta(created), request_id)
        self.assertEqual(_step_meta(created), "request-step-1")
        p.heard.flush()

    def test_older_delegation_continuation_does_not_take_the_next_request(self) -> None:
        # Run H: the BBC delegation kept going after its waiter closed; its continuation
        # response.created arrived after the DuckDuckGo request and stole that id.
        p, _ = _live()
        p.outbound(_create("bbc"))
        p.inbound(_dlg("d_wrong"))
        for ev in _answer("d_wrong", "r1", "Previous task complete."):
            p.inbound(ev)
        p.inbound(_dlg("d_bbc"))  # Live re-delegates the same request
        p.outbound(_create("ddg"))  # next typed request while d_bbc still runs
        late = [p.inbound(ev) for ev in _answer("d_bbc", "r2", "BBC headline.")]
        self.assertEqual(_meta(late[0]), "", "an older turn's response never carries the new id")
        self.assertEqual(_meta(late[2]), "")
        self.assertEqual(self.rows("live_stale_turn_output")[0]["delegation_id"], "d_bbc")
        p.inbound(_dlg("d_ddg"))
        own = [p.inbound(ev) for ev in _answer("d_ddg", "r3", "First result.")]
        self.assertEqual(_meta(own[0]), "ddg")
        self.assertEqual(_meta(own[2]), "ddg")

    def test_tool_continuation_with_the_same_request_id_stays_in_its_turn(self) -> None:
        p, _ = _live()
        user = {"type": "conversation.item.create", "item": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]}}
        p.outbound(user)
        p.outbound(_create("req"))
        p.inbound(_dlg("d1"))
        p.inbound(_ev("d1", {"type": "response.created", "response": {"id": "r1"}}))
        p.inbound(_fc("d1", "c1"))
        p.outbound({"type": "conversation.item.create", "item": {
            "type": "function_call_output", "call_id": "c1", "output": "{}"}})
        p.outbound(_create("req"))  # the loop's continuation keeps the request id
        p.inbound(_ev("d1", {"type": "response.completed", "response": {"id": "r1", "status": "completed"}}))
        out = [p.inbound(ev) for ev in _answer("d1", "r2", "Done.")]
        self.assertEqual(_meta(out[2]), "req")
        self.assertEqual(self.rows("live_stale_turn_output"), [])

    def test_delegation_created_before_the_recovery_message_is_stale(self) -> None:
        p, _ = _live()
        p.outbound(_create("req"))
        p.reset()  # drop -> fork
        p._ready = True
        p.inbound(_dlg("d_fork_spontaneous"))  # Live delegates on its own as the fork starts
        p.outbound({"type": "conversation.item.create", "item": {  # the recovery message
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": "again"}]}})
        p.outbound(_create("req"))  # the recovery turn re-sends the same request id
        stale = [p.inbound(ev) for ev in _answer("d_fork_spontaneous", "r0", "Nothing to do.")]
        self.assertEqual(_meta(stale[2]), "")
        p.inbound(_dlg("d_recovery"))
        own = [p.inbound(ev) for ev in _answer("d_recovery", "r1", "Done.")]
        self.assertEqual(_meta(own[2]), "req")

    def test_deferred_create_hands_the_turn_to_the_waiting_delegation(self) -> None:
        p, _ = _live()
        p.outbound(_create("a"))
        p.inbound(_dlg("d1"))
        p.inbound(_ev("d1", {"type": "response.created", "response": {"id": "r1"}}))
        p.inbound(_fc("d1", "c1"))
        self.assertEqual(p.outbound(_create("b")), [], "deferred while c1 is owed")
        p.outbound({"type": "conversation.item.create", "item": {
            "type": "function_call_output", "call_id": "c1", "output": "{}"}})
        p.inbound(_ev("d1", {"type": "response.completed", "response": {"id": "r1", "status": "completed"}}))
        out = [p.inbound(ev) for ev in _answer("d1", "r2", "Both done.")]
        self.assertEqual(_meta(out[2]), "b", "the continuation carries the queued message")


LEDGER_ARGS = {
    "operation": "preference", "scope": "fleet", "rule": "test_word_for_samantha_checks",
    "value": "pineapple",
    "owner_quote": "Please remember this for later: my test word for Samantha checks is pineapple.",
}


def _direct_turn(name: str, reply: str) -> dict:
    response_id = f"live-{name}"
    return {
        "name": name,
        "response_id": response_id,
        "events": [
            {"type": "response.created", "response": {"id": response_id}},
            {"type": "response.done", "response": {
                "id": response_id, "status": "completed",
                "output": [{"type": "message", "content": [
                    {"type": "output_text", "text": reply}]}],
            }},
        ],
    }


def _tool_turn(name: str, delegation: str, response_id: str, call_id: str,
               tool: str, arguments: dict, reply: str) -> dict:
    raw = json.dumps(arguments, sort_keys=True)
    call = {"type": "function_call", "call_id": call_id, "name": tool, "arguments": raw}
    return {
        "name": name,
        "response_id": response_id,
        "events": [
            _dlg(delegation),
            _ev(delegation, {"type": "response.created", "response": {"id": response_id}}),
            _fc(delegation, call_id, name=tool, args=raw),
            _ev(delegation, {"type": "response.completed", "response": {
                "id": response_id, "status": "completed", "output": [call]}}),
        ],
        "continuation": _answer(delegation, f"{response_id}-followup", reply),
    }


def _answer_turn(name: str, delegation: str, response_id: str, reply: str) -> dict:
    return {"name": name, "response_id": response_id,
            "events": [_dlg(delegation), *_answer(delegation, response_id, reply)]}


SMALL_TALK_TURN = _direct_turn("small-talk", "Hi! I am doing well, thanks for asking.")
TASK_TURN = _tool_turn(
    "task", "replay-task", "backend-task", "call-task", "cua_act",
    {"app": "Comet", "op": "read", "label": "Heading"}, "The heading is Example Domain.")
REMEMBER_TURN = _tool_turn(
    "remember", "replay-remember", "backend-remember", "call-ledger", "ledger_task",
    LEDGER_ARGS, "I saved pineapple for later.")
CASUAL_TURN = _direct_turn("casual", "Teal is a lovely colour. I am doing well, thanks.")
STORY_TURN = {
    "name": "story",
    "response_id": "backend-story",
    "events": [
        _dlg("replay-story"),
        _ev("replay-story", {"type": "response.created", "response": {"id": "backend-story"}}),
    ],
    "cancel": [_ev("replay-story", {"type": "response.done", "response": {
        "id": "backend-story", "status": "cancelled"}})],
}
REDIRECT_TURN = _answer_turn("redirect", "replay-redirect", "backend-redirect", "Calendar is open.")
REPLAY_SCRIPT = [SMALL_TALK_TURN, TASK_TURN, REMEMBER_TURN, CASUAL_TURN, STORY_TURN, REDIRECT_TURN]


class ScriptedGPTLiveReplayServer:
    """Offline GPT-Live stand-in: each typed turn replays its recorded server events, in order.

    Outcomes never depend on the turn's text. The script is fixed data, so a client that
    ignores the per-turn route cannot make the stand-in produce the expected delegation,
    ledger call, or spoken result.
    """

    ROUTE_MARKER = "Routing for this turn only"

    def __init__(self, session: LiveSession, script: list[dict] | None = None) -> None:
        self.session = session
        self.script = list(REPLAY_SCRIPT if script is None else script)
        self.pending: list[dict] = []
        self.turn = 0  # typed response.create index
        self.active_turn: dict | None = None
        self.continuation: list[dict] | None = None
        self.response_ids: dict[str, str] = {}
        self.tool_results: list[dict] = []
        self.cancel_requested: list[str] = []
        self.cancelled_responses: list[str] = []
        self.user_text = ""
        session.ws.on_client_event = self.on_client_event

    def _queue(self, *events: dict) -> None:
        self.pending.extend(events)

    def pump(self) -> int:
        events, self.pending = self.pending, []
        for event in events:
            feed(self.session, event)
        return len(events)

    def _replay(self, entry: dict) -> None:
        self.active_turn = entry
        if entry.get("response_id"):
            self.response_ids[str(entry["name"])] = entry["response_id"]
        self._queue(*entry["events"])

    def on_client_event(self, event: dict) -> None:
        event_type = event.get("type")
        if event_type == "response.item.create":
            item = event.get("item") or {}
            if item.get("type") == "function_call_output":
                try:
                    result = json.loads(str(item.get("output") or "{}"))
                except json.JSONDecodeError:
                    result = {"ok": False, "error": "invalid tool output"}
                self.tool_results.append(result)
                if self.active_turn and self.active_turn.get("continuation"):
                    self.continuation = self.active_turn["continuation"]
            elif item.get("type") == "message":
                parts = [str(part.get("text") or "") for part in item.get("content") or []]
                self.user_text = " ".join(part for part in parts if self.ROUTE_MARKER not in part)
        elif event_type == "response.cancel":
            response_id = str(event.get("response_id") or "")
            entry = next((item for item in self.script if item.get("response_id") == response_id), None)
            if entry and entry.get("cancel"):
                self.cancel_requested.append(response_id)
                self.cancelled_responses.append(response_id)
                self._queue(*entry["cancel"])
        elif event_type == "response.create":
            if self.continuation is not None:
                events, self.continuation = self.continuation, None
                self._queue(*events)
                return
            entry = self.script[self.turn] if self.turn < len(self.script) else {
                "name": "unscripted", "events": []}
            self.turn += 1
            self._replay(entry)


class StrictGPTLiveReplayTests(_Rows):
    """A deterministic replay against the GPT-Live wire contract; no socket or model calls."""

    def setUp(self) -> None:
        super().setUp()
        tools.reset_session_state()
        for target in ("voice_cua.workflow_store.begin_engagement",
                       "voice_cua.live_session.island_publish",
                       "voice_cua.tools.island_publish",
                       "voice_cua.live_session.hide_agent_cursor"):
            p = patch(target)
            p.start()
            self.addCleanup(p.stop)
        backend = MagicMock()
        backend.act.return_value = {"ok": True, "app": "Comet", "verified": True,
                                    "text": "Example Domain"}
        patched_backend = patch("voice_cua.tools.get_backend", return_value=backend)
        patched_backend.start()
        self.addCleanup(patched_backend.stop)
        self.s = live_session()
        self.s.ws = StrictGPTLiveWS()
        self.s._send(build_session_update(text_only=True))
        self.s.provider._ready = True
        self.server = ScriptedGPTLiveReplayServer(self.s)
        from voice_cua.ledger_helper import LedgerHelper
        self.transport = MagicMock()
        self.transport.call.return_value = {"ok": True, "data": {"decision_id": "pineapple-saved"}}
        self.helper = LedgerHelper(self.transport, Mock(side_effect=AssertionError("helper model called")),
                                   self.path.parent / "pending")
        ledger = patch.object(tools, "_LEDGER_HELPER", self.helper)
        ledger.start()
        self.addCleanup(ledger.stop)
        self.addCleanup(self.helper.close)

    def _prepare_turn(self) -> tuple[str, dict]:
        import uuid
        rid = uuid.uuid4().hex
        waiter = {"event": threading.Event(), "reply": "", "error": ""}
        with self.s._response_lock:
            self.s._text_waiters[rid] = waiter
        return rid, waiter

    def _ask(self, text: str) -> tuple[str, dict]:
        rid, waiter = self._prepare_turn()
        self.s.send_text(text, request_id=rid)
        return rid, waiter

    def _reply(self, rid: str, waiter: dict, timeout: float = 2.0) -> str:
        deadline = time.monotonic() + timeout
        while not waiter["event"].is_set() and time.monotonic() < deadline:
            self.server.pump()
            time.sleep(0.005)
        self.server.pump()
        self.assertTrue(waiter["event"].is_set(), "stand-in answered the typed turn")
        self.assertEqual(waiter["error"], "")
        with self.s._response_lock:
            self.s._text_waiters.pop(rid, None)
        return str(waiter["reply"])

    def _assert_typed_route(self, since: int, text: str, route: str) -> None:
        items = [event for event in self.s.ws.events[since:]
                 if event.get("type") == "response.item.create"
                 and (event.get("item") or {}).get("type") == "message"
                 and (event.get("item") or {}).get("role") == "user"]
        self.assertEqual(len(items), 1, "one typed user item on the wire")
        self.assertEqual(items[0]["item"]["content"], [
            {"type": "input_text", "text": text},
            {"type": "input_text", "text": providers.ROUTE_TEXT[route]},
        ])

    def _typed_turn(self, text: str, route: str) -> tuple[str, int]:
        since = len(self.s.ws.events)
        rid, waiter = self._ask(text)
        reply = self._reply(rid, waiter)
        self._assert_typed_route(since, text, route)
        return reply, since

    def _spoken_turn(self, delegation: str, response_id: str, reply: str,
                     start_ms: int) -> list[dict]:
        since = len(self.s.ws.events)
        feed(self.s, {"type": "session.input_transcript.delta", "delta": f"spoken {delegation}",
                      "start_ms": start_ms, "end_ms": start_ms + 800})
        feed(self.s, {"type": "session.delegation.created", "offset_ms": start_ms + 300,
                      "delegation": {"id": delegation}})
        for event in _answer(delegation, response_id, reply):
            feed(self.s, event)
        events = self.s.ws.events[since:]
        self.assertFalse([event for event in events
                          if ScriptedGPTLiveReplayServer.ROUTE_MARKER in json.dumps(event)],
                         "a spoken turn carries no route text")
        self.assertFalse([event for event in events
                          if event.get("type") == "session.instructions.append"],
                         "a spoken turn sends no persistent instruction")
        return events

    def _assert_route_only_in_typed_items(self, expected: int) -> None:
        routing = [event for event in self.s.ws.events
                   if ScriptedGPTLiveReplayServer.ROUTE_MARKER in json.dumps(event)]
        self.assertEqual(len(routing), expected, "one route part per typed turn")
        for event in routing:
            self.assertEqual(event.get("type"), "response.item.create")
            self.assertEqual((event.get("item") or {}).get("role"), "user")
            self.assertEqual(len((event.get("item") or {}).get("content") or []), 2)
        self.assertFalse([event for event in self.s.ws.events
                          if event.get("type") == "session.instructions.append"
                          and ScriptedGPTLiveReplayServer.ROUTE_MARKER in str(event.get("content") or "")],
                         "routing is never a persistent session instruction")

    def test_gpt_live_strict_replay_covers_chat_task_memory_and_redirect(self) -> None:
        # Turn 1: small talk is a direct reply and cannot save a ledger row.
        reply, _ = self._typed_turn("Hi there, how are you?", "direct")
        self.assertIn("doing well", reply)
        self.transport.call.assert_not_called()

        # Turn 2: a browser task is delegated; its function result is spoken back.
        reply, _ = self._typed_turn("Open example.com and read the heading.", "delegate")
        self.assertIn("Example Domain", reply)
        self.assertEqual(len(self.server.tool_results), 1)
        self.assertIs(self.server.tool_results[0]["ok"], True)
        self.transport.call.assert_not_called()

        # Turn 3: the explicit remember turn runs the real ledger save path exactly once.
        reply, _ = self._typed_turn(
            "Please remember this for later: my test word for Samantha checks is pineapple.",
            "delegate")
        self.assertIn("saved pineapple", reply)
        self.assertEqual(len(self.server.tool_results), 2)
        self.assertIs(self.server.tool_results[-1]["ok"], True)
        self.assertEqual(self.transport.call.call_count, 1)
        self.assertEqual(self.transport.call.call_args.args[0], "save")
        payload = self.transport.call.call_args.args[1]
        self.assertEqual((payload["scope"], payload["rule"], payload["decision"]),
                         ("fleet", "test_word_for_samantha_checks", "pineapple"))

        # Turn 4: a casual favourite-colour mention is chat and saves nothing.
        reply, _ = self._typed_turn("Hey, my favourite colour is teal. How are you?", "direct")
        self.assertIn("doing well", reply)
        self.assertEqual(self.transport.call.call_count, 1)

        # Turn 5: the story is delegated; the redirect cancels it, then answers its own task.
        story_since = len(self.s.ws.events)
        story_text = "Open example.com and read the long story."
        _story_rid, story_waiter = self._ask(story_text)
        self.server.pump()
        self.assertFalse(story_waiter["event"].is_set())
        self._assert_typed_route(story_since, story_text, "delegate")
        story_response = self.server.response_ids["story"]
        before_creates = sum(event["type"] == "response.create" for event in self.s.ws.events)
        redirect_since = len(self.s.ws.events)
        rid, waiter = self._prepare_turn()
        sender = threading.Thread(target=self.s.send_text,
                                  kwargs={"text": "Actually, open Calendar instead.", "request_id": rid})
        sender.start()
        deadline = time.monotonic() + 1.0
        while story_response not in self.server.cancel_requested and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertIn(story_response, self.server.cancel_requested)
        self.assertEqual(sum(event["type"] == "response.create" for event in self.s.ws.events),
                         before_creates)
        self.server.pump()
        sender.join(timeout=1.0)
        self.assertFalse(sender.is_alive())
        self.assertEqual(sum(event["type"] == "response.create" for event in self.s.ws.events),
                         before_creates + 1)
        self.assertEqual(self.server.cancelled_responses, [story_response])
        self.assertIn("Calendar is open", self._reply(rid, waiter))
        self.assertTrue(story_waiter["event"].is_set())
        self._assert_typed_route(redirect_since, "Actually, open Calendar instead.", "delegate")

        self._assert_route_only_in_typed_items(6)
        self.assertEqual(self.s.ws.errors, [])

    def test_gpt_live_casual_favourite_colour_turn_replies_without_saving(self) -> None:
        self.server = ScriptedGPTLiveReplayServer(self.s, script=[CASUAL_TURN])
        text = "Hey, my favourite colour is teal. How are you?"
        reply, _ = self._typed_turn(text, "direct")
        self.assertIn("doing well", reply)
        self.transport.call.assert_not_called()
        self._assert_route_only_in_typed_items(1)
        self.assertEqual(self.s.ws.errors, [])

    def test_gpt_live_explicit_pineapple_remember_turn_saves_once(self) -> None:
        self.server = ScriptedGPTLiveReplayServer(self.s, script=[REMEMBER_TURN])
        quote = "Please remember this for later: my test word for Samantha checks is pineapple."
        reply, _ = self._typed_turn(quote, "delegate")
        self.assertIn("saved pineapple", reply)
        self.assertEqual(len(self.server.tool_results), 1)
        result = self.server.tool_results[0]
        self.assertIs(result["ok"], True)
        self.assertNotIn("error", result)
        self.assertEqual(self.transport.call.call_count, 1)
        self.assertEqual(self.transport.call.call_args.args[0], "save")
        payload = self.transport.call.call_args.args[1]
        self.assertEqual((payload["scope"], payload["rule"], payload["decision"]),
                         ("fleet", "test_word_for_samantha_checks", "pineapple"))
        self._assert_route_only_in_typed_items(1)
        self.assertEqual(self.s.ws.errors, [])

    def test_gpt_live_alternating_typed_and_spoken_turns_do_not_inherit_route(self) -> None:
        self.server = ScriptedGPTLiveReplayServer(
            self.s, script=[SMALL_TALK_TURN,
                            _answer_turn("calendar", "replay-calendar", "backend-calendar",
                                         "Calendar is open.")])
        reply, _ = self._typed_turn("Hi there, how are you?", "direct")
        self.assertIn("doing well", reply)
        self._spoken_turn("spoken-one", "spoken-response-one", "It is sunny.", start_ms=1000)
        reply, _ = self._typed_turn("Open Calendar and read it.", "delegate")
        self.assertIn("Calendar is open", reply)
        self._spoken_turn("spoken-two", "spoken-response-two", "It is cold.", start_ms=2000)
        self._assert_route_only_in_typed_items(2)
        self.assertEqual(self.s.ws.errors, [])


class LiveDropThenThreeRequestsTests(_Rows):
    """A drop mid-request, then three sequential requests: every waiter gets its own answer."""

    def setUp(self) -> None:
        super().setUp()
        # hide_agent_cursor talks to the ACU service; unstubbed it blocks on a host without one
        # (or moves the real agent cursor on a Mac), outlasting the settle window.
        for target in (
            "voice_cua.live_session.island_publish",
            "voice_cua.tools.island_publish",
            "voice_cua.live_session.hide_agent_cursor",
        ):
            p = patch(target)
            p.start()
            self.addCleanup(p.stop)
        settle = patch("voice_cua.live_session.RECOVERY_SETTLE_S", 0.2)
        settle.start()
        self.addCleanup(settle.stop)
        self.s = live_session()
        self.s._send(build_session_update(text_only=True))
        self.s.provider._ready = True
        self.n = 0

    def _ask(self, text: str) -> tuple[str, dict]:
        import threading
        import uuid
        rid = uuid.uuid4().hex
        waiter = {"event": threading.Event(), "reply": "", "error": ""}
        with self.s._response_lock:
            self.s._text_waiters[rid] = waiter
        self.s.send_text(text, request_id=rid)
        return rid, waiter

    def _delegate(self, d: str, text: str) -> None:
        self.n += 1
        feed(self.s, _dlg(d))
        for ev in _answer(d, f"resp_{self.n}", text):
            feed(self.s, ev)

    def _reply(self, waiter: dict, timeout: float = 1.0) -> str:
        self.assertTrue(waiter["event"].wait(timeout), "waiter answered")
        self.assertEqual(waiter["error"], "")
        with self.s._response_lock:  # send_text_and_wait pops its waiter when it returns
            for rid, w in list(self.s._text_waiters.items()):
                if w is waiter:
                    self.s._text_waiters.pop(rid)
        return str(waiter["reply"])

    def _drop(self) -> None:
        import contextlib
        import io
        s = self.s
        with contextlib.redirect_stdout(io.StringIO()):
            s._on_close(None, None, None)  # TCP lost, no close frame
        s._prepare_reconnect()
        s._conn_epoch += 1
        s.ws = StrictGPTLiveWS()
        s.provider._ready = True  # forked session started

    def test_drop_then_three_sequential_requests_each_get_their_own_answer(self) -> None:
        import contextlib
        import io
        s = self.s
        with contextlib.redirect_stdout(io.StringIO()):
            _a, wa = self._ask("Open example.com and read the heading.")
            self._delegate("d_a", "The heading is Example Domain.")
            self.assertEqual(self._reply(wa), "The heading is Example Domain.")

            _b, wb = self._ask("Read the BBC top headline.")
            feed(s, _dlg("d_b"))
            feed(s, _ev("d_b", {"type": "response.created", "response": {"id": "resp_b"}}))
            s._turn_tools = 1  # a tool ran, then the socket dropped
            self._drop()
            self.assertFalse(wb["event"].is_set(), "the dropped request's waiter survives")
            feed(s, _dlg("d_fork"))  # spontaneous delegation as the fork starts
            s._run_drop_recovery()
            self.assertIn("response.create", [e["type"] for e in s.ws.events])
            for ev in _answer("d_fork", "resp_fork", "No new request is present."):
                feed(s, ev)
            # The recovery delegation answers from stale context, then Live re-delegates.
            self._delegate("d_recovery", "The previous task is complete: Example Domain.")
            self._delegate("d_b2", "The BBC headline is X.")
            self.assertEqual(self._reply(wb), "The BBC headline is X.")
            self.assertEqual(len(self.rows("live_recovery_answer_superseded")), 1)

            _c, wc = self._ask("Search DuckDuckGo and read the first result.")
            # A late continuation of the previous turn's delegation must not answer C.
            for ev in _answer("d_b2", "resp_late", "I have closed the window."):
                feed(s, ev)
            self.assertFalse(wc["event"].is_set())
            self._delegate("d_c", "The first result is Y.")
            self.assertEqual(self._reply(wc), "The first result is Y.")

            _d, wd = self._ask("What time is it?")
            self._delegate("d_d", "It is noon.")
            self.assertEqual(self._reply(wd), "It is noon.")

            _e, we = self._ask("Close the window.")
            self._delegate("d_e", "Closed.")
            self.assertEqual(self._reply(we), "Closed.")

    def test_recovery_answer_without_a_follow_up_is_delivered_after_the_settle(self) -> None:
        import contextlib
        import io
        s = self.s
        with contextlib.redirect_stdout(io.StringIO()):
            _b, wb = self._ask("Read the BBC top headline.")
            feed(s, _dlg("d_b"))
            s._turn_tools = 1
            self._drop()
            s._run_drop_recovery()
            self._delegate("d_recovery", "The BBC headline is X.")
            self.assertFalse(wb["event"].is_set(), "held for the settle window")
            self.assertEqual(self._reply(wb), "The BBC headline is X.")
            _c, wc = self._ask("Next one.")
            self._delegate("d_c", "Y.")
            self.assertEqual(self._reply(wc), "Y.")


if __name__ == "__main__":
    unittest.main()
