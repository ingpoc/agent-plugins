"""Unit tests for durable_lease_controller request/response sequencing.

These guard the one-behind race that returned stale fill errors for later
page_context / sessions / closeout commands when send() deleted response.json
before the controller finished writing the prior result.
"""

from __future__ import annotations

import importlib.util
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
CTRL_PATH = PLUGIN_ROOT / "skills" / "comet-control" / "scripts" / "durable_lease_controller.py"


def _load_controller():
    spec = importlib.util.spec_from_file_location("durable_lease_controller", CTRL_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class DurableLeaseControllerSequencingTests(unittest.TestCase):
    def test_controller_source_requires_seq_envelope(self) -> None:
        source = CTRL_PATH.read_text()
        self.assertIn('"seq": seq', source)
        self.assertIn("send.lock", source)
        self.assertIn("wrapped.get(\"seq\") != seq", source)
        self.assertIn('"renewal_failed"', source)
        self.assertIn('driver_command["_controller_command_id"] = seq', source)
        self.assertIn('resp.get("command_id") != seq', source)
        # Must not delete response before writing the next request (race).
        self.assertNotIn('p["response"].unlink(missing_ok=True)', source)

    def test_send_waits_for_matching_seq_not_stale_response(self) -> None:
        mod = _load_controller()
        work = Path("/tmp/comet-control-durable-seq-unit")
        work.mkdir(parents=True, exist_ok=True)
        for name in ("request.json", "response.json", "seq.counter", "send.lock", "controller.alive"):
            p = work / name
            if p.exists():
                p.unlink()
        (work / "controller.alive").write_text("1")

        # Stale prior response with a non-matching seq already on disk.
        (work / "response.json").write_text(
            json.dumps(
                {
                    "seq": 99,
                    "result": {
                        "event": "run",
                        "response": {"success": False, "error": "stale fill"},
                    },
                }
            )
            + "\n"
        )

        def controller_side() -> None:
            # Wait for request seq=1 (fresh counter), then write matching response.
            deadline = time.time() + 5
            while time.time() < deadline:
                req = work / "request.json"
                if not req.exists():
                    time.sleep(0.01)
                    continue
                envelope = json.loads(req.read_text())
                req.unlink(missing_ok=True)
                self.assertEqual(envelope.get("seq"), 1)
                (work / "response.json").write_text(
                    json.dumps(
                        {
                            "seq": 1,
                            "result": {"event": "run", "response": {"success": True, "id": "ok-1"}},
                        }
                    )
                    + "\n"
                )
                return
            self.fail("controller side timed out waiting for request")

        thread = threading.Thread(target=controller_side, daemon=True)
        thread.start()

        class Args:
            workdir = str(work)
            payload = json.dumps({"actions": [{"type": "page_context"}]})
            payload_file = None
            timeout = 5.0

        # Capture stdout
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = mod.cmd_send(Args())
        thread.join(timeout=5)
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        self.assertEqual(out.get("event"), "run")
        self.assertTrue((out.get("response") or {}).get("success"))
        self.assertNotIn("stale fill", json.dumps(out))

    def test_send_injects_timeout_seconds_when_omitted(self) -> None:
        mod = _load_controller()
        work = Path("/tmp/comet-control-durable-timeout-unit")
        work.mkdir(parents=True, exist_ok=True)
        for name in ("request.json", "response.json", "seq.counter", "send.lock", "controller.alive"):
            p = work / name
            if p.exists():
                p.unlink()
        (work / "controller.alive").write_text("1")
        seen: dict = {}

        def controller_side() -> None:
            deadline = time.time() + 5
            while time.time() < deadline:
                req = work / "request.json"
                if not req.exists():
                    time.sleep(0.01)
                    continue
                envelope = json.loads(req.read_text())
                req.unlink(missing_ok=True)
                seen.update(envelope.get("body") or {})
                (work / "response.json").write_text(
                    json.dumps(
                        {
                            "seq": envelope.get("seq"),
                            "result": {"event": "run", "response": {"success": True}},
                        }
                    )
                    + "\n"
                )
                return
            self.fail("controller side timed out waiting for request")

        thread = threading.Thread(target=controller_side, daemon=True)
        thread.start()

        class Args:
            workdir = str(work)
            payload = json.dumps({"actions": [{"type": "page_context"}]})
            payload_file = None
            timeout = 120.0

        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = mod.cmd_send(Args())
        thread.join(timeout=5)
        self.assertEqual(rc, 0)
        self.assertEqual(seen.get("timeoutSeconds"), 120)
        self.assertIn("timeoutSeconds", CTRL_PATH.read_text())

    def test_terminal_response_wins_over_removed_alive_sentinel(self) -> None:
        mod = _load_controller()
        work = Path("/tmp/comet-control-durable-closeout-unit")
        work.mkdir(parents=True, exist_ok=True)
        for name in ("request.json", "response.json", "seq.counter", "send.lock", "controller.alive"):
            (work / name).unlink(missing_ok=True)
        (work / "controller.alive").write_text("1")

        def controller_side() -> None:
            while not (work / "request.json").exists():
                time.sleep(0.01)
            envelope = json.loads((work / "request.json").read_text())
            (work / "request.json").unlink()
            (work / "response.json").write_text(json.dumps({
                "seq": envelope["seq"],
                "result": {"event": "closeout", "response": {"success": True}},
            }))
            (work / "controller.alive").unlink()

        threading.Thread(target=controller_side, daemon=True).start()

        class Args:
            workdir = str(work)
            payload = json.dumps({"command": "closeout"})
            payload_file = None
            timeout = 5.0

        import io
        from contextlib import redirect_stdout

        output = io.StringIO()
        with redirect_stdout(output):
            rc = mod.cmd_send(Args())
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(output.getvalue())["response"]["success"])

    def test_session_absence_proof_queries_authoritative_inventory(self) -> None:
        mod = _load_controller()
        with tempfile.TemporaryDirectory(prefix="comet-session-proof-") as tmp:
            path = str(Path(tmp) / "bridge.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(path)
            server.listen(1)

            def serve() -> None:
                connection, _ = server.accept()
                with connection:
                    request = json.loads(connection.recv(65536))
                    self.assertEqual(request["type"], "sessions")
                    connection.sendall(json.dumps({"success": True, "sessions": []}).encode())
                server.close()

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            proof = mod._session_absence_proof(path, "closed-session")
            thread.join(timeout=5)
            self.assertEqual(proof, {"verified_absent": True, "matching_session_count": 0})

    def test_dead_controller_closeout_still_requires_absence_proof(self) -> None:
        mod = _load_controller()
        with tempfile.TemporaryDirectory(prefix="comet-dead-closeout-") as tmp:
            work = Path(tmp)
            (work / "ready.json").write_text(json.dumps({
                "session_id": "closed-session",
                "socket_path": "/unused",
            }))
            mod._session_absence_proof = lambda _socket, _session: {
                "verified_absent": True,
                "matching_session_count": 0,
            }

            class Args:
                workdir = str(work)
                timeout = 5.0

            import io
            from contextlib import redirect_stdout

            output = io.StringIO()
            with redirect_stdout(output):
                rc = mod.cmd_closeout(Args())
            payload = json.loads(output.getvalue())
            self.assertEqual(rc, 0)
            self.assertTrue(payload["response"]["verified_absent"])


if __name__ == "__main__":
    unittest.main()
