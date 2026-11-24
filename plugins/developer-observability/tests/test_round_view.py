#!/usr/bin/env python3
"""Read-only observability_round: tracker groups, inferred job lane ids, ranked needs-you."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
from backend.round_view import build_round, find_tracker, list_trackers  # noqa: E402


def _write_tracker(root: Path, project: str, pr: str, payload: dict) -> Path:
    folder = root / f"test--{project}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{pr}.json"
    path.write_text(json.dumps(payload))
    return path


def _write_events(root: Path, lines: list[dict]) -> None:
    folder = root / "aabbccddeeff"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "events.jsonl").write_text("".join(json.dumps(item) + "\n" for item in lines))


class RoundViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.tracker = {
            "title": "demo",
            "tracking": {
                "pull_request": {"label": "PR #42", "url": "https://example.test/42"},
                "cycle": 3,
                "round": 7,
                "last_written_at": "2026-11-24T07:41:48Z",
                "round_history": [
                    {"round": 6, "fixed": 14, "ended_at": "2026-11-24T07:07:37Z"},
                ],
                "plan": {
                    "round": 7,
                    "cycle": 3,
                    "groups": [
                        {
                            "id": "R7-ACU20",
                            "name": "acu20",
                            "label": "ACU-20",
                            "status": "review",
                            "phase": "plan_review",
                            "finding_ids": ["ACU-20"],
                            "worktree": "bcl42-r7-acu20",
                            "reason": "Show Grok Bot subagents on the island.",
                        },
                        {
                            "id": "R4-171",
                            "name": "bcl171",
                            "label": "BCL-171",
                            "status": "blocked",
                            "phase": "fix",
                            "finding_ids": ["BCL-171"],
                            "worktree": "bcl42-r5-171",
                            "reason": "Needs an owner to unblock.",
                        },
                        {
                            "id": "R4-106",
                            "name": "bcl106",
                            "label": "BCL-106",
                            "status": "parked",
                            "phase": "fix",
                            "finding_ids": ["BCL-106"],
                            "worktree": "bcl42-r5-106",
                            "parked_to": 8,
                            "reason": "Parked to round 8",
                            "failure_history": [{
                                "reason": "Failed at [verify_driver.py:78](/Users/gurusharan/code/worktrees/agent-plugins/bcl120-r5-106/verify_driver.py)",
                                "attempt_id": "a1",
                            }],
                        },
                    ],
                },
                "verification_run": {
                    "id": "r6-deadbeef-42",
                    "status": "active",
                    "cycle": 3,
                    "revision": "abc",
                    "checks": {
                        "ACU-20": {"state": "pending"},
                        "BCL-121": {"state": "fixed"},
                    },
                },
                "parked": {"count": 1, "needs": ["owner unblock"]},
            },
            "findings": [
                {"id": "ACU-20", "status": "in_progress", "title": "Island should show Grok subagents"},
                {"id": "BCL-171", "status": "blocked", "title": "Blocked until owner unblocks"},
                {"id": "BCL-106", "status": "blocked", "title": "R4-106 has failed attempts",
                 "observed": "See [verify_driver.py:78](/Users/gurusharan/code/worktrees/agent-plugins/bcl120-r5-106/verify_driver.py)"},
            ],
        }
        _write_tracker(self.root, "demo", "42", self.tracker)
        _write_events(self.root, [
            {"event": "start", "job_id": "j1", "phase": "presuite", "agent": "check.sh",
             "repo": "bcl42-r7-acu20", "started_at": "2026-11-24T07:38:16Z", "at": "2026-11-24T07:38:16Z"},
            {"event": "finish", "job_id": "j1", "phase": "presuite", "agent": "check.sh",
             "repo": "bcl42-r7-acu20", "exit_code": 0, "tokens": {"input": 10, "output": 5, "total": 15},
             "tokens_source": "stamp", "finished_at": "2026-11-24T07:41:48Z", "at": "2026-11-24T07:41:48Z"},
            {"event": "start", "job_id": "j2", "phase": "fix", "agent": "python3",
             "repo": "other-pr-worktree", "started_at": "2026-11-24T07:00:00Z", "at": "2026-11-24T07:00:00Z"},
        ])

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_round_reads_tracker_and_annotates_jobs(self):
        result = build_round("demo", pr="42", state_dir=str(self.root))
        self.assertEqual(result["pr"]["number"], "42")
        self.assertEqual(result["round"], 7)
        self.assertEqual(result["cycle"], 3)
        self.assertEqual(result["round_started_at"], "2026-11-24T07:07:37Z")
        self.assertIsNone(result["gate"]["threshold"])
        self.assertEqual(result["gate"]["fixed"], 1)
        self.assertEqual(result["gate"]["pending"], 1)
        self.assertEqual({group["id"] for group in result["groups"]}, {"R7-ACU20", "R4-171", "R4-106"})
        self.assertEqual(len(result["jobs"]), 1)
        job = result["jobs"][0]
        self.assertEqual(job["group_id"], "R7-ACU20")
        self.assertEqual(job["round"], 7)
        self.assertTrue(job["group_id_inferred"])
        self.assertEqual(job["tokens"]["total"], 15)
        self.assertEqual(job["duration_ms"], 212000)
        blocked = next(card for card in result["needs_you"] if card.get("group_id") == "R4-171")
        self.assertEqual(blocked["kind"], "blocked")
        self.assertLessEqual(len(result["needs_you"]), 3)

    def test_missing_project_is_unavailable(self):
        with self.assertRaises(ValueError) as ctx:
            build_round("missing", state_dir=str(self.root))
        self.assertTrue(str(ctx.exception).startswith("tracker_unavailable"))

    def test_does_not_write_the_tracker(self):
        path, _, _ = find_tracker("demo", "42", state_dir=str(self.root))
        before = path.read_text()
        mtime = path.stat().st_mtime
        build_round("demo", pr=42, state_dir=str(self.root))
        self.assertEqual(path.read_text(), before)
        self.assertEqual(path.stat().st_mtime, mtime)
    def test_round_history_stays_in_the_current_cycle(self):
        self.tracker["tracking"]["round_history"] = [
            {"round": 6, "fixed": 32, "ended_at": "2026-11-20T12:05:55Z"},
            {"round": 7, "fixed": 32, "ended_at": "2026-11-20T13:00:00Z"},
            {"round": 1, "fixed": 4, "ended_at": "2026-11-22T13:42:25Z"},
            {"round": 6, "fixed": 14, "ended_at": "2026-11-24T07:07:37Z"},
        ]
        _write_tracker(self.root, "demo", "42", self.tracker)
        result = build_round("demo", pr="42", state_dir=str(self.root))
        rounds = [(item["round"], item.get("fixed"), item.get("cycle")) for item in result["round_history"]]
        self.assertEqual(rounds, [(1, 4, 3), (6, 14, 3), (7, None, 3)])
        self.assertTrue(result["round_history"][-1]["live"])
        self.assertEqual(result["round_started_at"], "2026-11-24T07:07:37Z")

    def test_selecting_a_past_round_returns_that_round_jobs(self):
        _write_events(self.root, [
            {"event": "start", "job_id": "j6", "phase": "presuite", "agent": "run-presuite-r6.sh",
             "group_id": "R6-121", "round": 6, "started_at": "2026-11-24T06:00:00Z", "at": "2026-11-24T06:00:00Z"},
            {"event": "finish", "job_id": "j6", "phase": "presuite", "agent": "run-presuite-r6.sh",
             "group_id": "R6-121", "round": 6, "exit_code": 0,
             "tokens": {"input": 100, "output": 20, "total": 120}, "tokens_source": "stamp",
             "finished_at": "2026-11-24T06:10:00Z", "at": "2026-11-24T06:10:00Z"},
        ])
        result = build_round("demo", pr="42", round=6, state_dir=str(self.root))
        self.assertEqual(result["round"], 6)
        self.assertFalse(result["viewing_current"])
        self.assertEqual({group["id"] for group in result["groups"]}, {"R6-121"})
        self.assertEqual(len(result["jobs"]), 1)
        self.assertEqual(result["jobs"][0]["group_id"], "R6-121")
        self.assertEqual(result["time_tokens"]["tokens"], 120)
        self.assertIsNone(result["gate"]["run_id"])
        self.assertEqual(result["gate"]["fixed"], 14)
        self.assertEqual(result["status_counts"].get("verified_fixed"), 14)

    def test_needs_you_cards_include_fact_gap_and_action(self):
        result = build_round("demo", pr="42", state_dir=str(self.root))
        card = next(item for item in result["needs_you"] if item.get("group_id") == "R4-171")
        self.assertEqual(card["kind"], "blocked")
        self.assertTrue(card["fact"])
        self.assertTrue(card["gap"])
        self.assertEqual(card["action"]["target"], "R4-171")
        self.assertEqual(card["action"]["label"], "Open lane")

    def test_parked_lane_and_needs_share_one_status_and_hide_home_paths(self):
        result = build_round("demo", pr="42", state_dir=str(self.root))
        group = next(item for item in result["groups"] if item["id"] == "R4-106")
        self.assertEqual(group["view_status"], "parked")
        self.assertEqual(group["why"], "Parked to round 8")
        cards = [card for card in result["needs_you"] if card.get("group_id") == "R4-106"]
        self.assertTrue(cards)
        self.assertEqual(cards[0]["kind"], "parked")
        self.assertEqual(cards[0]["view_status"], "parked")
        blob = json.dumps(result)
        self.assertNotIn("/Users/", blob)
        self.assertNotIn("gurusharan/code", blob)

    def test_blocked_lane_stays_blocked_on_every_surface(self):
        result = build_round("demo", pr="42", state_dir=str(self.root))
        group = next(item for item in result["groups"] if item["id"] == "R4-171")
        self.assertEqual(group["view_status"], "blocked")
        card = next(card for card in result["needs_you"] if card.get("group_id") == "R4-171")
        self.assertEqual(card["kind"], "blocked")
        self.assertEqual(card["view_status"], "blocked")



    def _write_lists(self):
        previous = {
            "title": "demo",
            "tracking": {
                "pull_request": {"label": "PR #120", "url": "https://example.test/120"},
                "cycle": 12,
                "round": 7,
                "last_written_at": "2026-11-24T09:08:28Z",
                "round_history": [
                    {"round": 1, "fixed": 4, "ended_at": "2026-11-20T10:00:00Z"},
                    {"round": 6, "fixed": 14, "ended_at": "2026-11-24T07:07:37Z"},
                ],
                "plan": {
                    "round": 7,
                    "cycle": 12,
                    "groups": [
                        {"id": "R7-ACU20", "status": "verified", "phase": "verify", "finding_ids": ["ACU-20"]},
                        {"id": "R4-171", "status": "blocked", "phase": "fix", "finding_ids": ["BCL-171"]},
                    ],
                },
            },
            "findings": [
                {"id": "ACU-20", "status": "verified_fixed", "title": "Island"},
                {"id": "BCL-171", "status": "blocked", "title": "Blocked"},
            ],
        }
        newest = {
            "title": "demo",
            "tracking": {
                "pull_request": {"label": "PR #121", "url": "https://example.test/121"},
                "cycle": 13,
                "round": 1,
                "last_written_at": "2026-11-24T09:32:00Z",
                "round_history": [
                    {"round": 1, "fixed": 4, "ended_at": "2026-11-20T10:00:00Z"},
                    {"round": 6, "fixed": 14, "ended_at": "2026-11-24T07:07:37Z"},
                    {"round": 7, "fixed": 17, "ended_at": "2026-11-24T09:08:28Z"},
                ],
                "plan": {"round": 1, "cycle": 13, "groups": []},
            },
            "findings": [
                {"id": "DO-4", "status": "untested", "title": "Viewer"},
            ],
        }
        _write_tracker(self.root, "demo", "120", previous)
        _write_tracker(self.root, "demo", "121", newest)

    def test_empty_newest_list_falls_back_to_list_with_lanes(self):
        self._write_lists()
        summaries = list_trackers("demo", state_dir=str(self.root))
        self.assertEqual([row["pr"] for row in summaries], ["121", "120", "42"])
        self.assertFalse(next(row for row in summaries if row["pr"] == "121")["has_lanes"])
        self.assertTrue(next(row for row in summaries if row["pr"] == "120")["has_lanes"])

        result = build_round("demo", state_dir=str(self.root))
        self.assertEqual(result["pr"]["number"], "120")
        self.assertEqual(result["cycle"], 12)
        self.assertTrue(any(group["id"] == "R7-ACU20" for group in result["groups"]))
        self.assertTrue(result["fallback"]["auto"])
        self.assertEqual(result["fallback"]["from_pr"], "121")
        self.assertEqual(result["fallback"]["viewing_pr"], "120")
        self.assertEqual({item["round"] for item in result["round_history"]}, {1, 6, 7})
        self.assertTrue(all(item.get("cycle") == 12 for item in result["round_history"]))

    def test_empty_list_keeps_own_cycle_chips_and_offers_previous_list(self):
        self._write_lists()
        result = build_round("demo", pr="121", state_dir=str(self.root))
        self.assertEqual(result["pr"]["number"], "121")
        self.assertEqual(result["cycle"], 13)
        self.assertEqual(result["groups"], [])
        self.assertEqual([item["round"] for item in result["round_history"]], [1])
        self.assertTrue(result["round_history"][0]["live"])
        self.assertEqual(result["round_history"][0]["cycle"], 13)
        self.assertFalse(result["fallback"]["auto"])
        self.assertEqual(result["fallback"]["from_pr"], "121")
        self.assertEqual(result["fallback"]["suggest_pr"], "120")


if __name__ == "__main__":
    unittest.main()
