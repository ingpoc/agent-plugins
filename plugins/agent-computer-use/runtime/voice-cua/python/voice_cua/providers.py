"""Voice provider adapters: one session loop, three speech backends.

The session loop (realtime_session.RealtimeSession) speaks the OpenAI Realtime
event dialect. An adapter translates at the socket edge only:

- ``outbound(event)`` turns one loop event into zero or more wire events;
- ``inbound(event)`` turns one wire event into zero or more loop events.

So tools, the RiskGate (tools.dispatch), lazy tool groups, usage logging and
reconnect backoff are shared by every provider. Realtime is an identity adapter,
so gpt-realtime-2 / 2.1 behave exactly as before.

Providers:
- ``openai-realtime``: gpt-realtime-2, gpt-realtime-2.1, gpt-realtime-2.1-mini.
- ``xai``: Grok Voice 3.0 (``grok-voice-latest``), wss://api.x.ai/v1/realtime —
  a Realtime-compatible dialect with a flatter session (voice, turn_detection).
- ``openai-live``: GPT-Live-1 (``gpt-live-1``), wss://api.openai.com/v1/live/sessions,
  Responses delegation. The live model talks; a Responses backend runs our
  function tools. Two backend tiers: Bulk (default gpt-6-luna) and Intelligence
  (default gpt-6.1-sol). Bulk is the session default; the backend escalates once
  per delegation by calling ``use_intelligence`` (multi-step / ambiguous work), and
  the app escalates automatically when a Bulk tool step fails after its 2 tries.
  ``response.create`` uses the backend model stored in the session, so the switch
  is one ``session.update`` before the continuation.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Callable

from voice_cua.activity_log import log_event

REALTIME_MODELS = ("gpt-realtime-2", "gpt-realtime-2.1", "gpt-realtime-2.1-mini")
LIVE_MODEL = "gpt-live-1"
XAI_MODEL = "grok-voice-latest"
BACKEND_MODELS = ("gpt-6-luna", "gpt-6-sol", "gpt-6.1-sol", "gpt-6-astra")
DEFAULT_BULK = "gpt-6-luna"
DEFAULT_INTELLIGENCE = "gpt-6.1-sol"
MAX_STEP_TRIES = 2
ESCALATE_TOOL = "use_intelligence"
LIVE_APPEND_MAX_CHARS = 1800  # session.*.append content is capped at 500 tokens
XAI_VOICES = ("eve", "ara", "leo", "rex", "sal")
XAI_DEFAULT_VOICE = "eve"

LIVE_INSTRUCTIONS = (
    "You are Samantha, a warm, concise voice assistant that operates this Mac. "
    "Delegate every request that needs the computer, an app, the browser or a credential "
    "to the backend; never claim an action happened until the backend reports it. "
    "When the backend asks for confirmation of a risky action, ask the user plainly and wait."
)

ROUTING_INSTRUCTIONS = (
    "\n\nRouting: you are the Bulk tier, for simple, high-volume tool work (open an app, click, "
    f"type, a single Comet step). If the task needs multi-step planning or the request is "
    f"ambiguous, call {ESCALATE_TOOL} first (once), then continue."
)


def provider_for(model: str) -> str:
    m = (model or "").strip().lower()
    if m == LIVE_MODEL:
        return "openai-live"
    if m.startswith("grok-voice"):
        return "xai"
    return "openai-realtime"


class VoiceProvider:
    """Identity adapter (OpenAI Realtime GA)."""

    name = "openai-realtime"

    def __init__(self, model: str, *, send_raw: Callable[[dict[str, Any]], bool] | None = None) -> None:
        self.model = model
        self._send_raw = send_raw or (lambda _e: False)

    def bind(self, send_raw: Callable[[dict[str, Any]], bool]) -> None:
        self._send_raw = send_raw

    def url(self) -> str:
        return f"wss://api.openai.com/v1/realtime?model={self.model}"

    def resolve_key(self) -> str:
        from voice_cua.auth import resolve_openai_api_key

        return resolve_openai_api_key()

    def outbound(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        return [event]

    def inbound(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        return [event]

    def usage_labels(self, response: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"provider": self.name, "model": self.model}

    def reset(self) -> None:
        """New connection: drop per-conversation state."""

    def finalize(self, send: Callable[[dict[str, Any]], bool], timeout: float) -> None:
        """Before closing the socket: end the provider session gracefully (no-op for Realtime)."""


class RealtimeProvider(VoiceProvider):
    name = "openai-realtime"


class XAIProvider(VoiceProvider):
    """Grok Voice: Realtime-shaped events; flatter session.update; server VAD only."""

    name = "xai"

    def url(self) -> str:
        return f"wss://api.x.ai/v1/realtime?model={self.model}"

    def resolve_key(self) -> str:
        from voice_cua.auth import resolve_xai_api_key

        return resolve_xai_api_key()

    @staticmethod
    def _session(session: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key in ("instructions", "tools"):
            if key in session:
                out[key] = session[key]
        audio = session.get("audio") or {}
        if audio:
            inp, outp = audio.get("input") or {}, audio.get("output") or {}
            voice = str(outp.get("voice") or "")
            out["voice"] = voice if voice in XAI_VOICES else XAI_DEFAULT_VOICE
            out["turn_detection"] = {"type": "server_vad"}  # no semantic_vad / eagerness knobs
            fmt: dict[str, Any] = {}
            if inp.get("format"):
                fmt["input"] = {"format": inp["format"]}
            if outp.get("format"):
                fmt["output"] = {"format": outp["format"]}
            if fmt:
                out["audio"] = fmt
        elif "output_modalities" in session:
            out["turn_detection"] = None  # text-only: manual turns
        return out

    def outbound(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        et = event.get("type")
        if et == "session.update":
            return [{"type": "session.update", "session": self._session(event.get("session") or {})}]
        if et == "conversation.item.create":
            item = event.get("item") or {}
            if item.get("type") == "message" and item.get("role") == "system":
                # xAI items are user/assistant/function only: send notes as a tagged user item.
                text = " ".join(str(c.get("text") or "") for c in item.get("content") or [] if isinstance(c, dict))
                return [{"type": "conversation.item.create", "item": {
                    "type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": f"[system note] {text}"}]}}]
        if et == "response.create":
            return [{"type": "response.create"}]
        return [event]


class TranscriptBurst:
    """Groups GPT-Live transcript deltas into utterances (the docs: deltas carry no item id or
    turn-end event, so the app groups them). Flushes after a quiet gap on a timer."""

    def __init__(self, on_flush: Callable[[str], None], gap_s: float = 1.2) -> None:
        self._on_flush = on_flush
        self._gap_s = gap_s
        self._parts: list[str] = []
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    def add(self, delta: str) -> bool:
        """Append a fragment exactly as received; True when it opens a new utterance."""
        with self._lock:
            fresh = not self._parts
            self._parts.append(delta)
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self._gap_s, self.flush)
            self._timer.daemon = True
            self._timer.start()
        return fresh

    def flush(self) -> None:
        with self._lock:
            text, self._parts = "".join(self._parts).strip(), []
            if self._timer:
                self._timer.cancel()
                self._timer = None
        if text:
            self._on_flush(text)


class LiveProvider(VoiceProvider):
    """GPT-Live-1 with Responses delegation and a Bulk → Intelligence router."""

    name = "openai-live"

    def __init__(self, model: str = LIVE_MODEL, *, bulk: str | None = None, intelligence: str | None = None,
                 voice: str = "marin", send_raw: Callable[[dict[str, Any]], bool] | None = None) -> None:
        super().__init__(model, send_raw=send_raw)
        self.bulk = bulk if bulk in BACKEND_MODELS else DEFAULT_BULK
        self.intelligence = intelligence if intelligence in BACKEND_MODELS else DEFAULT_INTELLIGENCE
        self.voice = voice
        self._lock = threading.Lock()
        self.closed = threading.Event()
        self.heard = TranscriptBurst(self._on_heard)
        self.said = TranscriptBurst(self._on_said)
        self.reset()

    @staticmethod
    def _on_heard(text: str) -> None:
        from voice_cua.island_facade import island_publish

        # Words go to voice.log only; the activity log keeps metadata (as on Realtime).
        log_event("user_transcript", chars=len(text), provider="openai-live")
        print(f"You: {text}", flush=True)
        island_publish("thinking", title=text[:120], detail="Thinking")

    @staticmethod
    def _on_said(text: str) -> None:
        log_event("assistant_reply", text=text[:240], source="live_voice")
        print(f"Samantha: {text}", flush=True)

    # -- state ---------------------------------------------------------------
    def reset(self) -> None:
        with getattr(self, "_lock", threading.Lock()):
            self._started = False
            self.tier = "bulk"
            self._escalated: set[str] = set()  # delegation ids already escalated
            self._call_delegation: dict[str, str] = {}
            self._response_delegation: dict[str, str] = {}
            self._response_tier: dict[str, tuple[str, str]] = {}
            self._pending_calls: dict[str, set[str]] = {}  # delegation -> outstanding call ids
            self._last_delegation = ""
            self._pending_request = ""  # voice_cua_request_id of the last typed request
            self._skip_next_create = False
            self._delegation_request: dict[str, str] = {}
            self._delegation_response: dict[str, str] = {}
            self._response_text: dict[str, str] = {}
            self._response_calls: set[str] = set()
            self.voice_seconds = 0
            self._out_seq = 0
            self._last_out = 0.0

    def url(self) -> str:
        return "wss://api.openai.com/v1/live/sessions"

    def finalize(self, send: Callable[[dict[str, Any]], bool], timeout: float) -> None:
        """Docs: send session.close and keep receiving until session.closed (final usage)."""
        with self._lock:
            started = self._started
        if not started:
            return
        self.closed.clear()
        if send({"type": "session.close"}):
            if not self.closed.wait(timeout):
                log_event("live_close_unconfirmed", live_model=self.model)

    def backend_model(self, tier: str | None = None) -> str:
        return self.intelligence if (tier or self.tier) == "intelligence" else self.bulk

    def usage_labels(self, response: dict[str, Any] | None = None) -> dict[str, Any]:
        rid = str((response or {}).get("id") or "")
        tier, model = self._response_tier.get(rid, (self.tier, self.backend_model()))
        labels: dict[str, Any] = {"provider": self.name, "model": model, "live_model": self.model, "tier": tier}
        delegation = self._response_delegation.get(rid)
        if delegation:
            labels["delegation_id"] = delegation[:64]
        return labels

    # -- outbound ------------------------------------------------------------
    @staticmethod
    def escalate_tool() -> dict[str, Any]:
        return {
            "type": "function",
            "name": ESCALATE_TOOL,
            "description": "Hand this task to the Intelligence model (multi-step planning or ambiguity). Once, first.",
            "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
        }

    def _backend_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = [t for t in tools if t.get("name") != ESCALATE_TOOL]
        return out + [self.escalate_tool()]

    def _start(self, session: dict[str, Any]) -> dict[str, Any]:
        from voice_cua import SYSTEM_INSTRUCTIONS

        rate = (((session.get("audio") or {}).get("input") or {}).get("format") or {}).get("rate", 24000)
        return {"type": "session.start", "session": {
            "model": self.model,
            "instructions": LIVE_INSTRUCTIONS,
            "audio": {"format": {"type": "audio/pcm", "rate": rate}, "output": {"voice": self.voice}},
            "delegation": {"type": "responses", "responses": {
                "model": self.bulk,
                "instructions": SYSTEM_INSTRUCTIONS + ROUTING_INSTRUCTIONS,
                "tools": self._backend_tools(list(session.get("tools") or [])),
                "tool_choice": "auto",
                "parallel_tool_calls": False,
            }},
        }}

    def _set_model_event(self, model: str) -> dict[str, Any]:
        return {"type": "session.update", "session": {"delegation": {"responses": {"model": model}}}}

    def outbound(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        et = event.get("type")
        if et == "session.update":
            session = event.get("session") or {}
            with self._lock:
                first = not self._started
                self._started = True
            if first:
                return [self._start(session)]
            if "tools" in session:
                return [{"type": "session.update", "session": {"delegation": {"responses": {
                    "tools": self._backend_tools(list(session.get("tools") or []))}}}}]
            return []
        if et == "input_audio_buffer.append":
            return [{"type": "session.input_audio.append", "audio": event.get("audio") or ""}]
        if et in {"input_audio_buffer.clear", "input_audio_buffer.commit", "response.cancel",
                  "conversation.item.truncate", "conversation.item.delete"}:
            return []  # GPT-Live manages turns, barge-in and truncation itself
        if et == "conversation.item.create":
            item = event.get("item") or {}
            itype = item.get("type")
            if itype == "function_call_output":
                return self._tool_output(item)
            if itype == "function_call":
                return []  # stale-call re-creation after reconnect: Live has no such items
            if itype == "message":
                text = " ".join(str(c.get("text") or "") for c in item.get("content") or [] if isinstance(c, dict))
                if item.get("role") == "system":
                    return [{"type": "session.instructions.append", "content": text[:LIVE_APPEND_MAX_CHARS],
                             "delegation_id": None}]
                return [{"type": "response.item.create", "item": {
                    "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}}]
            return []
        if et == "response.create":
            with self._lock:
                skip, self._skip_next_create = self._skip_next_create, False
            if skip:
                return []
            meta = (event.get("response") or {}).get("metadata") or {}
            request_id = str(meta.get("voice_cua_request_id") or "") if isinstance(meta, dict) else ""
            if request_id:
                with self._lock:
                    self._pending_request = request_id
            return [{"type": "response.create"}]
        return [event]

    def _tool_output(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        call_id = str(item.get("call_id") or "")
        out: list[dict[str, Any]] = []
        with self._lock:
            known = call_id in self._call_delegation
            if not known:
                # The call belongs to a session that reconnected away (a new Live session has no
                # such delegation): hand the result over as quiet context and skip the continuation.
                self._skip_next_create = True
        if not known:
            note = f"Result of a task started before the reconnect: {str(item.get('output') or '')}"
            log_event("live_stale_result", call_id=call_id[:64] or None)
            return [{"type": "session.thinking.append", "content": note[:LIVE_APPEND_MAX_CHARS], "delegation_id": None}]
        with self._lock:
            delegation = self._call_delegation.get(call_id, self._last_delegation)
            if delegation in self._pending_calls:
                self._pending_calls[delegation].discard(call_id)
        if self.tier == "bulk" and self.bulk_failed(item.get("output")):
            out += self._escalate(delegation, reason="bulk_failed_twice")
        out.append({"type": "response.item.create", "item": {
            "type": "function_call_output", "call_id": call_id, "output": str(item.get("output") or "")}})
        return out

    @staticmethod
    def bulk_failed(output: Any) -> bool:
        """A Bulk tool step that failed after its 2 tries (or hit the retry cap)."""
        try:
            data = json.loads(output) if isinstance(output, str) else output
        except (TypeError, ValueError):
            return False
        if not isinstance(data, dict) or data.get("ok") is not False:
            return False
        if data.get("needs_confirmation"):
            return False  # a confirmation refusal is not a failure
        tries = data.get("tries")
        return data.get("error_type") == "retry_cap" or (
            isinstance(tries, int) and tries >= MAX_STEP_TRIES and data.get("retryable") is False)

    def _escalate(self, delegation: str, *, reason: str) -> list[dict[str, Any]]:
        key = delegation or "<no-delegation-id>"  # missing ids still escalate only once
        with self._lock:
            if key in self._escalated or self.tier == "intelligence":
                return []
            self._escalated.add(key)
            self.tier = "intelligence"
        log_event("live_escalate", reason=reason, delegation_id=delegation[:64] or None,
                  from_model=self.bulk, to_model=self.intelligence)
        return [self._set_model_event(self.intelligence)]

    def _deescalate(self) -> None:
        with self._lock:
            if self.tier == "bulk":
                return
            self.tier = "bulk"
        self._send_raw(self._set_model_event(self.bulk))

    # -- inbound -------------------------------------------------------------
    def inbound(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        et = event.get("type")
        if et == "session.started":
            log_event("live_session_started", live_model=self.model, bulk=self.bulk, intelligence=self.intelligence)
            return [{"type": "session.updated", "session": event.get("session") or {}}]
        if et == "session.updated":
            return [{"type": "session.updated", "session": event.get("session") or {}}]
        if et == "session.output_audio.delta":
            now = time.monotonic()
            with self._lock:
                if now - self._last_out > 1.5:
                    self._out_seq += 1
                self._last_out = now
                rid = f"live_out_{self._out_seq}"
            return [{"type": "response.output_audio.delta", "response_id": rid, "item_id": rid,
                     "delta": event.get("delta") or ""}]
        if et == "session.input_transcript.delta":
            self.said.flush()
            fresh = self.heard.add(str(event.get("delta") or ""))
            return [{"type": "input_audio_buffer.speech_started"}] if fresh or self._new_user_burst() else []
        if et == "session.output_transcript.delta":
            self.heard.flush()
            self.said.add(str(event.get("delta") or ""))
            return []
        if et in {"session.usage.updated", "session.closed"}:
            usage = event.get("usage") or {}
            seconds = usage.get("seconds") if isinstance(usage, dict) else None
            if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
                self.voice_seconds = int(seconds)
            log_event("live_usage", live_model=self.model, voice_seconds=self.voice_seconds,
                      final=et == "session.closed", reason=str(event.get("reason") or "") or None)
            if et == "session.closed":
                self.closed.set()
            return []
        if et == "session.delegation.created":
            delegation = str((event.get("delegation") or {}).get("id") or event.get("delegation_id") or "")
            with self._lock:
                self._last_delegation = delegation or self._last_delegation
            log_event("live_delegation", delegation_id=delegation[:64] or None,
                      target=str((event.get("delegation") or {}).get("target") or event.get("target") or ""))
            return []
        if et == "response.event":
            return self._backend_event(str(event.get("delegation_id") or ""), event.get("event") or {})
        if et == "error":
            return [event]
        return []

    def _new_user_burst(self) -> bool:
        now = time.monotonic()
        with self._lock:
            fresh = now - getattr(self, "_last_in", 0.0) > 1.5
            self._last_in = now
        return fresh

    def _backend_event(self, delegation: str, inner: dict[str, Any]) -> list[dict[str, Any]]:
        it = inner.get("type")
        response = inner.get("response") or {}
        rid = str(response.get("id") or inner.get("response_id") or "")
        if it == "response.created":
            with self._lock:
                self._delegation_response[delegation] = rid
        elif not rid:
            # Nested item/text events carry no response id: use the delegation's current response.
            rid = self._delegation_response.get(delegation, "")
        if it == "response.created":
            with self._lock:
                self._last_delegation = delegation or self._last_delegation
                if self._pending_request and delegation not in self._delegation_request:
                    self._delegation_request[delegation] = self._pending_request
                    self._pending_request = ""
                request_id = self._delegation_request.get(delegation, "")
                if rid:
                    self._response_delegation[rid] = delegation
                    self._response_tier[rid] = (self.tier, self.backend_model())
                    if len(self._response_tier) > 256:
                        self._response_tier.pop(next(iter(self._response_tier)))
            created: dict[str, Any] = {"id": rid}
            if request_id:
                created["metadata"] = {"voice_cua_request_id": request_id}
            return [{"type": "response.created", "response": created}]
        if it == "response.output_text.done":
            with self._lock:
                self._response_text[rid] = str(inner.get("text") or "")[:4000]
            return []
        if it == "response.output_item.done":
            item = inner.get("item") or {}
            if item.get("type") == "message":
                text = " ".join(str(c.get("text") or "") for c in item.get("content") or []
                                if isinstance(c, dict) and c.get("type") == "output_text").strip()
                if text:
                    with self._lock:
                        self._response_text[rid] = text[:4000]
                return []
            if item.get("type") != "function_call":
                return []
            call_id = str(item.get("call_id") or "")
            with self._lock:
                self._response_calls.add(rid)
                self._call_delegation[call_id] = delegation
                self._pending_calls.setdefault(delegation, set()).add(call_id)
            if item.get("name") == ESCALATE_TOOL:
                self._answer_escalation(delegation, call_id)
                return []
            return [{"type": "response.output_item.done", "response_id": rid, "item": item}]
        if it in {"response.completed", "response.done", "response.failed", "response.incomplete"}:
            with self._lock:
                outstanding = bool(self._pending_calls.get(delegation))
                had_calls = rid in self._response_calls
                self._response_calls.discard(rid)
                text = self._response_text.pop(rid, "")
                request_id = self._delegation_request.get(delegation, "")
                if not had_calls and not outstanding:
                    self._delegation_request.pop(delegation, None)
            if not had_calls and not outstanding:
                # Delegation finished: the next task starts on Bulk again.
                self._deescalate()
            # Lifecycle snapshots arrive with output: []; rebuild what the loop needs.
            output: list[dict[str, Any]] = []
            if had_calls:
                output.append({"type": "function_call"})
            elif text:
                output.append({"type": "message", "content": [{"type": "output_text", "text": text}]})
            done: dict[str, Any] = {"id": rid, "status": response.get("status") or it.split(".")[-1],
                                    "usage": response.get("usage") or {}, "output": output}
            if request_id:
                done["metadata"] = {"voice_cua_request_id": request_id}
            return [{"type": "response.done", "response": done}]
        return []

    def _answer_escalation(self, delegation: str, call_id: str) -> None:
        events = self._escalate(delegation, reason="backend_requested")
        with self._lock:
            self._pending_calls.get(delegation, set()).discard(call_id)
        for ev in events + [
            {"type": "response.item.create", "item": {"type": "function_call_output", "call_id": call_id,
                                                      "output": json.dumps({"ok": True, "tier": "intelligence"})}},
            {"type": "response.create"},
        ]:
            self._send_raw(ev)


def make_provider(model: str, settings: dict[str, str] | None = None) -> VoiceProvider:
    settings = settings or {}
    kind = provider_for(model)
    if kind == "openai-live":
        return LiveProvider(
            LIVE_MODEL,
            bulk=os.environ.get("VOICE_CUA_LIVE_BULK_MODEL") or settings.get("live_bulk_model"),
            intelligence=os.environ.get("VOICE_CUA_LIVE_INTELLIGENCE_MODEL") or settings.get("live_intelligence_model"),
            voice=os.environ.get("VOICE_CUA_LIVE_VOICE", "marin"),
        )
    if kind == "xai":
        return XAIProvider(model)
    return RealtimeProvider(model)
