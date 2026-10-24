from contextlib import nullcontext
import sys
import os
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

_LOG_DIR = tempfile.TemporaryDirectory(prefix="credential-capture-test-")
os.environ["VOICE_CUA_ACTIVITY_LOG"] = str(Path(_LOG_DIR.name) / "activity.jsonl")

from voice_cua import credential_capture as capture, session_hub, tools
from voice_cua.realtime_session import RealtimeSession


class CredentialCaptureTests(unittest.TestCase):
    fields = {"id": "test-login", "label": "Test login", "service": "samantha.test", "account": "test"}

    def setUp(self):
        self.backend = Mock()
        self.saved = {"ok": True, "request_id": "r1", "status": "saved", "stored": True, **self.fields}
        self.stack = [patch.object(capture, "get_backend", return_value=self.backend),
                      patch.object(capture, "island_publish"),
                      patch.object(capture.session_hub, "credential_privacy", side_effect=nullcontext),
                      patch.object(capture, "load_catalog", return_value={"keys": []}),
                      patch.object(capture, "upsert_key")]
        self.mocks = [p.start() for p in self.stack]
        for p in self.stack: self.addCleanup(p.stop)

    def test_native_save_precedes_metadata_and_no_secret_crosses_bridge(self):
        self.backend.credential.side_effect = [{"ok": True, "request_id": "r1", "status": "pending"}, self.saved]
        out = capture.capture_credential(self.fields)
        self.assertTrue(out["stored"])
        self.assertTrue(out["ok"])
        self.mocks[-1].assert_called_once()
        self.assertNotIn("value", json.dumps(out))
        self.assertEqual(self.backend.credential.call_args_list[0].args, ("request", self.fields))

    def test_cancel_and_timeout_do_not_create_locator(self):
        self.backend.credential.side_effect = [{"ok": True, "request_id": "r1", "status": "pending"},
                                                {"ok": False, "status": "cancelled", "error": "credential_entry_cancelled"}]
        self.assertFalse(capture.capture_credential(self.fields)["ok"])
        self.mocks[-1].assert_not_called()
        self.backend.reset_mock()
        self.backend.credential.side_effect = None
        self.backend.credential.return_value = {"ok": True, "request_id": "r1", "status": "pending"}
        self.assertFalse(capture.capture_credential(self.fields, timeout=0)["ok"])
        self.backend.credential.assert_called_with("cancel", {"request_id": "r1"})
        self.mocks[-1].assert_not_called()

    def test_raw_value_rejected_before_native_or_keychain_access(self):
        out = capture.capture_credential({**self.fields, "value": "fixture-only"})
        self.assertFalse(out["ok"])
        self.backend.credential.assert_not_called()
        schema = next(t for t in tools.tool_definitions(groups={"secrets"}) if t["name"] == "secrets_put")
        self.assertNotIn("value", schema["parameters"]["properties"])
        self.assertFalse(schema["parameters"]["additionalProperties"])

    def test_catalog_failure_reports_saved_without_requesting_second_secret(self):
        self.backend.credential.side_effect = [{"ok": True, "request_id": "r1", "status": "pending"}, self.saved]
        self.mocks[-1].side_effect = OSError("fixture")
        out = capture.capture_credential(self.fields)
        self.assertFalse(out["ok"])
        self.assertTrue(out["stored"])
        self.assertEqual(out["service"], self.fields["service"])


