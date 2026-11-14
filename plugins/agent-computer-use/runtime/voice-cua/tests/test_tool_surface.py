"""Tool surface: lazy groups, risk gate (fail closed), cua_act schema + retry cap."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_TMP = tempfile.TemporaryDirectory(prefix="voice-cua-test-")
os.environ.setdefault("VOICE_CUA_ACTIVITY_LOG", str(Path(_TMP.name) / "activity.jsonl"))
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from voice_cua import tools  # noqa: E402
from voice_cua import SYSTEM_INSTRUCTIONS  # noqa: E402
from voice_cua.cua_bridge import get_tool_schemas  # noqa: E402
from voice_cua.risk_gate import APPROVAL_TTL, RiskGate  # noqa: E402
from test_phase1_reliability import make_session  # noqa: E402

RISKY = {"app": "Mail", "op": "click", "label": "Send", "expect": "Sent"}


def names(**kw) -> set[str]:
    return {t["name"] for t in tools.tool_definitions(comet_active=False, **kw)}


class _Fresh(unittest.TestCase):
    def setUp(self) -> None:
        tools.reset_session_state()
        self.backend = MagicMock()
        self.backend.act.return_value = {"ok": True, "verified": True}
        for target, value in (
            ("voice_cua.tools.get_backend", lambda: self.backend),
            ("voice_cua.tools.island_publish", MagicMock()),
            ("voice_cua.tools._schedule_listening_reset", MagicMock()),
        ):
            p = patch(target, value)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(tools.reset_session_state)


class LazyGroupTests(unittest.TestCase):
    def test_base_surface_has_no_secrets_or_confirm(self) -> None:
        self.assertEqual(names(), {"cua_state", "cua_act", "more_tools", "show_details", "comet_begin", "comet_act", "comet_end", "system_volume", "ledger_task"})

    def test_comet_actions_ride_with_comet_begin(self) -> None:
        idle = {tool["name"] for tool in tools.tool_definitions(comet_active=False)}
        active = {tool["name"] for tool in tools.tool_definitions(comet_active=True)}
        self.assertIn("comet_begin", idle)
        self.assertIn("comet_act", idle)
        self.assertIn("comet_end", idle)
        self.assertTrue({"comet_begin", "comet_act", "comet_end"}.issubset(active))
        begin = next(tool for tool in tools.tool_definitions(comet_active=False) if tool["name"] == "comet_begin")
        self.assertIn("unless the user asks to close it", begin["description"])

    def test_comet_schema_distinguishes_page_text_from_compact_context(self) -> None:
        active = {tool["name"]: tool for tool in tools.tool_definitions(comet_active=True)}
        description = active["comet_act"]["description"]
        self.assertIn("text for visible page text", description)
        self.assertIn("page_context for compact orientation", description)

    def test_comet_act_without_lease_has_no_action_side_effects(self) -> None:
        with patch.object(tools, "comet_is_active", return_value=False), \
                patch.object(tools, "comet_act") as act, \
                patch.object(tools, "island_publish") as publish, \
                patch.object(tools.GATE, "check") as check, \
                patch.object(tools, "log_event") as log:
            out = tools._comet_act({"actions": [{"type": "click_text", "text": "Continue"}]})
        self.assertFalse(out["ok"])
        self.assertIn("comet_begin", out["error"])
        act.assert_not_called()
        publish.assert_not_called()
        check.assert_not_called()
        log.assert_not_called()

    def test_secrets_group_loads_secrets_and_confirm(self) -> None:
        loaded = names(groups={"secrets"})
        self.assertTrue(set(tools.TOOL_GROUPS["secrets"]) <= loaded)
        self.assertIn("more_tools", loaded)  # workflows remain available
        self.assertNotIn("more_tools", names(groups={"secrets", "workflows", "ledger", "dev"}))

    def test_workflow_group_exposes_create_update_and_delete(self) -> None:
        loaded = names(groups={"workflows"})
        self.assertTrue(set(tools.TOOL_GROUPS["workflows"]) <= loaded)

    def test_dev_tools_load_lazily_from_more_tools(self) -> None:
        self.assertTrue({"shell_run", "shell_job", "file_edit"}.isdisjoint(names()))
        loaded = names(groups={"dev"})
        self.assertTrue(set(tools.TOOL_GROUPS["dev"]) <= loaded)
        self.assertEqual(tools.dispatch("more_tools", {"group": "dev"}),
                         {"ok": True, "group": "dev", "loaded": ["shell_run", "shell_job", "file_edit"]})
        error = tools.dispatch("more_tools", {"group": "unknown"})
        self.assertIn("dev", error["hint"])
        self.assertIn("load more_tools {group: dev}", SYSTEM_INSTRUCTIONS)

    def test_workflow_delete_dispatches_to_confined_store(self) -> None:
        with patch("voice_cua.tools.workflow_store.delete_workflow", return_value={"ok": True}) as delete:
            self.assertTrue(tools.dispatch("workflow_delete", {"slug": "stale", "expected_revision": "rev"})["ok"])
        delete.assert_called_once_with({"slug": "stale", "expected_revision": "rev"})

    def test_confirm_group_exposes_confirm_risky_only(self) -> None:
        self.assertEqual(names(groups={"confirm"}) - names(), {"confirm_risky"})

    def test_session_loads_confirm_risky_when_a_risky_action_is_refused(self) -> None:
        session = make_session()
        session._sync_tool_groups("cua_act", {"ok": False, "needs_confirmation": True})
        update = [e for e in session.ws.events if e.get("type") == "session.update"][-1]
        self.assertIn("confirm_risky", {t["name"] for t in update["session"]["tools"]})

    def test_more_tools_loads_group_into_session(self) -> None:
        session = make_session()
        result = tools.dispatch("more_tools", {"group": "secrets"})
        session._sync_tool_groups("more_tools", result)
        update = [e for e in session.ws.events if e.get("type") == "session.update"][-1]
        self.assertIn("secrets_inject", {t["name"] for t in update["session"]["tools"]})
        session._prepare_reconnect()
        self.assertEqual(session._tool_groups, set())


class RiskGateTests(_Fresh):
    def test_risky_cua_act_never_runs_unconfirmed(self) -> None:
        with patch("voice_cua.tools.island_confirm") as ask:
            out = tools.dispatch("cua_act", dict(RISKY))
        self.assertFalse(out["ok"])
        self.assertTrue(out["needs_confirmation"])
        self.assertIn("confirm_risky", out["hint"])
        self.backend.act.assert_not_called()
        ask.assert_not_called()  # the gate refuses; it never self-approves

    def test_confirm_then_exact_retry_runs_once(self) -> None:
        tools.dispatch("cua_act", dict(RISKY))
        with patch("voice_cua.tools.island_confirm", return_value="approved") as ask:
            conf = tools.dispatch("confirm_risky", {"prompt": "ignored model text"})
        self.assertTrue(conf["approved"])
        self.assertIn("Mail", ask.call_args.args[1])  # server text, not the model's
        self.assertNotIn("ignored", ask.call_args.args[1])
        self.assertTrue(tools.dispatch("cua_act", dict(RISKY))["ok"])
        self.assertEqual(self.backend.act.call_count, 1)
        again = tools.dispatch("cua_act", dict(RISKY))  # approval is single-use
        self.assertTrue(again.get("needs_confirmation"))
        self.assertEqual(self.backend.act.call_count, 1)

    def test_denied_or_failed_confirmation_fails_closed(self) -> None:
        for outcome in ("denied", RuntimeError("island gone")):
            tools.reset_session_state()
            tools.dispatch("cua_act", dict(RISKY))
            with patch("voice_cua.tools.island_confirm", side_effect=[outcome] if isinstance(outcome, Exception) else None,
                       return_value=outcome):
                conf = tools.dispatch("confirm_risky", {})
            self.assertFalse(conf.get("approved"))
            self.assertTrue(tools.dispatch("cua_act", dict(RISKY)).get("needs_confirmation"))
        self.backend.act.assert_not_called()

    def test_confirmation_timeout_is_not_reported_as_a_user_denial(self) -> None:
        tools.dispatch("cua_act", dict(RISKY))
        events = []
        with patch("voice_cua.tools.island_confirm", return_value="timed_out"), \
                patch("voice_cua.tools.log_event", lambda kind, **fields: events.append((kind, fields))):
            result = tools.dispatch("confirm_risky", {})
        self.assertFalse(result["approved"])
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["error_type"], "confirm_timed_out")
        self.assertIn("No answer before the confirm timed out", result["hint"])
        answer = next(fields for kind, fields in events if kind == "confirm_answer")
        self.assertEqual((answer["outcome"], answer["reason"], answer["approved"], answer["timed_out"]),
                         ("timed_out", "timed_out", False, True))
        self.assertTrue(tools.dispatch("cua_act", dict(RISKY)).get("needs_confirmation"))

    def test_approval_does_not_cover_a_different_action(self) -> None:
        tools.dispatch("cua_act", dict(RISKY))
        with patch("voice_cua.tools.island_confirm", return_value="approved"):
            tools.dispatch("confirm_risky", {})
        other = dict(RISKY, label="Delete")
        self.assertTrue(tools.dispatch("cua_act", other).get("needs_confirmation"))
        self.backend.act.assert_not_called()

    def test_approval_does_not_cover_changed_dispatch_flags(self) -> None:
        tools.dispatch("cua_act", dict(RISKY))
        with patch("voice_cua.tools.island_confirm", return_value="approved"):
            tools.dispatch("confirm_risky", {})
        for changed in ({"expect": "Draft"}, {"allow_unverified": True}, {"wait": 5}):
            self.assertTrue(tools.dispatch("cua_act", dict(RISKY, **changed)).get("needs_confirmation"), changed)
        self.backend.act.assert_not_called()

    def test_interleaved_refusals_confirm_the_newest(self) -> None:
        other = dict(RISKY, label="Delete")
        tools.dispatch("cua_act", dict(RISKY))
        tools.dispatch("cua_act", other)
        with patch("voice_cua.tools.island_confirm", return_value="approved") as ask:
            tools.dispatch("confirm_risky", {})
        self.assertIn("Delete", ask.call_args.args[1])
        self.assertTrue(tools.dispatch("cua_act", dict(RISKY)).get("needs_confirmation"))
        self.assertTrue(tools.dispatch("cua_act", other)["ok"])
        self.assertEqual(self.backend.act.call_count, 1)

    def test_confirm_without_pending_never_prompts(self) -> None:
        with patch("voice_cua.tools.island_confirm") as ask:
            out = tools.dispatch("confirm_risky", {"prompt": "Delete everything?"})
        ask.assert_not_called()
        self.assertFalse(out.get("approved"))

    def test_approval_expires(self) -> None:
        now = [100.0]
        gate = RiskGate(clock=lambda: now[0])
        self.assertIsNotNone(gate.check("cua_act", RISKY, "x"))
        self.assertTrue(gate.confirm(lambda _c, _t: "approved")["approved"])
        now[0] += APPROVAL_TTL + 1
        self.assertIsNotNone(gate.check("cua_act", RISKY, "x"))

    def test_gate_error_refuses(self) -> None:
        with patch("voice_cua.risk_gate.fingerprint", side_effect=RuntimeError("boom")):
            out = tools.dispatch("cua_act", dict(RISKY))
        self.assertTrue(out.get("needs_confirmation"))
        self.backend.act.assert_not_called()

    def test_risky_flag_forces_gate(self) -> None:
        out = tools.dispatch("cua_act", {"app": "Notes", "op": "click", "label": "OK", "risky": True})
        self.assertTrue(out.get("needs_confirmation"))
        self.backend.act.assert_not_called()

    def test_secret_and_comet_paths_are_gated(self) -> None:
        item = {"label": "gh", "service": "s", "account": "a"}
        with patch("voice_cua.tools._resolve_secret_args", return_value=(item, None)), \
                patch("voice_cua.tools.fetch_value") as fetch, \
                patch("voice_cua.tools.comet_act") as cact, \
                patch("voice_cua.tools.comet_is_active", return_value=True), \
                patch("voice_cua.tools.island_confirm") as ask:
            for name, args in (
                ("secrets_inject", {"app": "Safari", "label": "gh"}),
                ("secrets_provide", {"action": "clipboard", "label": "gh"}),
                ("secrets_provide", {"action": "inject", "app": "Safari", "label": "gh"}),
                ("comet_act", {"actions": [{"type": "click_text", "text": "Delete account"}]}),
            ):
                out = tools.dispatch(name, args)
                self.assertTrue(out.get("needs_confirmation"), name)
        fetch.assert_not_called()
        cact.assert_not_called()
        ask.assert_not_called()

    def test_inject_confirm_copy_names_navigation(self) -> None:
        item = {"label": "gh", "service": "s", "account": "a"}
        nav = [{"op": "click", "label": "Send"}]
        seen: list[str] = []
        with patch("voice_cua.tools._resolve_secret_args", return_value=(item, None)), \
                patch("voice_cua.tools.fetch_value") as fetch, \
                patch("voice_cua.tools.island_confirm", side_effect=lambda _c, text: seen.append(text) or "denied"):
            for name, args in (
                ("secrets_inject", {"app": "Mail", "label": "gh", "navigate_steps": nav}),
                ("secrets_provide", {"action": "inject", "app": "Mail", "label": "gh", "navigate_steps": nav}),
            ):
                self.assertTrue(tools.dispatch(name, args).get("needs_confirmation"), name)
                tools.dispatch("confirm_risky", {})
                self.assertTrue(tools.dispatch(name, args).get("needs_confirmation"), name)
        self.assertEqual(len(seen), 2)
        for text in seen:
            self.assertIn("First: click", text)
        fetch.assert_not_called()
        self.backend.act.assert_not_called()

    def test_approval_binds_timeout_and_screenshot(self) -> None:
        tools.dispatch("cua_act", dict(RISKY))
        with patch("voice_cua.tools.island_confirm", return_value="approved"):
            tools.dispatch("confirm_risky", {})
        self.assertTrue(tools.dispatch("cua_act", dict(RISKY, timeout=99)).get("needs_confirmation"))
        self.backend.act.assert_not_called()

    def test_reconnect_clears_approvals(self) -> None:
        tools.dispatch("cua_act", dict(RISKY))
        with patch("voice_cua.tools.island_confirm", return_value="approved"):
            tools.dispatch("confirm_risky", {})
        tools.reset_session_state()
        self.assertTrue(tools.dispatch("cua_act", dict(RISKY)).get("needs_confirmation"))
        self.backend.act.assert_not_called()


class RetryCapTests(_Fresh):
    def test_app_not_running_is_terminal_for_the_target(self) -> None:
        self.backend.act.return_value = {"ok": False, "error_type": "app_not_running", "error": "App is not running"}
        args = {"app": "com.example.App", "op": "type_text", "text": "hello", "expect": "hello"}
        first = tools.dispatch("cua_act", args)
        second = tools.dispatch("cua_act", args)
        self.assertEqual(first["error_type"], "app_not_running")
        self.assertFalse(first["retryable"])
        self.assertEqual(second["error_type"], "app_not_running")
        self.assertIn("application name", second["hint"])
        self.backend.act.assert_called_once()

    def test_ledger_signal_is_telemetry_only_for_native_dispatch(self) -> None:
        args = {"app": "Notes", "op": "click", "label": "Preference",
                "ledger_decision_id": "dec_123456"}
        tools.dispatch("cua_act", args)
        self.assertNotIn("ledger_decision_id", self.backend.act.call_args.args[1])

    def test_retry_rows_have_unique_step_refs_and_explicit_parent(self) -> None:
        from voice_cua.activity_log import tool_context

        self.backend.act.return_value = {"ok": False, "error_type": "action_failed", "error": "no match"}
        args = {"app": "Notes", "op": "click", "label": "Save", "expect": "saved"}
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "activity.jsonl"
            with patch.dict(os.environ, {"VOICE_CUA_ACTIVITY_LOG": str(log)}):
                for index, expect in enumerate(("saved", "saved elsewhere", "saved"), start=1):
                    with tool_context(run_id="1234567890abcdef1234567890abcdef",
                                      step_ref=f"attempt-{index}",
                                      caused_by=["response-step"]):
                        tools.dispatch("cua_act", dict(args, expect=expect))
                starts = [json.loads(line) for line in log.read_text().splitlines()
                          if json.loads(line).get("kind") == "tool_start"]
        self.assertEqual([row["step_ref"] for row in starts], ["attempt-1", "attempt-2", "attempt-3"])
        self.assertNotIn("retry_of_step_ref", starts[0])
        self.assertEqual(starts[1]["retry_of_step_ref"], "attempt-1")
        self.assertEqual(starts[2]["retry_of_step_ref"], "attempt-2")
        self.assertTrue(all(row["run_id"] == "1234567890abcdef1234567890abcdef" for row in starts))

    def test_failure_gets_hint_and_retryable(self) -> None:
        self.backend.act.return_value = {"ok": False, "error_type": "target_missing", "error": "no match"}
        out = tools.dispatch("cua_act", {"app": "Notes", "op": "click", "label": "Foo", "expect": "x"})
        self.assertTrue(out["retryable"])
        self.assertLessEqual(len(out["hint"]), 100)
        self.assertEqual(out["tries"], 1)

    def test_two_tries_per_step_then_refused_without_running(self) -> None:
        self.backend.act.return_value = {"ok": False, "error_type": "action_failed", "error": "x"}
        args = {"app": "Notes", "op": "click", "label": "Foo", "expect": "x"}
        first, second = tools.dispatch("cua_act", dict(args)), tools.dispatch("cua_act", dict(args, expect="y"))
        self.assertTrue(first["retryable"])
        self.assertFalse(second["retryable"])
        third = tools.dispatch("cua_act", dict(args))
        self.assertEqual(third["error_type"], "retry_cap")
        self.assertEqual(self.backend.act.call_count, 2)
        # A different step is a fresh budget.
        tools.dispatch("cua_act", dict(args, label="Bar"))
        self.assertEqual(self.backend.act.call_count, 3)

    def test_unverified_dispatch_is_not_retryable(self) -> None:
        self.backend.act.return_value = {"ok": False, "error_type": "expect_unverified"}
        out = tools.dispatch("cua_act", {"app": "Notes", "op": "type_text", "text": "hi", "expect": "hi"})
        self.assertFalse(out["retryable"])

    def test_dispatch_only_does_not_retry_a_completed_action(self) -> None:
        self.backend.act.return_value = {"ok": False, "dispatched": True, "app_running": False, "error_type": "allow_unverified"}
        args = {"app": "Calculator", "op": "key", "key": "cmd+q", "allow_unverified": True}
        out = tools.dispatch("cua_act", args)
        self.assertFalse(out["retryable"])
        self.assertIn("do not repeat", out["hint"])
        self.assertIs(out["app_running"], False)
        self.assertEqual(tools.dispatch("cua_act", args)["error_type"], "retry_cap")
        self.backend.act.assert_called_once()

    def test_unverified_shortcut_is_not_dispatched_twice_with_aliases(self) -> None:
        self.backend.act.return_value = {"ok": False, "dispatched": True, "error_type": "expect_unverified"}
        first = {"app": "Finder", "steps": [{"op": "key", "key": "CMD+SHIFT+5"}], "expect": "recording toolbar"}
        second = {"app": "Finder", "steps": [{"op": "key", "key": "shift+command+5"}], "expect": "toolbar"}
        self.assertFalse(tools.dispatch("cua_act", first)["retryable"])
        self.assertEqual(tools.dispatch("cua_act", second)["error_type"], "retry_cap")
        self.backend.act.assert_called_once()

    def test_new_user_request_can_retry_unverified_action(self) -> None:
        self.backend.act.return_value = {"ok": False, "error_type": "expect_unverified"}
        args = {"app": "Finder", "op": "key", "key": "CMD+SHIFT+5", "expect": "toolbar"}
        tools.dispatch("cua_act", args)
        self.assertEqual(tools.dispatch("cua_act", args)["error_type"], "retry_cap")
        tools.note_user_turn("Please try again")
        tools.dispatch("cua_act", args)
        self.assertEqual(self.backend.act.call_count, 2)

    def test_success_resets_budget(self) -> None:
        args = {"app": "Notes", "op": "click", "label": "Foo", "expect": "x"}
        self.backend.act.return_value = {"ok": False, "error_type": "action_failed"}
        tools.dispatch("cua_act", dict(args))
        self.backend.act.return_value = {"ok": True}
        tools.dispatch("cua_act", dict(args))
        self.backend.act.return_value = {"ok": False, "error_type": "action_failed"}
        self.assertTrue(tools.dispatch("cua_act", dict(args))["retryable"])


class CompactActSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = get_tool_schemas()["act"]["inputSchema"]
        self.voice = next(t for t in tools.tool_definitions(comet_active=False) if t["name"] == "cua_act")

    def test_same_fields_ops_and_required_as_backend(self) -> None:
        props = self.voice["parameters"]["properties"]
        backend_props = self.backend["properties"]
        self.assertEqual(set(props) - {"step_label", "risky", "ledger_decision_id"}, set(backend_props))
        self.assertEqual(props["ledger_decision_id"]["type"], "string")
        self.assertEqual(props["op"]["enum"], backend_props["op"]["enum"])
        self.assertEqual(self.voice["parameters"]["required"], self.backend["required"])
        for key, prop in props.items():
            if key in backend_props and "type" in backend_props[key]:
                self.assertEqual(prop.get("type"), backend_props[key]["type"], key)
        # Every per-step field the backend accepts is described at top level.
        step_fields = set(backend_props["steps"]["items"]["properties"])
        self.assertTrue(step_fields <= set(props))
        comet = next(t for t in tools.tool_definitions(comet_active=True) if t["name"] == "comet_act")
        self.assertEqual(comet["parameters"]["properties"]["ledger_decision_id"]["type"], "string")

    def test_under_55_percent_of_the_old_size(self) -> None:
        extras = {"step_label": {"type": "string", "description": "Short human step for island UI"},
                  "risky": {"type": "boolean", "description": "Force confirm_risky before acting"}}
        old_tool = {
            "type": "function", "name": "cua_act", "description": get_tool_schemas()["act"]["description"],
            "parameters": {**self.backend, "properties": {**self.backend["properties"], **extras}},
        }
        old = len(json.dumps(old_tool))
        new = len(json.dumps(self.voice))
        self.assertLessEqual(new, old * 0.55)


if __name__ == "__main__":
    unittest.main()


class CometActArgumentTests(unittest.TestCase):
    def test_goto_url_aliases_are_normalized(self) -> None:
        from voice_cua.tools import _normalize_comet_action as norm
        self.assertEqual(norm({"type": "goto", "href": "https://www.amazon.in/s?k=x"}),
                         {"type": "goto", "url": "https://www.amazon.in/s?k=x"})
        self.assertEqual(norm({"type": "navigate", "value": "https://a.example/"})["url"], "https://a.example/")
        self.assertEqual(norm({"type": "goto", "url": "www.amazon.in/cart"})["url"], "https://www.amazon.in/cart")
        self.assertEqual(norm({"type": "click_text", "text": "Add to cart"}), {"type": "click_text", "text": "Add to cart"})

    def test_goto_without_url_fails_fast(self) -> None:
        from unittest.mock import patch
        from voice_cua import tools
        with patch.object(tools, "comet_is_active", return_value=True), patch.object(tools, "comet_act") as act:
            out = tools._comet_act({"actions": [{"type": "goto"}]})
        act.assert_not_called()
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "bad_arguments")

    def test_comet_error_is_lifted_to_top_level(self) -> None:
        from unittest.mock import patch
        from voice_cua import comet_bridge
        lease = comet_bridge.CometLease()
        lease._python, lease._controller, lease._workdir = "py", "ctl", "/tmp/x"
        reply = {"event": "run", "response": {"success": False, "error": "Host access is restricted by policy.",
                                              "error_code": "BROWSER_ACTION_FAILED"}}
        with patch.object(comet_bridge, "_run_json", return_value=reply):
            out = lease._send_locked([{"type": "click_text", "text": "Add to cart"}], 5)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "Host access is restricted by policy.")
        self.assertEqual(out["error_code"], "BROWSER_ACTION_FAILED")
