"""GPT-Live 403 at the handshake: no Origin header, no retry storm, a clear error.

Regression (Guru, 2026-10-21): selecting GPT-Live-1 meant Samantha never listened —
every handshake to wss://api.openai.com/v1/live/sessions got a bare 403 "Forbidden",
the helper retried 8x, exited 0, and CUAService restarted it into the same 403.
"""

from __future__ import annotations

import os
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("VOICE_CUA_ACTIVITY_LOG", str(Path(tempfile.mkdtemp(prefix="voice-cua-test-")) / "activity.jsonl"))
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "tests"))

from voice_cua import realtime_session  # noqa: E402
from test_phase1_reliability import make_session  # noqa: E402


class BadStatus(Exception):
    def __init__(self, status: int, body: bytes = b"Forbidden\n") -> None:
        super().__init__(f"Handshake status {status}")
        self.status_code = status
        self.resp_headers = {}
        self.resp_body = body


class HandshakeAccess(unittest.TestCase):
    def test_socket_is_opened_without_origin(self) -> None:
        session = make_session()
        app = MagicMock()
        with patch.object(realtime_session.websocket, "WebSocketApp", return_value=app):
            session.connect()
        self.assertTrue(app.run_forever.call_args.kwargs.get("suppress_origin"))

    def test_403_stops_the_loop_once_with_a_clear_error(self) -> None:
        session = make_session()
        calls = []

        def refused() -> None:
            calls.append(1)
            session._on_error(None, BadStatus(403))

        with patch.object(session, "connect", side_effect=refused), \
                patch.object(session, "_schedule_planned_reconnect"), \
                patch.object(session._stop, "wait", return_value=False), \
                patch.object(realtime_session, "island_publish") as pub:
            session.run()
        self.assertEqual(len(calls), 1)  # no 8x retry storm
        self.assertIn("403", session._access_refused)
        self.assertIn("Forbidden", session._access_refused)
        titles = [c.args[1] if len(c.args) > 1 else c.kwargs.get("title") for c in pub.call_args_list]
        self.assertIn("Couldn't connect: access denied", titles)

    def test_transport_errors_still_reconnect(self) -> None:
        session = make_session()
        session._on_error(None, ConnectionError("Connection to remote host was lost."))
        self.assertEqual(session._access_refused, "")
        session._on_error(None, BadStatus(503, b"busy"))
        self.assertEqual(session._access_refused, "")

    def test_unframed_drop_preserves_safe_trace_ids_and_event_type(self) -> None:
        session = make_session()
        ws = MagicMock()
        session.ws = ws
        ws.sock.handshake_response.headers = {"x-request-id": "request-123", "cf-ray": "edge-BOM",
                                              "authorization": "must-not-log"}
        with patch.object(session, "_send"), patch("voice_cua.realtime_session.log_event") as logged, \
                patch("voice_cua.realtime_session.island_publish"):
            session._on_open(ws)
            session._on_message(ws, json.dumps({"type": "session.output_audio.delta", "delta": "AAA="}))
            session._on_close(ws, None, None)
        row = next(c.kwargs for c in logged.call_args_list if c.args[0] == "ws_closed")
        self.assertFalse(row["close_frame_received"])
        self.assertEqual(row["request_id"], "request-123")
        self.assertEqual(row["edge_id"], "edge-BOM")
        self.assertEqual(row["last_server_event_type"], "session.output_audio.delta")
        self.assertNotIn("must-not-log", str(logged.call_args_list))

    def test_close_reports_successful_client_send_mic_and_ping_ages(self) -> None:
        session = make_session()
        ws = MagicMock()
        session.ws = ws
        ws.last_ping_tm = time.time() - 4
        ws.last_pong_tm = time.time() - 2
        with patch("voice_cua.realtime_session.log_event") as logged, \
                patch("voice_cua.realtime_session.island_publish"):
            session._on_open(ws)
            session._send_wire({"type": "input_audio_buffer.append", "audio": "ignored"})
            session._on_close(ws, None, None)
        row = next(c.kwargs for c in logged.call_args_list if c.args[0] == "ws_closed")
        self.assertEqual(row["last_client_wire_type"], "input_audio_buffer.append")
        self.assertIsInstance(row["last_client_wire_s"], float)
        self.assertIsInstance(row["last_mic_append_s"], float)
        self.assertGreaterEqual(row["last_ping_s"], 3)
        self.assertGreaterEqual(row["last_pong_s"], 1)

    def test_live_wire_audio_append_is_tracked(self) -> None:
        session = make_session()
        ws = MagicMock()
        session.ws = ws
        with patch("voice_cua.realtime_session.log_event"), patch("voice_cua.realtime_session.island_publish"):
            session._on_open(ws)
            session._send_wire({"type": "session.input_audio.append", "audio": "ignored"})
            session._on_close(ws, None, None)
        self.assertEqual(session._last_client_wire_type, "session.input_audio.append")
        self.assertGreater(session._last_mic_append_at, 0.0)

    def test_websocket_teardown_captures_ping_age_before_close(self) -> None:
        session = make_session()
        app = realtime_session.websocket.WebSocketApp("ws://example.invalid", on_error=session._on_error,
                                                       on_close=session._on_close)
        app.last_ping_tm = time.time() - 4
        app.last_pong_tm = time.time() - 2
        app.sock = MagicMock()
        with patch("voice_cua.realtime_session.log_event") as logged, \
                patch("voice_cua.realtime_session.island_publish"):
            session._on_error(app, ConnectionError("transport drop"))
            app._stop_ping_thread()
            app.sock = None
            session._on_close(app, None, None)
        row = next(c.kwargs for c in logged.call_args_list if c.args[0] == "ws_closed")
        self.assertGreaterEqual(row["last_ping_s"], 3)
        self.assertGreaterEqual(row["last_pong_s"], 1)

    def test_failed_send_does_not_update_success_timestamps(self) -> None:
        session = make_session()
        ws = MagicMock()
        session.ws = ws
        with patch("voice_cua.realtime_session.log_event"), patch("voice_cua.realtime_session.island_publish"):
            session._on_open(ws)
            ws.send.side_effect = ConnectionError("closed")
            self.assertFalse(session._send_wire({"type": "input_audio_buffer.append", "audio": "ignored"}))
            session._on_close(ws, None, None)
        self.assertEqual(session._last_client_wire_type, "session.update")
        self.assertGreater(session._last_client_wire_at, 0.0)
        self.assertEqual(session._last_mic_append_at, 0.0)

    def test_close_handles_missing_connection_fields(self) -> None:
        session = realtime_session.RealtimeSession.__new__(realtime_session.RealtimeSession)
        session._stop = MagicMock()
        session._stop.is_set.return_value = True
        with patch("voice_cua.realtime_session.log_event") as logged:
            session._on_close(None, None, None)
        row = next(c.kwargs for c in logged.call_args_list if c.args[0] == "ws_closed")
        self.assertIsNone(row["last_client_wire_type"])
        self.assertIsNone(row["last_client_wire_s"])
        self.assertIsNone(row["last_mic_append_s"])
        self.assertIsNone(row["last_ping_s"])
        self.assertIsNone(row["last_pong_s"])


if __name__ == "__main__":
    unittest.main()
