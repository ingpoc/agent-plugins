#!/usr/bin/env python3
"""Deterministic contracts for the Comet Control loopback broker."""

from __future__ import annotations

import base64
import importlib.util
import io
import json
from pathlib import Path
import queue
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

from websockets.sync.client import connect


HOST_PATH = Path(__file__).resolve().parents[1] / "native" / "broker.py"
SPEC = importlib.util.spec_from_file_location("comet_control_broker_under_test", HOST_PATH)
assert SPEC and SPEC.loader
host = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(host)


class TimeoutValidationTests(unittest.TestCase):
    def test_defaults_and_accepts_finite_timeout(self) -> None:
        self.assertEqual(host._validated_timeout_seconds(None), 90.0)
        self.assertEqual(host._validated_timeout_seconds("12.5"), 12.5)

    def test_rejects_invalid_or_unbounded_timeout(self) -> None:
        for value in (True, "nope", float("nan"), 0, 301):
            with self.subTest(value=value), self.assertRaises(ValueError):
                host._validated_timeout_seconds(value)


class RequestHandlingTests(unittest.TestCase):
    def setUp(self) -> None:
        host._set_cua_claim(None)
        with host.extension_connection_lock:
            host.active_extension = None
            host.active_extension_info = {}
            host.active_extension_connected_at = 0.0
        with host.extension_seen_lock:
            host.extension_seen_at = 0.0
        with host.pending_lock:
            host.pending.clear()
            host.pending_generation.clear()
        while True:
            try:
                host.outbound.get_nowait()
            except queue.Empty:
                break

    def tearDown(self) -> None:
        host._set_cua_claim(None)

    def test_client_cannot_override_broker_correlation_id(self) -> None:
        framed = host._outbound_request(
            "broker-id", {"id": "client-id", "type": "status"}, 1234
        )
        self.assertEqual(framed["id"], "broker-id")
        self.assertEqual(framed["deadlineAt"], 1234)

    def test_claim_mirror_keeps_only_public_bounded_state(self) -> None:
        claim = {
            "claim_id": "claim-1",
            "intent": "native-dialog",
            "expires_at": (time.time() + 60) * 1000,
        }
        host._sync_cua_claim(
            {"type": "cua_runtime_claim"},
            {"success": True, "claim_token": "secret", "claim": claim},
        )
        self.assertEqual(host._active_cua_claim(), claim)
        self.assertNotIn("claim_token", host._active_cua_claim())

        host._sync_cua_claim(
            {"type": "cua_runtime_release"},
            {"success": True, "released": True},
        )
        self.assertIsNone(host._active_cua_claim())

    def test_visual_lock_module_initializes_once_across_client_threads(self) -> None:
        class Loader:
            def __init__(self) -> None:
                self.calls = 0

            def exec_module(self, _module) -> None:
                self.calls += 1
                time.sleep(0.05)

        class Spec:
            def __init__(self, loader: Loader) -> None:
                self.loader = loader

        loader = Loader()
        spec = Spec(loader)
        loaded: list[object] = []
        barrier = threading.Barrier(6)

        def load() -> None:
            barrier.wait(timeout=1)
            loaded.append(host._visual_lock_module())

        with mock.patch.object(host, "_VISUAL_LOCK_MODULE", None), mock.patch.object(
            host.importlib.util, "spec_from_file_location", return_value=spec
        ), mock.patch.object(host.importlib.util, "module_from_spec", side_effect=lambda _spec: object()):
            threads = [threading.Thread(target=load) for _ in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=1)

        self.assertEqual(loader.calls, 1)
        self.assertEqual(len(loaded), 6)
        self.assertTrue(all(module is loaded[0] for module in loaded))

    def test_visual_request_waits_for_sibling_without_querying_busy_extension(self) -> None:
        lease = mock.Mock()

        class Focus:
            class VisualFocusBusy(TimeoutError):
                pass

            calls = 0

            @classmethod
            def acquire(cls, _owner: str, *, timeout: float):
                cls.calls += 1
                if cls.calls == 1:
                    raise cls.VisualFocusBusy()
                self.assertEqual(timeout, 5)
                return lease

        with mock.patch.object(host, "_visual_lock_module", return_value=Focus), mock.patch.object(
            host, "_forward_extension_request"
        ) as forward:
            acquired, error = host._acquire_visual_focus(
                {"type": "run", "sessionId": "agent-a"}, 5
            )

        self.assertIs(acquired, lease)
        self.assertIsNone(error)
        forward.assert_not_called()

    def test_reachability_probe_disconnects_without_forwarding(self) -> None:
        client, server = socket.socketpair()
        thread = threading.Thread(target=host.handle_client, args=(server,))
        thread.start()
        client.close()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(host.pending, {})

    def test_invalid_timeout_returns_structured_error(self) -> None:
        client, server = socket.socketpair()
        thread = threading.Thread(target=host.handle_client, args=(server,))
        thread.start()
        with client:
            client.sendall(json.dumps({"type": "status", "timeoutSeconds": "bad"}).encode())
            response = json.loads(client.recv(65536))
        thread.join(timeout=1)
        self.assertFalse(response["success"])
        self.assertEqual(response["error_code"], "INVALID_REQUEST")

    def test_runtime_attestation_accepts_only_exact_comet_profile(self) -> None:
        expected = str((Path.home() / "Library/Application Support/Comet").resolve())
        browser = "/Applications/Comet.app/Contents/MacOS/Comet"
        environment = {
            "COMET_CONTROL_EXPECTED_USER_DATA_DIR": expected,
            "COMET_CONTROL_EXPECTED_BROWSER_EXECUTABLE": browser,
            "COMET_CONTROL_EXPECTED_EXTENSION_ORIGIN": "chrome-extension://abcdefghijklmnopabcdefghijklmnop/",
        }
        process_list = mock.Mock(stdout="123\n", returncode=0)
        with mock.patch.dict(host.os.environ, environment), mock.patch.object(
            host.subprocess, "run", return_value=process_list
        ), mock.patch.object(host, "_process_field", side_effect=[browser, browser]
        ):
            accepted = host._attest_comet_runtime()
        self.assertTrue(accepted["verified"])
        self.assertEqual(accepted["browser_pid"], 123)

        with mock.patch.dict(host.os.environ, environment), mock.patch.object(
            host.subprocess,
            "run",
            return_value=process_list,
        ), mock.patch.object(
            host,
            "_process_field",
            side_effect=[browser, f"{browser} --user-data-dir=/tmp/other-comet"],
        ):
            rejected = host._attest_comet_runtime()
        self.assertEqual(rejected["error_code"], "COMET_RUNTIME_NOT_FOUND")

    def test_runtime_flag_parser_accepts_split_and_quoted_exact_values(self) -> None:
        expected = "/path with spaces/comet-control"
        self.assertTrue(host._has_exact_command_value(
            f"Comet --user-data-dir '{expected}'", "--user-data-dir", expected
        ))
        self.assertTrue(host._has_exact_command_value(
            f'Comet --user-data-dir="{expected}"', "--user-data-dir", expected
        ))
        self.assertFalse(host._has_exact_command_value(
            f"Comet --user-data-dir={expected}-other", "--user-data-dir", expected
        ))

    def test_runtime_attestation_fails_closed_without_contract(self) -> None:
        with mock.patch.dict(host.os.environ, {}, clear=True):
            result = host._attest_comet_runtime()
        self.assertFalse(result["verified"])
        self.assertEqual(result["error_code"], "RUNTIME_CONTRACT_MISSING")

    def test_main_rejects_missing_contract_before_socket_server_starts(self) -> None:
        with mock.patch.dict(host.os.environ, {}, clear=True), mock.patch.object(
            host, "socket_server"
        ) as socket_server, mock.patch(
            "sys.stderr", new_callable=io.StringIO
        ):
            exit_code = host.main()
        self.assertEqual(exit_code, 73)
        socket_server.assert_not_called()

    def test_loopback_transport_rejects_wrong_origin_and_routes_response(self) -> None:
        expected = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
        environment = {"COMET_CONTROL_EXPECTED_EXTENSION_ORIGIN": expected + "/"}
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(
            host, "PAIRING_PATH", Path(raw) / "pairing.json"
        ), mock.patch.dict(host.os.environ, environment), mock.patch.object(
            host, "_attest_comet_runtime", return_value={"verified": True, "browser_pid": 99}
        ), host.serve(
            host.extension_connection,
            "127.0.0.1",
            0,
            max_size=host.MAX_EXTENSION_RESPONSE_BYTES,
        ) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            uri = f"ws://127.0.0.1:{server.socket.getsockname()[1]}"
            hello = {
                "type": "broker_hello",
                "protocol_version": host.PROTOCOL_VERSION,
                "pairing_secret": "a" * 64,
                "extension_version": "1.0.0",
                "extension_build_sha256": "b" * 64,
                "capabilities": ["screenshots"],
            }
            try:
                with connect(uri, origin="chrome-extension://wrong") as websocket:
                    with self.assertRaises(host.ConnectionClosed):
                        websocket.recv(timeout=1)

                response_q: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)
                with connect(uri, origin=expected) as websocket:
                    websocket.send(json.dumps(hello))
                    acknowledgement = json.loads(websocket.recv(timeout=1))
                    self.assertEqual(acknowledgement["type"], "broker_hello_ack")
                    self.assertEqual(acknowledgement["protocol_version"], host.PROTOCOL_VERSION)
                    for _ in range(50):
                        if host._active_extension_snapshot() is not None:
                            break
                        time.sleep(0.01)
                    deadline = int(time.time() * 1000) + 2000
                    with host.extension_connection_lock:
                        generation = host.active_extension_generation
                    with host.pending_lock:
                        host.pending["request-1"] = response_q
                        host.pending_generation["request-1"] = generation
                    host.outbound.put({
                        "generation": generation,
                        "deadline_at_ms": deadline,
                        "message": host._outbound_request(
                            "request-1", {"type": "status"}, deadline
                        ),
                    })
                    self.assertEqual(json.loads(websocket.recv(timeout=1))["id"], "request-1")
                    websocket.send(json.dumps({
                        "id": "request-1",
                        "success": True,
                        "payload": "x" * (2 * 1024 * 1024),
                    }))
                    response = response_q.get(timeout=2)
                    self.assertTrue(response["success"])
                    self.assertEqual(len(response["payload"]), 2 * 1024 * 1024)
            finally:
                with host.pending_lock:
                    host.pending.pop("request-1", None)
                    host.pending_generation.pop("request-1", None)
                server.shutdown()

    def test_pairing_is_tofu_and_rejects_a_different_secret(self) -> None:
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(
            host, "PAIRING_PATH", Path(raw) / "pairing.json"
        ):
            self.assertTrue(host._accept_pairing_secret("a" * 64))
            self.assertTrue(host._accept_pairing_secret("a" * 64))
            self.assertFalse(host._accept_pairing_secret("b" * 64))
            self.assertEqual((Path(raw) / "pairing.json").stat().st_mode & 0o777, 0o600)

    def test_disconnect_invalidates_only_its_generation(self) -> None:
        response_q: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)
        current = object()
        with host.extension_connection_lock:
            host.active_extension = current
            host.active_extension_generation = 7
        with host.pending_lock:
            host.pending["request-7"] = response_q
            host.pending_generation["request-7"] = 7
        host._unregister_extension(current, 7)
        response = response_q.get(timeout=1)
        self.assertEqual(response["error_code"], "EXTENSION_DISCONNECTED")
        self.assertTrue(response["retryable"])

    def test_new_extension_connection_fences_the_previous_generation(self) -> None:
        class Connection:
            def __init__(self) -> None:
                self.closed = False

            def close(self, **_kwargs) -> None:
                self.closed = True

        previous = Connection()
        current = Connection()
        response_q: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)
        previous_generation = host._register_extension(previous, {"extension_version": "old"})
        with host.pending_lock:
            host.pending["old-request"] = response_q
            host.pending_generation["old-request"] = previous_generation
        current_generation = host._register_extension(current, {"extension_version": "new"})
        self.assertGreater(current_generation, previous_generation)
        self.assertTrue(previous.closed)
        self.assertEqual(response_q.get(timeout=1)["error_code"], "EXTENSION_REPLACED")
        self.assertEqual(host._active_extension_snapshot()[1]["extension_version"], "new")

    def test_pending_limit_fails_fast_without_dispatch(self) -> None:
        with host.extension_connection_lock:
            host.active_extension = object()
            host.active_extension_generation = 9
            host.active_extension_connected_at = time.monotonic()
        host._mark_extension_seen()
        with host.pending_lock:
            for index in range(host.MAX_PENDING_REQUESTS):
                request_id = f"busy-{index}"
                host.pending[request_id] = queue.Queue(maxsize=1)
                host.pending_generation[request_id] = 9
        response = host._forward_extension_request({"type": "status"}, 1)
        self.assertEqual(response["error_code"], "BROKER_BUSY")
        self.assertEqual(host.outbound.qsize(), 0)

    def test_extension_timeout_names_the_failed_operation(self) -> None:
        with host.extension_connection_lock:
            host.active_extension = object()
            host.active_extension_generation = 10
            host.active_extension_connected_at = time.monotonic()
        host._mark_extension_seen()
        with mock.patch.object(queue.Queue, "get", side_effect=queue.Empty):
            response = host._forward_extension_request(
                {"type": "session_preflight"}, 0.01
            )
        self.assertEqual(response["error_code"], "EXTENSION_TIMEOUT")
        self.assertIn("session_preflight", response["error"])

    def test_broker_status_is_local_and_reports_attested_runtime(self) -> None:
        client, server = socket.socketpair()
        attestation = {
            "verified": True,
            "expected_user_data_dir": "/tmp/comet-control-runtime",
            "browser_pid": 99,
        }
        with host.extension_connection_lock:
            host.active_extension = object()
            host.active_extension_generation = 3
            host.active_extension_info = {
                "protocol_version": host.PROTOCOL_VERSION,
                "extension_build_sha256": "b" * 64,
            }
            host.active_extension_connected_at = time.monotonic()
        host._mark_extension_seen()
        with mock.patch.object(host, "_attest_comet_runtime", return_value=attestation):
            thread = threading.Thread(target=host.handle_client, args=(server,))
            thread.start()
            with client:
                client.sendall(json.dumps({"type": "broker_status"}).encode())
                response = json.loads(client.recv(65536))
            thread.join(timeout=1)
        self.assertTrue(response["success"])
        self.assertTrue(response["broker"]["runtime_verified"])
        self.assertEqual(response["broker"]["browser_pid"], 99)
        self.assertEqual(response["broker"]["user_data_dir"], "/tmp/comet-control-runtime")
        self.assertEqual(host.pending, {})

    def test_visual_request_lock_survives_client_death_until_extension_finishes(self) -> None:
        class Lease:
            def __init__(self) -> None:
                self.released = False

            def release(self) -> None:
                self.released = True

        class Focus:
            class VisualFocusBusy(Exception):
                pass

            def __init__(self, lease: Lease) -> None:
                self.lease = lease

            def acquire(self, _owner: str, **_kwargs):
                return self.lease

        lease = Lease()
        forwarded = threading.Event()
        finish_extension = threading.Event()

        def fake_forward(message: dict) -> None:
            self.assertFalse(lease.released)
            forwarded.set()
            self.assertTrue(finish_extension.wait(timeout=2))
            with host.pending_lock:
                response_q = host.pending[message["message"]["id"]]
            response_q.put({"id": message["message"]["id"], "success": True, "results": []})

        client, server = socket.socketpair()
        with host.extension_connection_lock:
            host.active_extension = object()
            host.active_extension_generation = 4
            host.active_extension_connected_at = time.monotonic()
        host._mark_extension_seen()
        with mock.patch.object(host, "_VISUAL_LOCK_MODULE", Focus(lease)), mock.patch.object(
            host.outbound, "put_nowait", side_effect=fake_forward
        ):
            thread = threading.Thread(target=host.handle_client, args=(server,))
            thread.start()
            client.sendall(json.dumps({
                "type": "run",
                "sessionId": "direct-tool-route",
                "timeoutSeconds": 2,
                "actions": [{"type": "screenshot"}],
            }).encode())
            self.assertTrue(forwarded.wait(timeout=1))
            client.close()  # Simulate the short-lived driver/tool process dying.
            self.assertFalse(lease.released)
            finish_extension.set()
            thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertTrue(lease.released)
        self.assertEqual(host.pending, {})

    def test_active_cua_claim_rejects_visual_request_without_forwarding(self) -> None:
        host._set_cua_claim({
            "claim_id": "claim-1",
            "intent": "native-dialog",
            "session_id": "owner",
            "expires_at": (time.time() + 60) * 1000,
        })
        client, server = socket.socketpair()
        with mock.patch.object(host.outbound, "put") as forward_message:
            thread = threading.Thread(target=host.handle_client, args=(server,))
            thread.start()
            with client:
                client.sendall(json.dumps({
                    "type": "run",
                    "sessionId": "owner",
                    "timeoutSeconds": 2,
                    "actions": [{"type": "page_context"}],
                }).encode())
                response = json.loads(client.recv(65536))
            thread.join(timeout=1)

        self.assertFalse(thread.is_alive())
        self.assertFalse(response["success"])
        self.assertEqual(response["error_code"], "CUA_RUNTIME_CLAIMED")
        forward_message.assert_not_called()


