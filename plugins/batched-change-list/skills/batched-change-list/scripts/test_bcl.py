import importlib.util
from importlib.machinery import SourceFileLoader
import json, os, signal, socket, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[5]
PLUGIN = ROOT / "plugins/batched-change-list"
BCL = PLUGIN / "bin/bcl"
EXAMPLE = PLUGIN / "skills/batched-change-list/examples/findings.json"
sys.path.insert(0, str(PLUGIN / "skills/batched-change-list/scripts"))
import live_state
loader = SourceFileLoader("bcl_entrypoint", str(BCL))
spec = importlib.util.spec_from_loader("bcl_entrypoint", loader)
bcl_entrypoint = importlib.util.module_from_spec(spec)
loader.exec_module(bcl_entrypoint)

class BclTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.state = self.root / "state"
        self.bin = self.root / "bin"; self.bin.mkdir()
        self.command_log = self.root / "commands.jsonl"
        fake_cli = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ['BCL_COMMAND_LOG'], 'a') as log: log.write(json.dumps(args) + '\\n')
if args[:2] == ['pr', 'list']:
    if os.environ.get('BCL_FAKE_PR_LIST_FAIL'): sys.exit(1)
    print(json.dumps([{'number': 456}]))
elif args[:2] == ['pr', 'view']:
    print(json.dumps({'mergeCommit': {'oid': 'a' * 40}, 'mergedAt': '2026-11-14T12:00:00Z', 'headRefName': 'feature-123', 'state': 'MERGED'}))
elif args[:2] != ['pr', 'edit']:
    sys.exit(2)
