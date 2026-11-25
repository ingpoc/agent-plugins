"""Official per-job and per-round token totals, including cache read and cost."""

from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
sys.path.insert(0, str(ROOT / "runtime" / "backend"))

import issue_trail  # noqa: E402
import viewer  # noqa: E402


class OfficialUsageTests(unittest.TestCase):
    def test_official_total_includes_cache_read(self):
        usage = issue_trail.official_usage({
            "job_id": "j1",
            "tokens_source": "official",
            "tokens": {"uncached_input": 19126, "cache_read": 6400, "cache_write": 0, "output": 24},
        })
        self.assertEqual(usage["tokens"]["cache_read"], 6400)
        self.assertEqual(usage["tokens"]["total"], 25550)
        self.assertEqual(usage["tokens_source"], "official")
        self.assertIsNone(usage["cost"])
        self.assertEqual(usage["cost_source"], "not_reported")

    def test_missing_cost_and_tokens_are_not_reported(self):
        usage = issue_trail.official_usage({"job_id": "j2", "tokens_source": "none"})
        self.assertIsNone(usage["tokens"])
        self.assertEqual(usage["tokens_source"], "not_reported")
        self.assertIsNone(usage["cost"])
        self.assertEqual(usage["cost_source"], "not_reported")

    def test_round_totals_sum_official_jobs_only(self):
        jobs = [
            {"job_id": "a", "round": 1, "tokens_source": "official",
             "tokens": {"uncached_input": 10, "cache_read": 5, "cache_write": 0, "output": 2}},
            {"job_id": "b", "round": 1, "tokens_source": "estimated", "tokens": {"total": 999}},
            {"job_id": "c", "round": 2, "tokens_source": "official",
             "tokens": {"uncached_input": 1, "cache_read": 1, "cache_write": 0, "output": 1},
             "cost": 0.12, "cost_source": "official"},
        ]
        payload = issue_trail.official_usage_series(jobs)
        by_round = {row["round"]: row for row in payload["per_round"]}
        self.assertEqual(by_round[1]["tokens"]["total"], 17)
        self.assertEqual(by_round[1]["tokens"]["cache_read"], 5)
        self.assertEqual(by_round[1]["cost_source"], "not_reported")
        self.assertEqual(by_round[2]["tokens"]["total"], 3)
        self.assertEqual(by_round[2]["cost"], 0.12)
        self.assertEqual(by_round[2]["cost_source"], "official")
        job_rows = {row["job_id"]: row for row in payload["per_job"]}
        self.assertEqual(job_rows["a"]["tokens"]["total"], 17)
        self.assertEqual(job_rows["b"]["tokens_source"], "not_reported")


class UsageApiTests(unittest.TestCase):
    def test_viewer_usage_route_returns_official_series(self):
        jobs = [{"job_id": "j1", "round": 3, "tokens_source": "official",
                 "tokens": {"uncached_input": 4, "cache_read": 6, "cache_write": 0, "output": 1}}]
        series = issue_trail.official_usage_series(jobs)
        with mock.patch.object(viewer, "usage_payload", return_value=series):
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), viewer.ViewerHandler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                port = httpd.server_address[1]
                import urllib.request
                req = urllib.request.Request("http://127.0.0.1:%s/api/usage?project=agent-plugins" % port)
                with urllib.request.urlopen(req, timeout=5) as response:
                    body = json.loads(response.read().decode())
                self.assertEqual(body["per_job"][0]["tokens"]["cache_read"], 6)
                self.assertEqual(body["per_round"][0]["tokens"]["total"], 11)
                self.assertEqual(body["per_round"][0]["cost_source"], "not_reported")
            finally:
                httpd.shutdown()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