class ScreenshotMaterializationTests(unittest.TestCase):
    def test_failure_recorder_materializes_screenshot_and_retains_a_bounded_tail(self) -> None:
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(
            host, "_screenshot_dir", return_value=Path(raw) / "captures"
        ):
            recorder = Path(raw) / "flight-recorder"
            recorder.mkdir()
            for index in range(host.FLIGHT_RECORDER_LIMIT):
                stale = recorder / f"{index:02d}-stale.json"
                stale.write_text("{}\n")
                time.sleep(0.001)
            response = host._materialize_response({
                "id": "failed-command",
                "success": False,
                "failure_record": {
                    "error_code": "ACTIONABILITY_OBSCURED",
                    "screenshot": {
                        "type": "screenshot",
                        "format": "jpeg",
                        "base64": base64.b64encode(b"failure-image").decode(),
                    },
                },
            })
            record_path = Path(response["failure_record_path"])
            record = json.loads(record_path.read_text())
            self.assertEqual(record_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(len(list(recorder.glob("*.json"))), host.FLIGHT_RECORDER_LIMIT)
            self.assertNotIn("base64", record["screenshot"])
            self.assertEqual(Path(record["screenshot"]["screenshot_path"]).read_bytes(), b"failure-image")

    def test_writes_only_allowlisted_image_extensions(self) -> None:
        with tempfile.TemporaryDirectory() as home, mock.patch.object(
            host, "_screenshot_dir", return_value=Path(home)
        ):
            response = {
                "id": "proof",
                "results": [{
                    "type": "screenshot",
                    "format": "png",
                    "base64": base64.b64encode(b"png-bytes").decode(),
                }],
            }
            result = host._materialize_response(response)
            path = Path(result["results"][0]["screenshot_path"])
            self.assertEqual(path.suffix, ".png")
            self.assertEqual(path.read_bytes(), b"png-bytes")

    def test_rejects_path_like_format_and_invalid_base64(self) -> None:
        with tempfile.TemporaryDirectory() as home, mock.patch.object(
            host, "_screenshot_dir", return_value=Path(home)
        ):
            with self.assertRaises(ValueError):
                host._materialize_response({
                    "id": "escape",
                    "results": [{
                        "type": "screenshot",
                        "format": "../../escape",
                        "base64": base64.b64encode(b"x").decode(),
                    }],
                })
            with self.assertRaises(ValueError):
                host._materialize_response({
                    "id": "invalid",
                    "results": [{
                        "type": "screenshot",
                        "format": "jpeg",
                        "base64": "not-base64!",
                    }],
                })

    def test_capture_id_cannot_escape_screenshot_directory(self) -> None:
        with tempfile.TemporaryDirectory() as home, mock.patch.object(
            host, "_screenshot_dir", return_value=Path(home)
        ):
            result = host._materialize_response({
                "id": "../../outside",
                "results": [{
                    "type": "screenshot",
                    "format": "png",
                    "base64": base64.b64encode(b"safe").decode(),
                }],
            })
            path = Path(result["results"][0]["screenshot_path"])
            expected = Path(home)
            self.assertEqual(path.parent, expected)
            self.assertNotIn("..", path.name)


if __name__ == "__main__":
    unittest.main()
