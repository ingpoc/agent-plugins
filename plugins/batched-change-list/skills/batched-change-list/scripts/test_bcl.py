import json, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
PLUGIN = ROOT / "plugins/batched-change-list"
BCL = PLUGIN / "bin/bcl"
EXAMPLE = PLUGIN / "skills/batched-change-list/examples/findings.json"

class BclTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.data = self.root / "findings.json"; self.page = self.root / "page.html"
        self.data.write_text(EXAMPLE.read_text())
        self.progress = self.root / "progress.log"

    def bcl(self, stage, *extra):
        return subprocess.run([sys.executable, str(BCL), "--data", str(self.data), "--render", str(self.page), "--progress", str(self.progress), "--now", "2026-11-14T09:00:00Z", *extra, stage], capture_output=True, text=True)

    def test_workflow_cycle(self):
        self.assertEqual(self.bcl("start", "--ids", "ISSUE-01").returncode, 0)
        plan = {"round":1,"updated_at":"2026-11-14T09:00:00Z","groups":[{"id":"A","finding_ids":["ISSUE-01"],"reason":"Same root cause","worktree":"example-worktree","depends_on":[],"codex":{"model":"gpt-6-luna","effort":"medium","status":"running","started_at":"2026-11-14T09:00:00Z","ended_at":None,"duration_s":120},"verify":{"method":"Unit tests and a live page check","status":"pending"},"status":"running"}]}
        planfile = self.root / "plan.json"; planfile.write_text(json.dumps(plan))
        for stage, args in (("plan", ("--plan", str(planfile), "--ids", "ISSUE-01")), ("fix", ("--ids", "ISSUE-01")), ("install", ("--ids", "ISSUE-01")), ("verify", ("--ids", "ISSUE-01")), ("introspect", ("--result", "no issues"))):
            result = self.bcl(stage, *args); self.assertEqual(result.returncode, 0, result.stderr)
            if stage == "plan": self.assertIn('class="plan-lanes"', self.page.read_text())
        status = self.bcl("status")
        self.assertEqual(status.returncode, 0, status.stderr)
        state = json.loads(self.data.read_text())
        self.assertEqual(state["tracking"]["stages_reached"], ["collect", "plan_fix", "install", "verify", "introspect"])
        self.assertEqual(state["tracking"]["plan"]["groups"][0]["id"], "A")
        self.assertIn('class="plan-lanes"', self.page.read_text(), state["tracking"])
        self.assertEqual(len(self.progress.read_text().splitlines()), 6)
        self.assertEqual(json.loads(status.stdout)["plan"][0]["status"], "running")

    def test_guards(self):
        self.assertNotEqual(self.bcl("install", "--ids", "ISSUE-01").returncode, 0)
        self.assertNotEqual(self.bcl("verify", "--ids", "ISSUE-01").returncode, 0)
        self.assertNotEqual(self.bcl("report").returncode, 0)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True)

    def test_install_hooks_idempotent_chain_and_stale_page(self):
        self.git("init", "-q"); self.git("config", "user.email", "test@example.com"); self.git("config", "user.name", "Test")
        (self.root / ".githooks").mkdir()
        foreign = self.root / ".githooks/pre-commit"; foreign.write_text("#!/bin/sh\nexit 0\n"); foreign.chmod(0o755)
        (self.root / "docs/triage").mkdir(parents=True)
        tracker = json.loads(EXAMPLE.read_text()); tracker["tracking"] = {"pull_request":{"label":"PR #123","url":"https://example.test/pull/123"}}
        (self.root / "docs/triage/current-findings.json").write_text(json.dumps(tracker))
        report = self.root / "docs/triage/pr-123.html"; report.write_text("stale")
        def install(): return subprocess.run([sys.executable, str(BCL), "--repo", str(self.root), "install-hooks"], capture_output=True, text=True)
        self.assertEqual(install().returncode, 0)
        pre = (self.root / ".githooks/pre-commit").read_text()
        self.assertIn("pre-commit.bcl-original", pre); self.assertIn("bcl-hook", pre)
        self.assertEqual((self.root / ".githooks/pre-commit.bcl-original").read_text(), "#!/bin/sh\nexit 0\n")
        self.assertEqual(install().returncode, 0)
        self.assertEqual((self.root / ".githooks/pre-commit").read_text(), pre)
        self.assertTrue((self.root / ".githooks/pre-push").exists())
        self.git("add", "docs/triage/current-findings.json", "docs/triage/pr-123.html")
        result = subprocess.run([str(self.root / ".githooks/pre-commit")], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(report.read_text(), "stale", result.stderr)

if __name__ == "__main__": unittest.main()
