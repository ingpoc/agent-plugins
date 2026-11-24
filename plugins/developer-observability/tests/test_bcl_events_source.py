import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
from collector.cli import collect  # noqa: E402


class BclEventSourceTests(unittest.TestCase):
    def test_collect_combines_verified_codex_tokens_and_named_grok_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "bcl"
            root = state / "list-identity" / "events"
            root.mkdir(parents=True)
            rows = [
                {"event": "start", "job_id": "codex-job", "agent": "Codex", "at": "2026-11-24T10:00:00+00:00"},
                {"event": "finish", "job_id": "codex-job", "agent": "Codex", "repo": "example-repo",
                 "at": "2026-11-24T10:01:00+00:00",
                 "tokens_source": "stamp", "tokens": {"input": 10, "output": 5, "total": 15}},
                {"event": "start", "job_id": "grok-job", "agent": "Grok Bot"},
                {"event": "finish", "job_id": "grok-job", "agent": "python3", "model": "Grok Bot",
                 "repo": "example-repo", "at": "2026-11-24T10:02:00+00:00", "tokens_source": "none"},
                {"event": "incomplete", "job_id": "unfinished", "agent": "Grok Bot", "tokens": {"total": 500}},
            ]
            (root / "events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
            with patch.dict(os.environ, {"BCL_STATE_DIR": str(state)}):
                result = collect([], Path(tmp) / "out", home=Path(tmp), config=None)
            from collector.bcl_events import iter_events
            stats = {}
            emitted = list(iter_events(stats, state))
            self.assertEqual(emitted[0]["ts"], "2026-11-24T10:01:00+00:00")
            self.assertTrue(stats["bcl_events"]["read"])
            self.assertEqual(stats["bcl_events"]["last_event_at"], "2026-11-24T10:02:00+00:00")
            self.assertEqual(result["bcl_jobs"]["covered"], 1)
            self.assertEqual(result["bcl_jobs"]["jobs"], 2)
            self.assertEqual(result["bcl_jobs"]["coverage"], "1/2")
            self.assertEqual(result["bcl_jobs"]["gaps"], ["Grok Bot"])
            self.assertEqual(result["bcl_jobs"]["tokens"], 15)
            self.assertEqual(result["bcl_jobs"]["last_event_at"], "2026-11-24T10:02:00+00:00")
            self.assertTrue(result["bcl_jobs"]["read"])
            self.assertEqual(result["bcl_jobs"]["incomplete"], 1)
            self.assertNotIn("incomplete_tokens", result["bcl_jobs"])

    def test_wrapper_gap_uses_model_and_without_model_is_named_wrapper(self):
        from collector.bcl_events import _gap_name

        self.assertEqual(_gap_name({"agent": "python3", "model": "Grok Bot"}), "Grok Bot")
        self.assertEqual(_gap_name({"agent": "python3"}), "python3 (wrapper)")


if __name__ == "__main__":
    unittest.main()
