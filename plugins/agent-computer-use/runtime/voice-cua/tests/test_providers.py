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
        self.assertEqual(p.outbound({"type": "response.create"}), [], "no continuation for a dead delegation")
        self.assertEqual(p.outbound({"type": "response.create"}), [{"type": "response.create"}])

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


if __name__ == "__main__":
    unittest.main()
