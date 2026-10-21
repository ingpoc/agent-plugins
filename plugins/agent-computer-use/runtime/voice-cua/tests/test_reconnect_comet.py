"""Reconnect-during-tool, mic sender resilience, transcription, meter, Comet lease cleanup.

Fake sockets/streams only — no mic, no network, no real Comet lease.
"""

from __future__ import annotations

import contextlib
import shutil
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# Never read the machine's ~/.config/voice-cua/settings.json: a user who picked GPT-Live
# there switched every session in these tests to the Live provider.
os.environ["VOICE_CUA_SETTINGS"] = str(Path(tempfile.mkdtemp(prefix="voice-cua-test-settings-")) / "settings.json")
os.environ.setdefault(
    "VOICE_CUA_ACTIVITY_LOG",
    str(Path(tempfile.mkdtemp(prefix="voice-cua-test-")) / "activity.jsonl"),
)

# Keep the kept-open lease pointer out of ~/.cache; the directory object is
# finalized (removed) at interpreter exit, so nothing is left in $TMPDIR.
_STATE_DIR = tempfile.TemporaryDirectory(prefix="voice-cua-test-state-")
os.environ.setdefault("VOICE_CUA_COMET_STATE", str(Path(_STATE_DIR.name) / "lease.json"))

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from voice_cua import SYSTEM_INSTRUCTIONS  # noqa: E402
from voice_cua import comet_bridge  # noqa: E402
from voice_cua.audio_io import MicStreamer  # noqa: E402
from voice_cua.comet_bridge import CometLease, redact_actions, redact_url  # noqa: E402
from voice_cua.realtime_session import RealtimeSession  # noqa: E402


class FakeWS:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.fail = False

    def send(self, raw: str) -> None:
        if self.fail:
            raise ConnectionError("Connection is already closed.")
        self.events.append(json.loads(raw))


