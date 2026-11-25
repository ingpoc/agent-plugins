import json
import os
import sys
import unittest
from pathlib import Path

from test_bcl import BCL, BclTests


class JobReceiptTests(BclTests):
    def _plan_lane(self, worktree=None, implementer="writer"):
        self.assertEqual(self.call_without_viewer("start").returncode, 0)
        group = {"id": "A", "finding_ids": ["ISSUE-01"], "status": "planned",
                 "codex": {"implementer_session": implementer, "reviewer_session": "reviewer"}}
        if worktree:
            group["worktree"] = worktree
        plan = self.root / "receipts-plan.json"
        plan.write_text(json.dumps({"round": 1, "groups": [group]}))
        result = self.call("plan", "--plan", str(plan), "--ids", "ISSUE-01")
        self.assertEqual(result.returncode, 0, result.stderr)
        return Path(json.loads((self.state / "registry.json").read_text())["example/project/123"]["live"])

    def _fix_job(self, *command, model="Grok Bot", cwd=None):
        env = self.env
        return __import__("subprocess").run(
            [sys.executable, str(BCL), "--data", str(self.data), "--ids", "ISSUE-01",
             "--model", model, "--phase", "fix", "job", "--", *command],
            cwd=cwd, env=env, capture_output=True, text=True)

    def test_named_worktree_fix_without_commit_fails(self):
        self._plan_lane(worktree="home")
        result = self._fix_job(sys.executable, "-c", "pass", cwd=self.home)
        self.assertEqual(result.returncode, 3, result.stderr)
        job = json.loads(self.data.read_text())["tracking"]["plan"]["groups"][0]["codex"]["job"]
        self.assertEqual(job.get("finished_phase_status"), "failed")
        self.assertIn("no commit", job.get("first_error_line", ""))
        self.assertEqual(job.get("tokens_source"), "not_reported")
        self.assertIn("wall_time_s", job)
        self.assertEqual(job.get("model"), "Grok Bot")
        self.assertEqual(job.get("session"), "writer")

    def test_named_worktree_fix_commit_records_receipts(self):
        self._plan_lane(worktree="home")
        script = self.root / "commit-fix.py"
        script.write_text(
            "from pathlib import Path\n"
            "import subprocess\n"
            "root = Path.cwd()\n"
            "(root / 'fix.txt').write_text('ok\\n')\n"
            "subprocess.run(['git', 'add', 'fix.txt'], check=True)\n"
            "subprocess.run(['git', 'commit', '-qm', 'fix'], check=True)\n"
        )
        result = self._fix_job(sys.executable, str(script), cwd=self.home)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        job = json.loads(self.data.read_text())["tracking"]["plan"]["groups"][0]["codex"]["job"]
        self.assertEqual(job.get("finished_phase_status"), "passed")
        self.assertEqual(job.get("tokens_source"), "not_reported")
        self.assertIsInstance(job.get("wall_time_s"), int)
        self.assertEqual(job.get("model"), "Grok Bot")

    def test_named_worktree_dirty_after_commit_fails(self):
        self._plan_lane(worktree="home")
        script = self.root / "dirty-fix.py"
        script.write_text(
            "from pathlib import Path\n"
            "import subprocess\n"
            "root = Path.cwd()\n"
            "(root / 'fix.txt').write_text('ok\\n')\n"
            "subprocess.run(['git', 'add', 'fix.txt'], check=True)\n"
            "subprocess.run(['git', 'commit', '-qm', 'fix'], check=True)\n"
            "(root / 'leftover.txt').write_text('dirty\\n')\n"
        )
        result = self._fix_job(sys.executable, str(script), cwd=self.home)
        self.assertEqual(result.returncode, 3, result.stderr)
        job = json.loads(self.data.read_text())["tracking"]["plan"]["groups"][0]["codex"]["job"]
        self.assertIn("uncommitted", job.get("first_error_line", ""))


    def test_fix_credits_named_implementer_commit_outside_the_process(self):
        self._plan_lane(worktree="home")
        (self.home / "fix.txt").write_text("ok\n")
        self.git(self.home, "add", "fix.txt")
        self.git(self.home, "commit", "-qm", "implementer fix")
        sha = self.git(self.home, "rev-parse", "HEAD").stdout.strip()
        result = self._fix_job(sys.executable, "-c", "pass", cwd=self.home)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        job = json.loads(self.data.read_text())["tracking"]["plan"]["groups"][0]["codex"]["job"]
        self.assertEqual(job.get("finished_phase_status"), "passed")
        self.assertEqual(job.get("commit"), sha)

    def test_usage_includes_cached_tokens(self):
        from importlib.machinery import SourceFileLoader
        import importlib.util
        import tempfile
        loader = SourceFileLoader("bcl_entrypoint_receipts", str(BCL))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            path.write_text(json.dumps({
                "payload": {"type": "token_count", "info": {"total_token_usage": {
                    "input_tokens": 9, "cached_input_tokens": 3, "output_tokens": 2, "total_tokens": 14,
                }}}
            }) + "\n")
            Path(str(path) + ".bcl-job").write_text("job-1\n")
            usage = module._read_usage(path, "job-1")
            self.assertEqual(usage, {"input": 9, "cached": 3, "output": 2, "total": 14})



def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name in (
        "test_named_worktree_fix_without_commit_fails",
        "test_named_worktree_fix_commit_records_receipts",
        "test_named_worktree_dirty_after_commit_fails",
        "test_fix_credits_named_implementer_commit_outside_the_process",
        "test_usage_includes_cached_tokens",
    ):
        suite.addTest(JobReceiptTests(name))
    return suite


if __name__ == "__main__":
    unittest.main()
