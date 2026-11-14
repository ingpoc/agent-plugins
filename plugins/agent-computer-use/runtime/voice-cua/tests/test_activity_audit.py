"""Local activity rows preserve diagnostic task values and redact credentials only."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from voice_cua import activity_log  # noqa: E402


class ActivityAudit(unittest.TestCase):
    def test_tool_rows_carry_arguments_and_failed_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "a.jsonl"
            with patch.dict(os.environ, {"VOICE_CUA_ACTIVITY_LOG": str(log)}):
                with activity_log.tool_context(run_id="1234567890abcdef1234567890abcdef",
                                               step_ref="step-fail", caused_by=["step-request"]):
                    arguments = {"app": "TextEdit", "steps": [{"op": "type_text", "text": "abc"}]}
                    result = {"ok": False, "error_type": "stale_id",
                              "focused_role": "AXTextField", "focused_subrole": "AXStandardTextField",
                              "hint": "Ids changed: retry by label, not element id.",
                              "failure": {"completed_steps": 1, "failed_step": 2}}
                    activity_log.log_tool_start("cua_act", arguments)
                    activity_log.log_tool_result("cua_act", result, app="TextEdit", arguments=arguments)
            start, end = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(start["step_ref"], end["step_ref"])
        self.assertEqual(start["caused_by"], ["step-request"])
        self.assertNotIn("text", start["arguments"]["steps"][0])
        self.assertEqual(end["arguments"]["steps"][0]["text"], "abc")
        self.assertEqual(end["typed_text"], "abc")
        self.assertEqual(end["failed_step"], 2)
        self.assertEqual(end["result"]["failure"]["failed_step"], 2)
        self.assertIn("retry by label", end["hint"])

    def test_contract_fields_are_top_level_and_use_current_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "a.jsonl"
            with patch.dict(os.environ, {"VOICE_CUA_ACTIVITY_LOG": str(log)}):
                with activity_log.tool_context(run_id="1234567890abcdef1234567890abcdef",
                                               step_ref="ledger-step", caused_by=["request-step"],
                                               retry_of_step_ref="ledger-first",
                                               retry_of_run_ref="fedcba0987654321fedcba0987654321"):
                    args = {"operation": "lookup", "need": "preference query sentinel",
                            "situation": "browser choice"}
                    activity_log.log_tool_start("ledger_task", args)
                    activity_log.log_tool_result("ledger_task", {
                        "ok": False, "error_type": "ledger_invalid_argument",
                        "error": "find needs query words",
                    }, operation="lookup", arguments=args, duration_ms=12)
            rows = [json.loads(line) for line in log.read_text().splitlines()]
        start = next(row for row in rows if row["kind"] == "tool_start")
        error = next(row for row in rows if row["kind"] == "tool_error")
        self.assertEqual(start["query_terms"], ["preference query sentinel", "browser choice"])
        self.assertEqual(error["query_terms"], start["query_terms"])
        self.assertEqual(error["invalid_query_terms"], start["query_terms"])
        self.assertNotIn("selected_id", error)
        self.assertEqual(error["retry_of_step_ref"], "ledger-first")
        self.assertEqual(error["retry_of_run_ref"], "fedcba0987654321fedcba0987654321")
        self.assertEqual(error["caused_by"], ["request-step"])
        self.assertEqual(error["duration_ms"], 12)
        self.assertNotIn("retry_of_run_id", error)
        self.assertNotIn("causal_refs", error)

    def test_empty_ledger_lookup_records_none(self) -> None:
        for result in ({"ok": True, "decisions": []}, {"ok": True, "status": "unknown", "decisions": []},
                       {"ok": True, "data": {"matches": []}}):
            rows = []
            with self.subTest(result=result), patch.object(activity_log, "_append_row", side_effect=rows.append):
                activity_log.log_tool_result("ledger_task", result, operation="lookup")
            task = next(row for row in rows if row["kind"] == "tool_result")
            selection = next(row for row in rows if row["kind"] == "ledger_select")
            self.assertEqual(task["ledger_result_status"], "none")
            self.assertEqual(selection["ledger_selection_status"], "none")

    def test_failure_target_facts_are_flat_and_secure_typed_text_is_redacted(self) -> None:
        secure_text = "secure-field-password-sentinel"
        normal_text = "normal typed query sentinel"
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "a.jsonl"
            with patch.dict(os.environ, {"VOICE_CUA_ACTIVITY_LOG": str(log)}):
                with activity_log.tool_context(run_id="1234567890abcdef1234567890abcdef"):
                    secure_args = {"app": "Browser", "steps": [{"op": "type_text",
                                    "role": "AXSecureTextField", "text": secure_text}]}
                    activity_log.log_tool_start("cua_act", secure_args)
                    activity_log.log_tool_result("cua_act", {
                        "ok": False, "error_type": "TargetNotFound",
                        "failure": {"expected_target": {"op": "click", "label": "Save"},
                                    "found_target": {"label": "Save as", "role": "AXButton"},
                                    "observation": {"match_count": 0, "near_miss_count": 2}},
                        "error": f"failed with {secure_text}",
                    }, arguments=secure_args)
                    normal_args = {"app": "Browser", "steps": [{"op": "type_text",
                                    "text": normal_text, "secret": True}]}
                    activity_log.log_tool_start("cua_act", normal_args)
                    observed_args = {"app": "Browser", "steps": [{"op": "type_text",
                                      "label": "SSN", "text": secure_text}]}
                    activity_log.log_tool_start("cua_act", observed_args)
                    activity_log.log_tool_result("cua_act", {
                        "ok": False, "error": f"failed with {secure_text}",
                        "text": '[0] AXWindow "Login"\n[1] AXSecureTextField "SSN"',
                    }, arguments=observed_args)
                rows = [json.loads(line) for line in log.read_text().splitlines()]
        secure_start, secure_error, secret_start, observed_start, observed_error = rows
        self.assertNotIn("typed_text", secure_start)
        self.assertNotIn(secure_text, json.dumps(secure_start))
        self.assertEqual(secure_error["expected_target"], "Save")
        self.assertEqual(secure_error["found_target"], "Save as (AXButton)")
        self.assertEqual(secure_error["match_count"], 0)
        self.assertEqual(secure_error["near_miss_count"], 2)
        self.assertEqual(secure_error["typed_text"], "[redacted]")
        self.assertNotIn(secure_text, json.dumps(secure_error))
        self.assertNotIn("typed_text", secret_start)
        self.assertNotIn(normal_text, json.dumps(secret_start))
        self.assertNotIn("typed_text", observed_start)
        self.assertNotIn(secure_text, json.dumps(observed_start))
        self.assertEqual(observed_error["typed_text"], "[redacted]")
        self.assertNotIn(secure_text, json.dumps(observed_error))

    def test_unknown_typed_text_security_fails_closed_and_known_nonsecure_is_full(self) -> None:
        rows = []
        args = {"app": "Editor", "op": "type_text", "text": "PRIVATE_TYPED_SENTINEL"}
        with patch.object(activity_log, "_append_row", side_effect=rows.append):
            activity_log.log_tool_result("cua_act", {"ok": True}, arguments=args)
            activity_log.log_tool_result("cua_act", {"ok": True, "focused_role": "AXTextField",
                                                        "focused_subrole": "AXStandardTextField"},
                                         arguments=args)
            activity_log.log_tool_result("cua_act", {"ok": True, "focused_role": "AXTextField",
                                                        "focused_subrole": "AXSecureTextField"},
                                         arguments=args)
        unknown, normal, secure = rows
        self.assertEqual((unknown["typed_text"], unknown["typed_text_redacted_reason"],
                          unknown["typed_text_length"]), ("[redacted]", "security_unknown", 22))
        self.assertNotIn("PRIVATE_TYPED_SENTINEL", json.dumps(unknown))
        self.assertEqual(normal["typed_text"], "PRIVATE_TYPED_SENTINEL")
        self.assertNotIn("typed_text_redacted_reason", normal)
        self.assertEqual((secure["typed_text"], secure["typed_text_redacted_reason"]),
                         ("[redacted]", "secure_target"))
        self.assertNotIn("PRIVATE_TYPED_SENTINEL", json.dumps(secure))

    def test_comet_fields_expose_lease_and_only_report_loaded_urls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "a.jsonl"
            with patch.dict(os.environ, {"VOICE_CUA_ACTIVITY_LOG": str(log)}):
                activity_log.log_tool_result("comet_begin", {"ok": True, "lease": "isolated"},
                                             arguments={"url": "https://example.test/requested"})
                activity_log.log_tool_result("comet_act", {
                    "ok": False, "error": "page read timeout",
                    "current_url": "https://example.test/loaded",
                }, arguments={"actions": [{"type": "text"}]})
            begin, page_read = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(begin["lease"], "isolated")
        self.assertEqual(begin["url_gap"], "comet_begin_did_not_report_final_url")
        self.assertNotIn("url", begin)
        self.assertEqual(page_read["url"], "https://example.test/loaded")

    def test_full_task_values_are_kept_and_only_credentials_are_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "a.jsonl"
            with patch.dict(os.environ, {"VOICE_CUA_ACTIVITY_LOG": str(log)}):
                arguments = {
                    "app": "TextEdit", "query": "Ada Lovelace secret garden draft",
                    "url": "https://example.test/search?q=full%20term&api_key=url-secret-sentinel-987654321&next=preserve",
                    "steps": [{"op": "type_text", "text": "Send the report to ada@example.com",
                               "value": "Quarterly report"}],
                    "headers": {"Authorization": "Bearer credential-sentinel-987654321"},
                    "api_key": "api-key-sentinel-987654321",
                }
                result = {"ok": False, "error_type": "target_missing", "focused_role": "AXTextArea",
                          "message": "credential=message-secret-sentinel-987654321",
                          "nested": {"token": "nested-token-sentinel-987654321"},
                          "value": "normal diagnostic value"}
                with activity_log.tool_context(run_id="1234567890abcdef1234567890abcdef",
                                               step_ref="step-content", caused_by=["request-content"]):
                    activity_log.log_tool_start("cua_act", arguments)
                    activity_log.log_tool_result("cua_act", result, app="TextEdit", arguments=arguments)
                with activity_log.tool_context(run_id="1234567890abcdef1234567890abcdef",
                                               step_ref="step-password"):
                    activity_log.log_tool_start("cua_act", {"app": "Browser", "steps": [
                        {"op": "type_text", "label": "Password", "text": "typed-password-sentinel-987654321"}]})
                with activity_log.tool_context(run_id="1234567890abcdef1234567890abcdef",
                                               step_ref="step-secret-tool"):
                    activity_log.log_tool_start("secrets_provide", {
                        "label": "Login", "value": "secret-tool-sentinel-987654321"})
            rows = [json.loads(line) for line in log.read_text().splitlines()]
            start, end = rows[:2]
            raw = log.read_text()
        self.assertEqual(start["arguments"]["query"], "Ada Lovelace secret garden draft")
        self.assertEqual(start["arguments"]["url"],
                         "https://example.test/search?q=full%20term&api_key=[redacted]&next=preserve")
        self.assertNotIn("text", start["arguments"]["steps"][0])
        self.assertNotIn("value", start["arguments"]["steps"][0])
        self.assertEqual(end["arguments"]["steps"][0]["text"], "Send the report to ada@example.com")
        self.assertEqual(end["arguments"]["steps"][0]["value"], "Quarterly report")
        self.assertEqual(end["result"]["value"], "normal diagnostic value")
        for sentinel in ("credential-sentinel-987654321", "api-key-sentinel-987654321",
                         "message-secret-sentinel-987654321", "nested-token-sentinel-987654321",
                         "url-secret-sentinel-987654321", "typed-password-sentinel-987654321",
                         "secret-tool-sentinel-987654321"):
            self.assertNotIn(sentinel, raw)
        self.assertIn("ada@example.com", raw)

    def test_action_failure_records_target_facts_not_ax_tree(self) -> None:
        tree = '[0] AXWindow "Editor"\n' + "\n".join(
            f'[{i}] AXButton "Button {i}"' for i in range(1, 25)
        ) + '\n[25] AXButton "Save draft"'
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "a.jsonl"
            with patch.dict(os.environ, {"VOICE_CUA_ACTIVITY_LOG": str(log)}):
                with activity_log.tool_context(run_id="1234567890abcdef1234567890abcdef",
                                               step_ref="step-state"):
                    arguments = {"app": "TextEdit", "steps": [{"op": "click", "label": "Save"}]}
                    activity_log.log_tool_start("cua_act", arguments)
                    activity_log.log_tool_result("cua_act", {"ok": False, "error_type": "target_missing", "text": tree},
                                                 app="TextEdit", arguments=arguments)
            rows = [json.loads(line) for line in log.read_text().splitlines()]
            raw = log.read_text()
        result = rows[-1]["result"]
        self.assertEqual(result["observation"]["window"], '[0] AXWindow "Editor"')
        self.assertIn('[25] AXButton "Save draft"', result["observation"]["candidates"])
        self.assertEqual((result["observation"]["match_count"],
                          result["observation"]["near_miss_count"]), (0, 1))
        self.assertNotIn("Button 20", raw)

    def test_ledger_result_keeps_record_values_for_local_diagnosis(self) -> None:
        record = {"decision_id": "decision_123", "rationale": "Use project defaults for this repository",
                  "value": "Keep the user-selected output directory", "supersedes": "decision_122"}
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "a.jsonl"
            with patch.dict(os.environ, {"VOICE_CUA_ACTIVITY_LOG": str(log)}):
                with activity_log.tool_context(run_id="1234567890abcdef1234567890abcdef",
                                               step_ref="step-ledger"):
                    activity_log.log_tool_start("ledger_task", {"operation": "lookup", "need": "output directory"})
                    activity_log.log_tool_result("ledger_task", {"ok": True, "status": "found", "record": record},
                                                 operation="lookup", arguments={"need": "output directory"})
            rows = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(rows[-1]["result"]["record"], record)

    def test_comet_result_marks_missing_final_url_instead_of_guessing(self) -> None:
        rows = []
        with patch.object(activity_log, "_append_row", side_effect=rows.append), \
             activity_log.tool_context(run_id="1234567890abcdef1234567890abcdef", step_ref="comet-step"):
            activity_log.log_tool_result("comet_begin", {"ok": True},
                                         arguments={"url": "https://example.test/requested"})
        result = rows[-1]
        self.assertEqual(result["url_gap"], "comet_begin_did_not_report_final_url")
        self.assertNotIn("url", result)

    def test_password_observation_sentinel_is_not_persisted(self) -> None:
        sentinel = "password-observation-sentinel-987654321"
        tree = f'[0] AXWindow "Login"\n[1] AXTextField "Password" value="{sentinel}"'
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "a.jsonl"
            with patch.dict(os.environ, {"VOICE_CUA_ACTIVITY_LOG": str(log)}):
                with activity_log.tool_context(run_id="1234567890abcdef1234567890abcdef",
                                               step_ref="step-password"):
                    arguments = {"app": "Browser", "steps": [{"op": "type_text", "label": "Password",
                                                                   "text": sentinel}]}
                    activity_log.log_tool_start("cua_act", arguments)
                    activity_log.log_tool_result("cua_act", {"ok": False, "error_type": "target_missing",
                                                             "text": tree}, arguments=arguments)
            raw = log.read_text()
        self.assertNotIn(sentinel, raw)

    def test_cancel_and_confirmation_rows_do_not_duplicate_typed_redaction_fields(self) -> None:
        rows = []
        cases = (
            ({"app": "Login", "steps": [{"op": "type_text", "label": "Password",
                                               "text": "secure-sentinel"}]},
             {"ok": False, "cancelled": True, "error_type": "user_cancelled"}, "tool_cancelled"),
            ({"app": "Unknown", "steps": [{"op": "type_text", "text": "unknown-sentinel"}]},
             {"ok": False, "needs_confirmation": True, "error_type": "needs_confirmation"},
             "tool_needs_confirmation"),
        )
        with patch.object(activity_log, "_append_row", side_effect=rows.append), \
             activity_log.tool_context(run_id="1234567890abcdef1234567890abcdef"):
            for args, result, _kind in cases:
                activity_log.log_tool_result("cua_act", result, arguments=args)
        self.assertEqual([row["kind"] for row in rows], [case[2] for case in cases])
        self.assertEqual([row["typed_text"] for row in rows], ["[redacted]", "[redacted]"])
        self.assertEqual([row["typed_text_redacted_reason"] for row in rows],
                         ["secure_target", "security_unknown"])

    def test_dev_tool_rows_log_metadata_only(self) -> None:
        command_secret = "COMMAND_PRIVATE_SENTINEL"
        output_secret = "OUTPUT_PRIVATE_SENTINEL"
        file_secret = "FILE_PRIVATE_SENTINEL"
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "a.jsonl"
            with patch.dict(os.environ, {"VOICE_CUA_ACTIVITY_LOG": str(log)}):
                activity_log.log_tool_start("shell_run", {"command": f"printf {command_secret}", "cwd": "/repo"})
                activity_log.log_tool_result("shell_run", {"ok": True, "exit_code": 0, "duration_ms": 4,
                                                             "output": output_secret, "truncated": False},
                                             arguments={"command": f"printf {command_secret}", "cwd": "/repo"})
                activity_log.log_tool_start("file_edit", {"op": "write", "path": "/repo/token.txt",
                                                            "content": file_secret})
                activity_log.log_tool_result("file_edit", {"ok": True, "path": "token.txt", "bytes": len(file_secret)},
                                             arguments={"op": "write", "path": "/repo/token.txt",
                                                        "content": file_secret})
            raw = log.read_text()
            rows = [json.loads(line) for line in raw.splitlines()]
        for secret in (command_secret, output_secret, file_secret, "/repo/token.txt"):
            self.assertNotIn(secret, raw)
        self.assertEqual(rows[0]["arguments"]["command_head"], "printf")
        self.assertIn("command_hash", rows[0]["arguments"])
        self.assertEqual(rows[2]["arguments"]["path_basename"], "token.txt")


if __name__ == "__main__":
    unittest.main()
