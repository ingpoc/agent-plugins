import json
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import events


class EventTests(unittest.TestCase):
    def test_lifecycle_folds_by_stable_job_id_and_keeps_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            events.job_event("repo", "job-1", "start", state_dir=tmp, agent="Codex")
            events.job_event("repo", "job-1", "finish", state_dir=tmp, tokens_source="stamp", tokens={"total": 12})
            events.job_event("repo", "job-2", "incomplete", state_dir=tmp, agent="Grok Bot", tokens={"total": 99})
            rows = events.read("repo", tmp)
            self.assertEqual({row["job_id"] for row in rows}, {"job-1", "job-2"})
            self.assertEqual(next(row for row in rows if row["job_id"] == "job-1")["event"], "finish")
            self.assertEqual(next(row for row in rows if row["job_id"] == "job-2")["event"], "incomplete")

    def test_failed_start_stays_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            events.job_event("repo", "job", "failed_start", state_dir=tmp, agent="Codex")
            self.assertEqual(events.read("repo", tmp)[0]["event"], "failed_start")

    def test_events_are_scoped_to_resolved_list_identity_for_jobs_and_stages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "one" / "list.json"
            same = root / "other-worktree" / "list.json"
            second = root / "one" / "another-list.json"
            for path in (first, same, second):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}")
            alias = root / "alias.json"
            alias.symlink_to(first)
            events.job_event(first, "j1", "start", state_dir=root / "state")
            events.stage_event(alias, "review", "start", state_dir=root / "state")
            events.job_event(first, "j1", "finish", state_dir=root / "state")
            events.job_event(second, "j2", "start", state_dir=root / "state")
            events.job_event(same, "j3", "start", state_dir=root / "state")
            self.assertEqual(events.events_path(first, root / "state"), events.events_path(alias, root / "state"))
            self.assertNotEqual(events.events_path(first, root / "state"), events.events_path(second, root / "state"))
            self.assertEqual([row["event"] for row in events.read(first, root / "state")], ["stage_start", "finish"])
            self.assertEqual([row["job_id"] for row in events.read(same, root / "state")], ["j3"])


if __name__ == "__main__":
    unittest.main()
