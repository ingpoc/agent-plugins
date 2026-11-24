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
            root = state / "example-repo"
            root.mkdir(parents=True)
            rows = [
                {"event": "start", "job_id": "codex-job", "agent": "Codex"},
                {"event": "finish", "job_id": "codex-job", "agent": "Codex", "repo": "example-repo",
                 "tokens_source": "stamp", "tokens": {"input": 10, "output": 5, "total": 15}},
                {"event": "start", "job_id": "grok-job", "agent": "Grok Bot"},
                {"event": "finish", "job_id": "grok-job", "agent": "Grok Bot", "repo": "example-repo", "tokens_source": "none"},
                {"event": "incomplete", "job_id": "unfinished", "agent": "Grok Bot", "tokens": {"total": 500}},
            ]
            (root / "events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
            with patch.dict(os.environ, {"BCL_STATE_DIR": str(state)}):
                result = collect([], Path(tmp) / "out", home=Path(tmp), config=None)
            self.assertEqual(result["bcl_jobs"]["covered"], 1)
            self.assertEqual(result["bcl_jobs"]["jobs"], 2)
            self.assertEqual(result["bcl_jobs"]["coverage"], "1/2")
            self.assertEqual(result["bcl_jobs"]["gaps"], ["Grok Bot"])
            self.assertEqual(result["bcl_jobs"]["tokens"], 15)
            self.assertEqual(result["bcl_jobs"]["incomplete"], 1)
            self.assertNotIn("incomplete_tokens", result["bcl_jobs"])


if __name__ == "__main__":
    unittest.main()
