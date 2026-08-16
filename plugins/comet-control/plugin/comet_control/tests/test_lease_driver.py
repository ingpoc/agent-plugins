from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
DRIVER_PATH = ROOT / "skills" / "comet-control" / "scripts" / "lease_driver.py"
SECRET = "lease-secret-must-never-escape"

SPEC = importlib.util.spec_from_file_location("comet_control_lease_driver", DRIVER_PATH)
assert SPEC and SPEC.loader
DRIVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DRIVER)


class FakeBridge:
    def __init__(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="comet-control-driver-test-")
        self.path = Path(self.tempdir.name) / "bridge.sock"
        self.requests: list[dict[str, Any]] = []
        self.renew_failures_remaining = 0
        self.renew_window_id = 10
        self.preflight_session_id: str | None = None
        self.preflight_window_id: Any = 10
        self.preflight_tab_id: Any = 20
        self.preflight_failure_with_token = False
        self.closeout_failures_remaining = 0
        self.renew_entered = threading.Event()
        self.renew_release = threading.Event()
        self.renew_release.set()
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket.bind(str(self.path))
        self._socket.listen()
        self._socket.settimeout(0.1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _response(self, request: dict[str, Any]) -> dict[str, Any]:
        request_type = request.get("type")
        if request_type == "session_preflight":
            with self._lock:
                session_id = self.preflight_session_id or request.get("sessionId")
                window_id = self.preflight_window_id
                tab_id = self.preflight_tab_id
                failure_with_token = self.preflight_failure_with_token
            if failure_with_token:
                return {
                    "success": False,
                    "error_code": "LEASE_CLEANUP_INCOMPLETE",
                    "retryable": True,
                    "lease_token": SECRET,
                    "error": f"retry cleanup with {SECRET}",
                }
            return {
                "success": True,
                "session_id": session_id,
                "lease_token": SECRET,
                "window_id": window_id,
                "tab_id": tab_id,
            }
        if request_type == "run":
            return {
                "success": True,
                "session_id": request.get("sessionId"),
                "window_id": 10,
                "tab_id": 20,
                "results": [
                    {
                        "type": "page_context",
                        "nested": {"leaseToken": SECRET},
                        "message": f"private={SECRET}",
                    }
                ],
            }
        if request_type == "session_renew":
            self.renew_entered.set()
            self.renew_release.wait(timeout=5)
            with self._lock:
                if self.renew_failures_remaining:
                    self.renew_failures_remaining -= 1
                    return {
                        "success": False,
                        "error": f"transient renewal failure for {SECRET}",
                    }
                renew_window_id = self.renew_window_id
            return {
                "success": True,
                "session_id": request.get("sessionId"),
                "window_id": renew_window_id,
                "tab_id": 20,
            }
        if request_type == "sessions":
            return {
                "success": True,
                "sessions": [{"session_id": request.get("sessionId")}],
            }
        if request_type == "cua_runtime_claim":
            return {
                "success": True,
                "claim_token": "short-lived-cua-claim",
                "claim": {
                    "claim_id": "claim-1",
                    "intent": request.get("intent"),
                    "session_id": request.get("sessionId"),
                    "expires_at": 9999999999999,
                },
            }
        if request_type == "session_closeout":
            with self._lock:
                if self.closeout_failures_remaining:
                    self.closeout_failures_remaining -= 1
                    return {
                        "success": False,
                        "error_code": "LEASE_CLEANUP_INCOMPLETE",
                        "retryable": True,
                        "error": f"retry cleanup without exposing {SECRET}",
                    }
            return {
                "success": True,
                "session_id": request.get("sessionId"),
                "windows_closed": 1,
                "tabs_closed": 1,
            }
        return {"success": False, "error": f"unsupported {request_type}"}

    def _serve(self) -> None:
        while not self._stopping.is_set():
            try:
                connection, _ = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                payload = bytearray()
                request: dict[str, Any] | None = None
                connection.settimeout(1)
                while request is None:
                    try:
                        chunk = connection.recv(65536)
                    except socket.timeout:
                        break
                    if not chunk:
                        break
                    payload.extend(chunk)
                    try:
                        decoded = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(decoded, dict):
                        request = decoded
                if request is None:
                    continue
                with self._lock:
                    self.requests.append(request)
                connection.sendall(json.dumps(self._response(request)).encode())

    def count(self, request_type: str) -> int:
        with self._lock:
            return sum(1 for request in self.requests if request.get("type") == request_type)

    def last(self, request_type: str) -> dict[str, Any]:
        with self._lock:
            matches = [request for request in self.requests if request.get("type") == request_type]
        if not matches:
            raise AssertionError(f"no {request_type} request recorded")
        return matches[-1]

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.requests)

    def fail_next_renewals(self, count: int) -> None:
        with self._lock:
            self.renew_failures_remaining = count

    def change_renewal_window_identity(self, window_id: int) -> None:
        with self._lock:
            self.renew_window_id = window_id

    def change_preflight_identity(
        self, *, session_id: str | None = None, window_id: Any = 10, tab_id: Any = 20
    ) -> None:
        with self._lock:
            self.preflight_session_id = session_id
            self.preflight_window_id = window_id
            self.preflight_tab_id = tab_id

    def fail_preflight_with_cleanup_capability(self) -> None:
        with self._lock:
            self.preflight_failure_with_token = True

    def fail_next_closeouts(self, count: int) -> None:
        with self._lock:
            self.closeout_failures_remaining = count

    def block_renewal(self) -> None:
        self.renew_entered.clear()
        self.renew_release.clear()

    def release_renewal(self) -> None:
        self.renew_release.set()

    def close(self) -> None:
        self._stopping.set()
        self.renew_release.set()
        self._socket.close()
        self._thread.join(timeout=2)
        self.tempdir.cleanup()


class LeaseDriverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bridge = FakeBridge()

    def tearDown(self) -> None:
        self.bridge.close()

    def start_driver(
        self, *, renew_interval: float | None = None, ttl_seconds: int = 360
    ) -> subprocess.Popen[str]:
        command = [
            sys.executable,
            str(DRIVER_PATH),
            "--socket",
            str(self.bridge.path),
            "--session-id",
            "driver-test-session",
            "--label",
            "Driver Test",
            "--url",
            "https://example.com/",
            "--timeout-seconds",
            "5",
            "--ttl-seconds",
            str(ttl_seconds),
        ]
        if renew_interval is not None:
            command.extend(["--renew-interval-seconds", str(renew_interval)])
        return subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def wait_for_count(
        self, request_type: str, expected: int, timeout: float = 5
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.bridge.count(request_type) >= expected:
                return
            time.sleep(0.01)
        self.fail(
            f"missing {request_type} count {expected}; "
            f"got {self.bridge.count(request_type)}"
        )

    def close_process_streams(self, process: subprocess.Popen[str]) -> None:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()

    def read_event(
        self, process: subprocess.Popen[str], expected: str, timeout: float = 5
    ) -> dict[str, Any]:
        assert process.stdout is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([process.stdout], [], [], 0.1)
            if not ready:
                if process.poll() is not None:
                    break
                continue
            line = process.stdout.readline()
            if not line:
                break
            payload = json.loads(line)
            if payload.get("event") == expected:
                return payload
        stderr = process.stderr.read() if process.poll() is not None and process.stderr else ""
        self.fail(f"missing {expected} event; returncode={process.poll()} stderr={stderr}")

    def test_recursive_redaction_and_per_command_timeout(self) -> None:
        process = self.start_driver()
        ready = self.read_event(process, "ready")
        self.assertNotIn(SECRET, json.dumps(ready))
        assert process.stdin is not None
        process.stdin.write(
            json.dumps(
                {
                    "timeoutSeconds": 73,
                    "actions": [{"type": "page_context"}],
                }
            )
            + "\n"
        )
        process.stdin.flush()
        run = self.read_event(process, "run")
        self.assertNotIn(SECRET, json.dumps(run))
        self.assertNotIn("leaseToken", json.dumps(run))
        process.stdin.write('{"command":"closeout"}\n')
        process.stdin.flush()
        closeout = self.read_event(process, "closeout")
        self.assertTrue(closeout["response"]["success"])
        process.stdin.close()
        self.assertEqual(process.wait(timeout=5), 0)
        self.close_process_streams(process)
        self.assertEqual(self.bridge.last("run")["timeoutSeconds"], 73)
        self.assertEqual(DRIVER.transport_timeout(73), 108.0)
        self.assertEqual(self.bridge.count("session_closeout"), 1)

    def test_retryable_preflight_failure_closes_private_capability_once(self) -> None:
        self.bridge.fail_preflight_with_cleanup_capability()
        self.bridge.fail_next_closeouts(1)
        process = self.start_driver()
        failure = self.read_event(process, "preflight_failed")
        closeout = self.read_event(process, "closeout")
        self.assertNotIn(SECRET, json.dumps(failure))
        self.assertNotIn("lease_token", json.dumps(failure))
        self.assertNotIn(SECRET, json.dumps(closeout))
        self.assertTrue(closeout["response"]["success"])
        self.assertEqual(process.wait(timeout=5), 1)
        self.close_process_streams(process)
        self.assertEqual(self.bridge.count("session_preflight"), 1)
        self.assertEqual(closeout["response"]["attempts"], 2)
        self.assertEqual(self.bridge.count("session_closeout"), 2)
        self.assertEqual(self.bridge.last("session_closeout")["leaseToken"], SECRET)

    def test_eof_closes_once(self) -> None:
        process = self.start_driver()
        self.read_event(process, "ready")
        assert process.stdin is not None
        process.stdin.close()
        closeout = self.read_event(process, "closeout")
        self.assertTrue(closeout["response"]["success"])
        self.assertEqual(process.wait(timeout=5), 0)
        self.close_process_streams(process)
        self.assertEqual(self.bridge.count("session_closeout"), 1)

    def test_initial_renewal_handshake_fails_closed_without_ready(self) -> None:
        self.bridge.fail_next_renewals(1)
        process = self.start_driver(renew_interval=0.05)
        failure = self.read_event(process, "renewal_handshake_failed")
        self.assertNotIn(SECRET, json.dumps(failure))
        closeout = self.read_event(process, "closeout")
        self.assertTrue(closeout["response"]["success"])
        self.assertEqual(process.wait(timeout=5), 1)
        self.close_process_streams(process)
        self.assertEqual(self.bridge.count("session_preflight"), 1)
        self.assertEqual(self.bridge.count("session_renew"), 1)
        self.assertEqual(self.bridge.count("session_closeout"), 1)

    def test_invalid_preflight_identity_fails_before_persistent_ready(self) -> None:
        self.bridge.change_preflight_identity(session_id="wrong-session")
        process = self.start_driver(renew_interval=0.05)
        failure = self.read_event(process, "preflight_failed")
        self.assertEqual(failure["error"], "lease response changed session_id")
        closeout = self.read_event(process, "closeout")
        self.assertTrue(closeout["response"]["success"])
        self.assertEqual(process.wait(timeout=5), 1)
        self.close_process_streams(process)
        self.assertEqual(self.bridge.count("session_preflight"), 1)
        self.assertEqual(self.bridge.count("session_renew"), 0)
        self.assertEqual(self.bridge.count("session_closeout"), 1)

    def test_one_persistent_lease_spans_multiple_campaign_commands(self) -> None:
        process = self.start_driver(ttl_seconds=1)
        ready = self.read_event(process, "ready")
        self.assertTrue(ready["persistent"])
        self.assertAlmostEqual(ready["renew_interval_seconds"], 1 / 3)
        self.assertEqual(ready["lease"]["session_id"], "driver-test-session")
        self.assertEqual(ready["lease"]["window_id"], 10)
        self.assertEqual(ready["lease"]["tab_id"], 20)
        self.wait_for_count("session_renew", 1)

        assert process.stdin is not None
        process.stdin.write('{"actions":[{"type":"page_context"}]}\n')
        process.stdin.flush()
        self.assertTrue(self.read_event(process, "run")["response"]["success"])

        # Stay idle for longer than the configured TTL; the same driver renews
        # the same lease/window without opening a per-test replacement.
        time.sleep(1.1)
        self.wait_for_count("session_renew", 4)
        process.stdin.write('{"actions":[{"type":"screenshot"}]}\n')
        process.stdin.flush()
        self.assertTrue(self.read_event(process, "run")["response"]["success"])

        self.assertIsNone(process.poll())
        process.stdin.write('{"command":"closeout"}\n')
        process.stdin.flush()
        closeout = self.read_event(process, "closeout")
        self.assertTrue(closeout["response"]["success"])
        process.stdin.close()
        self.assertEqual(process.wait(timeout=5), 0)
        self.close_process_streams(process)

        requests = self.bridge.snapshot()
        request_types = [request.get("type") for request in requests]
        self.assertEqual(request_types.count("session_preflight"), 1)
        self.assertEqual(request_types.count("run"), 2)
        self.assertGreaterEqual(request_types.count("session_renew"), 4)
        self.assertEqual(request_types.count("session_closeout"), 1)
        self.assertEqual(request_types[-1], "session_closeout")
        for request in requests:
            if request.get("type") in {"run", "session_renew", "session_closeout"}:
                self.assertEqual(request["sessionId"], "driver-test-session")
                self.assertEqual(request["leaseToken"], SECRET)
            if request.get("type") == "session_renew":
                self.assertEqual(
                    request["timeoutSeconds"], DRIVER.RENEW_HOST_TIMEOUT_SECONDS
                )

    def test_renewal_failure_is_compact_private_and_retries_same_lease(self) -> None:
        process = self.start_driver(renew_interval=0.1)
        self.read_event(process, "ready")
        self.assertEqual(self.bridge.count("session_renew"), 1)
        self.bridge.block_renewal()
        self.bridge.fail_next_renewals(1)
        self.assertTrue(self.bridge.renew_entered.wait(timeout=5))
        self.bridge.release_renewal()
        failure = self.read_event(process, "renewal_failed")
        self.assertTrue(failure["retrying_same_lease"])
        self.assertNotIn(SECRET, json.dumps(failure))
        self.assertNotIn("leaseToken", json.dumps(failure))
        recovered = self.read_event(process, "renewal_recovered")
        self.assertEqual(recovered["failures"], 1)
        self.assertNotIn(SECRET, json.dumps(recovered))
        self.wait_for_count("session_renew", 3)
        self.assertIsNone(process.poll())

        assert process.stdin is not None
        process.stdin.write('{"actions":[{"type":"page_context"}]}\n')
        process.stdin.flush()
        self.assertTrue(self.read_event(process, "run")["response"]["success"])
        process.stdin.write('{"command":"closeout"}\n')
        process.stdin.flush()
        self.assertTrue(self.read_event(process, "closeout")["response"]["success"])
        process.stdin.close()
        self.assertEqual(process.wait(timeout=5), 0)
        self.close_process_streams(process)
        self.assertEqual(self.bridge.count("session_preflight"), 1)
        self.assertEqual(self.bridge.count("session_closeout"), 1)

    def test_controller_command_id_is_returned_only_on_its_reply(self) -> None:
        process = self.start_driver(renew_interval=1)
        self.read_event(process, "ready")
        assert process.stdin is not None
        process.stdin.write(
            '{"_controller_command_id":7,"actions":[{"type":"page_context"}]}\n'
        )
        process.stdin.flush()
        reply = self.read_event(process, "run")
        self.assertEqual(reply["kind"], "reply")
        self.assertEqual(reply["command_id"], 7)
        process.stdin.write('{"_controller_command_id":8,"command":"closeout"}\n')
        process.stdin.flush()
        closeout = self.read_event(process, "closeout")
        self.assertEqual(closeout["kind"], "reply")
        self.assertEqual(closeout["command_id"], 8)
        process.stdin.close()
        self.assertEqual(process.wait(timeout=5), 0)
        self.close_process_streams(process)

    def test_three_consecutive_renewal_failures_reap_driver(self) -> None:
        process = self.start_driver(renew_interval=0.05)
        self.read_event(process, "ready")
        self.bridge.fail_next_renewals(DRIVER.MAX_CONSECUTIVE_RENEWAL_FAILURES)
        failed = self.read_event(process, "renewal_failed")
        self.assertEqual(failed["kind"], "notification")
        exhausted = self.read_event(process, "renewal_exhausted")
        self.assertEqual(exhausted["failures"], 3)
        self.assertEqual(exhausted["kind"], "notification")
        self.assertTrue(self.read_event(process, "closeout")["response"]["success"])
        self.assertNotEqual(process.wait(timeout=5), 0)
        self.close_process_streams(process)
        self.assertEqual(self.bridge.count("session_closeout"), 1)

    def test_renewal_rejects_window_identity_drift_without_repreflight(self) -> None:
        process = self.start_driver(renew_interval=0.2)
        self.read_event(process, "ready")
        self.bridge.block_renewal()
        self.bridge.change_renewal_window_identity(999)
        self.assertTrue(self.bridge.renew_entered.wait(timeout=5))
        self.bridge.release_renewal()
        failure = self.read_event(process, "renewal_failed")
        self.assertEqual(failure["error"], "lease renewal changed window_id")
        self.assertTrue(failure["retrying_same_lease"])
        self.assertIsNone(process.poll())

        assert process.stdin is not None
        process.stdin.write('{"command":"closeout"}\n')
        process.stdin.flush()
        self.assertTrue(self.read_event(process, "closeout")["response"]["success"])
        process.stdin.close()
        self.assertEqual(process.wait(timeout=5), 1)
        self.close_process_streams(process)
        self.assertEqual(self.bridge.count("session_preflight"), 1)
        self.assertEqual(self.bridge.count("session_closeout"), 1)

    def test_closeout_waits_for_inflight_renewal_then_remains_last(self) -> None:
        process = self.start_driver(renew_interval=0.2)
        try:
            self.read_event(process, "ready")
            self.bridge.block_renewal()
            self.assertTrue(self.bridge.renew_entered.wait(timeout=5))
            assert process.stdin is not None
            process.stdin.write('{"command":"closeout"}\n')
            process.stdin.flush()
            time.sleep(0.1)
            self.assertEqual(self.bridge.count("session_closeout"), 0)

            self.bridge.release_renewal()
            closeout = self.read_event(process, "closeout")
            self.assertTrue(closeout["response"]["success"])
            process.stdin.close()
            self.assertEqual(process.wait(timeout=5), 0)
            self.close_process_streams(process)
        finally:
            self.bridge.release_renewal()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            self.close_process_streams(process)

        request_types = [
            request.get("type") for request in self.bridge.snapshot()
        ]
        self.assertEqual(request_types.count("session_preflight"), 1)
        self.assertEqual(request_types.count("session_closeout"), 1)
        self.assertEqual(request_types[-1], "session_closeout")

    def test_native_handoff_is_authenticated_by_private_lease(self) -> None:
        process = self.start_driver(ttl_seconds=1)
        ready = self.read_event(process, "ready")
        assert process.stdin is not None
        process.stdin.write('{"command":"native_handoff","ttlSeconds":90}\n')
        process.stdin.flush()
        handoff = self.read_event(process, "native_handoff")
        self.assertTrue(handoff["response"]["success"])
        self.assertEqual(handoff["response"]["claim_token"], "short-lived-cua-claim")
        self.assertNotIn(SECRET, json.dumps(handoff))
        request = self.bridge.last("cua_runtime_claim")
        self.assertEqual(request["leaseToken"], SECRET)
        self.assertEqual(request["intent"], "native-dialog")

        # The external CUA owner may hold the handoff longer than this lease's
        # TTL. The command-idle driver must keep the same browser target alive,
        # then resume through that identity after the external claim releases.
        idle_started = time.monotonic()
        self.wait_for_count("session_renew", 5)
        self.assertGreater(time.monotonic() - idle_started, 1.0)
        process.stdin.write('{"actions":[{"type":"page_context"}]}\n')
        process.stdin.flush()
        resumed = self.read_event(process, "run")
        self.assertTrue(resumed["response"]["success"])
        self.assertEqual(resumed["response"]["session_id"], ready["lease"]["session_id"])
        self.assertEqual(resumed["response"]["window_id"], ready["lease"]["window_id"])
        self.assertEqual(resumed["response"]["tab_id"], ready["lease"]["tab_id"])

        process.stdin.write('{"command":"closeout"}\n')
        process.stdin.flush()
        self.read_event(process, "closeout")
        process.stdin.close()
        self.assertEqual(process.wait(timeout=5), 0)
        self.close_process_streams(process)
        self.assertEqual(self.bridge.count("session_preflight"), 1)
        self.assertEqual(self.bridge.count("session_closeout"), 1)

    def test_interrupt_and_termination_signals_close_once(self) -> None:
        for signum, expected_code, expected_event in (
            (signal.SIGINT, 130, "interrupted"),
            (signal.SIGTERM, 128 + signal.SIGTERM, "terminated"),
            (signal.SIGHUP, 128 + signal.SIGHUP, "terminated"),
        ):
            with self.subTest(signal=signum):
                process = self.start_driver()
                self.read_event(process, "ready")
                process.send_signal(signum)
                self.read_event(process, expected_event)
                closeout = self.read_event(process, "closeout")
                self.assertTrue(closeout["response"]["success"])
                self.assertEqual(process.wait(timeout=5), expected_code)
                self.close_process_streams(process)
        self.assertEqual(self.bridge.count("session_closeout"), 3)

    def test_timeout_validation_rejects_non_finite_values(self) -> None:
        for value in (True, "", "nan", float("inf"), object()):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    DRIVER.bounded_timeout_seconds(value, 5)
        self.assertEqual(DRIVER.bounded_timeout_seconds(0, 5), 1)
        self.assertEqual(DRIVER.bounded_timeout_seconds(999, 5), 300)
        for value in (True, 0, -1, "", "nan", float("inf"), object()):
            with self.subTest(renew_interval=value):
                with self.assertRaises(ValueError):
                    DRIVER.renewal_interval_seconds(360, value)
        self.assertEqual(DRIVER.renewal_interval_seconds(360), 60.0)
        self.assertAlmostEqual(DRIVER.renewal_interval_seconds(1), 1 / 3)
        self.assertAlmostEqual(DRIVER.renewal_interval_seconds(0), 1 / 3)
        self.assertEqual(DRIVER.renewal_interval_seconds(1, 0.05), 0.05)
        self.assertLessEqual(DRIVER.RENEW_TRANSPORT_TIMEOUT_SECONDS, 10)
        self.assertEqual(DRIVER.CLOSEOUT_MAX_ATTEMPTS, 3)
        self.assertLessEqual(DRIVER.CLOSEOUT_RETRY_DELAY_SECONDS, 0.1)

    def test_identity_validation_requires_complete_exact_fields(self) -> None:
        expected = {"session_id": "s", "window_id": 10, "tab_id": 20}
        self.assertEqual(DRIVER.validated_lease_identity(expected, "s"), expected)
        for payload in (
            {"session_id": "other", "window_id": 10, "tab_id": 20},
            {"session_id": "s", "window_id": None, "tab_id": 20},
            {"session_id": "s", "window_id": 10, "tab_id": True},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(RuntimeError):
                    DRIVER.validated_lease_identity(payload, "s")


if __name__ == "__main__":
    unittest.main()
