import json, os, signal, socket, subprocess, sys, tempfile, time, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
PLUGIN = ROOT / "plugins/batched-change-list"
BCL = PLUGIN / "bin/bcl"
EXAMPLE = PLUGIN / "skills/batched-change-list/examples/findings.json"

class BclTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.state = self.root / "state"
        self.env = {**os.environ, "BCL_STATE_DIR": str(self.state)}
        self.home = self.root / "home"; self.home.mkdir()
        self.git(self.home, "init", "-q"); self.git(self.home, "config", "user.email", "test@example.com"); self.git(self.home, "config", "user.name", "Test")
        self.git(self.home, "remote", "add", "github", "https://github.com/example/project.git")
        tracker = json.loads(EXAMPLE.read_text()); tracker["tracking"] = {"pull_request":{"label":"PR #123","url":"https://github.com/example/project/pull/123"}}
        (self.home / "docs/triage").mkdir(parents=True)
        self.data = self.home / "docs/triage/pr-123-findings.json"; self.data.write_text(json.dumps(tracker))
        self.page = self.data.with_suffix(".html"); self.page.write_text("initial")
        self.git(self.home, "add", "."); self.git(self.home, "commit", "-qm", "initial")
        self.second = self.root / "second"; self.git(self.home, "worktree", "add", "-qb", "other", str(self.second))
        self.call("adopt", self.data)
        def cleanup_viewer():
            for path in (self.state / "viewers").glob("*.json") if (self.state / "viewers").exists() else []:
                try:
                    pid = json.loads(path.read_text()).get("pid")
                    if pid and pid != 87005: os.kill(pid, signal.SIGTERM)
                except (OSError, ValueError): pass
        self.addCleanup(cleanup_viewer)

    def git(self, cwd, *args): return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)
    def call(self, stage, *args, cwd=None):
        data, extra = (args[0], args[1:]) if args and isinstance(args[0], Path) else (self.data, args)
        return subprocess.run([sys.executable, str(BCL), "--data", str(data), *map(str, extra), stage], cwd=cwd, env=self.env, capture_output=True, text=True)

    def test_adopt_resolves_same_live_file_from_second_worktree(self):
        live = self.call("status", cwd=self.second)
        self.assertEqual(live.returncode, 0, live.stderr)
        state = json.loads(live.stdout)
        self.assertEqual(state["live"], self.call("adopt", self.second / "docs/triage/pr-123-findings.json").stdout.strip())

    def test_adopt_accepts_tracker_as_positional_argument(self):
        result = subprocess.run([sys.executable, str(BCL), "adopt", str(self.data)], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_workflow_cycle_uses_live_tracker_and_snapshots_each_stage(self):
        self.assertEqual(self.call("start").returncode, 0)
        plan = {"round":1,"groups":[{"id":"A","finding_ids":["ISSUE-01"],"reason":"Same root cause","depends_on":[],"codex":{"model":"gpt-6-luna","effort":"medium","status":"running","started_at":"2026-11-14T09:00:00Z","duration_s":120},"verify":{"method":"Unit tests and a live page check","status":"pending"},"status":"running"}]}
        planfile = self.root / "plan.json"; planfile.write_text(json.dumps(plan))
        sections = {"delete":{"none":"No deletions."},"simplify":{"none":"No simplifications."},"optimize":{"none":"No optimizations."},"automate":{"none":"No automations."}}
        sectionfile = self.root / "sections.json"; sectionfile.write_text(json.dumps(sections))
        self.assertNotEqual(self.call("introspect", "--result", "no issues").returncode, 0)
        for stage, extra in (("plan", ["--plan", str(planfile), "--ids", "ISSUE-01"]), ("fix", ["--ids", "ISSUE-01"]), ("install", ["--ids", "ISSUE-01"]), ("verify", ["--ids", "ISSUE-01"]), ("introspect", ["--sections", str(sectionfile)])):
            result = self.call(stage, *extra); self.assertEqual(result.returncode, 0, result.stderr)
            if stage == "plan": self.assertIn('class="plan"', self.page.read_text())
        status = self.call("status")
        self.assertEqual(status.returncode, 0, status.stderr)
        data = json.loads(self.data.read_text())
        self.assertEqual(data["tracking"]["stages_reached"], ["collect", "plan_fix", "install", "verify", "introspect"])
        self.assertEqual(data["tracking"]["plan"]["groups"][0]["id"], "A")
        self.assertEqual(data["tracking"]["plan"]["status"], "closed")
        self.assertEqual(json.loads(status.stdout)["plan"][0]["status"], "running")

    def test_stage_guards_remain(self):
        self.assertNotEqual(self.call("install").returncode, 0)
        self.assertNotEqual(self.call("verify").returncode, 0)
        self.assertNotEqual(self.call("report").returncode, 0)

    def test_stage_renders_before_then_command(self):
        live = Path(json.loads((self.state / "registry.json").read_text())["example/project/123"]["live"])
        tracker = json.loads(live.read_text())
        tracker["findings"][0]["status"] = "in_progress"
        tracker["tracking"]["plan"] = {"cycle": 1, "groups": [{"id": "A", "finding_ids": ["ISSUE-01"], "status": "running"}]}
        live.write_text(json.dumps(tracker)); self.data.write_text(json.dumps(tracker))
        stub = self.root / "check_stage.py"
        stub.write_text("import json, sys\nfrom pathlib import Path\ndata, page = map(Path, sys.argv[1:])\nassert json.loads(data.read_text())['tracking']['workflow']['stage'] == 'install'\nassert '<li data-stage=\\\"install\\\" class=\\\"wf-step current\\\" aria-current=\\\"step\\\">' in page.read_text()\n")
        command = f'"{sys.executable}" "{stub}" "{live}" "{self.page}"'
        result = self.call("install", live, "--ids", "ISSUE-01", "--then", command)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_install_hooks_idempotent_chain_and_stale_page(self):
        self.git(self.root, "init", "-q"); self.git(self.root, "config", "user.email", "test@example.com"); self.git(self.root, "config", "user.name", "Test")
        hooks = self.root / ".githooks"; hooks.mkdir()
        foreign = hooks / "pre-commit"; foreign.write_text("#!/bin/sh\nexit 0\n"); foreign.chmod(0o755)
        triage = self.root / "docs/triage"; triage.mkdir(parents=True)
        tracker = json.loads(EXAMPLE.read_text()); tracker["tracking"] = {"pull_request":{"label":"PR #123","url":"https://example.test/pull/123"}}
        (triage / "current-findings.json").write_text(json.dumps(tracker))
        report = triage / "pr-123.html"; report.write_text("stale")
        def install(): return subprocess.run([sys.executable, str(BCL), "--repo", str(self.root), "install-hooks"], env=self.env, capture_output=True, text=True)
        self.assertEqual(install().returncode, 0)
        pre = (hooks / "pre-commit").read_text()
        self.assertIn("pre-commit.bcl-original", pre); self.assertIn("bcl-hook", pre)
        self.assertEqual((hooks / "pre-commit.bcl-original").read_text(), "#!/bin/sh\nexit 0\n")
        self.assertEqual(install().returncode, 0); self.assertEqual((hooks / "pre-commit").read_text(), pre)
        self.assertTrue((hooks / "pre-push").exists())
        self.git(self.root, "add", "docs/triage/current-findings.json", "docs/triage/pr-123.html")
        result = subprocess.run([str(hooks / "pre-commit")], env=self.env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0, result.stderr); self.assertNotEqual(report.read_text(), "stale", result.stderr)

    def test_stale_snapshot_refuses_without_changing_live(self):
        live = Path(self.call("adopt", self.data).stdout.strip()); before = live.read_bytes()
        stale = json.loads(self.data.read_text()); stale["title"] += " stale"; self.data.write_text(json.dumps(stale))
        result = self.call("start")
        self.assertNotEqual(result.returncode, 0); self.assertIn(str(live), result.stderr)
        self.assertEqual(live.read_bytes(), before)

    def test_stage_snapshots_and_commits_only_home_worktree(self):
        other_head = self.git(self.second, "rev-parse", "HEAD").stdout.strip()
        result = self.call("start", cwd=self.second); self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git(self.home, "log", "-1", "--format=%s").stdout.strip(), "Change list: snapshot live tracker")
        self.assertEqual(self.git(self.second, "rev-parse", "HEAD").stdout.strip(), other_head)
        self.assertEqual(self.git(self.second, "branch", "--show-current").stdout.strip(), "other")

    def test_merge_guard_rejects_unsnapshotted_live_then_passes(self):
        live = Path(self.call("adopt", self.data).stdout.strip())
        doc = json.loads(live.read_text()); doc["title"] += " updated"; live.write_text(json.dumps(doc))
        self.assertNotEqual(self.call("merge").returncode, 0)
        self.assertEqual(self.call("start", live).returncode, 0)
        self.assertEqual(self.call("merge").returncode, 0)

    def test_missing_home_worktree_marks_live_unsnapshotted(self):
        registry = self.state / "registry.json"; records = json.loads(registry.read_text())
        record = next(iter(records.values())); record["home"] = str(self.root / "missing-home"); registry.write_text(json.dumps(records))
        second_tracker = self.second / record["snapshot"]
        result = self.call("start", second_tracker, cwd=self.second)
        self.assertEqual(result.returncode, 0, result.stderr)
        live = Path(record["live"])
        self.assertTrue(live.with_suffix(".unsnapshotted").exists())
        self.assertNotEqual(self.call("merge", live).returncode, 0)

    def test_viewer_state_has_ephemeral_port_and_package_version(self):
        path = next(p for p in (self.state / "viewers").glob("*.json") if not p.name.endswith(".viewer.json"))
        state = json.loads(path.read_text())
        self.assertNotEqual(state["port"], 53366); self.assertNotEqual(state["pid"], 87005)
        self.assertEqual(state["version"], json.loads((PLUGIN / "plugin.json").read_text())["version"])
        self.assertIn("started_at", state)
        self.assertTrue(state["data"].endswith("123.json"))
        for _ in range(20):
            try:
                with socket.create_connection(("127.0.0.1", state["port"]), timeout=.2): break
            except OSError: time.sleep(.05)
        else: self.fail("viewer did not bind its recorded loopback port")

    def test_viewer_restarts_for_version_and_data_path_mismatch(self):
        path = next(p for p in (self.state / "viewers").glob("*.json") if not p.name.endswith(".viewer.json"))
        state = json.loads(path.read_text()); first_pid = state["pid"]
        state["version"] = "0.1.16"; path.write_text(json.dumps(state))
        self.assertEqual(self.call("status").returncode, 0)
        updated = json.loads(path.read_text()); self.assertNotEqual(updated["pid"], first_pid)
        updated["data"] = "/tmp/wrong.json"; path.write_text(json.dumps(updated))
        old_pid = updated["pid"]
        self.assertEqual(self.call("status").returncode, 0)
        self.assertNotEqual(json.loads(path.read_text())["pid"], old_pid)

if __name__ == "__main__": unittest.main()
