"""Samantha's helper → CUAService island hub forwarder (source=samantha, bearer token)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

# Keep test events out of the real ~/.cache/macos-cua activity log.
os.environ.setdefault(
    "VOICE_CUA_ACTIVITY_LOG",
    str(Path(tempfile.mkdtemp(prefix="voice-cua-test-")) / "activity.jsonl"),
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from voice_cua.island_hub import HubForwarder, read_token
from voice_cua.island_state import IslandBus


class _Hub:
    def __init__(self, token: str = "tok-1") -> None:
        self.token = token
        self.posts: list[dict] = []
        self.auth: list[str] = []
        hub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: D401
                pass

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n))
                auth = self.headers.get("Authorization", "")
                hub.auth.append(auth)
                code = 200 if auth == f"Bearer {hub.token}" else 401
                if code == 200:
                    hub.posts.append(body)
                self.send_response(code)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()


def _wait(pred, timeout=3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


class HubForwarderTests(unittest.TestCase):
    def test_posts_as_samantha_with_bearer_and_coalesces(self) -> None:
        hub = _Hub()
        bus = IslandBus()
        fwd = HubForwarder(bus, url=hub.url, token_reader=lambda: "tok-1", min_interval=0.1).start()
        try:
            for i in range(30):  # meter burst
                bus.set_voice_meter("user", i / 30)
            bus.publish("listening", title="Listening")
            self.assertTrue(_wait(lambda: hub.posts and hub.posts[-1]["kind"] == "listening"))
            self.assertTrue(all(p["source"] == "samantha" for p in hub.posts))
            self.assertLess(len(hub.posts), 10, "coalesced to <= 10 Hz")
            self.assertTrue(all(a == "Bearer tok-1" for a in hub.auth))
        finally:
            fwd.stop(); hub.close()

    def test_rotated_token_is_reread_once(self) -> None:
        hub = _Hub(token="new")
        bus = IslandBus()
        tokens = iter(["old", "new"])
        fwd = HubForwarder(bus, url=hub.url, token_reader=lambda: next(tokens, "new"), min_interval=0.05).start()
        try:
            bus.publish("thinking", title="Thinking")
            self.assertTrue(_wait(lambda: any(p["kind"] == "thinking" for p in hub.posts)))
        finally:
            fwd.stop(); hub.close()

    def test_no_token_never_posts_unsigned(self) -> None:
        hub = _Hub()
        bus = IslandBus()
        fwd = HubForwarder(bus, url=hub.url, token_reader=lambda: None, min_interval=0.05).start()
        try:
            bus.publish("listening", title="Listening")
            time.sleep(0.3)
            self.assertEqual(hub.auth, [], "nothing sent without a token")
        finally:
            fwd.stop(); hub.close()

    def test_read_token_unapproved_never_runs_security(self) -> None:
        with patch("voice_cua.catalog.keychain_approved", return_value=False), \
                patch("voice_cua.island_hub.subprocess.run") as run:
            self.assertIsNone(read_token("samantha"))
        run.assert_not_called()

    def test_read_token_uses_security_output_not_argv_secret(self) -> None:
        class R:
            returncode = 0
            stdout = "secret-value\n"

        with patch("voice_cua.catalog.keychain_approved", return_value=True), \
                patch("voice_cua.island_hub.subprocess.run", return_value=R()) as run:
            self.assertEqual(read_token("samantha"), "secret-value")
        argv = run.call_args.args[0]
        self.assertEqual(argv[:2], ["/usr/bin/security", "find-generic-password"])
        self.assertNotIn("secret-value", " ".join(argv))
        self.assertTrue(run.call_args.kwargs["capture_output"])


class _SockHub:
    """Unix-socket stand-in for CUAService (one JSON line each way)."""

    def __init__(self) -> None:
        import shutil
        import socket as so
        import tempfile

        base = os.path.expanduser("~/.cache")
        os.makedirs(base, exist_ok=True)
        self.dir = tempfile.mkdtemp(prefix="acu-sk-", dir=base)
        self._rm = lambda: shutil.rmtree(self.dir, ignore_errors=True)
        self.path = os.path.join(self.dir, "s.sock")
        self.posts: list[dict] = []
        self.srv = so.socket(so.AF_UNIX, so.SOCK_STREAM)
        self.srv.bind(self.path)
        self.srv.listen(8)
        self.srv.settimeout(0.2)
        self._stop = threading.Event()
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                c, _ = self.srv.accept()
            except OSError:
                continue
            with c:
                buf = b""
                while not buf.endswith(b"\n"):
                    d = c.recv(65536)
                    if not d:
                        break
                    buf += d
                req = json.loads(buf)
                self.posts.append(req)
                c.sendall(json.dumps({"status": 200, "body": {"ok": "true"}}).encode() + b"\n")

    def close(self) -> None:
        self._stop.set()
        self.srv.close()
        self._rm()


class SocketTransportTests(unittest.TestCase):
    def test_socket_posts_without_any_keychain_read(self) -> None:
        hub = _SockHub()
        bus = IslandBus()
        calls = []
        fwd = HubForwarder(bus, sock=hub.path, token_reader=lambda: calls.append(1) or "x", min_interval=0.05).start()
        try:
            bus.publish("listening", title="Listening")
            self.assertTrue(_wait(lambda: any(p["body"].get("kind") == "listening" for p in hub.posts)))
            p = hub.posts[-1]
            self.assertEqual((p["method"], p["path"], p["body"]["source"]), ("POST", "/api/island/publish", "samantha"))
            self.assertEqual(calls, [], "socket transport never touches the Keychain")
        finally:
            fwd.stop(); hub.close()

    def test_socket_path_env_requires_our_socket(self) -> None:
        from voice_cua.island_hub import socket_path

        hub = _SockHub()
        try:
            with patch.dict(os.environ, {"VOICE_CUA_ISLAND_SOCKET": hub.path}):
                self.assertEqual(socket_path(), hub.path)
            plain = os.path.join(hub.dir, "plain")
            open(plain, "w").close()
            with patch.dict(os.environ, {"VOICE_CUA_ISLAND_SOCKET": plain}):
                self.assertIsNone(socket_path(), "regular file is not accepted")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("VOICE_CUA_ISLAND_SOCKET", None)
                self.assertIsNone(socket_path())
        finally:
            hub.close()


if __name__ == "__main__":
    unittest.main()
