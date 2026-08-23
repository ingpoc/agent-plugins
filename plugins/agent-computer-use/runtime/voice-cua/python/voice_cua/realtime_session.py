"""Realtime WebSocket session: voice + local tool dispatch.

Connects to OpenAI Realtime, registers function tools, executes them locally
via voice_cua.tools.dispatch, never echoes secrets to the model.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import threading
import time
import uuid
from typing import TYPE_CHECKING, Any

from voice_cua import SYSTEM_INSTRUCTIONS
from voice_cua.auth import resolve_openai_api_key
from voice_cua.activity_log import log_event
from voice_cua.cua_bridge import hide_agent_cursor
from voice_cua.island_facade import island_publish
from voice_cua.startup_trace import mark
from voice_cua.tools import dispatch, tool_definitions
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


def _ws_url() -> str:
    explicit = os.environ.get("VOICE_CUA_REALTIME_URL", "").strip()
    if explicit:
        return explicit
    model, _ = _model_and_voice()
    return f"wss://api.openai.com/v1/realtime?model={model}"


def build_session_update(*, text_only: bool = False) -> dict[str, Any]:
    """GA Realtime session.update (beta header/shape retired 2026)."""
    _, voice = _model_and_voice()
    eagerness = os.environ.get("VOICE_CUA_EAGERNESS") or load_settings()["eagerness"]
    session: dict[str, Any] = {
        "type": "realtime",
        "output_modalities": ["text"] if text_only else ["audio"],
        "instructions": SYSTEM_INSTRUCTIONS,
        "tools": tool_definitions(),
        "tool_choice": "auto",
    }
    if not text_only:
        session["audio"] = {
            "input": {
                "format": {"type": "audio/pcm", "rate": REALTIME_SAMPLE_RATE},
                "transcription": {"model": "gpt-4o-mini-transcribe"},
                "turn_detection": turn_detection_config(eagerness),
                "noise_reduction": noise_reduction_config(),
            },
            "output": {
                "format": {"type": "audio/pcm", "rate": REALTIME_SAMPLE_RATE},
                "voice": voice,
            },
        }
    return {"type": "session.update", "session": session}


class RealtimeSession:
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
        apply_env()

    def connect(self) -> None:
        headers = [f"Authorization: Bearer {self.api_key}"]
        self.ws = websocket.WebSocketApp(
            _ws_url(),
            header=headers,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self.ws.run_forever(ping_interval=20, ping_timeout=10)

    def _send(self, event: dict[str, Any]) -> None:
        assert self.ws is not None
        raw = json.dumps(event)
        with self._send_lock:
            self.ws.send(raw)

    def _start_audio(self) -> None:
        if not self._audio_enabled or self._audio is not None:
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
        self._audio = RealtimeAudio(self._send)
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
        self._send(build_session_update(text_only=self._text_only))
        if self._text_only:
            print("Text session open.", flush=True)
        else:
            print("Realtime session open — speak or type a task + Enter.", flush=True)

    def _on_message(self, _ws: Any, message: str) -> None:
        try:
            event = json.loads(message)
        except json.JSONDecodeError:
            return
        et = event.get("type")
        if et == "session.updated":
            mark("realtime_session_updated")
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
        elif et in {"response.output_audio.delta", "response.audio.delta"}:
            delta = event.get("delta") or ""
            if self._audio:
                self._audio.on_output_delta(delta)
        elif et == "input_audio_buffer.speech_started":
            if self._audio:
                self._audio.interrupt()
            island_publish("listening", title="Listening", detail="")
        elif et == "input_audio_buffer.speech_stopped":
            if not (self._audio and self._audio.has_playback()):
                island_publish("listening", title="Listening", detail="")
        elif et == "response.created":
            response = event.get("response") or {}
            response_id = str(response.get("id") or "")
            request_id = self._request_id(response)
            with self._response_condition:
                if response_id:
                    self._active_response_ids.add(response_id)
                    if request_id:
                        self._response_request_ids[response_id] = request_id
                self._response_condition.notify_all()
            island_publish("thinking", title="Thinking", detail="")
        elif et in {"response.output_audio.started", "response.audio.started"}:
            island_publish("speaking", title="Samantha", detail="")
        elif et in {
            "response.output_audio_transcript.done",
            "response.audio_transcript.done",
        }:
            chunk = str(event.get("transcript") or "").strip()
            response_id = str(event.get("response_id") or "")
            if chunk and response_id:
                with self._response_lock:
                    self._response_transcripts[response_id] = chunk
        elif et in {"response.done", "response.output_audio.done"}:
            if et == "response.done":
                response = event.get("response") or {}
                self._note_response_done(response)
                self._finish_response(str(response.get("id") or ""))
            self._publish_listening_when_idle()
        elif et == "error":
            err = event.get("error") or {}
            msg = str(err.get("message") or err)[:120]
            client_event_id = str(err.get("event_id") or "")
            if client_event_id.startswith("voice_text_"):
                request_id = client_event_id.removeprefix("voice_text_").split("_", 1)[0]
                self._finish_text_waiter(request_id, error=msg)
            mark("realtime_error", status="fail", error=msg)
            island_publish("error", title="Realtime error", detail=msg)
            log_event("realtime_error", error=msg, error_type=str(err.get("type") or "realtime"))
            print("error:", err, file=sys.stderr)

    def _handle_function_done(self, event: dict[str, Any]) -> None:
        call_id = event.get("call_id") or ""
        name = event.get("name") or ""
        raw_args = event.get("arguments") or "{}"
        self._run_tool(
            call_id,
            name,
            raw_args,
            request_id=self._request_for_response(str(event.get("response_id") or "")),
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
        )

    def _run_tool(
        self,
        call_id: str,
        name: str,
        raw_args: str,
        *,
        request_id: str = "",
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

        def work() -> None:
            try:
                island_publish("acting", title="Acting", detail=name)
                result = dispatch(name, arguments)
                if isinstance(result, dict):
                    for bad in ("value", "secret", "password", "api_key", "token"):
                        result.pop(bad, None)
                self._send({
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps(result),
                    },
                })
                if self._wait_for_responses_idle(timeout=10.0):
                    self._send_response_create(request_id=request_id)
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

    def _on_error(self, _ws: Any, error: Any) -> None:
        msg = str(error)[:120]
        mark("realtime_ws_error", status="fail", error=msg)
        island_publish("error", title="WS error", detail=msg)
        log_event("realtime_error", error=msg, error_type="websocket")
        print("ws error:", error, file=sys.stderr)

    def _on_close(self, _ws: Any, *_args: Any) -> None:
        mark("realtime_ws_close")
        self.shutdown()
        island_publish("idle", title="Idle", detail="Disconnected")
        print("Realtime closed", flush=True)

    def shutdown(self) -> None:
        from voice_cua.session_hub import unregister

        unregister(self)
        hide_agent_cursor()
        self._stop.set()
        with self._response_lock:
            request_ids = list(self._text_waiters)
        for request_id in request_ids:
            self._finish_text_waiter(request_id, error="session closed")
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
            if not isinstance(item, dict):
                continue
            if item.get("type") == "message":
                for part in item.get("content") or []:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") in {"output_text", "text"}:
                        chunk = str(part.get("text") or "").strip()
                        if chunk:
                            text_bits.append(chunk)
            for part in item.get("content") or []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") in {"output_text", "text"}:
                    chunk = str(part.get("text") or "").strip()
                    if chunk:
                        text_bits.append(chunk)
                elif part.get("type") == "audio" and part.get("transcript"):
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
        threading.Thread(target=session.connect, daemon=True).start()
        if not session._session_ready.wait(timeout=30.0):
            print("Timed out waiting for Realtime session", file=sys.stderr, flush=True)
            session.shutdown()
            return 1
        result = session.send_text_and_wait(args.text, timeout=90.0)
        if not result.get("ok"):
            print(str(result.get("error") or "Samantha reply failed"), file=sys.stderr, flush=True)
        session.shutdown()
        return 0 if result.get("ok") else 1

    ws_thread = threading.Thread(target=session.connect, daemon=True)
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
