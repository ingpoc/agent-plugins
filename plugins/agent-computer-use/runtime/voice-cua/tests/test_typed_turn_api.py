"""HTTP and session-hub contracts for typed turns and approval."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from voice_cua import gateway, island_facade, session_hub  # noqa: E402
from voice_cua.gateway import Handler  # noqa: E402
from voice_cua.island_state import IslandBus  # noqa: E402
from voice_cua.activity_log import tool_context  # noqa: E402


class TypedTurnAPITests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method: str, path: str, payload: dict | None = None, headers: dict | None = None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}{path}",
            data=data,
            headers={"Content-Type": "application/json", **(headers or {})},
            method=method,
        )
        try:
            response = urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read().decode())

    def test_async_text_caps_timeout_and_turn_endpoint_polls(self) -> None:
        with patch("voice_cua.session_hub.start_turn", return_value={
            "ok": True, "turn_id": "turn-1", "status": "running",
        }) as start:
            code, body = self.request("POST", "/api/session/text", {
                "text": "build the app", "timeout": 9000, "async": True,
            })
        self.assertEqual((code, body), (202, {"ok": True, "turn_id": "turn-1", "status": "running"}))
        start.assert_called_once_with("build the app", timeout=1800.0, asynchronous=True)

        turn = {"turn_id": "turn-1", "status": "done", "reply": "Done", "error": None,
                "elapsed_ms": 45, "pending_confirm": {"confirm_id": "c-1", "prompt": "Confirm?"}}
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(gateway, "_approve_token_path", return_value=Path(tmp) / "typed-approve.token"):
            gateway._create_approve_token()
            self.addCleanup(gateway._delete_approve_token)
            token_headers = {"X-Voice-CUA-Approve": gateway._APPROVE_TOKEN}
            with patch("voice_cua.session_hub.get_turn", return_value=turn):
                self.assertEqual(self.request("GET", "/api/session/turn?id=turn-1")[0], 403)
                code, body = self.request("GET", "/api/session/turn?id=turn-1", headers=token_headers)
        self.assertEqual((code, body), (200, turn))

        with patch("voice_cua.session_hub.start_turn", return_value={
            "ok": False, "error": "still_running", "turn_id": "turn-2", "status": "running",
        }) as start:
            code, body = self.request("POST", "/api/session/text", {"text": "default timeout"})
        self.assertEqual(code, 202)
        start.assert_called_once_with("default timeout", timeout=90.0, asynchronous=False)

    def test_text_rejects_invalid_timeout_and_async_flag(self) -> None:
        for payload in (
            {"text": "x", "timeout": 0}, {"text": "x", "timeout": -1},
            {"text": "x", "timeout": float("nan")}, {"text": "x", "timeout": True},
            {"text": "x", "timeout": 10 ** 400},
            {"text": "x", "async": "true"},
        ):
            with self.subTest(payload=payload):
                code, body = self.request("POST", "/api/session/text", payload)
                self.assertEqual(code, 400)
                self.assertFalse(body["ok"])

    def test_typed_confirmation_reads_and_approval_require_token_but_island_buttons_still_work(self) -> None:
        bus = IslandBus()
        with tempfile.TemporaryDirectory() as tmp, patch.object(gateway, "ISLAND", bus), \
                patch.object(gateway, "_approve_token_path", return_value=Path(tmp) / "typed-approve.token"):
            gateway._create_approve_token()
            token = gateway._APPROVE_TOKEN
            self.addCleanup(gateway._delete_approve_token)
            self.assertIsNotNone(token)
            typed_result = {}
            typed_thread = threading.Thread(
                target=lambda: typed_result.setdefault("outcome", bus.request_confirm(
                    "c-typed", "Run the approved command?", timeout=2, origin_turn_id="turn-a",
                )),
                daemon=True,
            )
            typed_thread.start()
            deadline = time.monotonic() + 1
            while bus.pending_confirm() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(self.request("GET", "/api/confirm/pending")[0], 403)
            self.assertEqual(self.request("POST", "/api/confirm", {
                "confirm_id": "c-typed", "approved": True,
            })[0], 409)
            headers = {"X-Voice-CUA-Approve": token}
            self.assertEqual(self.request("GET", "/api/confirm/pending", headers=headers), (200, {
                "confirm_id": "c-typed", "prompt": "Run the approved command?",
            }))
            code, body = self.request("POST", "/api/session/approve", {
                "confirm_id": "c-typed", "approved": True,
            }, headers)
            self.assertEqual((code, body["ok"]), (200, True))
            typed_thread.join(timeout=1)
            self.assertEqual(typed_result.get("outcome"), "approved")

            island_result = {}
            island_thread = threading.Thread(
                target=lambda: island_result.setdefault("outcome", bus.request_confirm(
                    "c-island", "Allow from the Island?", timeout=2,
                )),
                daemon=True,
            )
            island_thread.start()
            deadline = time.monotonic() + 1
            while bus.pending_confirm() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            code, body = self.request("POST", "/api/confirm", {
                "confirm_id": "c-island", "approved": True,
            })
            self.assertEqual((code, body["ok"]), (200, True))
            island_thread.join(timeout=1)
            self.assertEqual(island_result.get("outcome"), "approved")

    def test_legacy_confirm_requires_boolean(self) -> None:
        with patch.object(gateway.ISLAND, "resolve_confirm", return_value=True) as resolve:
            code, _ = self.request("POST", "/api/confirm", {"confirm_id": "c-1", "approved": "true"})
            self.assertEqual(code, 400)
            resolve.assert_not_called()

    def test_typed_approval_requires_per_process_token_and_rejects_duplicate_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "typed-approve.token"
            with patch.object(gateway, "_approve_token_path", return_value=path):
                gateway._create_approve_token()
                self.addCleanup(gateway._delete_approve_token)
                token = gateway._APPROVE_TOKEN
                self.assertIsNotNone(token)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                code, body = self.request("POST", "/api/session/approve", {
                    "confirm_id": "c-2", "approved": True,
                })
                self.assertEqual((code, body["ok"]), (403, False))
                code, body = self.request("POST", "/api/session/approve", {
                    "confirm_id": "c-2", "approved": "true",
                }, {"X-Voice-CUA-Approve": token})
                self.assertEqual(code, 400)
                with patch.object(gateway.ISLAND, "resolve_confirm", side_effect=[True, False]) as resolve:
                    code, body = self.request("POST", "/api/session/approve", {
                        "confirm_id": "c-2", "approved": True,
                    }, {"X-Voice-CUA-Approve": token})
                    self.assertEqual((code, body["ok"]), (200, True))
                    code, body = self.request("POST", "/api/session/approve", {
                        "confirm_id": "c-2", "approved": True,
                    }, {"X-Voice-CUA-Approve": token})
                    self.assertEqual((code, body["ok"]), (409, False))
                self.assertEqual(resolve.call_count, 2)
                self.assertTrue(resolve.call_args_list[0].kwargs["typed_authorized"])
                gateway._delete_approve_token()
                self.assertFalse(path.exists())

    def test_confirm_request_rejects_invalid_timeout(self) -> None:
        for timeout in (0, -1, float("inf"), True, 10 ** 400):
            with self.subTest(timeout=timeout):
                code, _ = self.request("POST", "/api/confirm/request", {
                    "confirm_id": "c-invalid", "prompt": "Confirm?", "timeout": timeout,
                })
                self.assertEqual(code, 400)

    def test_remote_confirm_request_requires_a_known_origin_turn(self) -> None:
        with patch.object(session_hub, "has_turn", side_effect=lambda turn_id: turn_id == "turn-known"), \
                patch.object(gateway.ISLAND, "request_confirm", return_value="approved") as request:
            code, body = self.request("POST", "/api/confirm/request", {
                "confirm_id": "c-remote", "prompt": "Build?", "timeout": 1,
                "origin_turn_id": "turn-unknown",
            })
            self.assertEqual(code, 409)
            code, body = self.request("POST", "/api/confirm/request", {
                "confirm_id": "c-remote", "prompt": "Build?", "timeout": 1,
                "origin_turn_id": "turn-known",
            })
        self.assertEqual((code, body["outcome"]), (200, "approved"))
        request.assert_called_once_with("c-remote", "Build?", timeout=1.0,
                                        origin_turn_id="turn-known")

    def test_island_facade_preserves_typed_turn_origin(self) -> None:
        with tool_context(typed_turn_id="turn-local"), \
                patch.object(island_facade, "use_remote", return_value=False), \
                patch.object(island_facade.ISLAND, "request_confirm", return_value="approved") as local:
            self.assertEqual(island_facade.island_confirm("c-local", "Build?", timeout=2), "approved")
        local.assert_called_once_with("c-local", "Build?", timeout=2, origin_turn_id="turn-local")

        with tool_context(typed_turn_id="turn-remote"), \
                patch.object(island_facade, "use_remote", return_value=True), \
                patch.object(island_facade, "_post", return_value={"ok": True, "approved": True}) as post:
            self.assertEqual(island_facade.island_confirm("c-remote", "Build?", timeout=2), "approved")
        self.assertEqual(post.call_args.args[1]["origin_turn_id"], "turn-remote")

    def test_remote_confirmation_timeout_is_distinct_from_transport_failure(self) -> None:
        with patch.object(island_facade, "use_remote", return_value=True), \
                patch.object(island_facade, "_post", return_value=None):
            self.assertEqual(island_facade.island_confirm("c-transport", "OK?", timeout=0.01), "error")
        with patch.object(island_facade, "use_remote", return_value=True), \
                patch.object(island_facade, "_post", return_value={
                    "ok": True, "approved": False, "timed_out": True,
                }):
            self.assertEqual(island_facade.island_confirm("c-timeout", "OK?", timeout=0.01), "timed_out")


class SessionHubTurnTests(unittest.TestCase):
    def setUp(self) -> None:
        with session_hub._turn_lock:
            session_hub._turns.clear()

    def tearDown(self) -> None:
        session_hub.shutdown_turns()
        with session_hub._turn_lock:
            session_hub._turns.clear()

    def test_wait_timeout_does_not_cancel_the_worker_turn(self) -> None:
        called, release = threading.Event(), threading.Event()
        received = {}
        session = SimpleNamespace(
            _session_ready=threading.Event(), _stop=threading.Event(), _dormant=None,
            _wake=threading.Event(), _spoken_task_cancel=lambda _text: False,
        )
        session._session_ready.set()

        def send_text_and_wait(text, *, timeout, cancel_event, typed_turn_id):
            received.update(text=text, timeout=timeout, cancel_event=cancel_event,
                            typed_turn_id=typed_turn_id)
            called.set()
            release.wait(2)
            return {"ok": True, "reply": "r" * 300}

        session.send_text_and_wait = send_text_and_wait
        with patch.object(session_hub, "_active", session):
            result = session_hub.start_turn("write a feature", timeout=0.02)
            self.assertEqual((result["ok"], result["error"], result["status"]),
                             (False, "still_running", "running"))
            self.assertTrue(called.wait(1))
            self.assertIsNone(received["timeout"])
            self.assertEqual(received["typed_turn_id"], result["turn_id"])
            self.assertFalse(received["cancel_event"].is_set())
            self.assertEqual(session_hub.get_turn(result["turn_id"])["status"], "running")
            release.set()
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                turn = session_hub.get_turn(result["turn_id"])
                if turn and turn["status"] != "running":
                    break
                time.sleep(0.01)
        self.assertEqual((turn["status"], len(turn["reply"])), ("done", 300))

    def test_async_turn_returns_pollable_id_and_only_completed_turns_are_evicted(self) -> None:
        called, release = threading.Event(), threading.Event()
        session = SimpleNamespace(
            _session_ready=threading.Event(), _stop=threading.Event(), _dormant=None,
            _wake=threading.Event(), _spoken_task_cancel=lambda _text: False,
        )
        session._session_ready.set()

        def send_text_and_wait(text, *, timeout, cancel_event, typed_turn_id):
            called.set()
            release.wait(2)
            return {"ok": True, "reply": text}

        session.send_text_and_wait = send_text_and_wait
        with patch.object(session_hub, "_active", session):
            result = session_hub.start_turn("async task", asynchronous=True)
            self.assertEqual((result["ok"], result["status"]), (True, "running"))
            self.assertTrue(called.wait(1))
            release.set()
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                turn = session_hub.get_turn(result["turn_id"])
                if turn and turn["status"] == "done":
                    break
                time.sleep(0.01)
        self.assertEqual((turn["status"], turn["reply"]), ("done", "async task"))

    def test_disconnect_and_shutdown_settle_turns_as_errors(self) -> None:
        releases = [threading.Event(), threading.Event()]
        called = [threading.Event(), threading.Event()]
        sessions, turns = [], []
        for index in range(2):
            session = SimpleNamespace(
                _session_ready=threading.Event(), _stop=threading.Event(), _dormant=None,
                _wake=threading.Event(), _spoken_task_cancel=lambda _text: False,
            )
            session._session_ready.set()

            def send_text_and_wait(text, *, timeout, cancel_event, typed_turn_id, i=index):
                called[i].set()
                releases[i].wait(2)
                return {"ok": True, "reply": text}

            session.send_text_and_wait = send_text_and_wait
            sessions.append(session)
            with patch.object(session_hub, "_active", session):
                turns.append(session_hub.start_turn(f"turn-{index}", asynchronous=True)["turn_id"])
        self.assertTrue(all(event.wait(1) for event in called))
        session_hub.fail_session_turns(sessions[0], "session disconnected")
        session_hub.shutdown_turns()
        first, second = [session_hub.get_turn(turn_id) for turn_id in turns]
        self.assertEqual((first["status"], first["error"]), ("error", "session disconnected"))
        self.assertEqual((second["status"], second["error"]), ("error", "gateway shutting down"))
        releases[0].set()
        releases[1].set()

    def test_eviction_keeps_running_turns_and_drops_completed_records(self) -> None:
        with session_hub._turn_lock:
            session_hub._turns.clear()
            session_hub._turns["completed"] = {"status": "done"}
            for index in range(session_hub.TURN_HISTORY):
                session_hub._turns[f"running-{index}"] = {
                    "status": "running", "started_at": time.monotonic(),
                    "event": threading.Event(), "cancel_event": threading.Event(),
                }
            session_hub._evict_turns_locked()
        self.assertNotIn("completed", session_hub._turns)
        self.assertEqual(len(session_hub._turns), session_hub.TURN_HISTORY)
        self.assertTrue(all(turn["status"] == "running" for turn in session_hub._turns.values()))

    def test_confirmation_poll_is_bound_to_its_originating_turn(self) -> None:
        bus = IslandBus()
        for turn_id, status in (("turn-a", "running"), ("turn-b", "running"), ("turn-done", "done")):
            session_hub._turns[turn_id] = {
                "status": status, "ok": status == "done", "reply": "", "error": None,
                "started_at": time.monotonic(), "elapsed_ms": 1 if status == "done" else None,
                "event": threading.Event(), "cancel_event": threading.Event(), "_session": None,
            }
        result = {}
        worker = threading.Thread(
            target=lambda: result.setdefault("outcome", bus.request_confirm(
                "confirm-a", "Run build?", timeout=2, origin_turn_id="turn-a",
            )),
            daemon=True,
        )
        worker.start()
        deadline = time.monotonic() + 1
        while bus.pending_confirm() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        with patch("voice_cua.island_state.ISLAND", bus):
            self.assertEqual(session_hub.get_turn("turn-a")["pending_confirm"], {
                "confirm_id": "confirm-a", "prompt": "Run build?",
            })
            self.assertIsNone(session_hub.get_turn("turn-b")["pending_confirm"])
            self.assertIsNone(session_hub.get_turn("turn-done")["pending_confirm"])
        self.assertTrue(bus.resolve_confirm("confirm-a", False, typed_authorized=True))
        worker.join(timeout=1)
        self.assertEqual(result.get("outcome"), "denied")


if __name__ == "__main__":
    unittest.main()
