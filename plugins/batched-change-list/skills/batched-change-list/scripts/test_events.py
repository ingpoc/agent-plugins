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


if __name__ == "__main__":
    unittest.main()
