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
        self.assertEqual(names(), {"cua_state", "cua_act", "more_tools", "show_details", "comet_begin"})

    def test_secrets_group_loads_secrets_and_confirm(self) -> None:
        loaded = names(groups={"secrets"})
        self.assertTrue(set(tools.TOOL_GROUPS["secrets"]) <= loaded)
        self.assertNotIn("more_tools", loaded)  # nothing left to load

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
        with patch("voice_cua.tools.island_confirm", return_value=True) as ask:
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
        for outcome in (False, RuntimeError("island gone")):
            tools.reset_session_state()
            tools.dispatch("cua_act", dict(RISKY))
            with patch("voice_cua.tools.island_confirm", side_effect=[outcome] if isinstance(outcome, Exception) else None,
                       return_value=outcome):
                conf = tools.dispatch("confirm_risky", {})
            self.assertFalse(conf.get("approved"))
            self.assertTrue(tools.dispatch("cua_act", dict(RISKY)).get("needs_confirmation"))
        self.backend.act.assert_not_called()

    def test_approval_does_not_cover_a_different_action(self) -> None:
        tools.dispatch("cua_act", dict(RISKY))
        with patch("voice_cua.tools.island_confirm", return_value=True):
            tools.dispatch("confirm_risky", {})
        other = dict(RISKY, label="Delete")
        self.assertTrue(tools.dispatch("cua_act", other).get("needs_confirmation"))
        self.backend.act.assert_not_called()

    def test_approval_does_not_cover_changed_dispatch_flags(self) -> None:
        tools.dispatch("cua_act", dict(RISKY))
        with patch("voice_cua.tools.island_confirm", return_value=True):
            tools.dispatch("confirm_risky", {})
        for changed in ({"expect": "Draft"}, {"allow_unverified": True}, {"wait": 5}):
            self.assertTrue(tools.dispatch("cua_act", dict(RISKY, **changed)).get("needs_confirmation"), changed)
        self.backend.act.assert_not_called()

    def test_interleaved_refusals_confirm_the_newest(self) -> None:
        other = dict(RISKY, label="Delete")
        tools.dispatch("cua_act", dict(RISKY))
        tools.dispatch("cua_act", other)
        with patch("voice_cua.tools.island_confirm", return_value=True) as ask:
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
        self.assertTrue(gate.confirm(lambda _c, _t: True)["approved"])
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
                patch("voice_cua.tools.keychain_put") as put, \
                patch("voice_cua.tools.comet_act") as cact, \
                patch("voice_cua.tools.island_confirm") as ask:
            for name, args in (
                ("secrets_inject", {"app": "Safari", "label": "gh"}),
                ("secrets_provide", {"action": "clipboard", "label": "gh"}),
                ("secrets_provide", {"action": "inject", "app": "Safari", "label": "gh"}),
                ("secrets_put", {"id": "gh", "label": "gh", "value": "v"}),
                ("comet_act", {"actions": [{"type": "click_text", "text": "Delete account"}]}),
            ):
                out = tools.dispatch(name, args)
                self.assertTrue(out.get("needs_confirmation"), name)
        fetch.assert_not_called()
        put.assert_not_called()
        cact.assert_not_called()
        ask.assert_not_called()

    def test_inject_confirm_copy_names_navigation(self) -> None:
        item = {"label": "gh", "service": "s", "account": "a"}
        nav = [{"op": "click", "label": "Send"}]
        seen: list[str] = []
        with patch("voice_cua.tools._resolve_secret_args", return_value=(item, None)), \
                patch("voice_cua.tools.fetch_value") as fetch, \
                patch("voice_cua.tools.island_confirm", side_effect=lambda _c, text: seen.append(text) or False):
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
        with patch("voice_cua.tools.island_confirm", return_value=True):
            tools.dispatch("confirm_risky", {})
        self.assertTrue(tools.dispatch("cua_act", dict(RISKY, timeout=99)).get("needs_confirmation"))
        self.backend.act.assert_not_called()

    def test_reconnect_clears_approvals(self) -> None:
        tools.dispatch("cua_act", dict(RISKY))
        with patch("voice_cua.tools.island_confirm", return_value=True):
            tools.dispatch("confirm_risky", {})
        tools.reset_session_state()
        self.assertTrue(tools.dispatch("cua_act", dict(RISKY)).get("needs_confirmation"))
        self.backend.act.assert_not_called()


class RetryCapTests(_Fresh):
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
        self.assertEqual(set(props) - {"step_label", "risky"}, set(backend_props))
        self.assertEqual(props["op"]["enum"], backend_props["op"]["enum"])
        self.assertEqual(self.voice["parameters"]["required"], self.backend["required"])
        for key, prop in props.items():
            if key in backend_props and "type" in backend_props[key]:
                self.assertEqual(prop.get("type"), backend_props[key]["type"], key)
        # Every per-step field the backend accepts is described at top level.
        step_fields = set(backend_props["steps"]["items"]["properties"])
        self.assertTrue(step_fields <= set(props))

    def test_at_most_half_the_old_size(self) -> None:
        extras = {"step_label": {"type": "string", "description": "Short human step for island UI"},
                  "risky": {"type": "boolean", "description": "Force confirm_risky before acting"}}
        old_tool = {
            "type": "function", "name": "cua_act", "description": get_tool_schemas()["act"]["description"],
            "parameters": {**self.backend, "properties": {**self.backend["properties"], **extras}},
        }
        old = len(json.dumps(old_tool))
        new = len(json.dumps(self.voice))
        self.assertLessEqual(new, old * 0.5)


if __name__ == "__main__":
    unittest.main()
