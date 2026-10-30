"""Provider adapter layer: Grok Voice (xAI), GPT-Live-1 (Responses delegation).

Offline only: fake sockets, no network, no API keys.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_TMP = tempfile.TemporaryDirectory(prefix="voice-cua-test-")
os.environ.setdefault("VOICE_CUA_ACTIVITY_LOG", str(Path(_TMP.name) / "activity.jsonl"))
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from voice_cua import auth, providers, tools  # noqa: E402
from voice_cua.providers import (  # noqa: E402
    ESCALATE_TOOL, LiveProvider, XAIProvider, make_provider, provider_for,
)
from voice_cua.realtime_session import RealtimeSession, build_session_update  # noqa: E402
from test_phase1_reliability import FakeWS, feed  # noqa: E402

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
        self.assertIsInstance(make_provider("grok-voice-latest"), XAIProvider)
        self.assertIsInstance(make_provider("gpt-live-1"), LiveProvider)

    def test_live_tier_defaults_and_bad_ids_fall_back(self) -> None:
        p = make_provider("gpt-live-1", {"live_bulk_model": "nope", "live_intelligence_model": "gpt-6-astra"})
        with patch.dict(os.environ, {"VOICE_CUA_LIVE_BULK_MODEL": "", "VOICE_CUA_LIVE_INTELLIGENCE_MODEL": ""}):
            p = make_provider("gpt-live-1", {"live_bulk_model": "nope", "live_intelligence_model": "gpt-6-astra"})
        self.assertEqual((p.bulk, p.intelligence), ("gpt-6-luna", "gpt-6-astra"))
        self.assertEqual(LiveProvider().intelligence, "gpt-6.1-sol")


class XAITests(unittest.TestCase):
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

    def test_system_note_becomes_tagged_user_item(self) -> None:
        (out,) = XAIProvider("grok-voice-latest").outbound({"type": "conversation.item.create", "item": {
            "type": "message", "role": "system", "content": [{"type": "input_text", "text": "reconnected"}]}})
        self.assertEqual(out["item"]["role"], "user")
        self.assertIn("[system note] reconnected", out["item"]["content"][0]["text"])

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
    def test_first_update_starts_session_with_bulk_delegation(self) -> None:
        p, _ = _live()
        self.assertEqual(p.url(), "wss://api.openai.com/v1/live/sessions")
        (start,) = p.outbound(UPDATE)
        self.assertEqual(start["type"], "session.start")
        s = start["session"]
        self.assertEqual(s["model"], "gpt-live-1")
        self.assertIn("Generate <|SpawnThinking|> when", s["instructions"])
        self.assertIn("Do not generate <|SpawnThinking|>", s["instructions"])
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

    def test_voice_seconds_logged_separately(self) -> None:
        p, _ = _live()
        self.assertEqual(p.inbound({"type": "session.usage.updated", "usage": {"seconds": 12},
                                    "context_window": {"usage_ratio": 0.4}}), [])
        p.inbound({"type": "session.closed", "usage": {"seconds": 15}, "reason": "close_requested"})
        rows = self.rows("live_usage")
        self.assertEqual([r["voice_seconds"] for r in rows], [12, 15])
        self.assertTrue(rows[1]["final"])
        self.assertNotIn("input_tokens", rows[0])


def live_session() -> RealtimeSession:
    settings = {"realtime_model": "gpt-live-1", "realtime_voice": "alloy", "eagerness": "balanced",
                "mic_profile": "near_field", "live_bulk_model": "gpt-6-luna",
                "live_intelligence_model": "gpt-6.1-sol"}
    with patch("voice_cua.voice_settings.load_settings", return_value=settings), \
            patch("voice_cua.realtime_session.load_settings", return_value=settings):
        session = RealtimeSession(api_key="test", enable_audio=False)
    session.ws = FakeWS()
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

    def test_risky_backend_call_is_gated_and_loads_confirm(self) -> None:
        s = live_session()
        s._send(build_session_update(text_only=True))
        s.provider._ready = True
        done: list[int] = []
        risky = json.dumps({"app": "Mail", "op": "click", "label": "Send"})
        with patch("voice_cua.realtime_session.island_publish"), \
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
        with patch("voice_cua.realtime_session.island_publish"):
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


class LiveTurnIdTests(_Rows):
    """Each typed request is a turn; only delegations created in that turn answer it."""

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


class LiveDropThenThreeRequestsTests(_Rows):
    """A drop mid-request, then three sequential requests: every waiter gets its own answer."""

    def setUp(self) -> None:
        super().setUp()
        # hide_agent_cursor talks to the ACU service; unstubbed it blocks on a host without one
        # (or moves the real agent cursor on a Mac), outlasting the settle window.
        for target in (
            "voice_cua.realtime_session.island_publish",
            "voice_cua.tools.island_publish",
            "voice_cua.realtime_session.hide_agent_cursor",
        ):
            p = patch(target)
            p.start()
            self.addCleanup(p.stop)
        settle = patch("voice_cua.realtime_session.RECOVERY_SETTLE_S", 0.2)
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
        s.ws = FakeWS()
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
