#!/usr/bin/env python3
"""Official observability totals are attached when a change-list job finishes."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import check_introspect
import job_usage


JOB = {
    "id": "job-cursor-1",
    "session": "sess-cursor-1",
    "started_at": "2026-11-25T15:20:00Z",
    "finished_at": "2026-11-25T15:23:00Z",
    "phase": "fix",
}

HARNESS_ROW = {
    "job_id": "job-cursor-1",
    "session_id": "sess-cursor-1",
    "started_at": "2026-11-25T15:20:05Z",
    "source": "cursor-usage",
    "tokens": {"uncached_input": 19126, "cache_read": 6400, "cache_write": 0, "output": 24},
    "cost": None,
    "cost_source": "not_reported",
}


class JobUsageTests(unittest.TestCase):
    def test_finish_job_attaches_devobs_total(self):
        attached = job_usage.attach_official_usage(dict(JOB), harness_rows=[HARNESS_ROW])
        self.assertEqual(attached["tokens_source"], "official")
        self.assertEqual(attached["tokens"]["total"], 25550)
        self.assertEqual(attached["tokens"]["cache_read"], 6400)
        self.assertEqual(attached["cost_source"], "not_reported")

    def test_missing_usage_is_not_reported(self):
        attached = job_usage.attach_official_usage(dict(JOB), harness_rows=[])
        self.assertEqual(attached["tokens_source"], "not_reported")
        self.assertIsNone(attached["tokens"])
        self.assertNotEqual(attached["tokens_source"], "none")