class PrivacyAndInjectionTests(unittest.TestCase):
    def test_entry_mutes_and_restores_previous_state_on_failure(self):
        session = SimpleNamespace(_input_muted=True, _stop=threading.Event(), provider=SimpleNamespace(), set_input_muted=Mock())
        with patch.object(session_hub, "_active", session):
            with self.assertRaises(RuntimeError):
                with session_hub.credential_privacy():
                    self.assertTrue(session._credential_capture)
                    self.assertTrue(session.provider._credential_capture)
                    raise RuntimeError("fixture")
        self.assertFalse(session._credential_capture)
        self.assertEqual([c.args for c in session.set_input_muted.call_args_list], [(True,), (True,)])

    def test_overlapping_capture_cannot_unmute_the_owner(self):
        session = SimpleNamespace(_input_muted=False, _stop=threading.Event(), provider=SimpleNamespace(), set_input_muted=Mock())
        with patch.object(session_hub, "_active", session):
            with session_hub.credential_privacy():
                with self.assertRaisesRegex(RuntimeError, "credential_entry_busy"):
                    with session_hub.credential_privacy():
                        self.fail("Second capture entered")
                self.assertTrue(session._credential_capture)
                self.assertTrue(session.provider._credential_capture)
                self.assertEqual(session.set_input_muted.call_count, 1)
        self.assertFalse(session._credential_capture)
        self.assertEqual([c.args for c in session.set_input_muted.call_args_list], [(True,), (False,)])

    def test_mic_audio_cannot_pass_send_guard_during_entry(self):
        session = RealtimeSession.__new__(RealtimeSession)
        session._credential_capture = True
        self.assertFalse(session._send({"type": "input_audio_buffer.append", "audio": "fixture"}))

    def test_unverified_paste_clears_clipboard_and_never_returns_field_text(self):
        result = {"ok": False, "dispatched": True, "verified": False, "text": "fixture-only"}
        with patch.object(tools, "fetch_value", return_value="fixture-only"), \
                patch.object(tools, "get_backend"), patch.object(tools, "clipboard_paste_secret", return_value=result), \
                patch.object(tools, "clear_clipboard") as clear:
            out = tools._run_inject({"label": "Test login"}, "TextEdit", None)
        self.assertFalse(out["ok"])
        self.assertFalse(out["injected"])
        self.assertTrue(out["dispatched"])
        self.assertNotIn("fixture-only", json.dumps(out))
        self.assertGreaterEqual(clear.call_count, 1)

    def test_cancel_is_not_logged_as_failure(self):
        from voice_cua import activity_log
        backend = Mock()
        backend.credential.side_effect = [{"ok": True, "request_id": "r1", "status": "pending"},
                                          {"ok": False, "status": "cancelled"}]
        with patch.object(capture, "get_backend", return_value=backend), patch.object(capture, "island_publish"), \
                patch.object(capture.session_hub, "credential_privacy", side_effect=nullcontext):
            result = capture.capture_credential({"id": "test-login", "label": "Test", "service": "samantha.test"})
        self.assertTrue(result["cancelled"])
        with patch.object(activity_log, "log_event") as logged:
            activity_log.log_tool_result("secrets_put", result)
        self.assertEqual(logged.call_args.args[0], "tool_cancelled")
        self.assertNotIn("error", logged.call_args.kwargs)

    def test_cancellation_does_not_latch_workflow_repair(self):
        cancelled = {"ok": False, "cancelled": True, "error_type": "user_cancelled"}
        with patch.object(tools, "capture_credential", return_value=cancelled), \
                patch.object(tools.workflow_store, "mutation_guard", return_value=None), \
                patch.object(tools.workflow_store, "note_failure") as note, \
                patch.object(tools, "log_tool_start"), patch.object(tools, "log_tool_result"):
            result = tools.dispatch("secrets_put", {})
        self.assertTrue(result["cancelled"])
        note.assert_not_called()

    def test_task_outcomes_validate_metadata_and_keep_values_out(self):
        from voice_cua import activity_log
        with patch.object(activity_log, "log_event") as logged:
            activity_log.log_task_outcome(check_id="browser_first_request", outcome="verified_incomplete",
                                          evidence_class="browser_inventory", run_id="a1234567")
            self.assertEqual(logged.call_args.args[0], "task_outcome")
            with self.assertRaises(ValueError):
                activity_log.log_task_outcome(check_id="free form user data", outcome="failed",
                                              evidence_class="transcript", run_id="a1234567")
            self.assertEqual(logged.call_count, 1)

    def test_platform_secret_listing_uses_inventory_without_reading_values(self):
        with patch("voice_cua.inventory.load_catalog", return_value={"keys": []}), \
                patch.object(tools, "island_publish"):
            result = tools.dispatch("secrets_list", {"platform": "synthetic-test"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "catalog")
        self.assertEqual(result["keys"], [])

    def test_workflows_are_lazy_and_failed_volume_result_is_preserved(self):
        base = {t["name"] for t in tools.tool_definitions()}
        self.assertNotIn("workflow_read", base)
        self.assertIn("workflow_read", {t["name"] for t in tools.tool_definitions(groups={"workflows"})})
        with patch.object(tools, "get_backend") as backend, patch.object(tools, "island_publish"):
            backend.return_value.volume.return_value = {"ok": False, "error_type": "volume_unverified"}
            self.assertFalse(tools.dispatch("system_volume", {"direction": "up"})["ok"])


if __name__ == "__main__":
    unittest.main()
