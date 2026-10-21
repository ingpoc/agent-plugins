"""Realtime WebSocket session: voice + local tool dispatch.

Connects to OpenAI Realtime, registers function tools, executes them locally
via voice_cua.tools.dispatch, never echoes secrets to the model.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import random
import re
import signal
import sys
import threading
import time
import uuid
from typing import TYPE_CHECKING, Any

from voice_cua import SYSTEM_INSTRUCTIONS
from voice_cua.auth import resolve_openai_api_key
from voice_cua.activity_log import log_event, log_usage, tool_context
from voice_cua.activity_log import log_exception as _log_exception
from voice_cua.cua_bridge import hide_agent_cursor
from voice_cua.comet_bridge import comet_add_listener, comet_end, comet_is_active, comet_remove_listener
from voice_cua.island_facade import describe_tool, island_publish, island_voice_meter, summarize_result
from voice_cua.startup_trace import mark
from voice_cua.tools import dispatch, reset_session_state, tool_definitions
from voice_cua.voice_settings import apply_env, load_settings, noise_reduction_config, turn_detection_config

if TYPE_CHECKING:
    from voice_cua.audio_io import RealtimeAudio

try:
    import websocket  # websocket-client
except ImportError:  # pragma: no cover
    websocket = None  # type: ignore

REALTIME_MODEL = os.environ.get("VOICE_CUA_REALTIME_MODEL", "gpt-realtime-2")
REALTIME_VOICE = os.environ.get("VOICE_CUA_REALTIME_VOICE", "alloy")
REALTIME_SAMPLE_RATE = int(os.environ.get("VOICE_CUA_SAMPLE_RATE", "24000"))


def _normalize_reply(text: str) -> str:
    """Collapse duplicated Realtime transcript chunks (e.g. 'pong pong' → 'pong')."""
    words = text.split()
    if len(words) == 2 and words[0] == words[1]:
        return words[0]
    if len(words) >= 2 and len(words) % 2 == 0:
        half = len(words) // 2
        if words[:half] == words[half:]:
            return " ".join(words[:half])
    return text


def _model_and_voice() -> tuple[str, str]:
    cfg = load_settings()
    model = os.environ.get("VOICE_CUA_REALTIME_MODEL") or cfg["realtime_model"]
    voice = os.environ.get("VOICE_CUA_REALTIME_VOICE") or cfg["realtime_voice"]
    return model, voice


def input_transcription_config() -> dict[str, Any] | None:
    """GA audio.input.transcription — lets voice.log show what Samantha heard.

    VOICE_CUA_TRANSCRIBE_MODEL overrides the model; "off"/"none"/"0" disables.
    VOICE_CUA_TRANSCRIBE_LANGUAGE overrides the "en" hint; "auto" omits it.
    """
    # gpt-transcribe: whisper-1 / gpt-4o-*-transcribe are deprecated (Feb 2027).
    model = os.environ.get("VOICE_CUA_TRANSCRIBE_MODEL", "gpt-transcribe").strip()
    if not model or model.lower() in {"off", "none", "0", "false"}:
        return None
    cfg: dict[str, Any] = {"model": model}
    # English by default (noise like "Eikö." drifted replies into Indonesian).
    # VOICE_CUA_TRANSCRIBE_LANGUAGE=auto lets the model detect the language.
    language = os.environ.get("VOICE_CUA_TRANSCRIBE_LANGUAGE", "en").strip().lower()
    if language and language not in {"auto", "off", "none"}:
        cfg["language"] = language
    return cfg


def _ws_url() -> str:
    explicit = os.environ.get("VOICE_CUA_REALTIME_URL", "").strip()
    if explicit:
        return explicit
    model, _ = _model_and_voice()
    return f"wss://api.openai.com/v1/realtime?model={model}"


def build_session_update(
    *, text_only: bool = False, comet_active: bool = False, groups: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """GA Realtime session.update (beta header/shape retired 2026)."""
    _, voice = _model_and_voice()
    eagerness = os.environ.get("VOICE_CUA_EAGERNESS") or load_settings()["eagerness"]
    session: dict[str, Any] = {
        "type": "realtime",
        "output_modalities": ["text"] if text_only else ["audio"],
        "instructions": SYSTEM_INSTRUCTIONS,
        "tools": tool_definitions(comet_active=comet_active, groups=groups),
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "reasoning": {"effort": "low"},
    }
    if not text_only:
        session["audio"] = {
            "input": {
                "format": {"type": "audio/pcm", "rate": REALTIME_SAMPLE_RATE},
                "turn_detection": turn_detection_config(eagerness),
                "noise_reduction": noise_reduction_config(),
            },
            "output": {
                "format": {"type": "audio/pcm", "rate": REALTIME_SAMPLE_RATE},
                "voice": voice,
            },
        }
        transcription = input_transcription_config()
        if transcription:
            session["audio"]["input"]["transcription"] = transcription
    return {"type": "session.update", "session": session}


_RATE_LIMIT_MARKERS = (
    "rate_limit",
    "rate limit",
    "ratelimit",
    "too many requests",
    "tokens_per_min",
    "tokens per min",
    "token_limit",
    "token limit",
    "insufficient_quota",
)
_RETRY_IN_RE = re.compile(
    r"(?:try again|retry)\s+(?:in|after)\s+(\d+(?:\.\d+)?)\s*(ms|milliseconds?|s|secs?|seconds?|m|mins?|minutes?)\b",
    re.I,
)


def _is_rate_limit(err: dict[str, Any]) -> bool:
    markers = " ".join(str(err.get(k) or "") for k in ("type", "code", "message")).lower()
    return any(marker in markers for marker in _RATE_LIMIT_MARKERS)


def _seconds(value: Any, *, scale: float = 1.0) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value) * scale
    except (TypeError, ValueError):
        return None
    return number if number == number and number >= 0 else None


def retry_after_seconds(err: dict[str, Any] | None = None, headers: Any = None) -> float | None:
    """Server retry-after in seconds, from an error event and/or HTTP headers."""
    err = err if isinstance(err, dict) else {}
    for key, scale in (("retry_after_ms", 0.001), ("retry_after", 1.0), ("retry_after_seconds", 1.0)):
        found = _seconds(err.get(key), scale=scale)
        if found is not None:
            return found
    if headers:
        try:
            lowered = {str(k).lower(): v for k, v in dict(headers).items()}
        except Exception:  # noqa: BLE001
            lowered = {}
        found = _seconds(lowered.get("retry-after-ms"), scale=0.001)
        if found is None:
            found = _seconds(lowered.get("retry-after"))
        if found is None and lowered.get("retry-after"):
            try:
                from email.utils import parsedate_to_datetime

                when = parsedate_to_datetime(str(lowered["retry-after"]))
                found = max(0.0, when.timestamp() - time.time())
            except Exception:  # noqa: BLE001 — unparseable date → ignore
                found = None
        if found is not None:
            return found
    match = _RETRY_IN_RE.search(str(err.get("message") or ""))
    if match:
        amount, unit = float(match.group(1)), match.group(2).lower()
        if unit.startswith("ms") or unit.startswith("milli"):
            return amount / 1000.0
        if unit.startswith("m"):
            return amount * 60.0
        return amount
    return None


class RealtimeSession:
    REALTIME_PING_INTERVAL = 20
    REALTIME_PING_TIMEOUT = 15
    # Server enforces a 60-minute WS cap — plan a proactive reconnect at 55 min.
    PLANNED_RECONNECT_SECONDS = 3300.0
    PLANNED_RECONNECT_RETRY_SECONDS = 60.0
    # Reconnect backoff: full jitter, delay ~ U(0, min(cap, base * factor**(n-1)))
    # for consecutive attempt n. The counter only resets once a connection has
    # stayed up RECONNECT_STABLE_SECONDS; after RECONNECT_MAX_ATTEMPTS straight
    # failures we stop and say so (island + voice.log).
    RECONNECT_DELAY_BASE = 0.5
    RECONNECT_DELAY_FACTOR = 2.0
    RECONNECT_DELAY_MAX = 30.0
    RECONNECT_DELAY_FLOOR = 0.1  # never hot-loop, even when jitter rolls ~0
    RECONNECT_STABLE_SECONDS = 30.0
    RECONNECT_MAX_ATTEMPTS = 8
    # rate_limit / token-limit errors wait at least this long; a server
    # retry-after (header or "try again in Xs") wins when longer (≤ max).
    RATE_LIMIT_MIN_DELAY = 20.0
    RETRY_AFTER_MAX = 300.0

    def __init__(self, api_key: str | None = None, *, enable_audio: bool = True) -> None:
        if websocket is None:
            raise RuntimeError("pip install websocket-client")
        self.api_key = (api_key or resolve_openai_api_key()).strip()
        self.ws: Any = None
        self._pending_calls: dict[str, dict[str, Any]] = {}
        self._inflight_tools = 0
        self._send_lock = threading.Lock()
        self._text_request_lock = threading.Lock()
        self._response_lock = threading.Lock()
        self._response_condition = threading.Condition(self._response_lock)
        self._active_response_ids: set[str] = set()
        self._response_request_ids: dict[str, str] = {}
        self._response_transcripts: dict[str, str] = {}
        self._text_waiters: dict[str, dict[str, Any]] = {}
        self._text_only = not enable_audio
        self._audio_enabled = enable_audio
        self._audio: RealtimeAudio | None = None
        self._stop = threading.Event()
        self._session_ready = threading.Event()
        # Phase 1 reliability state.
        self._comet_active = False
        self._speaking_response_ids: set[str] = set()
        self._cancelled_response_ids: set[str] = set()
        self._current_response_id = ""
        self._last_user_text = ""
        self._reconnect_attempts = 0
        self._connected_at = 0.0  # monotonic time of the last session.updated
        self._retry_not_before = 0.0  # monotonic; set by rate limits / retry-after
        self._retry_reason = ""
        self._reconnect_gave_up = False
        self._tool_groups: set[str] = set()  # lazy tool groups loaded this conversation
        self._response_started: dict[str, float] = {}  # response_id → monotonic at response.created
        self._config_error_published = False
        self._rate_limit_warned = False
        self._planned_reconnect_timer: threading.Timer | None = None
        self._planned_reconnect_lock = threading.Lock()
        # Connection epoch: bumped on every socket open. A tool that started in
        # an older epoch must not post function_call_output (the new server
        # conversation has no such call_id) — see _deliver_tool_result.
        self._conn_epoch = 0
        self._send_drops = 0
        # call_id -> summary note, for results re-created after a reconnect.
        self._recreated_calls: dict[str, str] = {}
        comet_add_listener(self._on_comet_change)
        apply_env()

    def connect(self) -> None:
        """One blocking WS attempt (returns when the socket closes)."""
        headers = [f"Authorization: Bearer {self.api_key}"]
        self.ws = websocket.WebSocketApp(
            _ws_url(),
            header=headers,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        # Keepalive: ping every 20s; a ping timeout closes the socket so the
        # reconnect loop takes over — it must never stop the session.
        self.ws.run_forever(
            ping_interval=self.REALTIME_PING_INTERVAL,
            ping_timeout=self.REALTIME_PING_TIMEOUT,
        )

    @classmethod
    def _reconnect_delay(cls, attempt: int, rng: Any = None, *, min_delay: float = 0.0) -> float:
        """Full-jitter backoff for consecutive attempt ``attempt`` (1-based).

        U(0, min(cap, base * factor**(attempt-1))), floored at RECONNECT_DELAY_FLOOR,
        then raised to ``min_delay`` (rate limit / server retry-after).
        """
        rng = rng if rng is not None else random
        exp = max(0, min(int(attempt) - 1, 63))
        ceiling = min(cls.RECONNECT_DELAY_BASE * (cls.RECONNECT_DELAY_FACTOR ** exp), cls.RECONNECT_DELAY_MAX)
        delay = max(rng.uniform(0.0, ceiling), cls.RECONNECT_DELAY_FLOOR)
        return max(delay, min(float(min_delay or 0.0), cls.RETRY_AFTER_MAX))

    def _note_retry_hint(self, *, min_delay: float, reason: str) -> None:
        """Hold the next reconnect back >= ``min_delay`` from now (rate limit / retry-after)."""
        min_delay = min(max(0.0, float(min_delay)), self.RETRY_AFTER_MAX)
        until = time.monotonic() + min_delay
        if until > self._retry_not_before:
            self._retry_not_before = until
            self._retry_reason = reason
            log_event("reconnect_hold", reason=reason, min_delay=round(min_delay, 3))

    def _take_retry_hint(self) -> tuple[float, str]:
        remaining = self._retry_not_before - time.monotonic()
        reason = self._retry_reason
        self._retry_not_before = 0.0
        self._retry_reason = ""
        return (remaining, reason) if remaining > 0 else (0.0, "")

    def run(self) -> None:
        """Resilient top-level loop: connect, retry with backoff until stopped or out of attempts."""
        self._schedule_planned_reconnect()
        self._reconnect_gave_up = False
        self._reconnect_attempts = 0  # a fresh run() always gets the full budget
        self._retry_not_before = 0.0  # and never inherits a stale hold
        self._retry_reason = ""
        while not self._stop.is_set():
            self._connected_at = 0.0
            try:
                self.connect()
            except Exception as exc:  # noqa: BLE001 — keep the loop alive
                log_event("reconnect_attempt_failed", error=str(exc)[:120])
            if self._stop.is_set():
                break
            if self._connected_at and time.monotonic() - self._connected_at >= self.RECONNECT_STABLE_SECONDS:
                self._reconnect_attempts = 0  # that connection was stable — fresh budget
            self._reconnect_attempts += 1
            attempt = self._reconnect_attempts
            if attempt > self.RECONNECT_MAX_ATTEMPTS:
                self._give_up_reconnect()
                break
            min_delay, reason = self._take_retry_hint()
            delay = self._reconnect_delay(attempt, min_delay=min_delay)
            self._prepare_reconnect()
            island_publish(
                "reconnecting",
                title="Reconnecting",
                detail=f"attempt {attempt} of {self.RECONNECT_MAX_ATTEMPTS}",
            )
            log_event(
                "reconnecting",
                attempt=attempt,
                max_attempts=self.RECONNECT_MAX_ATTEMPTS,
                delay=round(delay, 3),
                reason=reason or None,
            )
            why = f" ({reason})" if reason else ""
            print(
                f"Realtime closed — reconnect attempt {attempt}/{self.RECONNECT_MAX_ATTEMPTS} in {delay:.1f}s{why}",
                flush=True,
            )
            if self._stop.wait(timeout=delay):
                break
        if self._reconnect_gave_up:
            return
        island_publish("idle", title="Idle", detail="Disconnected")
        print("Realtime closed", flush=True)

    def _give_up_reconnect(self) -> None:
        self._reconnect_gave_up = True
        failures = self.RECONNECT_MAX_ATTEMPTS
        self._take_retry_hint()  # drop any pending hold with the session
        self._cancel_planned_reconnect()
        self._prepare_reconnect()
        mark("realtime_reconnect_gave_up", status="fail", detail=f"{failures} attempts")
        log_event("reconnect_gave_up", attempts=failures)
        # Island error copy must be a "Couldn't ..." line <= 32 chars (island_copy).
        island_publish("error", title="Couldn't reconnect voice")
        print(
            f"Realtime: gave up after {failures} failed reconnect attempts — "
            "voice is offline; turn Samantha off and on to retry",
            flush=True,
        )

    def _prepare_reconnect(self) -> None:
        """Reset per-connection bookkeeping; fail outstanding text waiters."""
        if self._audio is not None:
            self._audio.clear_playback()
        with self._response_condition:
            self._active_response_ids.clear()
            self._response_started.clear()
            self._speaking_response_ids.clear()
            self._cancelled_response_ids.clear()
            self._current_response_id = ""
            self._response_request_ids.clear()
            self._response_transcripts.clear()
            request_ids = list(self._text_waiters)
            self._response_condition.notify_all()
        self._pending_calls.clear()
        self._recreated_calls.clear()
        self._session_ready.clear()
        # New conversation: lazy tools start unloaded and no approval or retry
        # budget survives the reconnect (fail closed).
        self._tool_groups.clear()
        reset_session_state()
        for request_id in request_ids:
            self._finish_text_waiter(request_id, error="reconnected")

    def _request_reconnect(self, *, reason: str) -> None:
        """Trigger the reconnect loop by closing the current socket."""
        log_event("reconnect_requested", reason=reason)
        ws = self.ws
        if ws is None:
            return
        try:
            ws.close()
        except Exception as exc:  # noqa: BLE001 — close is best effort
            log_event("reconnect_close_failed", error=str(exc)[:120])

    def _schedule_planned_reconnect(self, *, delay: float | None = None) -> None:
        """Timer for the 55-minute planned reconnect while idle."""
        with self._planned_reconnect_lock:
            if self._planned_reconnect_timer is not None:
                self._planned_reconnect_timer.cancel()
            if self._stop.is_set():
                self._planned_reconnect_timer = None
                return
            timer = threading.Timer(
                self.PLANNED_RECONNECT_SECONDS if delay is None else delay,
                self._planned_reconnect,
            )
            timer.daemon = True
            self._planned_reconnect_timer = timer
        timer.start()

    def _cancel_planned_reconnect(self) -> None:
        with self._planned_reconnect_lock:
            timer = self._planned_reconnect_timer
            self._planned_reconnect_timer = None
        if timer is not None:
            timer.cancel()

    def _planned_reconnect(self) -> None:
        """Survive the server's 60-min cap: refresh the socket while idle."""
        if self._stop.is_set():
            return
        with self._response_condition:
            idle = (
                not self._active_response_ids
            )
        if self._pending_calls or self._inflight_tools:
            idle = False
        if self._audio is not None and self._audio.has_playback():
            idle = False
        if not idle:
            self._schedule_planned_reconnect(delay=self.PLANNED_RECONNECT_RETRY_SECONDS)
            return
        self._request_reconnect(reason="planned_reconnect_55m")

    def _send(self, event: dict[str, Any]) -> bool:
        """Send one event; drop it (never raise) while the socket is closed."""
        ws = self.ws
        if ws is None or self._stop.is_set():
            return False  # socket closed (shutdown or mid-reconnect) — drop safely
        raw = json.dumps(event)
        try:
            with self._send_lock:
                ws.send(raw)
            if event.get("type") == "session.update":
                log_event("session_update", bytes=len(raw.encode("utf-8")))
            return True
        except Exception as exc:  # noqa: BLE001 — closed/reconnecting socket
            self._send_drops += 1
            if self._send_drops == 1 or self._send_drops % 500 == 0:
                log_event(
                    "send_dropped",
                    event_type=str(event.get("type") or ""),
                    drops=self._send_drops,
                    error=type(exc).__name__,
                )
            return False

    def _start_audio(self) -> None:
        if not self._audio_enabled:
            return
        if self._audio is not None:
            # Reconnect path: audio already exists — revive a dead mic sender.
            if self._audio.ensure_mic():
                log_event("mic_restarted", reason="sender_dead")
                mark("audio_ready", status="ok", detail="mic sender restarted")
                print("Audio I/O active — speak now (mic restarted).", flush=True)
            return
        from voice_cua.audio_io import RealtimeAudio, audio_available, wait_mic_preflight

        if not audio_available():
            self._audio_enabled = False
            mark("audio_ready", status="fail", error="sounddevice unavailable")
            island_publish("error", title="Mic unavailable", detail="Install sounddevice and numpy")
            return

        pre = wait_mic_preflight(timeout=30.0)
        if pre != 0:
            self._audio_enabled = False
            mark("mic_preflight", status="fail", error="mic open failed")
            island_publish("error", title="Mic unavailable", detail="Allow microphone")
            print("voice-cua stack: mic preflight failed", file=sys.stderr, flush=True)
            return
        mark("mic_preflight", status="ok")
        self._audio = RealtimeAudio(
            self._send,
            meter=lambda side, level, bars: island_voice_meter(side, level, bars),
        )
        try:
            self._audio.start()
            mark("audio_ready", status="ok")
            island_publish("listening", title="Listening", detail="")
            print("Audio I/O active — speak now.", flush=True)
        except Exception as exc:  # pragma: no cover
            self._audio = None
            self._audio_enabled = False
            mark("audio_ready", status="fail", error=str(exc)[:120])
            island_publish("error", title="Mic unavailable", detail=str(exc)[:80])
            print(f"Audio unavailable ({exc}); text-only.", file=sys.stderr, flush=True)

    def _on_open(self, _ws: Any) -> None:
        mark("realtime_ws_open")
        self._conn_epoch += 1
        reconnected = self._conn_epoch > 1
        self._comet_active = comet_is_active()
        # Sent on every open (also after reconnect), preserving the active
        # Comet tool set so the refreshed session keeps its capabilities.
        self._send(build_session_update(
            text_only=self._text_only,
            comet_active=self._comet_active,
            groups=frozenset(self._tool_groups),
        ))
        if reconnected:
            note = self.reconnect_note(comet_active=self._comet_active)
            self._send_system_note(note)
            log_event("reconnect_note", comet_active=self._comet_active)
        if self._text_only:
            print("Text session open.", flush=True)
        else:
            print("Realtime session open — speak or type a task + Enter.", flush=True)

    @staticmethod
    def reconnect_note(*, comet_active: bool) -> str:
        note = (
            "The realtime connection was re-established; earlier turns are not in "
            "this conversation. Continue the user's current task if one is pending."
        )
        if comet_active:
            note += (
                " A Comet window is still open from before the reconnect: follow-ups use "
                "comet_act or comet_begin (same window). Leave the user's window open; "
                "call comet_end {user_requested: true} only if the user asks to close it."
            )
        return note

    def _send_system_note(self, text: str) -> bool:
        return self._send({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "system",
                "content": [{"type": "input_text", "text": text}],
            },
        })

    def _send_tools_update(self) -> None:
        self._send({
            "type": "session.update",
            "session": {
                "type": "realtime",
                "tools": tool_definitions(comet_active=self._comet_active, groups=frozenset(self._tool_groups)),
                "tool_choice": "auto",
            },
        })

    def _sync_tool_groups(self, name: str, result: dict[str, Any]) -> None:
        """Lazy tools: more_tools loads a group; a refused risky action loads confirm_risky.

        Sent before the follow-up response.create so the next turn can call them.
        """
        add: set[str] = set()
        if name == "more_tools" and result.get("ok") and result.get("group"):
            add.add(str(result["group"]))
        if result.get("needs_confirmation"):
            add.add("confirm")
        new = add - self._tool_groups
        if not new:
            return
        self._tool_groups |= new
        self._send_tools_update()
        log_event("tools_loaded", groups=sorted(self._tool_groups))

    def _on_comet_change(self, active: bool) -> None:
        """Lease opened/closed (incl. idle auto-release): resync the tool list."""
        if active == self._comet_active:
            return
        self._comet_active = active
        self._send_tools_update()
        log_event("comet_tools_synced", comet_active=active)
        if not active and not self._inflight_tools:
            # Background close (self-test idle release): the tool result can't tell
            # her, so a note keeps her narration honest. Tool-driven closes report
            # window:"closed" in their own result.
            self._send_system_note("The Comet window is now closed. Do not say it is open.")

    def _on_message(self, _ws: Any, message: str) -> None:
        try:
            event = json.loads(message)
        except json.JSONDecodeError:
            return
        et = event.get("type")
        if et == "session.updated":
            mark("realtime_session_updated")
            # Successful (re)connect — reset the planned reconnect. The backoff
            # counter resets in run() only once this connection proved stable.
            self._connected_at = time.monotonic()
            self._schedule_planned_reconnect()
            self._session_ready.set()
            from voice_cua.session_hub import register

            register(self)
            if self._text_only:
                mark("realtime_ready", status="ok")
                island_publish("listening", title="Listening", detail="")
            else:
                self._start_audio()
        elif et == "response.function_call_arguments.done":
            self._handle_function_done(event)
        elif et == "response.output_item.done":
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                self._handle_function_item(item, response_id=str(event.get("response_id") or ""))
        elif et == "response.output_audio.delta":
            self._handle_output_audio_delta(event)
        elif et == "response.content_part.added":
            self._handle_content_part_added(event)
        elif et == "input_audio_buffer.speech_started":
            self._handle_speech_started()
            island_publish("listening", title="Listening", detail="")
        elif et == "conversation.item.input_audio_transcription.completed":
            heard = str(event.get("transcript") or "").strip()
            if heard:
                # Words go to voice.log only; the activity log keeps metadata.
                log_event("user_transcript", chars=len(heard), item_id=str(event.get("item_id") or ""))
                self._last_user_text = heard
                print(f"You: {heard}", flush=True)
                # Card title = the final utterance (compact view never shows text).
                if not (self._audio and self._audio.has_playback()):
                    island_publish("thinking", title=heard, detail="Thinking")
        elif et == "conversation.item.input_audio_transcription.failed":
            err = event.get("error") or {}
            log_event(
                "user_transcript_failed",
                error=str(err.get("message") if isinstance(err, dict) else err)[:120],
            )
        elif et == "input_audio_buffer.speech_stopped":
            if not (self._audio and self._audio.has_playback()):
                island_publish("listening", title="Listening", detail="")
        elif et == "conversation.item.truncated":
            log_event(
                "conversation_truncated",
                item_id=str(event.get("item_id") or ""),
                audio_end_ms=event.get("audio_end_ms"),
            )
        elif et == "response.created":
            response = event.get("response") or {}
            response_id = str(response.get("id") or "")
            request_id = self._request_id(response)
            with self._response_condition:
                if response_id:
                    if len(self._response_started) > 256:  # never grow without bound
                        self._response_started.clear()
                    self._response_started[response_id] = time.monotonic()
                    self._active_response_ids.add(response_id)
                    self._current_response_id = response_id
                    if request_id:
                        self._response_request_ids[response_id] = request_id
                    self._cancelled_response_ids.discard(response_id)
                self._response_condition.notify_all()
            island_publish("thinking", title="Thinking", detail="")
        elif et == "response.output_audio_transcript.done":
            chunk = str(event.get("transcript") or "").strip()
            response_id = str(event.get("response_id") or "")
            if chunk and response_id:
                with self._response_lock:
                    self._response_transcripts[response_id] = chunk
                if self._audio and self._audio.has_playback():
                    first, _, rest = chunk.partition(". ")
                    island_publish("speaking", title=first.rstrip("."), detail=rest)
        elif et == "rate_limits.updated":
            self._note_rate_limits(event.get("rate_limits"))
        elif et in {"response.done", "response.output_audio.done"}:
            if et == "response.done":
                response = event.get("response") or {}
                with self._response_condition:
                    started = self._response_started.pop(str(response.get("id") or ""), None)
                latency_ms = int((time.monotonic() - started) * 1000) if started else None
                try:
                    log_usage(response, latency_ms=latency_ms)  # counts + ids only
                except Exception:  # noqa: BLE001 — logging must never break a turn
                    pass
                self._note_response_done(response)
                self._finish_response(str(response.get("id") or ""))
            self._publish_listening_when_idle()
        elif et == "error":
            self._handle_error_event(event)

    def _handle_output_audio_delta(self, event: dict[str, Any]) -> None:
        response_id = str(event.get("response_id") or "")
        item_id = str(event.get("item_id") or "")
        with self._response_condition:
            first_delta = (
                bool(response_id) and response_id not in self._speaking_response_ids
            )
            if first_delta:
                self._speaking_response_ids.add(response_id)
            cancelled = response_id in self._cancelled_response_ids
        if first_delta:
            island_publish("speaking", title="Speaking", detail="")
        if cancelled:
            return
        delta = event.get("delta") or ""
        if self._audio and delta:
            self._audio.on_output_delta(delta, item_id=item_id)

    def _handle_content_part_added(self, event: dict[str, Any]) -> None:
        part = event.get("part") or {}
        if not isinstance(part, dict) or part.get("type") != "output_audio":
            return
        response_id = str(event.get("response_id") or "")
        if not response_id:
            return
        with self._response_condition:
            first = response_id not in self._speaking_response_ids
            if first:
                self._speaking_response_ids.add(response_id)
            self._response_condition.notify_all()
        if first:
            island_publish("speaking", title="Speaking", detail="")

    def _handle_speech_started(self) -> None:
        """Barge-in: drop buffered audio; truncation + cancel per session config."""
        audio = self._audio
        if audio is None:
            return
        if not audio.has_playback():
            return
        audio_end_ms, item_id = audio.interrupt()
        with self._response_lock:
            active = self._current_response_id
            response_active = active in self._active_response_ids
        cancelled = False
        if response_active and not self._interrupt_response_enabled():
            # Server keeps speaking (interrupt_response=false) → cancel explicitly.
            cancelled = True
            with self._response_condition:
                self._cancelled_response_ids.add(active)
                self._response_condition.notify_all()
            self._send({"type": "response.cancel"})
        if item_id:
            self._send({
                "type": "conversation.item.truncate",
                "item_id": item_id,
                "content_index": 0,
                "audio_end_ms": audio_end_ms,
            })
            log_event(
                "barge_in_truncate",
                item_id=item_id,
                audio_end_ms=audio_end_ms,
                cancelled=cancelled,
                response_id=active,
            )

    @staticmethod
    def _interrupt_response_enabled() -> bool:
        """Server-side VAD interrupt policy for the current session config."""
        try:
            eagleish = os.environ.get("VOICE_CUA_EAGERNESS") or load_settings()["eagerness"]
            config = turn_detection_config(eagleish)
        except Exception:  # noqa: BLE001 — settings unreadable → default on
            return True
        return bool(config.get("interrupt_response", True))

    def _note_rate_limits(self, rate_limits: Any) -> None:
        """Record remaining tokens when the token budget drops below 10%."""
        for entry in rate_limits or []:
            if not isinstance(entry, dict):
                continue
            if entry.get("name") != "tokens":
                continue
            try:
                remaining = round(float(entry.get("remaining")))
                limit = round(float(entry.get("limit")))
            except (TypeError, ValueError):
                return
            if limit <= 0:
                return
            low = remaining / limit < 0.10
            if low and not self._rate_limit_warned:
                self._rate_limit_warned = True
                log_event("rate_limit_low", name="tokens", remaining=remaining, limit=limit)
            elif not low:
                self._rate_limit_warned = False
            return

    @staticmethod
    def _classify_error(err: dict[str, Any]) -> str:
        """recoverable | session | rate_limit | config | client — see _handle_error_event."""
        message = str(err.get("message") or "").lower()
        code = str(err.get("code") or "").lower()
        markers = code + " " + message
        recoverable_markers = (
            "cancel_not_active",               # response.cancel with no active response
            "truncate_past_end",               # truncate beyond actually played audio
            "already shorter than",            # truncate audio_end_ms past the item's audio
            "conversation_item_not_found",     # conversation.item.* unknown id
            "item_not_found",
            "buffer_clear_empty",              # clear on empty input buffer
        )
        if any(marker in markers for marker in recoverable_markers):
            return "recoverable"
        session_markers = (
            "session expired",
            "session not found",
            "session_expired",
            "session not active",
        )
        if any(marker in markers for marker in session_markers):
            return "session"
        if _is_rate_limit(err):
            return "rate_limit"
        if str(err.get("type") or "") == "invalid_request_error":
            return "config"
        return "client"

    def _handle_error_event(self, event: dict[str, Any]) -> None:
        err = event.get("error") or {}
        code = str(err.get("code") or "")
        param = str(err.get("param") or "")
        event_id = str(err.get("event_id") or "")
        msg = str(err.get("message") or err)[:120]
        if event_id.startswith("voice_text_"):
            request_id = event_id.removeprefix("voice_text_").split("_", 1)[0]
            self._finish_text_waiter(request_id, error=msg)
        classification = self._classify_error(err)
        log_event(
            "realtime_error",
            code=code or None,
            param=param or None,
            event_id=event_id or None,
            classification=classification,
            error=msg,
            error_type=str(err.get("type") or "realtime"),
        )
        print("error:", err, file=sys.stderr)
        if code == "invalid_tool_call_id" or "tool call id" in msg.lower():
            pending = list(self._recreated_calls.items())
            for call_id, note in pending:
                # Match the id in the message; if the server omitted it and only
                # one recreated call is pending, that one is the rejected call.
                if call_id in msg or len(pending) == 1:
                    self._recreated_calls.pop(call_id, None)
                    log_event("tool_result_note_fallback", call_id=call_id)
                    self._send_system_note(note)
                    self._send_response_create()
                    return
        if classification == "recoverable":
            # Expected bumps from barge-in/clear races — never alarm the user.
            return
        retry_after = retry_after_seconds(err)
        if classification == "rate_limit":
            # Longer floor; a server retry-after wins when it is longer.
            self._note_retry_hint(
                min_delay=max(self.RATE_LIMIT_MIN_DELAY, retry_after or 0.0),
                reason=f"rate_limit:{code or 'rate_limit'}",
            )
        elif retry_after is not None:
            self._note_retry_hint(min_delay=retry_after, reason=f"retry_after:{code or 'server'}")
        if classification == "session":
            mark("realtime_error", status="fail", error=msg)
            self._request_reconnect(reason=f"error:{code or msg[:60]}")
            return
        if classification == "config":
            mark("realtime_error", status="fail", error=msg)
            # Session config was rejected — tell the user once, not per event.
            if not self._config_error_published:
                self._config_error_published = True
                island_publish("error", title="Realtime config error", detail=msg)
            return
        mark("realtime_error", status="fail", error=msg)
        island_publish("error", title="Realtime error", detail=msg)

    def _handle_function_done(self, event: dict[str, Any]) -> None:
        call_id = event.get("call_id") or ""
        name = event.get("name") or ""
        raw_args = event.get("arguments") or "{}"
        self._run_tool(
            call_id,
            name,
            raw_args,
            request_id=self._request_for_response(str(event.get("response_id") or "")),
            response_id=str(event.get("response_id") or ""),
        )

    def _handle_function_item(self, item: dict[str, Any], *, response_id: str = "") -> None:
        call_id = item.get("call_id") or ""
        name = item.get("name") or ""
        raw_args = item.get("arguments") or "{}"
        if call_id in self._pending_calls:
            return
        self._run_tool(
            call_id,
            name,
            raw_args,
            request_id=self._request_for_response(response_id),
            response_id=response_id,
        )

    def _run_tool(
        self,
        call_id: str,
        name: str,
        raw_args: str,
        *,
        request_id: str = "",
        response_id: str = "",
    ) -> None:
        if not call_id or not name:
            return
        if call_id in self._pending_calls:
            return
        self._pending_calls[call_id] = {"name": name, "request_id": request_id}
        try:
            arguments = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
        except json.JSONDecodeError:
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}

        self._inflight_tools += 1
        epoch = self._conn_epoch

        def work() -> None:
            try:
                kind, title, detail, app = describe_tool(name, arguments)
                island_publish(kind, title=title, detail=detail, app=app, step=name)
                with tool_context(call_id=call_id, response_id=response_id):
                    try:
                        result = dispatch(name, arguments)
                    except Exception as exc:  # noqa: BLE001 — always answer the tool call
                        result = {"ok": False, "error": str(exc)[:200]}
                        _log_exception(name, exc)
                if not isinstance(result, dict):
                    result = {"ok": False, "error": "non-dict tool result"}
                for bad in ("value", "secret", "password", "api_key", "token"):
                    result.pop(bad, None)
                rkind, rtitle, rdetail = summarize_result(name, result, fallback=detail)
                island_publish(rkind, title=rtitle, detail=rdetail, app=app)
                stale = self._deliver_tool_result(
                    call_id, name, result, epoch=epoch, arguments=arguments
                )
                self._sync_tool_groups(name, result)
                if name.startswith("comet_"):
                    self._on_comet_change(comet_is_active())
                if stale:
                    request_id_local = ""
                else:
                    request_id_local = request_id
                if self._wait_for_responses_idle(timeout=10.0):
                    self._send_response_create(request_id=request_id_local)
                else:
                    log_event(
                        "realtime_error",
                        error="tool follow-up response timed out waiting for active response",
                        error_type="response_busy",
                    )
            finally:
                self._pending_calls.pop(call_id, None)
                self._inflight_tools = max(0, self._inflight_tools - 1)

        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def stale_result_note(name: str, result: dict[str, Any]) -> str:
        """Plain-message fallback for a tool result that outlived its connection.

        Summary only (ok/kind/title/detail + a few safe scalar fields) — never the
        raw tool payload (page text, paths, nested fields).
        """
        kind, title, detail = summarize_result(name, result)
        ok = bool(result.get("ok")) if isinstance(result, dict) else False
        extras: list[str] = []
        if isinstance(result, dict):
            for key in ("verified", "dispatched", "completion", "lease", "closed", "error_type"):
                value = result.get(key)
                if isinstance(value, (bool, int, str)) and str(value):
                    extras.append(f"{key}={str(value)[:40]}")
            if not ok and result.get("error"):
                extras.append(f"error={str(result.get('error'))[:120]}")
        summary = f"ok={ok}; {title}: {detail}".strip()
        if extras:
            summary += "; " + ", ".join(extras)
        return (
            f"Tool {name} (started before the connection was re-established) "
            f"finished with result: {summary[:400]}. Use this result to continue the user's task."
        )

    def _wait_session_ready(self, timeout: float) -> bool:
        """Wait for a (re)opened session, bailing out early on shutdown."""
        deadline = time.monotonic() + timeout
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if self._session_ready.wait(timeout=min(0.25, remaining)):
                return True
        return False

    def _deliver_tool_result(
        self,
        call_id: str,
        name: str,
        result: dict[str, Any],
        *,
        epoch: int,
        arguments: dict[str, Any] | None = None,
    ) -> bool:
        """Post the tool output; returns True when the stale-epoch fallback was used."""
        output = {
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(result),
            },
        }
        if epoch == self._conn_epoch and self._send(output):
            return False
        # Socket dropped while the tool ran. Wait for the (re)opened session.
        while not self._stop.is_set():
            if not self._wait_session_ready(timeout=20.0):
                log_event("tool_result_waiting", tool=name, reason="no session yet")
                continue  # keep waiting for the reconnect until shutdown
            if epoch != self._conn_epoch:
                break
            if self._send(output):
                return False
            # Same connection but send failed (socket closing) — wait for the reopen.
            time.sleep(0.25)
        if self._stop.is_set():
            return True
        # New server session: it has no such call_id. Re-create the function_call
        # item (same call_id/name/args), then its output; the caller follows with
        # response.create. If the server still rejects the call id, the error
        # handler falls back to a plain system note (see _handle_error_event).
        log_event("tool_result_stale_epoch", tool=name, started_epoch=epoch, epoch=self._conn_epoch)
        self._recreated_calls[call_id] = self.stale_result_note(name, result)
        self._send({
            "type": "conversation.item.create",
            "item": {
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": json.dumps({
                    k: v for k, v in (arguments or {}).items()
                    if k not in {"value", "secret", "password"}
                }),
            },
        })
        self._send(output)
        return True

    def _on_error(self, _ws: Any, error: Any) -> None:
        # Transport-level (e.g. ping timeout): reconnectable, never fatal here.
        # Island feedback stays on "reconnecting" (published at close/retry).
        msg = str(error)[:120]
        mark("realtime_ws_error", status="fail", error=msg)
        log_event(
            "realtime_error",
            error=msg,
            error_type="websocket",
            classification="transport",
        )
        print("ws error:", error, file=sys.stderr)
        # Handshake refusals (e.g. HTTP 429) carry status + retry-after headers.
        status = getattr(error, "status_code", None)
        headers = getattr(error, "resp_headers", None)
        retry_after = retry_after_seconds(None, headers)
        if status == 429 or _is_rate_limit({"message": msg}):
            self._note_retry_hint(
                min_delay=max(self.RATE_LIMIT_MIN_DELAY, retry_after or 0.0),
                reason=f"rate_limit:http_{status or 'ws'}",
            )
        elif retry_after is not None:
            self._note_retry_hint(min_delay=retry_after, reason=f"retry_after:http_{status or 'ws'}")

    def _on_close(self, _ws: Any, *_args: Any) -> None:
        mark("realtime_ws_close")
        if self._stop.is_set():
            return
        # Phase 1: close no longer kills the session — run() reconnects.
        island_publish("reconnecting", title="Reconnecting", detail="")
        print("Realtime closed", flush=True)

    def shutdown(self) -> None:
        from voice_cua.session_hub import unregister

        unregister(self)
        comet_remove_listener(self._on_comet_change)
        self._cancel_planned_reconnect()
        hide_agent_cursor()
        comet_end()
        self._stop.set()
        with self._response_lock:
            request_ids = list(self._text_waiters)
        for request_id in request_ids:
            self._finish_text_waiter(request_id, error="session closed")
        ws = self.ws
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001 — close is best effort
                pass
        self.ws = None
        if self._audio:
            self._audio.stop()
            self._audio = None

    def send_text_and_wait(self, text: str, *, timeout: float = 90.0) -> dict[str, Any]:
        started = time.monotonic()
        if not self._text_request_lock.acquire(timeout=timeout):
            return {"ok": False, "error": "timeout waiting for another text request"}
        request_id = uuid.uuid4().hex
        waiter = {"event": threading.Event(), "reply": "", "error": ""}
        try:
            if self._audio:
                self._audio.pause_input()
                self._send({"type": "input_audio_buffer.clear"})
            remaining = max(0.0, timeout - (time.monotonic() - started))
            if not self._wait_for_responses_idle(timeout=min(5.0, remaining)):
                return {"ok": False, "error": "voice response still active; retry when Samantha is listening"}
            with self._response_lock:
                self._text_waiters[request_id] = waiter
            self.send_text(text, request_id=request_id)
            remaining = max(0.0, timeout - (time.monotonic() - started))
            if not waiter["event"].wait(timeout=remaining):
                return {"ok": False, "error": "timeout", "reply": str(waiter["reply"])[:240]}
            if waiter["error"]:
                return {"ok": False, "error": str(waiter["error"])[:240], "reply": str(waiter["reply"])[:240]}
            return {"ok": True, "reply": str(waiter["reply"])[:240]}
        finally:
            with self._response_lock:
                self._text_waiters.pop(request_id, None)
            if self._audio:
                self._audio.resume_input()
            self._text_request_lock.release()

    @staticmethod
    def _request_id(response: dict[str, Any]) -> str:
        metadata = response.get("metadata") or {}
        return str(metadata.get("voice_cua_request_id") or "") if isinstance(metadata, dict) else ""

    def _request_for_response(self, response_id: str) -> str:
        with self._response_lock:
            return self._response_request_ids.get(response_id, "")

    def _wait_for_responses_idle(self, *, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._response_condition:
            while self._active_response_ids and not self._stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._response_condition.wait(timeout=remaining)
            return not self._active_response_ids

    def _finish_response(self, response_id: str) -> None:
        if not response_id:
            return
        with self._response_condition:
            self._active_response_ids.discard(response_id)
            self._speaking_response_ids.discard(response_id)
            self._cancelled_response_ids.discard(response_id)
            if self._current_response_id == response_id:
                self._current_response_id = ""
            self._response_request_ids.pop(response_id, None)
            self._response_transcripts.pop(response_id, None)
            self._response_condition.notify_all()

    def _finish_text_waiter(self, request_id: str, *, reply: str = "", error: str = "") -> None:
        with self._response_lock:
            waiter = self._text_waiters.get(request_id)
            if not waiter:
                return
            waiter["reply"] = reply
            waiter["error"] = error
            waiter["event"].set()

    def _note_response_done(self, response: dict[str, Any]) -> None:
        status = str(response.get("status") or "")
        if status and status not in {"completed", "incomplete", "cancelled", "failed"}:
            return
        if any(
            isinstance(item, dict) and item.get("type") == "function_call"
            for item in response.get("output") or []
        ):
            return
        text_bits: list[str] = []
        for item in response.get("output") or []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            # GA assistant parts: output_text (text) and output_audio (transcript).
            # Scan each item's content exactly once — never double-add output_text.
            for part in item.get("content") or []:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype in {"output_text", "text"}:
                    chunk = str(part.get("text") or "").strip()
                    if chunk:
                        text_bits.append(chunk)
                elif ptype == "output_audio":
                    chunk = str(part.get("transcript") or "").strip()
                    if chunk:
                        text_bits.append(chunk)
        response_id = str(response.get("id") or "")
        with self._response_lock:
            transcript = self._response_transcripts.get(response_id, "")
        reply = _normalize_reply(" ".join(text_bits).strip() or transcript.strip())
        if reply:
            log_event("assistant_reply", text=reply[:240])
            print(f"Samantha: {reply}", flush=True)
        mark("response_done", status="ok" if reply else "empty")
        request_id = self._request_id(response) or self._request_for_response(response_id)
        if request_id:
            error = "" if status == "completed" else f"response {status or 'failed'}"
            self._finish_text_waiter(request_id, reply=reply, error=error)

    def _publish_listening_when_idle(self) -> None:
        """After response completes: hide agent cursor, then return pill to Listening."""
        if self._stop.is_set():
            return
        if self._audio and self._audio.has_playback():
            threading.Timer(0.12, self._publish_listening_when_idle).start()
            return
        if self._pending_calls or self._inflight_tools:
            return
        hide_agent_cursor()
        island_publish("listening", title="Listening", detail="")

    def _send_response_create(self, *, request_id: str = "") -> None:
        event: dict[str, Any] = {"type": "response.create"}
        if request_id:
            event["event_id"] = f"voice_text_{request_id}_{uuid.uuid4().hex[:8]}"
            event["response"] = {"metadata": {"voice_cua_request_id": request_id}}
        self._send(event)

    def island_control(self, action: str) -> bool:
        """Island card buttons (Stop / Cancel / Replay / Retry). True when the session acted."""
        if action in ("stopSpeaking", "cancel", "stopTask"):
            self._handle_speech_started()          # drop buffered audio + truncate what was heard
            with self._response_lock:
                active = self._current_response_id
                response_active = active in self._active_response_ids
            if response_active:
                with self._response_condition:
                    self._cancelled_response_ids.add(active)
                    self._response_condition.notify_all()
                self._send({"type": "response.cancel"})
            log_event("island_control", action=action, cancelled=bool(response_active))
            island_publish("listening", title="Listening", detail="")
            return True
        if action == "retry":
            last = getattr(self, "_last_user_text", "")
            if not last:
                return False
            log_event("island_control", action=action, chars=len(last))
            self.send_text(last)
            return True
        if action == "replay":
            log_event("island_control", action=action)
            self.send_text("Please repeat your last reply.")
            return True
        return False

    def send_text(self, text: str, *, request_id: str = "") -> None:
        self._send({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        })
        self._send_response_create(request_id=request_id)
        island_publish("thinking", title="Thinking", detail=text[:80])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Voice CUA Realtime session")
    parser.add_argument("--text", help="One-shot text task then exit after tools settle")
    parser.add_argument(
        "--no-audio",
        action="store_true",
        help="Disable mic/speaker (text-only)",
    )
    args = parser.parse_args(argv)
    no_audio = args.no_audio or os.environ.get("VOICE_CUA_NO_AUDIO", "").strip() in {"1", "true", "yes"}
    session = RealtimeSession(enable_audio=not no_audio and not args.text)

    if args.text:
        threading.Thread(target=session.run, daemon=True).start()
        if not session._session_ready.wait(timeout=30.0):
            print("Timed out waiting for Realtime session", file=sys.stderr, flush=True)
            session.shutdown()
            return 1
        result = session.send_text_and_wait(args.text, timeout=90.0)
        if not result.get("ok"):
            print(str(result.get("error") or "Samantha reply failed"), file=sys.stderr, flush=True)
        session.shutdown()
        return 0 if result.get("ok") else 1

    def _sigterm(_signum: int, _frame: Any) -> None:
        # Supervisor stop → run the normal shutdown path (comet_end, cursor hide).
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _sigterm)
    except ValueError:  # not main thread (embedded/tests)
        pass

    ws_thread = threading.Thread(target=session.run, daemon=True)
    ws_thread.start()

    def stdin_reader() -> None:
        pending: str | None = None
        try:
            while ws_thread.is_alive() and not session._stop.is_set():
                if pending is None:
                    line = sys.stdin.readline()
                    if not line:
                        break
                    line = line.strip()
                    if line:
                        pending = line
                    continue
                if session.ws and session._session_ready.wait(timeout=0.05):
                    session.send_text(pending)
                    pending = None
                else:
                    time.sleep(0.05)
        except (KeyboardInterrupt, OSError):
            pass

    threading.Thread(target=stdin_reader, name="stdin-reader", daemon=True).start()

    if session._audio_enabled:
        print("Starting voice session — speak, or type a task + Enter (Ctrl-C to quit).", flush=True)
    else:
        print("Text session — type a task + Enter (Ctrl-C to quit).", flush=True)

    try:
        while ws_thread.is_alive() and not session._stop.is_set():
            time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    session.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
