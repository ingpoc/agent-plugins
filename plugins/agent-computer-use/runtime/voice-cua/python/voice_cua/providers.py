"""Voice provider adapters: one session loop, two speech backends.

The session loop (realtime_session.RealtimeSession) speaks the OpenAI Realtime
event dialect internally (the loop's own event schema; the OpenAI Realtime API itself is no
longer a backend). An adapter translates at the socket edge only:

- ``outbound(event)`` turns one loop event into zero or more wire events;
- ``inbound(event)`` turns one wire event into zero or more loop events.

So tools, the RiskGate (tools.dispatch), lazy tool groups, usage logging and
reconnect backoff are shared by every provider.

Providers:
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

import base64
import binascii
import json
import os
import re
import threading
import time
from typing import Any, Callable

from voice_cua.activity_log import log_event

LIVE_MODEL = "gpt-live-1"  # the only OpenAI voice path (Realtime removed)
XAI_MODEL = "grok-voice-latest"
BACKEND_MODELS = ("gpt-6-luna", "gpt-6-sol", "gpt-6.1-sol", "gpt-6-astra")
DEFAULT_BULK = "gpt-6-luna"
DEFAULT_INTELLIGENCE = "gpt-6.1-sol"
MAX_STEP_TRIES = 2
ESCALATE_TOOL = "use_intelligence"
# Context budget (live-conversations: seed sessions with only the history they need).
# A delegation whose backend input passed this many tokens marks the conversation for a
# seeded restart (recent utterances only) at the next quiet moment instead of a fork.
LIVE_CONTEXT_ROTATE_TOKENS = int(os.environ.get("VOICE_CUA_LIVE_CONTEXT_ROTATE_TOKENS", "40000"))
# Chars of one tool result sent to Live (was a blind 8000-char prefix cut). The server drops the
# socket intermittently (TCP FIN, no close frame or error) with or without large outputs, so
# drops are handled by reconnect + redelivery; a smaller, structure-keeping result lowers the
# backend's re-read cost and kept AX dumps under the size seen in most drops.
LIVE_TOOL_OUTPUT_MAX = int(os.environ.get("VOICE_CUA_LIVE_TOOL_OUTPUT_MAX", "6000"))
HISTORY_MAX = 24          # seeded restart: recent utterances (docs cap: 128 msgs / 8,192 tokens)
PENDING_AUDIO_MAX = 150   # ~3 s of 20 ms chunks buffered until session.started
LIVE_APPEND_MAX_CHARS = 1200  # session.*.append content is capped at 500 tokens (URLs tokenize densely)
_INDENT = re.compile(r"(?m)^( +)")


def _string_leaves(node: Any, path: tuple = ()) -> list[tuple[int, tuple]]:
    out: list[tuple[int, tuple]] = []
    if isinstance(node, str):
        out.append((len(node), path))
    elif isinstance(node, dict):
        for key, value in node.items():
            out += _string_leaves(value, path + (key,))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            out += _string_leaves(value, path + (index,))
    return out


def _cut_text(text: str, room: int) -> str:
    if len(text) <= room:
        return text
    cut = text.rfind("\n", 0, room)
    cut = cut if cut > room // 2 else room
    hidden = text.count("\n", cut) + 1
    return text[:cut] + f"\n…[{hidden} more lines cut; ask for a narrower view if needed]"


def compact_tool_output(output: str, cap: int = LIVE_TOOL_OUTPUT_MAX) -> str:
    """Fit a tool result under ``cap`` chars and keep it valid JSON.

    Accessibility trees get their indentation halved; then the longest text fields (AX dump,
    page text — wherever they nest, e.g. Comet's response.results[].text) are cut, so the
    ok/error/url fields always survive instead of a blind prefix cut.
    """
    if len(output) <= cap:
        return output
    try:
        data = json.loads(output)
    except (TypeError, ValueError):
        data = None
    if not isinstance(data, (dict, list)):
        return output[:cap] + "…[truncated]"

    def get(path: tuple) -> Any:
        node = data
        for key in path:
            node = node[key]
        return node

    def put(path: tuple, value: str) -> None:
        get(path[:-1])[path[-1]] = value

    for size, path in _string_leaves(data):
        if size > 400 and path:
            put(path, _INDENT.sub(lambda m: " " * (len(m.group(1)) // 2), get(path)))
    for _ in range(12):
        out = json.dumps(data)
        over = len(out) - cap
        if over <= 0:
            return out
        leaves = sorted((leaf for leaf in _string_leaves(data) if leaf[1]), reverse=True)
        if not leaves or leaves[0][0] < 120:
            break
        size, path = leaves[0]
        # Escaping (newlines, quotes) makes the JSON longer than the raw text: overshoot a bit.
        escaped = len(json.dumps(get(path)))
        put(path, _cut_text(get(path), max(80, int(size * (escaped - over - 80) / escaped))))
    return output[:cap] + "…[truncated]"


def _is_silence(b64: str) -> bool:
    try:
        raw = base64.b64decode(b64, validate=False)
    except (binascii.Error, ValueError):
        return False
    return not raw.strip(b"\x00")


XAI_VOICES = ("eve", "ara", "leo", "rex", "sal")
XAI_DEFAULT_VOICE = "eve"

# Live prompt = conversation only (Migrate to GPT-Live: identity, speaking style, backchannel and
# interruption policy, plus what the backend can do in plain language). Tool names, workflows and
# safety rules stay in the backend prompt (SYSTEM_INSTRUCTIONS) with the tool definitions.
LIVE_INSTRUCTIONS = (
    "Identity: You are Samantha, a warm, concise voice assistant that operates this Mac for the user.\n"
    "Style: Speak English unless the user clearly speaks a full sentence in another language. One or two "
    "short sentences. Say what was done, not what could be done. Never end with offers like 'anything else?'.\n"
    "Backchannel policy: Brief acknowledgments only ('On it.'). Greetings, small talk and 'can you hear me' "
    "get a short spoken reply and no delegation.\n"
    "Interruption policy: Stop speaking when interrupted and listen. If the user changes or cancels the "
    "request, delegate the change; never say work was cancelled until the backend confirms it.\n"
    "Delegation policy:\n"
    "Backend tools:\n- Mac apps (Calculator, Finder, TextEdit, Music, Calendar, Settings), Comet browsing, and "
    "saved credential lookup without revealing secrets.\n"
    "Generate <|SpawnThinking|> when the user asks for computer, app, website or credential work, a "
    "correction changes work already requested, or the request needs careful reasoning beyond a simple reply.\n"
    "Do not generate <|SpawnThinking|> for greetings, small talk, a brief clarification, a repeated result, "
    "or an answer from the conversation or a still-current result.\n"
    "Generate <|SpawnThinking|> before answering when the answer depends on backend work. Do not guess "
    "the result while waiting. Never claim an "
    "action happened until the backend reports it. Unclear or partial speech, or a vague 'that thing'/'it' the last "
    "exchange didn't name: ask one short question (which one?) instead of delegating a guess. When the backend asks to confirm a risky action (send, buy, delete, "
    "pay, overwrite), ask the user plainly and wait for a clear yes."
)

ROUTING_INSTRUCTIONS = (
    "\n\nRouting: you are the Bulk tier, for simple, high-volume tool work (open an app, click, "
    f"type, a single Comet step). If the task needs multi-step planning or the request is "
    f"ambiguous, call {ESCALATE_TOOL} first (once), then continue."
)


def provider_for(model: str) -> str:
    m = (model or "").strip().lower()
    if m.startswith("grok-voice"):
        return "xai"
    return "openai-live"  # gpt-live-1, and any retired gpt-realtime-* value


class VoiceProvider:
    """Adapter base: identity translation; subclasses own the wire dialect."""

    name = "base"

    def __init__(self, model: str, *, send_raw: Callable[[dict[str, Any]], bool] | None = None) -> None:
        self.model = model
        self._send_raw = send_raw or (lambda _e: False)

    def bind(self, send_raw: Callable[[dict[str, Any]], bool]) -> None:
        self._send_raw = send_raw

    def url(self) -> str:
        raise NotImplementedError

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
        """Before closing the socket: end the provider session gracefully (no-op by default)."""


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
    manages_barge_in = True  # no response.cancel / truncate: Live owns turn-taking

    def __init__(self, model: str = LIVE_MODEL, *, bulk: str | None = None, intelligence: str | None = None,
                 voice: str = "marin", store: bool = True, send_raw: Callable[[dict[str, Any]], bool] | None = None) -> None:
        super().__init__(model, send_raw=send_raw)
        self.bulk = bulk if bulk in BACKEND_MODELS else DEFAULT_BULK
        self.intelligence = intelligence if intelligence in BACKEND_MODELS else DEFAULT_INTELLIGENCE
        self.voice = voice
        self._lock = threading.Lock()
        self.closed = threading.Event()
        self._usage_unconfirmed = False
        self.store = store
        self.source_session_id = ""   # last session that reached session.started (fork source)
        self.fork_failed = False      # a fork attempt failed at startup: next connect starts fresh
        self.restart_requested = ""   # set when the loop should drop this socket and reconnect
        self.history: list[tuple[str, str]] = []  # (role, text) utterances for seeded restarts
        self._seq = 0
        self.heard = TranscriptBurst(self._on_heard)
        self.said = TranscriptBurst(self._on_said)
        self.reset()

    def _remember(self, role: str, text: str) -> None:
        self.history.append((role, text[:600]))
        del self.history[:-HISTORY_MAX]

    def _on_heard(self, text: str) -> None:
        if getattr(self, "_credential_capture", False):
            return
        from voice_cua.island_facade import island_publish
        from voice_cua.workflow_store import begin_engagement

        begin_engagement()
        self._remember("user", text)
        with self._lock:
            backend_busy = bool(self._active_backend_responses or any(self._pending_calls.values()))
            if backend_busy:
                self._pending_spoken_update = text
            else:
                self._pending_spoken_update = ""
        if backend_busy:
            log_event("live_backend_input_queued", chars=len(text))
        # 'You:' line in voice.log plus the text (capped) in the local activity log.
        log_event("user_transcript", chars=len(text), text=text[:240], provider="openai-live")
        print(f"You: {text}", flush=True)
        island_publish("thinking", title=text[:120], detail="Thinking")

    def _on_said(self, text: str) -> None:
        self._remember("assistant", text)
        log_event("assistant_reply", text=text[:240], source="live_voice")
        print(f"Samantha: {text}", flush=True)

    # -- state ---------------------------------------------------------------
    def reset(self) -> None:
        with getattr(self, "_lock", threading.Lock()):
            self._started = False
            self._ready = False           # session.started seen on this connection
            rotate = bool(getattr(self, "rotate_requested", False))
            # Forks require a finalized recording, not just a session.started ID.
            self._forking = bool(self.store and getattr(self, "source_session_id", "")
                                 and self.closed.is_set()
                                 and not getattr(self, "fork_failed", False) and not rotate)
            self.closed.clear()
            self._usage_unconfirmed = False
            self.rotate_requested = False
            self.seeded_for_context = rotate
            self._pending_audio: list[str] = []
            self._pending_wire: list[dict[str, Any]] = []
            self.restart_requested = ""
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
            # Turn ids: every typed request (response.create carrying voice_cua_request_id,
            # including a drop-recovery replay) opens a new turn. A delegation belongs to the
            # turn that was current when it was created; output of an older turn's delegation
            # never answers the current request. The counter survives reconnects.
            self._turn_seq = getattr(self, "_turn_seq", 0)
            self._turn_request = getattr(self, "_turn_request", "")
            self._delegation_turn: dict[str, int] = {}
            self._delegation_response: dict[str, str] = {}
            self._response_text: dict[str, str] = {}
            self._response_calls: set[str] = set()
            self._active_backend_responses: set[str] = set()
            self._pending_spoken_update = ""
            self.voice_seconds = 0
            self._out_seq = getattr(self, "_out_seq", 0)
            self._last_out = 0.0

    def url(self) -> str:
        # Managing sessions > Store and fork: a WebSocket fork continues the stored conversation.
        if self._forking:
            return f"wss://api.openai.com/v1/live/sessions/{self.source_session_id}/fork"
        return "wss://api.openai.com/v1/live/sessions"

    def event_id(self, kind: str) -> str:
        with self._lock:
            self._seq += 1
            return f"voice_{kind}_{self._seq}"

    @property
    def forked(self) -> bool:
        return self._forking

    def mute_event(self, muted: bool) -> dict[str, Any]:
        """Managing sessions > Control microphone input (acknowledged by muted/unmuted)."""
        kind = "mute" if muted else "unmute"
        return {"type": f"session.input_audio.{kind}", "event_id": self.event_id(kind)}

    def finalize(self, send: Callable[[dict[str, Any]], bool], timeout: float) -> None:
        """Docs: send session.close and keep receiving until session.closed (final usage)."""
        with self._lock:
            if self.closed.is_set():
                return
            started = self._started
        if not started:
            return
        if not send({"type": "session.close"}) or not self.closed.wait(timeout):
            self.mark_usage_unconfirmed()

    def mark_usage_unconfirmed(self) -> None:
        """Keep the last observed usage when a running session closes without session.closed."""
        with self._lock:
            if not self._ready or self.closed.is_set() or self._usage_unconfirmed:
                return
            self._usage_unconfirmed = True
            seconds = self.voice_seconds
        log_event("live_close_unconfirmed", live_model=self.model, voice_seconds=seconds, final=False)

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
        from voice_cua import SYSTEM_INSTRUCTIONS, session_instructions

        rate = (((session.get("audio") or {}).get("input") or {}).get("format") or {}).get("rate", 24000)
        if self._forking:
            # Fork startup accepts only store, Responses delegation settings and audio.format;
            # model, voice, instructions and input are inherited from the source.
            return {"type": "session.start", "event_id": self.event_id("start"), "session": {
                "store": self.store,
                "audio": {"format": {"type": "audio/pcm", "rate": rate}},
            }}
        start: dict[str, Any] = {"type": "session.start", "event_id": self.event_id("start"), "session": {
            "model": self.model,
            "instructions": LIVE_INSTRUCTIONS,
            "audio": {"format": {"type": "audio/pcm", "rate": rate}, "output": {"voice": self.voice}},
            "delegation": {"type": "responses", "responses": {
                "model": self.bulk,
                "instructions": str(session.get("instructions") or session_instructions()) + ROUTING_INSTRUCTIONS,
                "tools": self._backend_tools(list(session.get("tools") or [])),
                "tool_choice": "auto",
                "parallel_tool_calls": False,
            }},
        }}
        start["session"]["store"] = self.store
        seeded = self.seed_input()
        if seeded:
            start["session"]["input"] = seeded
        return start

    def seed_input(self) -> list[dict[str, Any]]:
        """Managing sessions > Seed a session: prior text turns when no fork is possible."""
        out = []
        for role, text in self.history[-HISTORY_MAX:]:
            part = "input_text" if role == "user" else "output_text"
            out.append({"type": "message", "role": role, "content": [{"type": part, "text": text}]})
        return out

    def _set_model_event(self, model: str) -> dict[str, Any]:
        # The server requires delegation.type on every update (seen on forked sessions:
        # missing_required_parameter session.delegation.type); it names the current mode.
        return {"type": "session.update", "event_id": self.event_id("upd"),
                "session": {"delegation": {"type": "responses", "responses": {"model": model}}}}

    def outbound(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        wires = self._outbound(event)
        with self._lock:
            if self._ready:
                return wires
            # Connections: nothing but session.start before session.started; hold the rest.
            now = [w for w in wires if w.get("type") == "session.start"]
            self._pending_wire.extend(w for w in wires if w.get("type") != "session.start")
            del self._pending_wire[:-64]
        return now

    def _outbound(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        et = event.get("type")
        if et == "session.update":
            session = event.get("session") or {}
            with self._lock:
                first = not self._started
                self._started = True
            if first:
                return [self._start(session)]
            if "tools" in session:
                return [{"type": "session.update", "event_id": self.event_id("upd"),
                         "session": {"delegation": {"type": "responses", "responses": {
                             "tools": self._backend_tools(list(session.get("tools") or []))}}}}]
            return []
        if et == "input_audio_buffer.append":
            wire = {"type": "session.input_audio.append", "audio": event.get("audio") or ""}
            with self._lock:
                if not self._ready:
                    # Connections: wait for session.started before sending audio. Keep the
                    # opening speech (wake from idle) and deliver it once the session is ready.
                    # Keepalive silence is not kept: replaying it in a burst after a reconnect
                    # tripped "Input audio is arriving too quickly" (max 1.2x real time).
                    if _is_silence(wire["audio"]):
                        return []
                    self._pending_audio.append(wire["audio"])
                    del self._pending_audio[:-PENDING_AUDIO_MAX]
                    return []
            return [wire]
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
                    if self._forking and text.startswith("The realtime connection was re-established"):
                        return []  # a fork keeps the conversation: no 'earlier turns are gone' note
                    return [{"type": "session.instructions.append", "event_id": self.event_id("instructions"),
                             "content": text[:LIVE_APPEND_MAX_CHARS], "delegation_id": None}]
                self._remember("user", text)  # typed turns seed restarts like spoken ones
                with self._lock:
                    self._pending_spoken_update = ""  # this newer explicit request supersedes queued speech
                    self._user_item_queued = True  # the next response.create opens a turn
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
            with self._lock:
                outstanding = sum(len(c) for c in self._pending_calls.values())
                new_turn = bool(request_id) and (
                    request_id != self._turn_request or getattr(self, "_user_item_queued", False))
                self._user_item_queued = False
                if new_turn:
                    # A typed request (or its drop-recovery replay); a tool continuation that
                    # carries the same request id stays in its turn.
                    self._pending_request = request_id
                    self._turn_seq += 1
                    self._turn_request = request_id
                    if outstanding:
                        # The queued message rides on the waiting delegation's continuation:
                        # that delegation now answers this turn.
                        for d, calls in self._pending_calls.items():
                            if calls:
                                self._delegation_turn[d] = self._turn_seq
                                self._delegation_request[d] = request_id
            if outstanding:
                # Live rejects response.create while function results are owed
                # (function_call_outputs_required). The queued message rides on the
                # continuation the loop sends after the last output.
                log_event("live_create_deferred", outstanding_calls=outstanding)
                return []
            with self._lock:
                update = "" if self._active_backend_responses else self._pending_spoken_update
                if update:
                    self._pending_spoken_update = ""
            out = []
            if update:
                # Only amend the backend input between responses, never mid-generation.
                out.append({"type": "response.item.create", "item": {
                    "type": "message", "role": "user", "content": [{"type": "input_text", "text":
                        "Latest spoken update while your task was running (may be partial): "
                        + update[:LIVE_APPEND_MAX_CHARS]
                        + "\nUse this to revise the pending task. Do not repeat completed actions or guess at incomplete speech."}]}})
                log_event("live_backend_input_updated", chars=len(update))
            return out + [{"type": "response.create"}]
        return [event]

    def _tool_output(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        call_id = str(item.get("call_id") or "")
        out: list[dict[str, Any]] = []
        with self._lock:
            known = call_id in self._call_delegation
        if not known:
            # The call belongs to a session that reconnected away (a new Live session has no
            # such delegation): hand the result over as context; the caller's response.create
            # then continues the task once (drop recovery).
            note = f"Result of a task started before the reconnect: {str(item.get('output') or '')}"
            log_event("live_stale_result", call_id=call_id[:64] or None)
            return [{"type": "session.thinking.append", "content": note[:LIVE_APPEND_MAX_CHARS], "delegation_id": None}]
        with self._lock:
            delegation = self._call_delegation.get(call_id, self._last_delegation)
            if delegation in self._pending_calls:
                self._pending_calls[delegation].discard(call_id)
        if self.tier == "bulk" and self.bulk_failed(item.get("output")):
            out += self._escalate(delegation, reason="bulk_failed_twice")
        raw = str(item.get("output") or "")
        # Every result stays in the conversation the backend re-reads on each delegation, and
        # large ones risk a server-side socket drop (see LIVE_TOOL_OUTPUT_MAX).
        output = compact_tool_output(raw)
        if len(output) != len(raw):
            log_event("live_tool_output_compacted", chars=len(raw), sent_chars=len(output))
        out.append({"type": "response.item.create", "item": {
            "type": "function_call_output", "call_id": call_id, "output": output}})
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
            session = event.get("session") or {}
            with self._lock:
                self._ready = True
                pending, self._pending_audio = self._pending_audio, []
                held, self._pending_wire = self._pending_wire, []
                if session.get("id"):
                    self.source_session_id = str(session["id"])
                self.fork_failed = False
            log_event("live_session_started", live_model=self.model, bulk=self.bulk, intelligence=self.intelligence,
                      forked=self._forking or None, store=bool(session.get("store")) or None,
                      buffered_chunks=len(pending) or None,
                      seeded_for_context=getattr(self, "seeded_for_context", False) or None,
                      seeded_messages=(len(self.history[-HISTORY_MAX:]) if not self._forking else None) or None)
            for wire in held:
                self._send_raw(wire)
            for chunk in pending:
                self._send_raw({"type": "session.input_audio.append", "audio": chunk})
            return [{"type": "session.updated", "session": session}]
        if et in {"session.input_audio.muted", "session.input_audio.unmuted"}:
            log_event("live_input_audio", state=et.rsplit(".", 1)[-1],
                      client_event_id=str(event.get("client_event_id") or "") or None)
            return []
        if et in {"session.instructions.appended", "session.thinking.appended", "session.commentary.appended"}:
            return []
        if et == "session.updated":
            return [{"type": "session.updated", "session": event.get("session") or {}}]
        if et == "session.output_audio.delta":
            now = time.monotonic()
            audible = not _is_silence(str(event.get("delta") or ""))
            with self._lock:
                # Live streams silence too; only audible frames define a reply burst.
                if audible:
                    if not self._out_seq or now - self._last_out > 1.5:
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
                with self._lock:
                    self.closed.set()
                reason = str(event.get("reason") or "")
                if reason in {"expired", "content", "connection_lost", "remote_hangup"} and not self.restart_requested:
                    with self._lock:
                        self.restart_requested = f"live_closed_{reason}"
            return []
        if et == "session.delegation.created":
            delegation = str((event.get("delegation") or {}).get("id") or event.get("delegation_id") or "")
            with self._lock:
                self._last_delegation = delegation or self._last_delegation
                if delegation:
                    self._claim_delegation(delegation)
            log_event("live_delegation", delegation_id=delegation[:64] or None,
                      target=str((event.get("delegation") or {}).get("target") or event.get("target") or ""))
            return []
        if et == "response.event":
            return self._backend_event(str(event.get("delegation_id") or ""), event.get("event") or {})
        if et == "error":
            return self._error(event)
        return []

    def _error(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        """Managing sessions > Handle errors: startup vs running session, rejected commands, moderation."""
        err = event.get("error") or {}
        code = str(err.get("code") or "")
        param = str(err.get("param") or "")
        client_event_id = str(err.get("client_event_id") or "")
        with self._lock:
            started = self._ready
            forking = self._forking
        phase = "running" if started else "startup"
        log_event("live_error", phase=phase, code=code or None, param=param or None,
                  client_event_id=client_event_id or None, error_type=str(err.get("type") or "") or None,
                  message=str(err.get("message") or "")[:160] or None, forked=forking or None)
        if not started and forking:
            # The stored source could not be forked (storage off, ZDR, not finalized): start a
            # fresh session seeded with saved text history instead.
            with self._lock:
                self.fork_failed = True
                self.restart_requested = "live_fork_failed"
            return []
        if not started and self.store and (param == "session.store" or "store" in str(err.get("message") or "").lower()):
            # Docs: storage must be enabled for the project and is off under ZDR. Fall back to an
            # unstored session (no fork on reconnect; seeded history instead).
            with self._lock:
                self.store = False
                self.restart_requested = "live_store_unavailable"
            return []
        if started and client_event_id.startswith(("voice_instructions", "voice_mute", "voice_unmute")):
            return []  # a rejected context/mic command is logged, not surfaced as a session failure
        return [event]

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
                self._active_backend_responses.add(rid)
                self._delegation_response[delegation] = rid
        elif not rid:
            # Nested item/text events carry no response id: use the delegation's current response.
            rid = self._delegation_response.get(delegation, "")
        if it == "response.created":
            with self._lock:
                self._last_delegation = delegation or self._last_delegation
                if delegation not in self._delegation_turn:
                    self._claim_delegation(delegation)  # no delegation.created seen
                request_id = self._current_request(delegation)
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
                self._active_backend_responses.discard(rid)
                outstanding = bool(self._pending_calls.get(delegation))
                had_calls = rid in self._response_calls
                self._response_calls.discard(rid)
                if not had_calls and not outstanding:
                    self._pending_spoken_update = ""  # no continuation: do not leak speech into a later task
                text = self._response_text.pop(rid, "")
                request_id = self._current_request(delegation)
                stale = bool(text) and not had_calls and not request_id and bool(
                    self._delegation_request.get(delegation))
            if stale:
                log_event("live_stale_turn_output", delegation_id=delegation[:64] or None,
                          chars=len(text))
            if not had_calls and not outstanding:
                # Delegation finished: the next task starts on Bulk again.
                self._deescalate()
            # Lifecycle snapshots arrive with output: []; rebuild what the loop needs.
            output: list[dict[str, Any]] = []
            if had_calls:
                output.append({"type": "function_call"})
            elif text:
                output.append({"type": "message", "content": [{"type": "output_text", "text": text}]})
            usage = response.get("usage") or {}
            if not had_calls and not outstanding and isinstance(usage, dict):
                used = int(usage.get("input_tokens") or 0)
                if used >= LIVE_CONTEXT_ROTATE_TOKENS and not self.rotate_requested:
                    self.rotate_requested = True
                    log_event("live_context_rotate_due", input_tokens=used)
            done: dict[str, Any] = {"id": rid, "status": response.get("status") or it.split(".")[-1],
                                    "usage": usage, "output": output}
            if request_id:
                done["metadata"] = {"voice_cua_request_id": request_id}
            return [{"type": "response.done", "response": done}]
        return []

    def _claim_delegation(self, delegation: str) -> None:
        """Bind a new delegation to the turn current at its creation (caller holds the lock)."""
        if delegation in self._delegation_turn:
            return
        self._pending_spoken_update = ""  # a new delegation already carries the newer conversation
        self._delegation_turn[delegation] = self._turn_seq
        if self._turn_request:
            self._delegation_request[delegation] = self._turn_request
            self._pending_request = ""
        if len(self._delegation_turn) > 256:
            old = next(iter(self._delegation_turn))
            self._delegation_turn.pop(old, None)
            self._delegation_request.pop(old, None)

    def _current_request(self, delegation: str) -> str:
        """The request id this delegation answers, or "" when it belongs to an older turn."""
        if self._delegation_turn.get(delegation) != self._turn_seq:
            return ""
        return self._delegation_request.get(delegation, "")

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
    if kind == "xai":
        return XAIProvider(model)
    from voice_cua.voice_settings import live_voice

    return LiveProvider(
        LIVE_MODEL,
        bulk=os.environ.get("VOICE_CUA_LIVE_BULK_MODEL") or settings.get("live_bulk_model"),
        intelligence=os.environ.get("VOICE_CUA_LIVE_INTELLIGENCE_MODEL") or settings.get("live_intelligence_model"),
        voice=os.environ.get("VOICE_CUA_LIVE_VOICE") or live_voice(settings.get("realtime_voice")),
        store=str(settings.get("live_store", "true")).lower() not in {"0", "false", "no", "off"},
    )
