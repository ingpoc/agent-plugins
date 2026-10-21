"""GPT-Live 403 at the handshake: no Origin header, no retry storm, a clear error.

Regression (Guru, 2026-10-21): selecting GPT-Live-1 meant Samantha never listened —
every handshake to wss://api.openai.com/v1/live/sessions got a bare 403 "Forbidden",
the helper retried 8x, exited 0, and CUAService restarted it into the same 403.
"""

from __future__ import annotations

import os
import sys
import tempfile
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


if __name__ == "__main__":
    unittest.main()