"""
        for name in ("gh", "origin"):
            executable = self.bin / name; executable.write_text(fake_cli); executable.chmod(0o755)
        self.env = {**os.environ, "BCL_STATE_DIR": str(self.state), "BCL_COMMAND_LOG": str(self.command_log),
                    "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", "")}
        self.extra_pids = set()
        self.home = self.root / "home"; self.home.mkdir()
        self.git(self.home, "init", "-q"); self.git(self.home, "config", "user.email", "test@example.com"); self.git(self.home, "config", "user.name", "Test")
        self.git(self.home, "remote", "add", "github", "https://github.com/example/project.git")
        tracker = json.loads(EXAMPLE.read_text()); tracker["tracking"] = {"pull_request":{"label":"PR #123","url":"https://github.com/example/project/pull/123"}}
        (self.home / "docs/triage").mkdir(parents=True)
        self.data = self.home / "docs/triage/pr-123-findings.json"; self.data.write_text(json.dumps(tracker))
        self.page = self.data.with_suffix(".html"); self.page.write_text("initial")
        self.git(self.home, "add", "."); self.git(self.home, "commit", "-qm", "initial")
        self.second = self.root / "second"; self.git(self.home, "worktree", "add", "-qb", "other", str(self.second))
        other_viewer = self.state / "viewers" / "other-list.json"; other_viewer.parent.mkdir(parents=True, exist_ok=True)
        other_viewer.write_text(json.dumps({"port":53366, "data":"/another/list.json"}))
        self.call("adopt", self.data)
        def cleanup_viewer():
            pids = set(self.extra_pids)
            for path in (self.state / "viewers").glob("*.json") if (self.state / "viewers").exists() else []:
                try:
                    pid = json.loads(path.read_text()).get("pid")
                    if pid: pids.add(int(pid))
                except (OSError, ValueError): pass
            for pid in pids:
                if pid not in (87005, 28743):
                    try: os.kill(pid, signal.SIGTERM)
                    except OSError: pass
        self.addCleanup(cleanup_viewer)

    def git(self, cwd, *args): return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)
    def call(self, stage, *args, cwd=None):
        data, extra = (args[0], args[1:]) if args and isinstance(args[0], Path) else (self.data, args)
        return subprocess.run([sys.executable, str(BCL), "--data", str(data), *map(str, extra), stage], cwd=cwd, env=self.env, capture_output=True, text=True)

    def viewer_state_path(self, live=None):
        if live is None:
            live = Path(json.loads((self.state / "registry.json").read_text())["example/project/123"]["live"])
        expected = Path(live).resolve()
        for path in (self.state / "viewers").glob("*.json"):
            if path.name.endswith(".viewer.json"):
                continue
            try:
                state = json.loads(path.read_text())
                if state.get("pid") and Path(state.get("data", "")).resolve() == expected:
                    return path
            except (OSError, ValueError):
                continue
        self.fail(f"no viewer state recorded for {expected}")

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

    def test_rollover_preserves_old_list_and_defers_new_viewer_until_collect(self):
        old_live = Path(json.loads((self.state / "registry.json").read_text())["example/project/123"]["live"])
        doc = json.loads(old_live.read_text())
        doc["findings"][0]["status"] = "in_progress"
        doc["findings"][1]["status"] = "failing"
        doc["findings"].append({**doc["findings"][1], "id":"BLOCKED-01", "status":"blocked"})
        contents = (json.dumps(doc, indent=2) + "\n").encode()
        self.data.write_bytes(contents); old_live.write_bytes(contents)
        before = old_live.read_bytes()
        old_source = self.data.read_bytes(); old_page = self.page.read_bytes()
        result = self.call("rollover", "--new-pr", "124", "--worktree", "feature-124", "--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(old_live.read_bytes(), before)
        self.assertEqual(self.data.read_bytes(), old_source)
        self.assertEqual(self.page.read_bytes(), old_page)
        registry = json.loads((self.state / "registry.json").read_text())
        new_record = registry["example/project/124"]
        self.assertTrue(new_record["rollover_pending"])
        new_live = Path(new_record["live"])
        carried = {item["id"]: item["status"] for item in json.loads(new_live.read_text())["findings"]}
        self.assertEqual(carried["ISSUE-01"], "untested")
        self.assertEqual(carried["ISSUE-02"], "failing")
        self.assertEqual(carried["BLOCKED-01"], "blocked")
        self.assertNotIn("now", json.loads(new_live.read_text())["tracking"]["workflow"])
        viewers = [json.loads(path.read_text()) for path in (self.state / "viewers").glob("*.json") if not path.name.endswith(".viewer.json")]
        self.assertFalse(any(Path(viewer.get("data", "/missing")).resolve() == new_live.resolve() for viewer in viewers))
        old_viewer = json.loads(self.viewer_state_path(old_live).read_text())
        started = self.call("start", new_live, "--ids", "ISSUE-01")
        self.assertEqual(started.returncode, 0, started.stderr)
        new_viewer = json.loads(self.viewer_state_path(new_live).read_text())
        self.assertEqual(Path(new_viewer["data"]).resolve(), new_live.resolve())
        self.assertEqual(json.loads(self.viewer_state_path(old_live).read_text()), old_viewer)
        self.assertIn("Working on it", (self.home / registry["example/project/124"]["html"]).read_text())
        self.assertEqual(self.page.read_bytes(), old_page)

    def test_merged_records_terminal_state_and_retargets_stacked_prs(self):
        sha = "a" * 40; merged_at = "2026-11-14T12:00:00Z"
        result = self.call("merged")
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = json.loads(Path(json.loads((self.state / "registry.json").read_text())["example/project/123"]["live"]).read_text())
        self.assertEqual(doc["tracking"]["workflow"]["stage"], "merged")
        self.assertEqual(doc["tracking"]["merged"]["commit_sha"], sha)
        self.assertEqual(doc["tracking"]["merged"]["merged_at"], merged_at)
        self.assertEqual(doc["tracking"]["merged"]["stacked_prs"]["status"], "complete")
        self.assertEqual(doc["tracking"]["merged"]["stacked_prs"]["retargeted_prs"], [456])
        page = self.data.with_suffix(".html").read_text()
        self.assertIn("Merged / Done", page)
        self.assertNotIn('data-stage="report_rollover" class="wf-step current"', page)
        calls = [json.loads(line) for line in self.command_log.read_text().splitlines()]
        self.assertTrue(any(row[:3] == ["pr", "edit", "456"] and "--base" in row and "main" in row for row in calls))
        self.assertTrue(any(row[:3] == ["pr", "view", "https://github.com/example/project/pull/123"] for row in calls))
        self.assertTrue(any(row[:3] == ["pr", "list", "--repo"] and "--limit" in row and "1000" in row for row in calls))

    def test_merged_keeps_terminal_data_and_emits_todo_when_retarget_api_fails(self):
        doc = json.loads(self.data.read_text())
        doc["tracking"]["pull_request"]["url"] = "https://origin.example/example/project/pull/123"
        contents = (json.dumps(doc, indent=2) + "\n").encode()
        self.data.write_bytes(contents)
        live = Path(json.loads((self.state / "registry.json").read_text())["example/project/123"]["live"])
        live.write_bytes(contents)
        env = {**self.env, "BCL_FAKE_PR_LIST_FAIL":"1"}
        result = subprocess.run([sys.executable, str(BCL), "--data", str(live), "merged", "--merge-commit", "c" * 40,
                                 "--merged-at", "2026-11-14T12:00:00Z", "--base-branch", "feature-123"],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TODO:", result.stderr)
        saved = json.loads(live.read_text())["tracking"]["merged"]
        self.assertEqual(saved["stacked_prs"]["status"], "pending")
        self.assertIn("feature-123", saved["stacked_prs"]["todo"])

    def test_missing_home_worktree_marks_live_unsnapshotted(self):
        registry = self.state / "registry.json"; records = json.loads(registry.read_text())
        record = next(iter(records.values())); record["home"] = str(self.root / "missing-home"); registry.write_text(json.dumps(records))
        second_tracker = self.second / record["snapshot"]
        result = self.call("start", second_tracker, cwd=self.second)
        self.assertEqual(result.returncode, 0, result.stderr)
        live = Path(record["live"])
        self.assertTrue(live.with_suffix(".unsnapshotted").exists())
        self.assertNotEqual(self.call("merge", live).returncode, 0)

    def test_viewer_avoids_default_port_reserved_by_another_list(self):
        path = self.viewer_state_path()
        state = json.loads(path.read_text())
        self.assertEqual(json.loads((self.state / "viewers/other-list.json").read_text())["port"], 53366)
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
        path = self.viewer_state_path()
        state = json.loads(path.read_text()); first_pid = state["pid"]
        state["version"] = "0.1.16"; path.write_text(json.dumps(state))
        self.assertEqual(self.call("status").returncode, 0)
        updated = json.loads(path.read_text()); self.assertNotEqual(updated["pid"], first_pid)
        updated["data"] = "/tmp/wrong.json"; path.write_text(json.dumps(updated))
        old_pid = updated["pid"]
        self.extra_pids.add(old_pid)
        self.assertEqual(self.call("status").returncode, 0)
        self.assertNotEqual(json.loads(path.read_text())["pid"], old_pid)

    def test_collect_start_uses_default_port_when_free(self):
        self.call("rollover", "--new-pr", "124", "--worktree", "feature-124", "--force")
        records = json.loads((self.state / "registry.json").read_text())
        new_record = records["example/project/124"]
        new_live = Path(new_record["live"])
        (self.state / "viewers" / "other-list.json").unlink()
        binds = []

        class FakeSocket:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def bind(self, address): binds.append(address)

        real_popen = subprocess.Popen
        def safe_popen(command, *args, **kwargs):
            if len(command) > 1 and str(command[1]).endswith("/serve.py"):
                return SimpleNamespace(pid=87005)
            return real_popen(command, *args, **kwargs)

        with patch.dict(os.environ, self.env), patch.object(live_state, "STATE", self.state), \
             patch.object(live_state.socket, "socket", FakeSocket), \
             patch.object(bcl_entrypoint.subprocess, "Popen", safe_popen), \
             patch.object(sys, "argv", [str(BCL), "--data", str(new_live), "--ids", "ISSUE-01", "start"]):
            bcl_entrypoint.main()

        viewer = json.loads(self.viewer_state_path(new_live).read_text())
        self.assertEqual(viewer["port"], 53366)
        self.assertEqual(binds, [("127.0.0.1", 53366)])

    def test_viewer_reuses_recorded_default_port_when_this_list_owns_it(self):
        records = json.loads((self.state / "registry.json").read_text())
        key, record = next(iter(records.items()))
        live = Path(record["live"])
        viewer_path = self.state / "viewers" / (live_state.safe_component(key.replace("/", "--")) + ".json")
        current = json.loads(viewer_path.read_text())
        self.extra_pids.add(int(current["pid"]))
        viewer_path.write_text(json.dumps({"port":53366, "pid":None, "version":"old", "data":str(live)}))
        (self.state / "viewers" / "other-list.json").unlink()
        binds = []
        class FakeSocket:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def bind(self, address): binds.append(address)
        with patch.object(live_state, "STATE", self.state), patch.object(live_state.socket, "socket", FakeSocket), \
             patch.object(live_state.subprocess, "Popen", return_value=SimpleNamespace(pid=87005)) as popen:
            value = live_state.ensure_viewer(key, record, live.resolve(), "0.1.17")
        self.assertEqual(value["port"], 53366)
        self.assertEqual(binds, [("127.0.0.1", 53366)])
        self.assertEqual(popen.call_args.args[0][popen.call_args.args[0].index("--port") + 1], "53366")

if __name__ == "__main__": unittest.main()
