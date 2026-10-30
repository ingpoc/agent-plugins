"""v2 retrieval: text-first ranking with a relevance floor, user_decision first, compact traces."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from context_ledger.contracts import MAX_FIND_BYTES, LedgerError, canonical_json, ok, tool_content  # noqa: E402
from context_ledger.store import Store, init_ledger  # noqa: E402
os.environ["LEDGER_AB_DATE"] = "2026-10-17"  # pre-A/B-window day: find on; A/B tests pin their own days


def _u() -> str:
    return str(uuid.uuid4())


EVIDENCE = [{"ref": "session://tier-test", "revision": None, "sha256": None, "state": "unverified"}]


def _legacy(summary: str, rationale: str = "legacy reason", entities=None, applicability="any") -> dict:
    return {
        "kind": "decision", "summary": summary, "situation": summary, "action": summary,
        "rationale": rationale, "constraints": [], "applicability": applicability,
        "entities": entities or ["legacy"], "scope": "fleet", "evidence": [], "sensitivity": "model_safe",
        "approval": {"state": "none", "authority_ref": None},
    }


class V2Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name) / "data"
        self.data.mkdir(mode=0o700)
        init_ledger(self.data, None, "v2-test", "tester")
        self.store = Store(self.data, actor_channel="agent")
        self._quiet = contextlib.redirect_stderr(io.StringIO())
        self.log = self._quiet.__enter__()

    def tearDown(self) -> None:
        self._quiet.__exit__(None, None, None)
        self.store.close()
        self._tmp.cleanup()

    def legacy(self, summary: str, **kw) -> str:
        return self.store.record(
            {"request_id": _u(), "occurred_at": None, "body": _legacy(summary, **kw),
             "outcome": {"status": "pending", "note": "", "evidence": []}},
            mcp=True,
        )["decision_id"]

    def save(self, **kw) -> str:
        args = {"request_id": _u(), "type": "agent_conclusion", "criticality": 3, "actor": "test-bot",
                "entities": ["codex-cli"], "scope": "fleet", **kw}
        return self.store.save(args, mcp=True)["decision_id"]

    def find(self, query: str, **kw) -> dict:
        return self.store.find({"query": query, **kw}, mcp=True)

    def ids(self, query: str, **kw) -> list[str]:
        return [m["decision_id"] for m in self.find(query, **kw)["matches"]]


class RankingThresholdTests(V2Base):
    def setUp(self) -> None:
        super().setUp()
        # A high-confidence, success record about something else entirely (the other-project problem).
        self.offtopic = self.legacy(
            "The service forge is a hosted git origin; CI runs on a Mini reporter",
            rationale="Owner decision for the other project's merges",
            entities=["open-resource-network"],
        )
        for i in range(4):
            self.legacy(f"Unrelated fixture number {i} about travel quotes and fares")
        self.codex = self.save(
            decision="Mini Codex login rejects the pinned model; use an API-key login",
            why="The account cannot use that model, so use a key login",
            evidence=["gpt-6.1-sol ChatGPT"],
        )

    def test_off_topic_query_returns_nothing(self) -> None:
        for q in ("FPL captain pick for this gameweek", "weekly digest of new AI model releases",
                  "smallcase rebalance sell a stock that dropped"):
            self.assertEqual(self.find(q)["matches"], [], q)

    def test_one_shared_common_word_is_below_the_floor(self) -> None:
        # "Mini" appears in both records, but one generic word is not relevance.
        self.assertNotIn(self.offtopic, self.ids("Mini fan noise is loud tonight"))

    def test_natural_problem_query_finds_the_record_first(self) -> None:
        got = self.find("codex login rejects the pinned model account")
        self.assertEqual(got["matches"][0]["decision_id"], self.codex)
        self.assertIn("key login", got["matches"][0]["why"])

    def test_confidence_never_lifts_an_off_topic_record(self) -> None:
        rev = self.store.get({"decision_id": self.offtopic}, mcp=True)["record"]["revision"]
        for _ in range(3):
            task = _u()
            applied = self.store.append_event(
                {"request_id": _u(), "decision_id": self.offtopic, "expected_revision": rev, "occurred_at": None,
                 "event_type": "applied", "payload": {"task_id": task, "decision_ids": [self.offtopic]}}, mcp=True)
            rev += 1
            self.store.append_event(
                {"request_id": _u(), "decision_id": self.offtopic, "expected_revision": rev, "occurred_at": None,
                 "event_type": "outcome", "payload": {"status": "success", "note": "ok", "evidence": [],
                                                      "task_id": task, "applied_event_id": applied["event_id"],
                                                      "helpful": True}},
                mcp=True)
            rev += 1
        got = self.store.get({"decision_id": self.offtopic}, mcp=True)["record"]
        self.assertEqual(got["confidence"]["score"], 1.0)
        self.assertEqual(self.ids("codex login rejects the pinned model"), [self.codex])

    def test_hourly_check_in_outcomes_do_not_count(self) -> None:
        rev = self.store.get({"decision_id": self.offtopic}, mcp=True)["record"]["revision"]
        for hour in range(3):
            self.store.append_event(
                {"request_id": _u(), "decision_id": self.offtopic, "expected_revision": rev, "occurred_at": None,
                 "event_type": "outcome", "payload": {"status": "success", "note": f"Hourly {hour}:00 check-in",
                                                      "evidence": [], "task_id": _u()}}, mcp=True)
            rev += 1
        conf = self.store.get({"decision_id": self.offtopic}, mcp=True)["record"]["confidence"]
        self.assertEqual(conf["sample_count"], 0)

    def test_entities_and_constraints_are_optional(self) -> None:
        self.assertEqual(self.find("codex account key login")["matches"][0]["decision_id"], self.codex)

    def test_stop_words_only_query_is_a_named_error(self) -> None:
        with self.assertRaises(LedgerError) as ctx:
            self.find("how do I do it")
        self.assertIn("query words", ctx.exception.message)


class UserDecisionRankingTests(V2Base):
    def test_self_reported_user_decision_does_not_outrank(self) -> None:
        guru = self.save(type="user_decision", decision="Kite reads for ACCT7 go through Mini Comet",
                         why="Guru standing rule: never the Kite MCP for funds or holdings",
                         entities=["kite"], criticality=None, approver="guru")
        agent = self.save(decision="Kite reads for ACCT7 go through Mini Comet",
                          why="The Kite MCP needs authorize links on every session",
                          entities=["kite"], criticality=5)
        rec = self.store.get({"decision_id": guru}, mcp=True)["record"]
        self.assertEqual(rec["body"]["approval"]["state"], "self_reported")
        self.assertEqual(self.ids("kite reads for ACCT7 through comet")[:2], [agent, guru])

    def test_owner_attested_user_decision_outranks_on_equal_match(self) -> None:
        guru = self.save(type="user_decision", decision="Kite reads for ACCT7 go through Mini Comet",
                         why="Guru standing rule: never the Kite MCP for funds or holdings",
                         entities=["kite"], criticality=None, approver="guru")
        rev = self.store.get({"decision_id": guru}, mcp=False)["record"]["revision"]
        self.store.attest(guru, rev, _u(), "guru", "owner attested")
        self.save(decision="Kite reads for ACCT7 go through Mini Comet",
                  why="The Kite MCP needs authorize links on every session",
                  entities=["kite"], criticality=5)
        rec = self.store.get({"decision_id": guru}, mcp=False)["record"]
        self.assertEqual(rec["body"]["approval"]["state"], "owner_attested")
        self.assertEqual(self.ids("kite reads for ACCT7 through comet")[0], guru)

    def test_user_decision_below_the_floor_is_not_returned(self) -> None:
        self.save(type="user_decision", decision="Kite reads for ACCT7 go through Mini Comet",
                  why="Guru standing rule for Zerodha", entities=["kite"], approver="guru")
        self.assertEqual(self.find("IRCTC tatkal captcha keeps failing")["matches"], [])

    def test_hit_is_compact_and_has_no_duplicate_fields(self) -> None:
        self.save(type="user_decision", decision="Private X items use the signed-in Comet path",
                  why="Guru note: bookmarks redirect unless signed in; public reads use x connector",
                  entities=["x.com"], rule="x-read-path", exception="private items", approver="guru")
        hit = self.find("x bookmarks redirect")["matches"][0]
        self.assertEqual(hit["type"], "user_decision")
        self.assertEqual(hit["override_by"], "guru")
        self.assertNotIn("summary", hit)
        self.assertNotIn("applicability", hit)
        values = [v for k, v in hit.items() if isinstance(v, str) and k != "tier"]  # tier user_decision repeats type by spec
        self.assertEqual(len(values), len(set(values)), hit)


class TraceLimitTests(V2Base):
    def assert_rejected(self, needle: str, **kw) -> None:
        with self.assertRaises(LedgerError) as ctx:
            self.save(**kw)
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")
        self.assertIn(needle, ctx.exception.message)
        self.assertIn("nothing was saved", ctx.exception.message)
        self.assertEqual(self.store.open().execute("select count(*) from events").fetchone()[0], 0)

    def test_decision_over_15_words_is_rejected_not_trimmed(self) -> None:
        self.assert_rejected("decision is 16 words; max 15", decision=" ".join(["word"] * 16), why="short why")

    def test_why_over_20_words_is_rejected(self) -> None:
        self.assert_rejected("why is 21 words; max 20", decision="short decision", why=" ".join(["w"] * 21))

    def test_multi_line_fields_are_rejected(self) -> None:
        self.assert_rejected("one line", decision="first line\nsecond line", why="short why")
        self.assert_rejected("one line", decision="short", why="why\nmore")

    def test_why_is_required(self) -> None:
        self.assert_rejected("why is required", decision="short decision", why="")

    def test_agent_conclusion_needs_criticality(self) -> None:
        self.assert_rejected("criticality", decision="short decision", why="short why", criticality=None)

    def test_user_decision_needs_guru_as_approver(self) -> None:
        self.assert_rejected("approver guru", type="user_decision", decision="short", why="short why",
                             approver="smm-bot")

    def test_exactly_at_the_limits_is_saved(self) -> None:
        did = self.save(decision=" ".join(["w"] * 15), why=" ".join(["y"] * 20))
        rec = self.store.get({"decision_id": did}, mcp=True)["record"]
        self.assertEqual(rec["body"]["trace"]["criticality"], 3)
        self.assertEqual(rec["provenance"]["actor_id"], "test-bot")

    def test_precedent_must_exist(self) -> None:
        self.assert_rejected("precedent", decision="short decision", why="short why", precedent_ids=[_u()])


class LinksAndSignalsTests(V2Base):
    def test_internal_access_is_persisted_without_stderr(self) -> None:
        did = self.save(decision="Keep internal reads quiet", why="Preserve audit without prompt noise")
        self.log.seek(0)
        self.log.truncate(0)
        self.store.get({"decision_id": did, "actor": "ledger-internal"}, mcp=True)
        self.assertEqual(self.log.getvalue(), "")
        row = self.store.open().execute("select tool, caller, ids from access_log").fetchone()
        self.assertEqual((row["tool"], row["caller"]), ("get", "ledger-internal"))
        self.assertEqual(json.loads(row["ids"]), [did])
        self.store.get({"decision_id": did, "actor": "public-helper"}, mcp=True)
        self.assertIn('"caller":"public-helper"', self.log.getvalue())

    def test_find_surfaces_linked_precedent_once(self) -> None:
        base = self.save(decision="Mini Codex login rejects the pinned model",
                         why="The account cannot use that pinned model, so keep a fallback reviewer",
                         entities=["codex-cli"])
        ud = self.save(type="user_decision", decision="Fallback reviewer covers a Codex login reject",
                       why="because the pinned model login needs a fallback reviewer",
                       entities=["codex-cli"], approver="guru", precedent_ids=[base])
        got = self.find("codex login rejects the pinned model fallback reviewer", limit=1)
        self.assertEqual(got["matches"][0]["decision_id"], ud)
        self.assertEqual(got["matches"][0]["related"], [{"id": base, "decision": "Mini Codex login rejects the pinned model"}])

    def test_rank_tie_follows_event_order_not_the_projection_row(self) -> None:
        """updated_at has millisecond resolution, so two records can tie all the way down to the
        last rank key. That order has to come from event append order: a projection row is deleted
        and re-inserted by maintenance, and by another record's conflict or supersession, so its
        row is not decision order. A random uuid must never decide it either."""
        frozen = "2026-10-18T00:00:00.000Z"
        with mock.patch("context_ledger.store.now_ts", lambda *a, **k: frozen):
            same = {"decision": "Mini Codex login rejects the pinned model",
                    "why": "The account cannot use that pinned model, so keep a fallback reviewer"}
            older = self.save(**same)
            newer = self.save(**same)
            query = "codex login rejects the pinned model"
            self.assertEqual(self.ids(query)[0], newer, "the newest append wins the tie")
            conn = self.store.open()
            self.store._rebuild_one(conn, older)
            conn.commit()
            self.assertEqual(self.ids(query)[0], newer, "a projection rewrite must not reorder the tie")
            self.store.rebuild()
            self.assertEqual(self.ids(query)[0], newer, "a full rebuild must not reorder the tie")

    def test_off_topic_link_is_not_surfaced(self) -> None:
        other = self.save(decision="Travel quotes go stale after a day", why="fares move", entities=["travel"])
        hit = self.save(decision="Kite reads go through Mini Comet", why="Guru rule", entities=["kite"],
                        precedent_ids=[other])
        got = self.find("kite reads comet")
        self.assertEqual(got["matches"][0]["decision_id"], hit)
        self.assertNotIn("related", got["matches"][0])

    def test_open_does_not_rebuild_links(self) -> None:
        base = self.save(decision="Mini Codex login rejects the pinned model",
                         why="The account cannot use that pinned model", entities=["codex-cli"])
        hit = self.save(decision="Fallback reviewer covers a Codex login reject",
                        why="because the pinned model login needs a fallback reviewer", entities=["codex-cli"],
                        precedent_ids=[base])
        extra = _u()
        conn = self.store.open()
        conn.execute("insert into links(src,dst,kind) values (?,?, 'precedent')", (hit, extra))
        self.store.close()
        again = Store(self.data, actor_channel="agent")
        try:
            dsts = [r[0] for r in again.open().execute("select dst from links where src=?", (hit,))]
            self.assertIn(base, dsts)
            self.assertIn(extra, dsts)
        finally:
            again.close()

    def test_unmarked_ledger_backfills_links_once(self) -> None:
        base = self.save(decision="Mini Codex login rejects the pinned model",
                         why="The account cannot use that pinned model", entities=["codex-cli"])
        hit = self.save(decision="Fallback reviewer covers a Codex login reject",
                        why="because the pinned model login needs a fallback reviewer", entities=["codex-cli"],
                        precedent_ids=[base])
        conn = self.store.open()
        conn.execute("delete from links")
        conn.execute("delete from v2_meta")
        self.store.close()
        again = Store(self.data, actor_channel="agent")
        try:
            dsts = [r[0] for r in again.open().execute("select dst from links where src=?", (hit,))]
            self.assertEqual(dsts, [base])
            self.assertEqual(
                again.conn.execute("select value from v2_meta where key='links_backfill'").fetchone()[0],
                "1",
            )
            extra = _u()
            again.conn.execute("insert into links(src,dst,kind) values (?,?, 'precedent')", (hit, extra))
            again.close()
            third = Store(self.data, actor_channel="agent")
            try:
                dsts = [r[0] for r in third.open().execute("select dst from links where src=?", (hit,))]
                self.assertEqual(sorted(dsts), sorted([base, extra]))
            finally:
                third.close()
        finally:
            if again.conn is not None:
                again.close()

    def test_repeated_rule_and_exception_counts_as_automation_candidate(self) -> None:
        for i in range(3):
            self.save(decision=f"Skipped Kite MCP for ACCT7 read {i}", why="Guru rule applies",
                      entities=["kite"], rule="kite-via-comet", exception="none")
        hit = self.find("kite mcp ACCT7 read")["matches"][0]
        self.assertEqual(hit["repeats"], 3)

    def test_find_and_get_are_logged_with_caller_query_count_and_bytes(self) -> None:
        did = self.save(decision="Kite reads go through Mini Comet", why="Guru rule", entities=["kite"])
        task = _u()
        self.find("kite comet", actor="smallcase-bot", task_id=task)
        self.store.get({"decision_id": did, "actor": "smallcase-bot"}, mcp=True)
        rows = self.store.open().execute("select tool, caller, task_id, query, n, bytes, ids from access_log").fetchall()
        self.assertEqual([r["tool"] for r in rows], ["find", "get"])
        self.assertEqual(rows[0]["caller"], "smallcase-bot")
        self.assertEqual(rows[0]["task_id"], task)
        self.assertEqual(rows[0]["n"], 1)
        self.assertGreater(rows[0]["bytes"], 0)
        self.assertEqual(json.loads(rows[0]["ids"]), [did])
        self.assertIn('"event":"find"', self.log.getvalue())
        # helpful mark at closeout joins back to what find returned
        rev = self.store.get({"decision_id": did}, mcp=True)["record"]["revision"]
        self.store.append_event(
            {"request_id": _u(), "decision_id": did, "expected_revision": rev, "occurred_at": None,
             "event_type": "outcome", "payload": {"status": "success", "note": "used", "evidence": [],
                                                  "task_id": task, "helpful": True}}, mcp=True)
        report = self.store.access_report()
        self.assertEqual(report["helpful"], {"marked": 1, "helpful": 1})
        self.assertEqual(report["by_caller"]["smallcase-bot"]["find"], 1)

    def test_top3_with_precedents_stays_under_the_byte_budget(self) -> None:
        prev = None
        for i in range(5):
            prev = self.save(
                decision=f"Codex review fallback case {i} keeps a reviewer ready",
                why="The pinned reviewer covers a Codex login reject because the account login fails",
                entities=["codex-cli", "mac-mini"], rule="reviewer-fallback", exception="account-login",
                precedent_ids=[prev] if prev else None)
        got = self.find("codex review fallback reviewer")
        self.assertLessEqual(len(canonical_json(got).encode("utf-8")), MAX_FIND_BYTES)
        self.assertTrue(got["matches"])


class NotHelpedFixTests(V2Base):
    """Applied decision did not help -> outcome not_helped|misled + reason, a linked fix with context,
    and in that context the old decision drops below the fix."""

    def setUp(self) -> None:
        super().setUp()
        self.old = self.save(type="user_decision", decision="Reviews run on the Codex reviewer for code review",
                             why="because Codex plans and reviews and another agent implements",
                             entities=["code-review"], rule="reviewer-model")
        self.task = _u()
        rev = self.store.get({"decision_id": self.old}, mcp=True)["record"]["revision"]
        self.applied = self.store.append_event(
            {"request_id": _u(), "decision_id": self.old, "expected_revision": rev, "occurred_at": None,
             "event_type": "applied", "payload": {"task_id": self.task, "decision_ids": [self.old]}}, mcp=True)

    def outcome(self, **payload) -> dict:
        rev = self.store.get({"decision_id": self.old}, mcp=True)["record"]["revision"]
        base = {"status": "success", "note": "", "evidence": [], "task_id": self.task,
                "applied_event_id": self.applied["event_id"]}
        return self.store.append_event(
            {"request_id": _u(), "decision_id": self.old, "expected_revision": rev, "occurred_at": None,
             "event_type": "outcome", "payload": {**base, **payload}}, mcp=True)

    def reject(self, fn, needle: str) -> None:
        with self.assertRaises(LedgerError) as ctx:
            fn()
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")
        self.assertIn(needle, ctx.exception.message or "")

    def test_applied_outcome_requires_helpful(self) -> None:
        self.reject(lambda: self.outcome(), "needs helpful")

    def test_not_helped_requires_effect_and_short_reason(self) -> None:
        self.reject(lambda: self.outcome(helpful=False), "effect not_helped|misled")
        self.reject(lambda: self.outcome(helpful=False, effect="wrong"), "effect not_helped|misled")
        self.reject(lambda: self.outcome(helpful=False, effect="misled"), "reason is required")
        self.reject(lambda: self.outcome(helpful=False, effect="misled", reason=" ".join(["w"] * 21)), "max 20")
        self.reject(lambda: self.outcome(helpful=True, effect="misled", reason="x"), "only with helpful false")
        self.outcome(helpful=False, effect="not_helped", reason="Codex subscription lapsed; reviewer unavailable")

    def test_fix_needs_context_and_a_real_target(self) -> None:
        kw = dict(type="user_decision", decision="Reviews run on the Cursor agent", why="because the reviewer path lapsed",
                  entities=["code-review"])
        self.reject(lambda: self.save(supersedes=self.old, **kw), "needs context")
        self.reject(lambda: self.save(supersedes=_u(), context="code review", **kw), "not a visible record")
        with self.assertRaises(LedgerError):  # exception_for is deferred (Guru 2026-10-17): only supersedes links
            self.save(exception_for=self.old, context="x", **kw)
        self.reject(lambda: self.save(context="code review", **kw), "context goes with")
        self.reject(lambda: self.save(supersedes=self.old, context=" ".join(["w"] * 11), **kw), "max 10")

    def test_fix_ranks_first_in_context_and_old_confidence_drops(self) -> None:
        # Before: the old rule is the top hit for a review question.
        self.assertEqual(self.ids("who reviews the code now")[0], self.old)
        self.outcome(helpful=False, effect="not_helped", reason="Codex subscription lapsed; reviewer unavailable")
        fix = self.save(type="user_decision", decision="Reviews run on the Cursor agent",
                        why="because the previous reviewer path lapsed", entities=["code-review"],
                        supersedes=self.old, context="code review and planning", task_id=self.task)
        got = self.find("who reviews the code now")
        ids = [m["decision_id"] for m in got["matches"]]
        self.assertEqual(ids[0], fix)
        self.assertEqual(got["matches"][0]["supersedes"], self.old)
        self.assertEqual(got["matches"][0]["context"], "code review and planning")
        if self.old in ids:
            self.assertEqual(got["matches"][ids.index(self.old)]["flag"], "not_helped here")
        self.assertLessEqual(len(canonical_json(got).encode()), MAX_FIND_BYTES)
        rec = self.store.get({"decision_id": self.old}, mcp=True)["record"]
        self.assertEqual(rec["fixed_by"][0]["id"], fix)
        self.assertEqual(rec["fixed_by"][0]["not_helped"], 1)
        self.assertLess(rec["fixed_by"][0]["confidence_here"], rec["confidence"]["score"] or 1.0)

    def test_out_of_context_query_is_not_reordered(self) -> None:
        other = self.save(decision="Mini fan noise: check launchd jobs first", why="fan spikes track cron jobs",
                          entities=["mac-mini"])
        self.save(type="user_decision", decision="Reviews run on the Cursor agent",
                  why="because the previous reviewer path lapsed", entities=["code-review"], supersedes=self.old,
                  context="code review and planning", task_id=self.task)
        self.assertEqual(self.ids("mini fan noise launchd"), [other])


class McpOutputTests(V2Base):
    def test_find_sends_one_copy(self) -> None:
        text, structured = tool_content(ok({"matches": [], "has_more": False}), structured=False)
        self.assertIsNone(structured)
        self.assertEqual(json.loads(text)["data"], {"matches": [], "has_more": False})
        self.assertEqual(text.count('"matches"'), 1)



class ABSwitchTests(V2Base):
    """2026-10-18..31 IST: find OFF on even days, ON on odd days; saves stay on; same rule in ledger_loop."""

    def test_rule_matches_ledger_loop(self) -> None:
        import importlib.util
        from datetime import date, timedelta
        from context_ledger.contracts import ab_arm
        spec = importlib.util.spec_from_file_location("ledger_loop", ROOT / "scripts" / "ledger_loop.py")
        loop = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loop)
        days = [date(2026, 10, 16) + timedelta(days=i) for i in range(18)]
        self.assertEqual([ab_arm(d) for d in days], [loop.ab_arm(d) for d in days])
        self.assertEqual((ab_arm(date(2026, 10, 17)), ab_arm(date(2026, 10, 18)), ab_arm(date(2026, 10, 19)),
                          ab_arm(date(2026, 10, 31)), ab_arm(date(2026, 11, 1))), ("none", "off", "on", "on", "none"))

    def test_off_day_find_is_empty_and_save_still_works(self) -> None:
        from datetime import date
        self.save(decision="Kite connector never reads Zerodha holdings", why="Comet only")
        self.store.ab_today = lambda: date(2026, 10, 18)
        self.assertEqual(self.find("kite zerodha holdings"), {"matches": [], "has_more": False, "arm": "off"})
        self.save(decision="Saves stay on during OFF days", why="A/B keeps writes in both arms")
        self.store.ab_today = lambda: date(2026, 10, 19)
        self.assertTrue(self.ids("kite zerodha holdings"))



class TrustTierTests(V2Base):
    """trusted needs >= TRUST_MIN_HELPED (3) helped observations from distinct tasks, failure
    <= TRUST_MAX_FAIL (10%), each observation final, evidenced and linked to its applied event."""

    def test_pure_boundaries(self) -> None:
        from context_ledger.ranking import trust_tier
        self.assertEqual(trust_tier("agent_conclusion", 2, 0, 2, False), "hint")
        self.assertEqual(trust_tier("agent_conclusion", 3, 0, 3, False), "trusted")
        self.assertEqual(trust_tier("agent_conclusion", 9, 1, 10, False), "trusted")   # 10% fail
        self.assertEqual(trust_tier("agent_conclusion", 8, 1, 9, False), "hint")       # 11% fail
        self.assertEqual(trust_tier("user_decision", 2, 0, 2, False), "user_decision")
        self.assertEqual(trust_tier("user_decision", 3, 0, 3, False), "trusted")
        self.assertEqual(trust_tier("agent_conclusion", 9, 0, 9, True), "hint")        # superseded here

    def observe(self, did: str, *, task: str, helped: bool, effect: str | None = None,
                status: str = "success", evidence: list[dict] | None = None, link: bool = True) -> dict:
        """One task applying the decision, then the outcome that judges it. Returns the receipt."""
        rev = self.store.get({"decision_id": did}, mcp=False)["record"]["revision"]
        ap = self.store.append_event(
            {"request_id": _u(), "decision_id": did, "expected_revision": rev, "occurred_at": None,
             "event_type": "applied", "payload": {"task_id": task, "decision_ids": [did]}}, mcp=True)
        payload: dict = {"status": status, "note": "test observation", "task_id": task, "helpful": helped,
                         "evidence": EVIDENCE if evidence is None else evidence}
        if link:
            payload["applied_event_id"] = ap["event_id"]
        if effect:
            payload.update({"effect": effect, "reason": "did not fit this case"})
        return self.store.append_event(
            {"request_id": _u(), "decision_id": did, "expected_revision": rev + 1, "occurred_at": None,
             "event_type": "outcome", "payload": payload}, mcp=True)

    def mark(self, did: str, helped: int, failed: int) -> None:
        for k in range(helped + failed):
            self.observe(did, task=_u(), helped=k < helped, effect=None if k < helped else "not_helped")

    def tier(self, query: str) -> str:
        return self.find(query)["matches"][0]["tier"]

    def test_one_task_cannot_be_its_own_three_observations(self) -> None:
        """The rubber stamp: three applied events on one task used to read as three observations."""
        d = self.save(decision="Comet reads zerodha holdings for ACCT7", why="kite connector is the only path")
        for _ in range(3):
            self.observe(d, task=_u(), helped=True)
        self.assertEqual(self.tier("comet zerodha holdings ACCT7"), "trusted")
        one_task = self.save(decision="Smallcase rebalance reads basket weights first", why="avoid drift")
        task = _u()
        for _ in range(3):
            self.observe(one_task, task=task, helped=True)
        self.assertEqual(self.tier("smallcase rebalance basket weights"), "hint",
                         "one task reporting three times is one observation")

    def test_an_observation_needs_a_final_status(self) -> None:
        d = self.save(decision="Comet reads zerodha holdings for ACCT7", why="kite connector is the only path")
        for _ in range(3):
            self.observe(d, task=_u(), helped=True, status="pending")
        self.assertEqual(self.tier("comet zerodha holdings ACCT7"), "hint")
        self.mark(d, 3, 0)
        self.assertEqual(self.tier("comet zerodha holdings ACCT7"), "trusted")

    def test_an_observation_needs_usable_evidence(self) -> None:
        d = self.save(decision="Comet reads zerodha holdings for ACCT7", why="kite connector is the only path")
        for _ in range(3):
            self.observe(d, task=_u(), helped=True, evidence=[])
        self.assertEqual(self.tier("comet zerodha holdings ACCT7"), "hint",
                         "closeout demands evidence, so the tier cannot count evidence-free outcomes")
        self.mark(d, 3, 0)
        self.assertEqual(self.tier("comet zerodha holdings ACCT7"), "trusted")

    def test_an_unlinked_outcome_is_not_an_observation(self) -> None:
        d = self.save(decision="Comet reads zerodha holdings for ACCT7", why="kite connector is the only path")
        for _ in range(3):
            self.observe(d, task=_u(), helped=True, link=False)
        self.assertEqual(self.tier("comet zerodha holdings ACCT7"), "hint")
        self.mark(d, 3, 0)
        self.assertEqual(self.tier("comet zerodha holdings ACCT7"), "trusted")

    def test_a_corrected_outcome_is_a_new_observation(self) -> None:
        """A positive observation corrected to not_helped is the evidence the tier has to see."""
        d = self.save(decision="Kite connector keeps reads on the Mini", why="the token never leaves the machine")
        task = _u()
        rev = self.store.get({"decision_id": d}, mcp=False)["record"]["revision"]
        ap = self.store.append_event(
            {"request_id": _u(), "decision_id": d, "expected_revision": rev, "occurred_at": None,
             "event_type": "applied", "payload": {"task_id": task, "decision_ids": [d]}}, mcp=True)["event_id"]
        base = {"status": "success", "note": "test observation", "task_id": task,
                "evidence": EVIDENCE, "applied_event_id": ap}

        def outcome(payload: dict, revision: int) -> dict:
            return self.store.append_event(
                {"request_id": _u(), "decision_id": d, "expected_revision": revision, "occurred_at": None,
                 "event_type": "outcome", "payload": payload}, mcp=True)

        first = outcome({**base, "helpful": True}, rev + 1)
        self.assertFalse(first["replayed"])
        corrected = {**base, "helpful": False, "effect": "not_helped", "reason": "read the wrong account"}
        second = outcome(corrected, rev + 2)
        self.assertFalse(second["replayed"], "the correction must not be swallowed as a replay")
        self.assertNotEqual(second["event_id"], first["event_id"])
        again = outcome(corrected, rev + 3)
        self.assertTrue(again["replayed"], "an identical retry is still a replay")

    def test_find_tiers_from_outcomes(self) -> None:
        a = self.save(decision="Comet reads zerodha holdings for ACCT7", why="kite connector is the only path")
        self.mark(a, 2, 0)
        self.assertEqual(self.tier("comet zerodha holdings ACCT7"), "hint")
        self.mark(a, 1, 0)
        self.assertEqual(self.tier("comet zerodha holdings ACCT7"), "trusted")
        b = self.save(decision="Tijori overview scrape uses shortlist only", why="full scrape wastes tokens")
        self.mark(b, 9, 1)
        self.assertEqual(self.tier("tijori overview scrape shortlist"), "trusted")
        c = self.save(decision="Smallcase rebalance reads basket weights first", why="avoid drift")
        self.mark(c, 8, 1)
        self.assertEqual(self.tier("smallcase rebalance basket weights"), "hint")
        g = self.save(type="user_decision", decision="Guru approves every Kite order himself", why="Guru rule",
                      criticality=None, approver="guru")
        self.assertEqual(self.tier("guru approves kite order"), "hint")  # self_reported: no Guru tier
        rev = self.store.get({"decision_id": g}, mcp=False)["record"]["revision"]
        self.store.attest(g, rev, _u(), "guru", "owner attested")
        self.assertEqual(self.tier("guru approves kite order"), "user_decision")
        self.mark(g, 3, 0)
        self.assertEqual(self.tier("guru approves kite order"), "trusted")


if __name__ == "__main__":
    unittest.main()
