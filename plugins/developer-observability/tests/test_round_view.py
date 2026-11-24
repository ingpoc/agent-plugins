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
from backend.round_view import build_round, find_tracker  # noqa: E402


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
                            "reason": "Parked until an owner unblocks.",
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
        self.assertEqual({group["id"] for group in result["groups"]}, {"R7-ACU20", "R4-171"})
        self.assertEqual(len(result["jobs"]), 1)
        job = result["jobs"][0]
        self.assertEqual(job["group_id"], "R7-ACU20")
        self.assertEqual(job["round"], 7)
        self.assertTrue(job["group_id_inferred"])
        self.assertEqual(job["tokens"]["total"], 15)
        self.assertEqual(job["duration_ms"], 212000)
        self.assertEqual(result["needs_you"][0]["kind"], "blocked")
        self.assertEqual(result["needs_you"][0]["group_id"], "R4-171")
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


if __name__ == "__main__":
    unittest.main()