def _wait(pred, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


class MicSenderTests(unittest.TestCase):
    def test_sender_survives_closed_socket(self) -> None:
        calls = {"n": 0}

        def send(_event):
            calls["n"] += 1
            raise ConnectionError("Connection is already closed.")

        mic = MicStreamer(send)
        mic._start_sender()
        for _ in range(5):
            mic._q.put(b"\x00\x10" * 480)
        self.assertTrue(_wait(lambda: calls["n"] >= 5))
        self.assertTrue(mic.sender_alive())
        self.assertGreaterEqual(mic.send_errors, 5)
        mic._stop.set()

    def test_ensure_sender_restarts_dead_thread(self) -> None:
        mic = MicStreamer(lambda _e: None)
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        mic._sender = dead
        self.assertFalse(mic.sender_alive())
        self.assertTrue(mic.ensure_sender())
        self.assertTrue(mic.sender_alive())
        self.assertFalse(mic.ensure_sender())
        mic._stop.set()

    def test_meter_throttled_to_10hz_and_idle_once(self) -> None:
        seen: list[str] = []
        mic = MicStreamer(lambda _e: None, meter=lambda side, _l, _b: seen.append(side))
        for _ in range(50):
            mic._emit_meter("user", 0.5, [0.5] * 8)
        self.assertEqual(seen, ["user"])
        for _ in range(20):
            mic._emit_meter("idle", 0.0, [0.0] * 8)
        self.assertEqual(seen, ["user", "idle"])


class SendAndAudioTests(unittest.TestCase):
    def test_send_drops_when_socket_closed(self) -> None:
        session = RealtimeSession(api_key="test", enable_audio=False)
        session.ws = FakeWS()
        session.ws.fail = True
        self.assertFalse(session._send({"type": "input_audio_buffer.append"}))
        self.assertEqual(session._send_drops, 1)

    def test_start_audio_revives_dead_mic_on_reconnect(self) -> None:
        session = RealtimeSession(api_key="test", enable_audio=False)
        session._audio_enabled = True

        class FakeAudio:
            restarted = 0

            def ensure_mic(self):
                FakeAudio.restarted += 1
                return True

        session._audio = FakeAudio()  # type: ignore[assignment]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            session._start_audio()
        self.assertEqual(FakeAudio.restarted, 1)
        self.assertIn("Audio I/O active", out.getvalue())


class ReconnectDuringToolTests(unittest.TestCase):
    def test_result_after_reconnect_recreates_call_then_output(self) -> None:
        session = RealtimeSession(api_key="test", enable_audio=False)
        session.ws = FakeWS()
        session._conn_epoch = 1

        def dispatch(_name, _args):
            # Socket drops and reopens while the tool runs.
            session._prepare_reconnect()
            session._conn_epoch += 1
            session._session_ready.set()
            return {"ok": True, "lease": "isolated"}

        with patch("voice_cua.realtime_session.dispatch", side_effect=dispatch), patch(
            "voice_cua.realtime_session.comet_is_active", return_value=False
        ):
            session._run_tool("call_old", "cua_state", '{"app":"Calculator"}')
            self.assertTrue(_wait(lambda: any(e["type"] == "response.create" for e in session.ws.events)))
        items = [e["item"] for e in session.ws.events if e["type"] == "conversation.item.create"]
        self.assertEqual([i["type"] for i in items], ["function_call", "function_call_output"])
        self.assertEqual(items[0]["call_id"], "call_old")
        self.assertEqual(items[0]["name"], "cua_state")
        self.assertEqual(json.loads(items[0]["arguments"]), {"app": "Calculator"})
        self.assertEqual(items[1]["call_id"], "call_old")
        types = [e["type"] for e in session.ws.events]
        self.assertLess(max(i for i, t in enumerate(types) if t == "conversation.item.create"), types.index("response.create"))

    def test_rejected_recreated_call_falls_back_to_note(self) -> None:
        session = RealtimeSession(api_key="test", enable_audio=False)
        session.ws = FakeWS()
        session._recreated_calls["call_kj1"] = "Tool cua_state ... finished with result: ok=True"
        with contextlib.redirect_stderr(io.StringIO()):
            session._handle_error_event({"type": "error", "error": {
                "type": "invalid_request_error", "code": "invalid_tool_call_id",
                "message": "Tool call ID 'call_kj1' not found in conversation.",
            }})
        notes = [e for e in session.ws.events if e["type"] == "conversation.item.create"]
        self.assertEqual(notes[0]["item"]["role"], "system")
        self.assertEqual(session.ws.events[-1]["type"], "response.create")
        self.assertNotIn("call_kj1", session._recreated_calls)

    def test_stale_note_is_summary_not_raw_payload(self) -> None:
        note = RealtimeSession.stale_result_note(
            "comet_act", {"ok": True, "page_text": "PRIVATE PAGE BODY", "nested": {"path": "/Users/x"}}
        )
        self.assertNotIn("PRIVATE PAGE BODY", note)
        self.assertNotIn("/Users/x", note)
        self.assertIn("ok=True", note)

    def test_shutdown_unblocks_stale_delivery(self) -> None:
        session = RealtimeSession(api_key="test", enable_audio=False)
        session.ws = FakeWS()
        session.ws.fail = True
        session._stop.set()
        started = time.monotonic()
        self.assertTrue(session._deliver_tool_result("c", "cua_state", {"ok": True}, epoch=0))
        self.assertLess(time.monotonic() - started, 1.0)

    def test_same_epoch_posts_function_call_output(self) -> None:
        session = RealtimeSession(api_key="test", enable_audio=False)
        session.ws = FakeWS()
        with patch("voice_cua.realtime_session.dispatch", return_value={"ok": True}), patch(
            "voice_cua.realtime_session.comet_is_active", return_value=False
        ):
            session._run_tool("call_1", "cua_state", '{"app":"Calculator"}')
            self.assertTrue(_wait(lambda: any(e["type"] == "response.create" for e in session.ws.events)))
        self.assertEqual(session.ws.events[0]["item"]["type"], "function_call_output")
        self.assertEqual(session.ws.events[0]["item"]["call_id"], "call_1")

    def test_on_open_after_reconnect_sends_lease_note(self) -> None:
        session = RealtimeSession(api_key="test", enable_audio=False)
        session.ws = FakeWS()
        with patch("voice_cua.realtime_session.comet_is_active", return_value=True):
            with contextlib.redirect_stdout(io.StringIO()):
                session._on_open(None)
                session._on_open(None)
        notes = [e for e in session.ws.events if e["type"] == "conversation.item.create"]
        self.assertEqual(len(notes), 1, "first open sends no note")
        text = notes[0]["item"]["content"][0]["text"]
        self.assertIn("Comet window is still open", text)
        updates = [e for e in session.ws.events if e["type"] == "session.update"]
        names = {t["name"] for t in updates[-1]["session"]["tools"]}
        self.assertIn("comet_act", names)


class TranscriptionTests(unittest.TestCase):
    def test_completed_transcript_prints_you_line(self) -> None:
        session = RealtimeSession(api_key="test", enable_audio=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            session._on_message(None, json.dumps({
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "item_1",
                "transcript": "open Calculator",
            }))
        self.assertIn("You: open Calculator", out.getvalue())


BROKER_OK = {
    "success": True,
    "broker": {
        "runtime_verified": True,
        "extension_connected": True,
        "python_executable": sys.executable,
        "socket_path": "/nonexistent/comet.sock",
    },
}


class CometLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._state = Path(tempfile.mkdtemp(prefix="voice-cua-test-state-")) / "lease.json"
        self.addCleanup(shutil.rmtree, self._state.parent, ignore_errors=True)
        patcher = patch.object(comet_bridge, "_state_path", return_value=self._state)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _active_lease(self, purpose: str = "self_test") -> CometLease:
        lease = CometLease()
        lease._session_id = "samantha-test"
        workdir = Path(tempfile.mkdtemp(prefix="samantha-comet-test-"))
        # Paths that never reach closeout (stale timer, kept-open) must not leak.
        self.addCleanup(shutil.rmtree, workdir, ignore_errors=True)
        lease._workdir = workdir
        lease._python = sys.executable
        lease._controller = Path("/nonexistent/controller.py")
        lease._purpose = purpose
        lease._adopt_tried = True
        return lease

    def _begin(self, lease: CometLease, url: str, purpose: str, responses: list[dict]) -> dict:
        root = Path(tempfile.mkdtemp(prefix="voice-cua-test-root-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        calls: list[list[str]] = []

        def fake(cmd: list[str], *, timeout: float) -> dict:
            calls.append(cmd)
            return responses.pop(0)

        with patch.object(comet_bridge, "_runtime_root", return_value=root), patch.object(
            comet_bridge, "_run_json", side_effect=fake
        ):
            result = lease.begin(url, purpose=purpose)
        if result.get("ok") and lease._workdir is not None:
            self.addCleanup(shutil.rmtree, lease._workdir, ignore_errors=True)
        self._calls = calls
        return result

    def test_end_clears_and_notifies(self) -> None:
        lease = self._active_lease()
        seen: list[bool] = []
        lease.add_listener(seen.append)
        with patch.object(comet_bridge, "_run_json", return_value={"ok": True}):
            out = lease.end()
        self.assertTrue(out["ok"])
        self.assertEqual(out["window"], "closed")
        self.assertFalse(lease.active())
        self.assertEqual(seen, [False])

    def test_self_test_idle_release(self) -> None:
        lease = self._active_lease("self_test")
        with patch.object(comet_bridge, "LEASE_IDLE_SECONDS", 0.05), patch.object(
            comet_bridge, "_run_json", return_value={"ok": True}
        ):
            lease._arm_idle()
            self.assertTrue(_wait(lambda: not lease.active(), timeout=2.0))

    def test_user_task_never_idle_released(self) -> None:
        lease = self._active_lease("user_task")
        with patch.object(comet_bridge, "LEASE_IDLE_SECONDS", 0.05), patch.object(
            comet_bridge, "_run_json", return_value={"ok": True}
        ) as run:
            lease._arm_idle()
            self.assertIsNone(lease._idle_timer)
            lease._idle_release(lease._idle_gen)
            time.sleep(0.15)
        self.assertTrue(lease.active())
        run.assert_not_called()

    def test_user_task_end_keeps_window_open(self) -> None:
        lease = self._active_lease("user_task")
        with patch.object(comet_bridge, "_run_json", return_value={"ok": True}) as run:
            out = lease.end()
        self.assertTrue(out["ok"])
        self.assertTrue(out["kept_open"])
        self.assertEqual(out["window"], "open")
        self.assertTrue(lease.active())
        run.assert_not_called()  # no closeout sent

    def test_user_task_closes_only_when_user_asks(self) -> None:
        lease = self._active_lease("user_task")
        with patch.object(comet_bridge, "_run_json", return_value={"ok": True}) as run:
            out = lease.end(user_requested=True)
        self.assertTrue(out["closed"])
        self.assertFalse(lease.active())
        self.assertEqual(run.call_args[0][0][2], "closeout")

    def test_exit_hook_closes_self_test_only(self) -> None:
        for purpose, closes in (("self_test", True), ("user_task", False)):
            lease = self._active_lease(purpose)
            with patch.object(comet_bridge, "_LEASE", lease), patch.object(
                comet_bridge, "_run_json", return_value={"ok": True}
            ) as run:
                comet_bridge._end_at_exit()
            self.assertEqual(lease.active(), not closes, purpose)
            self.assertEqual(run.called, closes, purpose)

    def test_stale_idle_timer_does_not_end_refreshed_lease(self) -> None:
        lease = self._active_lease()
        with patch.object(comet_bridge, "_run_json", return_value={"ok": True}):
            stale_gen = lease._idle_gen
            lease._cancel_idle()  # e.g. act() started / re-armed after the timer fired
            lease._idle_release(stale_gen)
            self.assertTrue(lease.active())
            self.assertTrue(lease.end(_idle_gen=stale_gen).get("skipped"))
            self.assertTrue(lease.active())

    def test_begin_user_task_persists_pointer_without_secrets(self) -> None:
        lease = CometLease()
        out = self._begin(lease, "https://www.netflix.com/?token=abc", "user_task", [dict(BROKER_OK), {"ok": True}])
        self.assertTrue(out["ok"])
        self.assertEqual(out["purpose"], "user_task")
        self.assertFalse(out["reused"])
        state = json.loads(self._state.read_text())
        self.assertEqual(state["session_id"], out["session_id"])
        self.assertEqual(state["url"], "https://www.netflix.com/")
        self.assertNotIn("token", json.dumps(state).replace("session_id", ""))
        self.assertEqual(self._state.stat().st_mode & 0o777, 0o600)
        self.assertIsNone(lease._idle_timer)

    def test_followup_reattaches_same_window(self) -> None:
        lease = CometLease()
        first = self._begin(lease, "https://www.netflix.com", "user_task", [dict(BROKER_OK), {"ok": True}])
        second = self._begin(lease, "https://www.youtube.com", "user_task", [{"ok": True}])
        self.assertTrue(second["ok"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["session_id"], second["session_id"])
        cmd = self._calls[0]
        self.assertEqual(cmd[2], "send")  # goto in the existing lease, no new start
        self.assertEqual(json.loads(cmd[5])["actions"], [{"type": "goto", "url": "https://www.youtube.com"}])

    def test_self_test_never_uses_user_window(self) -> None:
        lease = CometLease()
        self._begin(lease, "https://www.netflix.com", "user_task", [dict(BROKER_OK), {"ok": True}])
        out = self._begin(lease, "https://example.com", "self_test", [])
        self.assertFalse(out["ok"])
        self.assertEqual(self._calls, [])  # no goto, no closeout
        self.assertEqual(lease.purpose(), "user_task")

    def test_reconnect_note_keeps_user_window_open(self) -> None:
        note = RealtimeSession.reconnect_note(comet_active=True)
        self.assertIn("user_requested: true", note)
        self.assertNotIn("if the browser task is done", note)

    def test_followup_starts_fresh_when_window_gone(self) -> None:
        lease = CometLease()
        first = self._begin(lease, "https://www.netflix.com", "user_task", [dict(BROKER_OK), {"ok": True}])
        old_workdir = lease._workdir
        second = self._begin(
            lease,
            "https://www.netflix.com",
            "user_task",
            [{"ok": False, "error": "tab closed"}, {"ok": True}, dict(BROKER_OK), {"ok": True}],
        )
        self.assertTrue(second["ok"])
        self.assertFalse(second["reused"])
        self.assertNotEqual(first["session_id"], second["session_id"])
        self.assertFalse(old_workdir.exists())

    def test_self_test_begin_closes_on_end(self) -> None:
        lease = CometLease()
        out = self._begin(lease, "https://example.com", "self_test", [dict(BROKER_OK), {"ok": True}])
        self.assertEqual(out["purpose"], "self_test")
        self.assertFalse(self._state.exists())  # self-tests are never persisted
        with patch.object(comet_bridge, "_run_json", return_value={"ok": True}):
            lease._cancel_idle()
            self.assertTrue(lease.end()["closed"])

    def test_restarted_helper_adopts_open_user_window(self) -> None:
        first = CometLease()
        out = self._begin(first, "https://www.netflix.com", "user_task", [dict(BROKER_OK), {"ok": True}])
        # Simulate the controller script existing for the adoption check.
        controller = Path(tempfile.mkdtemp(prefix="voice-cua-test-ctl-")) / "controller.py"
        self.addCleanup(shutil.rmtree, controller.parent, ignore_errors=True)
        controller.write_text("")
        state = json.loads(self._state.read_text())
        state["controller"] = str(controller)
        self._state.write_text(json.dumps(state))
        fresh = CometLease()
        with patch.object(
            comet_bridge, "_run_json", return_value={"ok": True, "session_id": out["session_id"]}
        ):
            self.assertTrue(fresh.active())
        self.assertEqual(fresh.purpose(), "user_task")

    def test_adoption_drops_dead_pointer(self) -> None:
        self._state.write_text(json.dumps({"session_id": "x", "workdir": "/nonexistent", "python": "p", "controller": "/nonexistent"}))
        self.assertFalse(CometLease().active())
        self.assertFalse(self._state.exists())

    def test_ttl_is_short(self) -> None:
        self.assertLessEqual(comet_bridge.LEASE_TTL_SECONDS, 300)

    def test_redaction(self) -> None:
        self.assertEqual(redact_url("https://u:p@example.com/a?q=secret#f"), "https://example.com/a")
        self.assertEqual(
            redact_actions([{"type": "fill_selector", "value": "pw"}, {"type": "goto", "url": "https://x.com/?t=1"}]),
            ["fill_selector", "goto https://x.com/"],
        )

    def test_routing_rules_in_instructions(self) -> None:
        self.assertIn("Native macOS apps", SYSTEM_INSTRUCTIONS)
        self.assertIn("Never cua_act on Comet", SYSTEM_INSTRUCTIONS)
        self.assertIn("NEVER", SYSTEM_INSTRUCTIONS)
        self.assertIn("user_requested: true", SYSTEM_INSTRUCTIONS)
        self.assertIn("https://www.netflix.com", SYSTEM_INSTRUCTIONS)
        self.assertIn("Speak English by default", SYSTEM_INSTRUCTIONS)
        self.assertIn("Keep replies short", SYSTEM_INSTRUCTIONS)
        self.assertIn("Greetings, small talk", SYSTEM_INSTRUCTIONS)
        self.assertIn("Never\n  call a tool or touch any app", SYSTEM_INSTRUCTIONS)
        self.assertIn("Do not end replies with offers", SYSTEM_INSTRUCTIONS)

    def test_comet_begin_island_label_shows_site(self) -> None:
        from voice_cua.tools import comet_begin_label

        self.assertEqual(comet_begin_label("https://www.netflix.com/in/", "user_task"), ("Opening netflix.com", "Comet"))
        self.assertEqual(comet_begin_label("https://www.youtube.com", "user_task", reuse=True), ("Loading youtube.com", "Comet"))
        self.assertEqual(comet_begin_label("https://example.com", "self_test"), ("Self-check · example.com", "Comet"))
        self.assertNotIn("Isolated", " ".join(comet_begin_label("https://x.com", "user_task")))


class TruncateAndLanguageTests(unittest.TestCase):
    def test_played_ms_is_per_item(self) -> None:
        from voice_cua.audio_io import RealtimeAudio

        audio = RealtimeAudio(lambda _m: None)
        speaker = audio._speaker
        one_second = __import__("base64").b64encode(b"\x00\x00" * 24000).decode()
        audio.on_output_delta(one_second, item_id="item_a")
        speaker._played_samples += 24000  # item_a fully played
        speaker._buffer.clear()
        audio.on_output_delta(one_second, item_id="item_b")
        speaker._played_samples += 12000  # half of item_b
        ms, item = audio.interrupt()
        self.assertEqual(item, "item_b")
        self.assertEqual(ms, 500)

    def test_already_shorter_is_benign(self) -> None:
        err = {"type": "invalid_request_error", "code": "invalid_value",
               "message": "Audio content of 1850ms is already shorter than 8640ms"}
        self.assertEqual(RealtimeSession._classify_error(err), "recoverable")

    def test_transcription_language_en(self) -> None:
        from voice_cua.realtime_session import input_transcription_config

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VOICE_CUA_TRANSCRIBE_LANGUAGE", None)
            self.assertEqual(input_transcription_config()["language"], "en")
        with patch.dict(os.environ, {"VOICE_CUA_TRANSCRIBE_LANGUAGE": "auto"}):
            self.assertNotIn("language", input_transcription_config())



def setUpModule() -> None:  # loop tests run on the provider-neutral pass-through
    import loop_harness

    loop_harness.install()


def tearDownModule() -> None:
    import loop_harness

    loop_harness.uninstall()


if __name__ == "__main__":
    unittest.main()
