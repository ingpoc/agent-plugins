import importlib.util
import contextlib
import io
from importlib.machinery import SourceFileLoader
import errno, json, os, shutil, socket, subprocess, sys, tempfile, threading, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

def plugin_root(skill_dir, installed_root=None):
    candidate = skill_dir.parents[1]
    manifest = candidate / "plugin.json"
    if (candidate / "bin/bcl").is_file() and manifest.is_file() and json.loads(manifest.read_text()).get("name") == "batched-change-list":
        return candidate
    return Path(installed_root or Path.home() / ".agents/plugins/batched-change-list")

SKILL_DIR = Path(__file__).resolve().parents[1]
PLUGIN = plugin_root(SKILL_DIR)
BCL = PLUGIN / "bin/bcl"
if not BCL.is_file():
    raise unittest.SkipTest("plugin bin is not shipped beside the skill and is not installed")
EXAMPLE = PLUGIN / "skills/batched-change-list/examples/findings.json"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_state
import viewer_test_guard
import check_introspect
loader = SourceFileLoader("bcl_entrypoint", str(BCL))
spec = importlib.util.spec_from_loader("bcl_entrypoint", loader)
bcl_entrypoint = importlib.util.module_from_spec(spec)
try:
    loader.exec_module(bcl_entrypoint)
finally:
    sys.path.remove(str(bcl_entrypoint.SCRIPTS))

def tearDownModule():
    viewer_test_guard.assert_no_viewers(viewer_test_guard.TEMP_ROOTS)

class BclTests(unittest.TestCase):
    def test_plugin_root_stays_inside_plugin_or_installed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); plugin = root / "plugins/batched-change-list"
            skill = plugin / "skills/batched-change-list"; skill.mkdir(parents=True)
            (plugin / "bin").mkdir(); (plugin / "bin/bcl").write_text("")
            (plugin / "plugin.json").write_text('{"name":"batched-change-list"}')
            self.assertEqual(plugin_root(skill), plugin)
            installed = root / "installed/batched-change-list"
            self.assertEqual(plugin_root(root / "outside/skills/batched-change-list", installed), installed)

    def test_verify_failure_reason_has_checked_expected_and_observed(self):
        reason = bcl_entrypoint._verification_failure_reason('python3 check.py', 'Verification command exited 7')
        self.assertIn('Checked: verification command', reason)
        self.assertIn('Expected: command exits 0', reason)
        self.assertIn('Observed: Verification command exited 7', reason)
        self.assertNotIn('check.py', reason)

    def test_status_labels_dead_unfinished_lane_job_as_interrupted(self):
        registry = json.loads((self.state / "registry.json").read_text())
        live = Path(registry["example/project/123"]["live"])
        for status, phase in (("running", "fix"), ("review", "review"), ("review", "presuite")):
            with self.subTest(status=status, phase=phase):
                data = json.loads(live.read_text())
                data.setdefault("tracking", {})["plan"] = {"round":1,"groups":[{
                    "id":"A","finding_ids":["ISSUE-01"],"status":status,"phase":"review" if phase != "fix" else "fix",
                    "codex":{"job":{"pid":987654321,"phase":phase,"started_at":"2026-11-18T08:00:00Z"}}}]}
                live.write_text(json.dumps(data))
                self.data.write_text(json.dumps(data))
                result = subprocess.run([sys.executable, str(BCL), "--data", str(self.data), "status"],
                                        env=self.env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                lane = json.loads(result.stdout)["plan"][0]
                self.assertEqual(lane["message"], "Interrupted · resume needed · A")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.state = self.root / "state"
        self.addCleanup(self.cleanup_viewers)
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
                    "BCL_MAX_LOAD": "100000",
                    "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", "")}
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
        self.call_without_viewer("adopt", self.data)
        record = next(iter(json.loads((self.state / "registry.json").read_text()).values()))
        key = "example/project/123"
        self.viewer_port = 40000 + os.getpid() % 19000
        while self.viewer_port in {53366, 8765, 8766, 52432, 57666} or not live_state._port_is_free(self.viewer_port):
            self.viewer_port = 40000 + (self.viewer_port - 39999) % 19000
        viewer = self.state / "viewers" / (live_state.safe_component(key.replace("/", "--")) + ".json")
        viewer.write_text(json.dumps({"port": self.viewer_port, "pid": None, "data": record["live"]}))

    def cleanup_viewers(self):
        owned = viewer_test_guard.viewer_pids_under([self.root])
        viewer_test_guard.stop_viewers(self.root, owned)

    def test_suite_leaves_no_viewer_processes(self):
        proc = subprocess.Popen([sys.executable, str(live_state.SCRIPTS / "serve.py"), str(self.page), "--port", str(self.viewer_port)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not live_state._viewer_process(proc.pid, self.page):
                time.sleep(.05)
            self.assertTrue(live_state._viewer_process(proc.pid, self.page))
        finally:
            self.cleanup_viewers()
            proc.wait(timeout=5)
        self.assertFalse(live_state._pid_is_running(proc.pid), "orphan process still running")

    def test_hotfix_adds_lane_outside_plan_limits_and_runs_fix(self):
        registry = json.loads((self.state / "registry.json").read_text())
        live = Path(registry["example/project/123"]["live"])
        doc = json.loads(live.read_text())
        doc["findings"][0]["id"] = "BCL-119"
        doc["tracking"].update(cycle=1, round=1, workflow={"stage": "gate", "updated_at": "2026-11-14T12:00:00Z"}, plan={"cycle": 1, "round": 1,
            "groups": [{"id": f"LANE-{i}", "finding_ids": ["ISSUE-02"], "status": "planned"} for i in range(4)]})
        live.write_text(json.dumps(doc))
        self.data.write_text(live.read_text())
        result = self.call_without_viewer("hotfix", "--ids", "BCL-119", "--reason", "Fix loop blocker")
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = json.loads(live.read_text())
        lane = doc["tracking"]["plan"]["groups"][-1]
        self.assertEqual(lane["id"], "HOTFIX-BCL-119")
        self.assertEqual(lane["finding_ids"], ["BCL-119"])
        self.assertEqual(lane["status"], "planned")
        self.assertEqual(lane["hotfix"]["reason"], "Fix loop blocker")
        job = self.call_job_without_viewer(live, "--ids", "BCL-119", "--phase", "fix", "job", "--", sys.executable, "-c", "pass")
        self.assertEqual(job.returncode, 0, job.stderr)

    def test_hotfix_rejects_invalid_and_second_open_lane(self):
        missing = self.call_without_viewer("hotfix", "--ids", "BCL-119")
        self.assertNotEqual(missing.returncode, 0)
        invalid = self.call_without_viewer("hotfix", "--ids", "ISSUE-01", "--reason", "why")
        self.assertNotEqual(invalid.returncode, 0)
        registry = json.loads((self.state / "registry.json").read_text())
        live = Path(registry["example/project/123"]["live"])
        doc = json.loads(live.read_text())
        doc["findings"][0]["id"] = "BCL-119"
        doc["tracking"].update(cycle=1, round=1, plan={"cycle": 1, "round": 1, "groups": [
            {"id": "HOTFIX-BCL-OTHER", "finding_ids": ["BCL-119"], "status": "planned", "hotfix": {"reason": "existing"}}]})
        live.write_text(json.dumps(doc))
        self.data.write_text(live.read_text())
        second = self.call_without_viewer("hotfix", "--ids", "BCL-119", "--reason", "again")
        self.assertNotEqual(second.returncode, 0)

    @staticmethod
    def merge_ready(document):
        template = document["findings"][0]
        document["findings"] = []
        for index in range(16):
            finding = json.loads(json.dumps(template))
            finding.update(id=f"ISSUE-{index + 1:02}", status="verified_fixed", verification="Verified", verification_refs=["receipt"])
            document["findings"].append(finding)
        document["tracking"].update(stages_reached=["report_rollover"], rollover_threshold=15)
        return document

    def test_install_local_refreshes_only_the_requested_temp_destinations(self):
        checkout = self.root / "checkout"
        shutil.copytree(PLUGIN, checkout)
        (checkout / "skills/batched-change-list/scripts/__pycache__").mkdir(exist_ok=True)
        (checkout / "skills/batched-change-list/scripts/__pycache__/stale.pyc").write_bytes(b"cache")
        (checkout / "bin/stale.pyc").write_bytes(b"bytecode")
        (checkout / ".DS_Store").write_bytes(b"finder")
        self.git(checkout, "init", "-q")
        self.git(checkout, "config", "user.email", "test@example.com")
        self.git(checkout, "config", "user.name", "Test")
        self.git(checkout, "add", ".")
        self.git(checkout, "add", "-f", "skills/batched-change-list/references/review.md")
        self.git(checkout, "commit", "-qm", "fixture")
        home = self.root / "install-home"
        old_plugin = home / ".agents/plugins/batched-change-list"
        old_skill = home / ".agents/skills/batched-change-list"
        for old in (old_plugin, old_skill):
            old.mkdir(parents=True)
            (old / "old-copy").write_text("remove")
        skill_sha, skill_count = bcl_entrypoint._install_tree_digest(checkout / "skills/batched-change-list")
        version = json.loads((checkout / "plugin.json").read_text())["version"]
        revision = self.git(checkout, "rev-parse", "HEAD").stdout.strip()
        with patch.object(bcl_entrypoint, "HERE", checkout), patch.object(Path, "home", return_value=home), \
             patch.object(sys, "argv", [str(BCL), "install-local"]), contextlib.redirect_stdout(io.StringIO()) as receipt:
            bcl_entrypoint.main()
        plugin_sha, plugin_count = bcl_entrypoint._install_tree_digest(checkout)
        plugin_destinations = [
            home / ".agents/plugins/batched-change-list",
            *(home / f".{client}/plugins/cache/ingpoc-agent-plugins/batched-change-list/{ref}"
              for client in ("cursor", "codex") for ref in (version, revision)),
        ]
        skill_destination = home / ".agents/skills/batched-change-list"
        self.assertIn(revision, receipt.getvalue())
        self.assertEqual(receipt.getvalue().count(plugin_sha), len(plugin_destinations))
        self.assertEqual(receipt.getvalue().count(skill_sha), 1)
        self.assertEqual(receipt.getvalue().count(f"({plugin_count} files"), len(plugin_destinations))
        self.assertEqual(receipt.getvalue().count(f"({skill_count} files"), 1)
        for destination in plugin_destinations:
            self.assertTrue((destination / "plugin.json").is_file())
            self.assertFalse((destination / "old-copy").exists())
            self.assertFalse(any(path.name == ".DS_Store" or path.suffix == ".pyc" or "__pycache__" in path.parts
                                 for path in destination.rglob("*")))
            self.assertEqual(bcl_entrypoint._install_tree_digest(destination), (plugin_sha, plugin_count))
        self.assertTrue((skill_destination / "SKILL.md").is_file())
        self.assertTrue((skill_destination / "scripts").is_dir())
        self.assertFalse((skill_destination / "plugin.json").exists())
        self.assertFalse((skill_destination / "skills").exists())
        self.assertFalse((skill_destination / "old-copy").exists())
        self.assertFalse(any(path.name == ".DS_Store" or path.suffix == ".pyc" or "__pycache__" in path.parts
                             for path in skill_destination.rglob("*")))
        self.assertEqual(bcl_entrypoint._install_tree_digest(skill_destination), (skill_sha, skill_count))

    def test_repo_id_uses_cursor_origin_over_github_mirror(self):
        self.git(self.home, "remote", "add", "origin", "https://origin.cursor.com/gurusharan/agent-plugins")
        self.assertEqual(live_state.repo_id(self.home), "example/project")
        self.assertEqual(live_state.repo_id(self.home, "origin"), "gurusharan/agent-plugins")
        self.assertEqual(bcl_entrypoint._provider("https://github.com/ingpoc/agent-plugins/pull/110"), "gh")
        self.assertEqual(bcl_entrypoint._provider("https://cursor.com/codebase/gurusharan/agent-plugins/pull/110"), "origin")

    def test_plan_reports_viewer_pid_permission_error_without_traceback(self):
        self.assertEqual(self.call("start").returncode, 0)
        stderr = io.StringIO()
        plan = self.root / "plan.json"
        plan.write_text(json.dumps({"round": 1, "groups": []}))
        with patch.object(live_state, "STATE", self.state), patch.object(sys, "argv", [str(BCL), "--data", str(self.data), "--plan", str(plan), "plan"]), \
             patch.object(live_state.os, "kill", side_effect=PermissionError("operation not permitted")), \
             contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as exit_result:
            bcl_entrypoint.main()
        self.assertEqual(exit_result.exception.code, 1, stderr.getvalue())
        self.assertEqual(len(stderr.getvalue().splitlines()), 1, stderr.getvalue())
        self.assertRegex(stderr.getvalue(), r"\Abcl: viewer PID probe failed: (?:\[Errno 1\] )?[Oo]peration not permitted\n\Z")

    def test_plan_reports_non_probe_oserror_without_pid_probe_label(self):
        stderr = io.StringIO()
        parser = bcl_entrypoint.argparse.ArgumentParser()
        with patch.object(live_state, "ensure_viewer", side_effect=OSError("metadata write failed")), \
             contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as exit_result:
            bcl_entrypoint._ensure_viewer(parser, "repo/123", {}, Path("tracker.json"), "0.1.17", "plan")
        self.assertEqual(exit_result.exception.code, 2)
        self.assertIn("metadata write failed", stderr.getvalue())
        self.assertNotIn("viewer PID probe failed", stderr.getvalue())

    def git(self, cwd, *args): return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)
    def call(self, stage, *args, cwd=None):
        data, extra = (args[0], args[1:]) if args and isinstance(args[0], Path) else (self.data, args)
        if stage == "plan" and "--plan" in extra:
            plan_path = Path(extra[extra.index("--plan") + 1])
            plan = json.loads(plan_path.read_text())
            for group in plan.get("groups", []):
                codex = group.setdefault("codex", {})
                codex.setdefault("implementer_session", "fixture-implementer")
                codex.setdefault("reviewer_session", "fixture-reviewer")
                codex.setdefault("review_status", "pending")
            plan_path.write_text(json.dumps(plan))
        return subprocess.run([sys.executable, str(BCL), "--data", str(data), *map(str, extra), stage], cwd=cwd, env=self.env, capture_output=True, text=True)

    def prepare_review(self, ids=None):
        live = Path(json.loads((self.state / "registry.json").read_text())["example/project/123"]["live"])
        document = json.loads(live.read_text())
        tracking = document.setdefault("tracking", {})
        plan = tracking.get("plan", {})
        for group in plan.get("groups", []):
            if ids and not set(ids).intersection(group.get("finding_ids", [])): continue
            codex = group.setdefault("codex", {})
            codex.update(implementer_session="fixture-implementer", reviewer_session="fixture-reviewer",
                         fix_status="passed", review_status="passed", job={"phase":"fix", "exit_code":0,
                         "finished_phase_status":"passed"})
            group.update(status="review", phase="review", presuite={"status":"passed"})
        reached = tracking.setdefault("stages_reached", [])
        for stage in ("plan_fix", "review"):
            if stage not in reached: reached.append(stage)
        tracking["workflow"] = {"stage":"review", "updated_at":"2026-11-18T00:00:00Z"}
        payload = json.dumps(document)
        live.write_text(payload)
        self.data.write_text(payload)

    def install_after_review(self, *args):
        ids = [args[i + 1] for i, value in enumerate(args[:-1]) if value == "--ids"]
        self.prepare_review(ids or None)
        return self.call("install", *args)

    def _active_verification(self):
        live = Path(json.loads((self.state / "registry.json").read_text())["example/project/123"]["live"])
        manager = PLUGIN / "skills/batched-change-list/scripts/manage_findings.py"
        run = {"id":"verify-once", "build":"1.0", "revision":"abc", "installed_at":"2026-11-18T00:00:00Z", "install_evidence":"fixture receipt"}
        result = subprocess.run([sys.executable, str(manager), "batch", str(live), "-", "--render", str(self.page)],
            input=json.dumps([{"action":"start-verification", "run":run}]), text=True, capture_output=True, env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.data.write_bytes(live.read_bytes())
        return live

    def _lane_ready(self, *install_args):
        self.assertEqual(self.call_without_viewer("start").returncode, 0)
        check = self.root / "lane.py"; check.write_text("pass\n")
        command = f'"{sys.executable}" "{check}"'
        plan = self.root / "verify-plan.json"
        plan.write_text(json.dumps({"round":1,"groups":[{"id":"A","finding_ids":["ISSUE-01"],"status":"pending","verify":{"command":command}}]}))
        self.assertEqual(self.call_without_viewer("plan", "--plan", str(plan), "--ids", "ISSUE-01").returncode, 0)
        self.prepare_review(["ISSUE-01"])
        self.assertEqual(self.call_without_viewer("install", "--ids", "ISSUE-01", *install_args).returncode, 0)
        live = self._active_verification()
        return live, command

    def test_shared_check_is_recorded_once_for_active_install(self):
        live = self._active_verification()
        marker = self.root / "shared-ran"
        command_file = self.root / "shared.py"; command_file.write_text(f"from pathlib import Path; Path({str(marker)!r}).touch()\n")
        command = f'"{sys.executable}" "{command_file}"'
        result = self.call("verify", "--shared", "--then", command)
        self.assertEqual(result.returncode, 0, result.stderr)
        check = json.loads(live.read_text())["tracking"]["verification_run"]["shared_check"]
        self.assertEqual(check, {"command":command,"state":"passed","exit_code":0,"finished_at":check["finished_at"]})
        self.assertTrue(marker.exists())
        self.assertEqual(result.stdout.count("Shared checks passed once for this install"), 1)
        self.assertIn("Shared checks passed once for this install", self.page.read_text())

    def test_second_shared_check_on_same_run_is_refused(self):
        self._active_verification()
        command = f'"{sys.executable}" -c "pass"'
        self.assertEqual(self.call("verify", "--shared", "--then", command).returncode, 0)
        second = self.call("verify", "--shared", "--then", command)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("already recorded", second.stderr)

    def test_lane_verify_is_refused_after_failed_shared_check(self):
        live, command = self._lane_ready()
        fail = self.root / "shared-fail.py"; fail.write_text("raise SystemExit(7)\n")
        shared_command = f'"{sys.executable}" "{fail}"'
        self.assertNotEqual(self.call("verify", "--shared", "--then", shared_command).returncode, 0)
        lane = self.call("verify", "--ids", "ISSUE-01", "--then", command)
        self.assertNotEqual(lane.returncode, 0)
        self.assertIn("requires the shared check to pass first", lane.stderr)
        self.assertEqual(json.loads(live.read_text())["tracking"]["verification_run"]["shared_check"]["state"], "failed")

    def test_failed_shared_check_reruns_with_reason_and_keeps_the_attempt(self):
        live, command = self._lane_ready()
        flag = self.root / "shared-ok"
        check = self.root / "shared-flaky.py"
        check.write_text(f"from pathlib import Path\nraise SystemExit(0 if Path({str(flag)!r}).exists() else 7)\n")
        shared_command = f'"{sys.executable}" "{check}"'
        self.assertNotEqual(self.call("verify", "--shared", "--then", shared_command).returncode, 0)
        bare = self.call("verify", "--shared", "--then", shared_command)
        self.assertNotEqual(bare.returncode, 0)
        self.assertIn("--reason", bare.stderr)
        flag.touch()
        rerun = self.call("verify", "--shared", "--reason", "script lacked execute permission", "--then", shared_command)
        self.assertEqual(rerun.returncode, 0, rerun.stderr)
        run = json.loads(live.read_text())["tracking"]["verification_run"]
        self.assertEqual(run["shared_check"]["state"], "passed")
        self.assertEqual(run["shared_check"]["rerun_reason"], "script lacked execute permission")
        self.assertEqual([a["state"] for a in run["shared_check_attempts"]], ["failed"])
        again = self.call("verify", "--shared", "--reason", "again", "--then", shared_command)
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("already recorded", again.stderr)
        self.assertEqual(self.call("verify", "--ids", "ISSUE-01", "--then", command).returncode, 0)

    def test_shared_check_refuses_a_script_without_execute_permission(self):
        live = self._active_verification()
        script = self.root / "shared.sh"; script.write_text("#!/bin/sh\nexit 0\n"); script.chmod(0o644)
        refused = self.call("verify", "--shared", "--then", str(script))
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("not executable", refused.stderr)
        self.assertIsNone(json.loads(live.read_text())["tracking"]["verification_run"].get("shared_check"))
        missing = self.call("verify", "--shared", "--then", str(self.root / "missing.sh"))
        self.assertIn("not executable", missing.stderr)
        script.chmod(0o755)
        self.assertEqual(self.call("verify", "--shared", "--then", str(script)).returncode, 0)

    def test_install_refuses_a_lane_verify_script_without_execute_permission(self):
        self.assertEqual(self.call_without_viewer("start").returncode, 0)
        script = self.root / "lane.sh"; script.write_text("#!/bin/sh\nexit 0\n"); script.chmod(0o644)
        plan = self.root / "verify-plan.json"
        plan.write_text(json.dumps({"round":1,"groups":[{"id":"A","finding_ids":["ISSUE-01"],"status":"pending","verify":{"command":str(script)}}]}))
        self.assertEqual(self.call_without_viewer("plan", "--plan", str(plan), "--ids", "ISSUE-01").returncode, 0)
        self.prepare_review(["ISSUE-01"])
        refused = self.call_without_viewer("install", "--ids", "ISSUE-01")
        self.assertNotEqual(refused.returncode, 0)
        script.chmod(0o755)
        self.assertEqual(self.call_without_viewer("install", "--ids", "ISSUE-01").returncode, 0)

    def test_lane_verify_without_shared_check_is_unchanged(self):
        _, command = self._lane_ready()
        lane = self.call("verify", "--ids", "ISSUE-01", "--then", command)
        self.assertEqual(lane.returncode, 0, lane.stderr)

    def test_verify_records_fixed_result_from_a_verifying_lane(self):
        live, command = self._lane_ready()
        check = Path(command.split('"')[3])
        manager = PLUGIN / "skills/batched-change-list/scripts/manage_findings.py"
        check.write_text(
            "import json, subprocess, sys\n"
            "from pathlib import Path\n"
            f"live = Path({str(live)!r})\n"
            "document = json.loads(live.read_text())\n"
            "assert document['tracking']['plan']['groups'][0]['status'] == 'verifying'\n"
            f"manager = {str(manager)!r}\n"
            f"page = {str(self.page)!r}\n"
            "operations = [{'action':'update','id':'ISSUE-01','patch':{'status':'verified_fixed',"
            "'verification':'The behavior was fixed.','verification_refs':['Verification receipt']}}]\n"
            "result = subprocess.run([sys.executable, manager, 'batch', str(live), '-', '--render', page], "
            "input=json.dumps(operations), text=True, capture_output=True)\n"
            "if result.returncode:\n print(result.stderr or result.stdout, file=sys.stderr)\n"
            "raise SystemExit(result.returncode)\n")
        result = self.call_without_viewer("verify", "--ids", "ISSUE-01", "--then", command)
        self.assertEqual(result.returncode, 0, result.stderr)
        group = json.loads(live.read_text())["tracking"]["plan"]["groups"][0]
        self.assertEqual(group["status"], "verified")
        self.assertEqual(group["phase"], "verify")

    def test_install_and_verify_record_the_model_shown_for_the_lane(self):
        live, command = self._lane_ready("--model", "release script")
        group = json.loads(live.read_text())["tracking"]["plan"]["groups"][0]
        self.assertEqual((group["stage_model"]["stage"], group["stage_model"]["model"]), ("install", "release script"))
        check = Path(command.split('"')[3])
        seen = self.root / "seen-model.json"
        check.write_text(
            "import json\n"
            "from pathlib import Path\n"
            f"group = json.loads(Path({str(live)!r}).read_text())['tracking']['plan']['groups'][0]\n"
            f"Path({str(seen)!r}).write_text(json.dumps(group.get('stage_model')))\n")
        result = self.call_without_viewer("verify", "--ids", "ISSUE-01", "--model", "Grok Bot subagent (typed verify)", "--then", command)
        self.assertEqual(result.returncode, 0, result.stderr)
        stage_model = json.loads(seen.read_text())
        self.assertEqual((stage_model["stage"], stage_model["model"]), ("verify", "Grok Bot subagent (typed verify)"))

    def test_verify_without_model_records_a_scripted_check(self):
        live, command = self._lane_ready("--model", "release script")
        result = self.call_without_viewer("verify", "--ids", "ISSUE-01", "--then", command)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("stage_model", json.loads(live.read_text())["tracking"]["plan"]["groups"][0])

    def test_failed_lane_check_reruns_once_per_round_with_a_reason(self):
        live, command = self._lane_ready()
        check = Path(command.split('"')[3])
        check.write_text("raise SystemExit(7)\n")
        self.assertEqual(self.call_without_viewer("verify", "--ids", "ISSUE-01", "--then", command).returncode, 7)
        refused = self.call_without_viewer("verify", "--ids", "ISSUE-01", "--then", command)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("--reason", refused.stderr)
        check.write_text("pass\n")
        rerun = self.call_without_viewer("verify", "--ids", "ISSUE-01", "--reason", "verifier needed a git repo", "--then", command)
        self.assertEqual(rerun.returncode, 0, rerun.stderr)
        group = json.loads(live.read_text())["tracking"]["plan"]["groups"][0]
        self.assertEqual(len(group["verify_reruns"]), 1)
        self.assertEqual(group["verify_reruns"][0]["reason"], "verifier needed a git repo")
        self.assertIn("exited 7", group["verify_reruns"][0]["previous_failure"])
        self.assertNotIn("failure", group["verify"])
        check.write_text("raise SystemExit(7)\n")
        self.assertEqual(self.call_without_viewer("verify", "--ids", "ISSUE-01", "--then", command).returncode, 7)
        again = self.call_without_viewer("verify", "--ids", "ISSUE-01", "--reason", "second try at the same check", "--then", command)
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("already rerun this round", again.stderr)

    def test_refused_rerun_spends_nothing(self):
        live, command = self._lane_ready()
        check = Path(command.split('"')[3])
        check.write_text("raise SystemExit(7)\n")
        self.assertEqual(self.call_without_viewer("verify", "--ids", "ISSUE-01", "--then", command).returncode, 7)
        wrong = self.call_without_viewer("verify", "--ids", "ISSUE-01", "--reason", "verifier needed a git repo", "--then", command + " extra")
        self.assertNotEqual(wrong.returncode, 0)
        self.assertIn("exact stored command", wrong.stderr)
        group = json.loads(live.read_text())["tracking"]["plan"]["groups"][0]
        self.assertEqual(group["status"], "failed", "a refused rerun leaves the lane failed")
        self.assertNotIn("verify_reruns", group, "a refused rerun does not spend the once-per-round slot")
        check.write_text("pass\n")
        rerun = self.call_without_viewer("verify", "--ids", "ISSUE-01", "--reason", "verifier needed a git repo", "--then", command)
        self.assertEqual(rerun.returncode, 0, rerun.stderr)
        self.assertEqual(len(json.loads(live.read_text())["tracking"]["plan"]["groups"][0]["verify_reruns"]), 1)

    def test_reason_is_refused_for_a_lane_that_did_not_fail(self):
        _, command = self._lane_ready()
        result = self.call_without_viewer("verify", "--ids", "ISSUE-01", "--reason", "no failure to rerun here", "--then", command)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("none of these lanes failed", result.stderr)

    def test_correct_round_moves_the_counter_forward_and_keeps_history(self):
        self.assertEqual(self.call_without_viewer("start").returncode, 0)
        plan = self.root / "round-plan.json"
        plan.write_text(json.dumps({"round": 1, "groups": [{"id": "A", "finding_ids": ["ISSUE-01"], "status": "planned"}]}))
        self.assertEqual(self.call_without_viewer("plan", "--plan", str(plan), "--ids", "ISSUE-01").returncode, 0)
        self.assertNotEqual(self.call_without_viewer("correct-round", "--round", "3").returncode, 0)
        result = self.call_without_viewer("correct-round", "--round", "3", "--reason", "two rounds ran without rollover")
        self.assertEqual(result.returncode, 0, result.stderr)
        live = Path(json.loads((self.state / "registry.json").read_text())["example/project/123"]["live"])
        tracking = json.loads(live.read_text())["tracking"]
        self.assertEqual((tracking["round"], tracking["plan"]["round"]), (3, 3))
        self.assertEqual(tracking["round_corrections"][0]["from"], 1)
        self.assertEqual(tracking["round_corrections"][0]["reason"], "two rounds ran without rollover")
        self.assertIn("Round 3", self.page.read_text())
        backwards = self.call_without_viewer("correct-round", "--round", "2", "--reason", "trying to go back down")
        self.assertNotEqual(backwards.returncode, 0)
        self.assertIn("above the current round 3", backwards.stderr)

    def test_verify_records_failure_after_its_lane_enters_verifying(self):
        live, command = self._lane_ready()
        check = Path(command.split('"')[3])
        check.write_text(
            "import json\n"
            "from pathlib import Path\n"
            f"document = json.loads(Path({str(live)!r}).read_text())\n"
            "assert document['tracking']['plan']['groups'][0]['status'] == 'verifying'\n"
            "raise SystemExit(7)\n")
        result = self.call_without_viewer("verify", "--ids", "ISSUE-01", "--then", command)
        self.assertEqual(result.returncode, 7, result.stderr)
        group = json.loads(live.read_text())["tracking"]["plan"]["groups"][0]
        self.assertEqual(group["status"], "failed")
        self.assertEqual(group["phase"], "verify")
        self.assertIn("exited 7", group["verify"]["failure"])

    def call_without_viewer(self, stage, *args):
        output, error = io.StringIO(), io.StringIO()
        data, extra = (args[0], args[1:]) if args and isinstance(args[0], Path) else (self.data, args)
        argv = [str(BCL), "--data", str(data), *map(str, extra), stage]
        with patch.object(sys, "argv", argv), patch.object(bcl_entrypoint, "_ensure_viewer"), \
                patch.object(live_state, "STATE", self.state), patch.dict(os.environ, self.env), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            try:
                bcl_entrypoint.main()
                code = 0
            except SystemExit as result:
                code = result.code if isinstance(result.code, int) else 1
        return SimpleNamespace(returncode=code, stdout=output.getvalue(), stderr=error.getvalue())

    def call_job_without_viewer(self, live, *args):
        output, error = io.StringIO(), io.StringIO()
        argv = [str(BCL), "--data", str(live), *map(str, args)]
        with patch.object(sys, "argv", argv), patch.object(bcl_entrypoint, "_ensure_viewer"), \
                patch.object(live_state, "STATE", self.state), patch.dict(os.environ, self.env), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            try:
                bcl_entrypoint.main()
                code = 0
            except SystemExit as result:
                code = result.code if isinstance(result.code, int) else 1
        return SimpleNamespace(returncode=code, stdout=output.getvalue(), stderr=error.getvalue())

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
        self.assertEqual(state["live"], self.call_without_viewer("adopt", self.second / "docs/triage/pr-123-findings.json").stdout.strip())

    def test_adopt_accepts_tracker_as_positional_argument(self):
        result = subprocess.run([sys.executable, str(BCL), "adopt", str(self.data)], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_two_concurrent_jobs_both_finish(self):
        live = Path(json.loads((self.state / "registry.json").read_text())["example/project/123"]["live"])
        tracker = json.loads(self.data.read_text())
        tracker["findings"][1]["status"] = "untested"
        tracker["findings"][1].pop("verification", None)
        tracker["findings"][1].pop("verification_refs", None)
        self.data.write_text(json.dumps(tracker))
        live.write_text(self.data.read_text())
        self.assertEqual(self.call_without_viewer("start").returncode, 0)
        plan = self.root / "concurrent-plan.json"
        plan.write_text(json.dumps({"round": 1, "groups": [
            {"id": "A", "finding_ids": ["ISSUE-01"], "status": "planned"},
            {"id": "B", "finding_ids": ["ISSUE-02"], "status": "planned"}]}))
        prepared = self.call("plan", "--plan", str(plan), "--ids", "ISSUE-01", "--ids", "ISSUE-02")
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        commands = [[sys.executable, str(BCL), "--data", str(self.data), "--ids", issue,
                     "--phase", "fix", "job", "--", sys.executable, "-c",
                     f"import time; time.sleep(0.35); raise SystemExit({code})"]
                    for issue, code in (("ISSUE-01", 0), ("ISSUE-02", 7))]
        processes = [subprocess.Popen(command, env=self.env, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True) for command in commands]
        results = [process.communicate(timeout=30) for process in processes]
        self.assertEqual([process.returncode for process in processes], [0, 7], results)
        groups = {group["id"]: group for group in json.loads(live.read_text())["tracking"]["plan"]["groups"]}
        self.assertEqual([groups[key]["codex"]["job"]["exit_code"] for key in ("A", "B")], [0, 7])
        self.assertEqual([groups[key]["status"] for key in ("A", "B")], ["review", "failed"])

    def test_passed_fix_job_is_not_shown_planned(self):
        self.assertEqual(self.call_without_viewer("start").returncode, 0)
        plan = self.root / "passed-fix-plan.json"
        plan.write_text(json.dumps({"round": 1, "groups": [{
            "id": "A", "finding_ids": ["ISSUE-01"], "status": "planned"}]}))
        prepared = self.call("plan", "--plan", str(plan), "--ids", "ISSUE-01")
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        result = subprocess.run([sys.executable, str(BCL), "--data", str(self.data), "--ids", "ISSUE-01",
            "--phase", "fix", "job", "--", sys.executable, "-c", "pass"],
            env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        group = json.loads(self.data.read_text())["tracking"]["plan"]["groups"][0]
        self.assertEqual((group["status"], group["phase"]), ("review", "review"))
        self.assertIn('class="lc rev"', self.page.read_text())

    def test_fix_then_review_then_presuite_succeeds(self):
        self.assertEqual(self.call_without_viewer("start").returncode, 0)
        plan = self.root / "review-order-plan.json"
        plan.write_text(json.dumps({"round": 1, "groups": [{"id": "A", "finding_ids": ["ISSUE-01"],
            "status": "planned", "codex": {"implementer_session": "writer"}}]}))
        prepared = self.call_without_viewer("plan", "--plan", str(plan), "--ids", "ISSUE-01")
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        review_result = self.root / "review-result.txt"
        live = Path(json.loads((self.state / "registry.json").read_text())["example/project/123"]["live"])
        def job(phase):
            command = [sys.executable, "-c", f"from pathlib import Path; Path({str(review_result)!r}).write_text('VERDICT: PASS\\n## Must-fix\\nnone\\n')"] if phase == "review" else [sys.executable, "-c", "pass"]
            args = ["--ids", "ISSUE-01", *( ["--result", str(review_result)] if phase == "review" else []),
                    "--phase", phase, "job", "--", *command]
            return self.call_job_without_viewer(live, *args)
        self.assertEqual(job("fix").returncode, 0)
        reviewed = self.call_without_viewer("review", "--ids", "ISSUE-01")
        self.assertEqual(reviewed.returncode, 0, reviewed.stderr)
        review_job = job("review")
        self.assertEqual(review_job.returncode, 0, review_job.stderr)
        self.assertEqual(job("presuite").returncode, 0)
        group = json.loads(self.data.read_text())["tracking"]["plan"]["groups"][0]
        self.assertEqual(group["codex"]["fix_status"], "passed")
        self.assertEqual(group["codex"]["review_status"], "passed")
        self.assertNotIn("reviewer_session", group["codex"])

    def _review_job_verdict(self, cases):
        live = Path(json.loads((self.state / "registry.json").read_text())["example/project/123"]["live"])
        document = json.loads(self.data.read_text())
        document["findings"][0]["status"] = "in_progress"
        document["tracking"].update(stages_reached=["collect", "plan_fix", "review"],
            workflow={"stage": "review", "updated_at": bcl_entrypoint._utc_now()}, cycle=1,
            plan={"cycle": 1, "round": 1, "groups": [{"id": "A", "finding_ids": ["ISSUE-01"], "status": "review",
                "phase": "review", "codex": {"implementer_session": "writer", "reviewer_session": "reviewer",
                    "fix_status": "passed"}}]})
        prepared = json.dumps(document).encode()
        live.write_bytes(prepared)
        self.data.write_bytes(prepared)
        result = self.root / "review.txt"
        for verdict, writes in cases:
            with self.subTest(verdict=verdict, writes=writes):
                live.write_bytes(prepared)
                self.data.write_bytes(prepared)
                result.unlink(missing_ok=True)
                if not writes:
                    result.write_text("VERDICT: PASS\nReview note\nstale note\n## Must-fix\nnone\n")
                    writer = "pass"
                elif isinstance(verdict, bytes):
                    writer = f"from pathlib import Path; Path({str(result)!r}).write_bytes(bytes.fromhex({verdict.hex()!r}))"
                else:
                    contents = (verdict + "\n" if verdict else "") + "Review note\nreview note\n## Must-fix\n" + ("- named issue\n" if verdict == "VERDICT: CHANGES" else "none\n")
                    writer = f"from pathlib import Path; Path({str(result)!r}).write_text({contents!r})"
                job = self.call_job_without_viewer(self.data, "--ids", "ISSUE-01", "--result", result,
                    "--phase", "review", "job", "--", sys.executable, "-c", writer)
                self.assertEqual(job.returncode, 0 if writes and verdict == "VERDICT: PASS" else 1, job.stderr)
                group = json.loads(self.data.read_text())["tracking"]["plan"]["groups"][0]
                expected_status = "passed" if writes and verdict == "VERDICT: PASS" else "changes_requested" if verdict == "VERDICT: CHANGES" else "failed"
                self.assertEqual(group["codex"].get("review_status"), expected_status, job.stdout + job.stderr)
                if writes and not isinstance(verdict, bytes):
                    self.assertIn("review note", result.read_text())

    def test_review_job_model_is_recorded_for_the_lane_model_column(self):
        self.assertEqual(self.call_without_viewer("start").returncode, 0)
        plan = self.root / "review-model-plan.json"
        plan.write_text(json.dumps({"round": 1, "groups": [{"id": "A", "finding_ids": ["ISSUE-01"], "status": "planned",
            "codex": {"model": "gpt-6-luna", "implementer_session": "writer", "reviewer_session": "reviewer"}}]}))
        self.assertEqual(self.call_without_viewer("plan", "--plan", str(plan), "--ids", "ISSUE-01").returncode, 0)
        def run(phase, *extra):
            return subprocess.run([sys.executable, str(BCL), "--data", str(self.data), "--ids", "ISSUE-01", *extra,
                "--phase", phase, "job", "--", sys.executable, "-c", "pass"], env=self.env, capture_output=True, text=True)
        self.assertEqual(run("fix", "--model", "gpt-6-luna max").returncode, 0)
        codex = json.loads(self.data.read_text())["tracking"]["plan"]["groups"][0]["codex"]
        self.assertEqual((codex["job"]["model"], codex.get("reviewer_model")), ("gpt-6-luna max", None))
        self.assertEqual(self.call_without_viewer("review", "--ids", "ISSUE-01").returncode, 0)
        result = self.root / "review-model.txt"
        job = subprocess.run([sys.executable, str(BCL), "--data", str(self.data), "--ids", "ISSUE-01", "--result", str(result),
            "--model", "Grok Bot subagent (own review)", "--phase", "review", "job", "--", sys.executable, "-c",
            f"from pathlib import Path; Path({str(result)!r}).write_text('VERDICT: PASS\\n## Must-fix\\nnone\\n')"], env=self.env, capture_output=True, text=True)
        self.assertEqual(job.returncode, 0, job.stderr)
        codex = json.loads(self.data.read_text())["tracking"]["plan"]["groups"][0]["codex"]
        self.assertEqual(codex["job"]["model"], "Grok Bot subagent (own review)")
        self.assertEqual(codex["reviewer_model"], "Grok Bot subagent (own review)")
        self.assertEqual(codex["model"], "gpt-6-luna")
        self.assertEqual(codex["review_status"], "passed")

    def test_review_job_verdicts(self):
        self._review_job_verdict([
            ("VERDICT: CHANGES", True),
            ("VERDICT: PASS", True),
            (None, True),
            ("VERDICT: CHANGES\nVERDICT: PASS", True),
            ("VERDICT: MAYBE\nVERDICT: PASS", True),
            (b"\xff", True),
            ("VERDICT: PASS", False),
        ])

    def test_release_check_on_an_unadopted_tracker_is_not_clear(self):
        stray = self.root / "stray" / "list.json"; stray.parent.mkdir()
        self.git(stray.parent, "init", "-q")
        stray.write_text(EXAMPLE.read_text())
        result = self.call("release-check", stray)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('"adopted": false', result.stdout)

    def release_check_for_groups(self, groups, *args):
        record = json.loads((self.state / "registry.json").read_text())["example/project/123"]
        live = Path(record["live"])
        tracker = json.loads(live.read_text())
        tracker["tracking"]["plan"] = {"groups": groups}
        serialized = json.dumps(tracker)
        live.write_text(serialized)
        self.data.write_text(serialized)
        return self.call("release-check", *args)

    def test_release_check_includes_named_reviewed_lane(self):
        for review_status in ("passed", "skipped_one_file"):
            with self.subTest(review_status=review_status):
                group = {"id": "A", "finding_ids": ["ISSUE-01", "ISSUE-02"], "status": "running", "phase": "review",
                         "codex": {"fix_status": "passed", "review_status": review_status}}
                result = self.release_check_for_groups([group], "--ids", "ISSUE-01", "--ids", "ISSUE-02")

                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["lanes"], [])
                self.assertEqual(payload["included"], [{"id": "A", "finding_ids": ["ISSUE-01", "ISSUE-02"]}])

    def test_release_check_blocks_named_lane_with_running_or_failed_presuite(self):
        cases = (
            {"status": "running"},
            {"status": "failed", "job": {"exit_code": 1}},
        )
        for presuite in cases:
            with self.subTest(presuite=presuite):
                group = {"id": "A", "finding_ids": ["ISSUE-01"], "status": "running", "phase": "review",
                         "codex": {"fix_status": "passed", "review_status": "passed", "job": {"exit_code": 0}},
                         "presuite": presuite}
                result = self.release_check_for_groups([group], "--ids", "ISSUE-01")
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout)["lanes"][0]["id"], "A")

    def test_release_check_without_ids_preserves_payload_keys(self):
        group = {"id": "A", "finding_ids": ["ISSUE-01"], "status": "running", "phase": "review",
                 "codex": {"fix_status": "passed", "review_status": "passed"}}
        result = self.release_check_for_groups([group])
        self.assertEqual(set(json.loads(result.stdout)), {"clear", "lanes"})

    def test_release_check_keeps_unnamed_reviewed_lane_in_flight(self):
        group = {"id": "A", "finding_ids": ["ISSUE-01"], "status": "running", "phase": "review",
                 "codex": {"fix_status": "passed", "review_status": "passed"}}
        result = self.release_check_for_groups([group])

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["lanes"][0]["id"], "A")

    def test_release_check_requires_every_lane_finding_to_be_named(self):
        group = {"id": "A", "finding_ids": ["ISSUE-01", "ISSUE-02"], "status": "running", "phase": "review",
                 "codex": {"fix_status": "passed", "review_status": "passed"}}
        result = self.release_check_for_groups([group], "--ids", "ISSUE-01")

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["lanes"][0]["finding_ids"], ["ISSUE-01", "ISSUE-02"])

    def test_release_check_keeps_named_lane_in_flight_until_fix_and_review_pass(self):
        cases = (("running", {"fix_status": "passed", "review_status": "changes_requested"}),
                 ("running", {"fix_status": "running", "review_status": "running"}),
                 ("blocked", {"fix_status": "passed", "review_status": "passed"}),
                 ("failed", {"fix_status": "passed", "review_status": "passed"}))
        for status, codex in cases:
            with self.subTest(status=status, codex=codex):
                group = {"id": "A", "finding_ids": ["ISSUE-01"], "status": status, "phase": "review",
                         "codex": codex}
                result = self.release_check_for_groups([group], "--ids", "ISSUE-01")
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout)["lanes"][0]["id"], "A")

    def test_release_check_keeps_other_unfinished_lane_in_flight(self):
        ready = {"id": "A", "finding_ids": ["ISSUE-01"], "status": "running", "phase": "review",
                 "codex": {"fix_status": "passed", "review_status": "passed"}}
        unfinished = {"id": "B", "finding_ids": ["ISSUE-02"], "status": "running", "phase": "fix",
                      "codex": {"fix_status": "running", "review_status": "pending"}}
        result = self.release_check_for_groups([ready, unfinished], "--ids", "ISSUE-01")

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual([lane["id"] for lane in payload["lanes"]], ["B"])
        self.assertEqual([lane["id"] for lane in payload["included"]], ["A"])

    def test_workflow_cycle_uses_live_tracker_and_snapshots_each_stage(self):
        result = self.call_without_viewer("start")
        self.assertEqual(result.returncode, 0, result.stderr)
        marker = self.root / 'proof-ran'
        proof = self.root / 'proof.py'; proof.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('yes')\n")
        command = f'"{sys.executable}" "{proof}"'
        plan = {"round":1,"groups":[{"id":"A","finding_ids":["ISSUE-01"],"reason":"Same root cause","depends_on":[],"codex":{"model":"gpt-6-luna","effort":"medium","status":"running","started_at":"2026-11-14T09:00:00Z","duration_s":120,"implementer_session":"fixture-implementer","reviewer_session":"fixture-reviewer","review_status":"pending"},"verify":{"command":command,"method":"Unit tests and a live page check","status":"pending"},"status":"running"}]}
        planfile = self.root / "plan.json"; planfile.write_text(json.dumps(plan))
        sectionfile = self.root / "sections.json"
        self.assertNotEqual(self.call("introspect", "--result", "no issues").returncode, 0)
        for stage, extra in (("plan", ["--plan", str(planfile), "--ids", "ISSUE-01"]),):
            result = self.call_without_viewer(stage, *extra); self.assertEqual(result.returncode, 0, result.stderr)
            if stage == "plan": self.assertIn('class="plan"', self.page.read_text())
        job = [sys.executable, str(proof)]
        result = subprocess.run([sys.executable, str(BCL), "--data", str(self.data), "--ids", "ISSUE-01",
                                 "--phase", "fix", "job", "--", *job], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        review_result = self.call_without_viewer("review", "--ids", "ISSUE-01")
        self.assertEqual(review_result.returncode, 0, review_result.stderr)
        verdict_file = self.root / "workflow-review-result.txt"
        review_command = [sys.executable, "-c", f"from pathlib import Path; Path({str(verdict_file)!r}).write_text('VERDICT: PASS\\n## Must-fix\\nnone\\n')"]
        result = subprocess.run([sys.executable, str(BCL), "--data", str(self.data), "--ids", "ISSUE-01",
                                 "--result", str(verdict_file), "--phase", "review", "job", "--", *review_command], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run([sys.executable, str(BCL), "--data", str(self.data), "--ids", "ISSUE-01", "--phase", "presuite", "job", "--", *job], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        installed = self.call_without_viewer("install", "--ids", "ISSUE-01")
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertEqual(self.call_without_viewer("verify", "--ids", "ISSUE-01", "--then", command).returncode, 0)
        result = self.call_without_viewer("introspect", "--draft", sectionfile)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(self.data.read_text())
        self.assertNotIn("introspect", data["tracking"]["stages_reached"])
        draft = json.loads(sectionfile.read_text())
        self.assertEqual(draft["steps"], check_introspect.performed_round_steps(data["tracking"]))
        self.assertIn(f"Test command: A: {command}", draft["steps"])
        self.assertEqual(list(draft["sections"]), ["delete", "simplify", "optimize", "automate"])
        self.assertEqual(draft["none"], [])
        result = self.call_without_viewer("introspect", "--sections", sectionfile)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("introspect", json.loads(self.data.read_text())["tracking"]["stages_reached"])
        draft["none"] = draft["steps"]
        sectionfile.write_text(json.dumps(draft))
        result = self.call_without_viewer("introspect", "--sections", sectionfile)
        self.assertEqual(result.returncode, 0, result.stderr)
        status = self.call("status")
        self.assertEqual(marker.read_text(), 'yes')
        self.assertEqual(status.returncode, 0, status.stderr)
        data = json.loads(self.data.read_text())
        self.assertEqual(data["tracking"]["stages_reached"], ["collect", "plan_fix", "review", "install", "verify", "introspect"])
        self.assertEqual(data["tracking"]["plan"]["groups"][0]["id"], "A")
        self.assertEqual(data["tracking"]["plan"]["status"], "closed")
        self.assertEqual(json.loads(status.stdout)["plan"][0]["status"], "verifying")

    def test_introspect_draft_rejects_input_tracker_and_live_hard_link(self):
        input_before = self.data.read_bytes()
        result = self.call_without_viewer("introspect", "--draft", self.data)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("input and live trackers", result.stderr)
        self.assertEqual(self.data.read_bytes(), input_before)

        live = Path(json.loads((self.state / "registry.json").read_text())["example/project/123"]["live"])
        hard_link = self.root / "live-hard-link.json"
        os.link(live, hard_link)
        live_before = live.read_bytes()
        result = self.call_without_viewer("introspect", "--draft", hard_link)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("input and live trackers", result.stderr)
        self.assertTrue(live.samefile(hard_link))
        self.assertEqual(live.read_bytes(), live_before)

    def test_verify_rejects_placeholder_and_mismatched_command(self):
        self.assertEqual(self.call('start').returncode, 0)
        check = self.root / 'check.sh'; check.write_text('#!/bin/sh\nexit 0\n'); check.chmod(0o755)
        other = self.root / 'other.sh'; other.write_text('#!/bin/sh\nexit 0\n'); other.chmod(0o755)
        plan = {'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'status':'pending','verify':{'command':str(check)}}]}
        path = self.root / 'plan.json'; path.write_text(json.dumps(plan))
        self.assertEqual(self.call('plan','--plan',str(path),'--ids','ISSUE-01').returncode, 0)
        self.assertEqual(self.install_after_review('--ids','ISSUE-01').returncode, 0)
        result = self.call('verify','--ids','ISSUE-01','--then',str(other))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('exact stored command', result.stderr)
        fail = self.root / 'fail.py'; fail.write_text('raise SystemExit(7)\n')
        command = f'"{sys.executable}" "{fail}"'
        plan['groups'][0]['verify']['command'] = command
        path.write_text(json.dumps(plan))
        self.assertEqual(self.call('plan','--plan',str(path),'--ids','ISSUE-01').returncode, 0)
        result = self.call('verify','--ids','ISSUE-01','--then',command)
        self.assertNotEqual(result.returncode, 0)
        live = Path(json.loads((self.state / 'registry.json').read_text())['example/project/123']['live'])
        written = json.loads(live.read_text())
        group = written['tracking']['plan']['groups'][0]
        self.assertEqual(group['status'], 'failed')
        self.assertEqual(group['phase'], 'verify')
        self.assertIn('exited 7', group['verify']['failure'])
        self.assertEqual(next(item for item in written['findings'] if item['id'] == 'ISSUE-01')['status'], 'in_progress')
        self.assertIn('exited 7', self.page.read_text())

        masked = command + ' ; true'
        plan['groups'][0]['verify']['command'] = masked
        path.write_text(json.dumps(plan))
        self.assertEqual(self.call('plan','--plan',str(path),'--ids','ISSUE-01').returncode, 0)
        before = live.read_bytes()
        result = self.call('verify','--ids','ISSUE-01','--then',masked)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('verify cannot start a blocked, failed, or parked lane', result.stderr)
        self.assertEqual(live.read_bytes(), before)

        plan['groups'][0]['verify']['command'] = "sh -c 'exit 7; true'"
        path.write_text(json.dumps(plan))
        self.assertEqual(self.call('plan','--plan',str(path),'--ids','ISSUE-01').returncode, 0)
        result = self.call('verify','--ids','ISSUE-01','--then',plan['groups'][0]['verify']['command'])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('verify cannot start a blocked, failed, or parked lane', result.stderr)

        changed = self.root / 'changed.py'
        changed.write_text(f"import json\nfrom pathlib import Path\np=Path({str(live)!r})\nd=json.loads(p.read_text())\nd['findings'][0]['status']='failing'\np.write_text(json.dumps(d))\nraise SystemExit(7)\n")
        command = f'"{sys.executable}" "{changed}"'
        plan['groups'][0]['verify']['command'] = command
        path.write_text(json.dumps(plan))
        self.assertEqual(self.call('plan','--plan',str(path),'--ids','ISSUE-01').returncode, 0)
        result = self.call('verify','--ids','ISSUE-01','--then',command)
        self.assertNotEqual(result.returncode, 0)
        written = json.loads(live.read_text())
        self.assertEqual(written['findings'][0]['status'], 'in_progress')
        self.assertEqual(written['tracking']['plan']['groups'][0]['status'], 'failed')

    def test_failed_lane_verify_keeps_findings_in_progress(self):
        fail = self.root / 'fail.py'; fail.write_text('raise SystemExit(7)\n')
        command = f'"{sys.executable}" "{fail}"'
        live = self._start_verification(command)
        result = self.call('verify','--ids','ISSUE-01','--then',command)
        self.assertNotEqual(result.returncode, 0)
        written = json.loads(live.read_text())
        check = written['tracking']['verification_run']['checks']['ISSUE-01']
        self.assertEqual(check['state'], 'inconclusive', result.stderr)
        self.assertEqual(check['observation'], 'Visible before check')
        self.assertIn('exited 7', check['evidence'])
        self.assertIn(command, check['details'])
        self.assertNotIn(sys.executable, check['evidence'])
        self.assertEqual(next(item for item in written['findings'] if item['id'] == 'ISSUE-01')['status'], 'in_progress')
        group = written['tracking']['plan']['groups'][0]
        self.assertEqual(group['status'], 'failed')
        self.assertIn('exited 7', group['verify']['failure'])

    def _start_verification(self, command):
        self.assertEqual(self.call('start').returncode, 0)
        plan = {'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'status':'pending','verify':{'command':command}}]}
        path = self.root / 'plan.json'; path.write_text(json.dumps(plan))
        self.assertEqual(self.call('plan','--plan',str(path),'--ids','ISSUE-01').returncode, 0)
        self.assertEqual(self.install_after_review('--ids','ISSUE-01').returncode, 0)
        live = Path(json.loads((self.state / 'registry.json').read_text())['example/project/123']['live'])
        operations = [
            {'action':'start-verification','run':{'id':'run1','build':'1.0','revision':'abc','installed_at':'2020-01-01T00:00:00Z','install_evidence':'receipt'}},
            {'action':'start-check','run_id':'run1','id':'ISSUE-01','observation':'Visible before check','observed_at':'2020-01-02T00:00:00Z'},
        ]
        manager = PLUGIN / 'skills/batched-change-list/scripts/manage_findings.py'
        result = subprocess.run([sys.executable,str(manager),'batch',str(live),'-','--render',str(self.page)],
                                input=json.dumps(operations),text=True,capture_output=True,env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.data.write_bytes(live.read_bytes())
        return live

    def test_failed_lane_verify_spawn_error_is_recorded_in_details(self):
        missing = self.root / 'missing executable'
        command = f'"{missing}"'
        self.assertEqual(self.call('start').returncode, 0)
        plan = {'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'status':'pending','verify':{'command':command}}]}
        path = self.root / 'plan.json'; path.write_text(json.dumps(plan))
        self.assertEqual(self.call('plan','--plan',str(path),'--ids','ISSUE-01').returncode, 0)
        before = self.data.read_bytes()
        result = self.call('install','--ids','ISSUE-01')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('verify command is not executable', result.stderr)
        self.assertEqual(self.data.read_bytes(), before)

    def test_plain_command_rejects_shell_operators_and_wrappers(self):
        for command in ("check.py; true", "sh -c 'exit 0'", "echo pass"):
            with self.subTest(command=command), self.assertRaisesRegex(ValueError, "command must name one executable"):
                bcl_entrypoint._plain_command(command)

    def test_plan_requires_a_restore_lane_for_a_regression_against_main(self):
        self.assertEqual(self.call("start").returncode, 0)
        live = Path(json.loads((self.state / 'registry.json').read_text())['example/project/123']['live'])
        document = json.loads(live.read_text())
        document["findings"][0]["details"] = {"regression_against_main": "yes"}
        live.write_text(json.dumps(document)); self.data.write_text(json.dumps(document))
        plan = self.root / "restore-plan.json"
        plan.write_text(json.dumps({"round": 1, "groups": [{"id": "A", "finding_ids": ["ISSUE-01"], "status": "planned"}]}))
        refused = self.call_without_viewer("plan", "--plan", plan, "--ids", "ISSUE-01")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("must restore main's behaviour first", refused.stderr)
        plan.write_text(json.dumps({"round": 1, "groups": [{"id": "A", "finding_ids": ["ISSUE-01"], "status": "planned", "restores_main": True}]}))
        accepted = self.call_without_viewer("plan", "--plan", plan, "--ids", "ISSUE-01")
        self.assertEqual(accepted.returncode, 0, accepted.stderr)

    def test_plan_refuses_more_than_four_lanes(self):
        import manage_findings
        groups = [{"id": f"G{n}", "finding_ids": [f"X-{n}"], "status": "planned"} for n in range(5)]
        with self.assertRaisesRegex(ValueError, "at most 4 lanes"):
            manage_findings.validate_plan_groups({"groups": groups})
        groups[4]["status"] = "deferred"
        manage_findings.validate_plan_groups({"groups": groups})

    def test_job_and_verify_check_the_machine_load(self):
        live, command = self._lane_ready()
        with patch.dict(self.env, {"BCL_MAX_LOAD": "-1"}):
            verify = self.call_without_viewer("verify", "--ids", "ISSUE-01", "--then", command)
            job = self.call_job_without_viewer(live, "--ids", "ISSUE-01", "--phase", "fix", "job", "--", sys.executable, "-c", "pass")
        for result in (verify, job):
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("machine load", result.stderr)
        self.assertEqual(json.loads(live.read_text())["tracking"]["verification_run"]["checks"]["ISSUE-01"]["state"], "pending")

    def test_stopped_run_keeps_verified_issues_counted(self):
        live, command = self._lane_ready()
        document = json.loads(live.read_text())
        run_id = document["tracking"]["verification_run"]["id"]
        import check_gate_count
        before = check_gate_count.verified_gate_count(document)[0]
        document["findings"][1]["status"] = "verified_fixed"; document["findings"][1]["verification_refs"] = ["ref"]
        live.write_text(json.dumps(document)); self.data.write_text(json.dumps(document))
        counted = check_gate_count.verified_gate_count(json.loads(live.read_text()))[0]
        refused = self.call_without_viewer("stop-verify")
        self.assertNotEqual(refused.returncode, 0)
        stopped = self.call_without_viewer("stop-verify", "--reason", "owner asked to roll over")
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        after = json.loads(live.read_text())
        run = after["tracking"]["verification_run"]
        self.assertEqual((run["id"], run["status"], run["stopped"]["reason"]), (run_id, "complete", "owner asked to roll over"))
        self.assertTrue(all(c["state"] == "inconclusive" and c["stopped"] for c in run["checks"].values()))
        import manage_findings
        self.assertTrue(all(manage_findings.has_observation_for_run(c, run) for c in run["checks"].values()))  # audit keeps them
        self.assertEqual(check_gate_count.verified_gate_count(after)[0], counted)
        self.assertGreaterEqual(counted, before)

    def test_stop_verify_leaves_finished_verdicts_alone(self):
        live, command = self._lane_ready()
        document = json.loads(live.read_text())
        run = document["tracking"]["verification_run"]
        run["checks"]["ISSUE-01"] = {"state": "fixed", "observation": "o", "observed_at": run["installed_at"], "evidence": "kept", "started_at": run["installed_at"], "updated_at": run["installed_at"], "finished_at": run["installed_at"]}
        live.write_text(json.dumps(document)); self.data.write_text(json.dumps(document))
        stop = self.call_without_viewer("stop-verify", "--reason", "rollover"); self.assertEqual(stop.returncode, 0, stop.stderr[-300:])
        kept = json.loads(live.read_text())["tracking"]["verification_run"]["checks"]["ISSUE-01"]
        self.assertEqual((kept["state"], kept["evidence"]), ("fixed", "kept"))
        self.assertNotIn("stopped", kept)

    def test_failed_fix_requires_plan_review_before_retry(self):
        self.assertEqual(self.call_without_viewer('start').returncode, 0)
        plan = self.root / 'retry-plan.json'
        plan.write_text(json.dumps({'round': 1, 'groups': [{'id': 'A', 'finding_ids': ['ISSUE-01'], 'status': 'planned'}]}))
        planned = self.call_without_viewer('plan', '--plan', str(plan), '--ids', 'ISSUE-01')
        self.assertEqual(planned.returncode, 0, planned.stderr)
        live = Path(json.loads((self.state / 'registry.json').read_text())['example/project/123']['live'])
        failed = self.call_job_without_viewer(live, '--ids', 'ISSUE-01', '--phase', 'fix', 'job', '--',
                                               sys.executable, '-c', 'raise SystemExit(7)')
        self.assertEqual(failed.returncode, 7, failed.stderr)
        retry = self.call_job_without_viewer(live, '--ids', 'ISSUE-01', '--phase', 'fix', 'job', '--',
                                              sys.executable, '-c', 'pass')
        self.assertNotEqual(retry.returncode, 0)
        self.assertIn('passed plan_review', retry.stderr)
        result = self.root / 'plan-review.txt'
        command = f"from pathlib import Path; Path({str(result)!r}).write_text('VERDICT: PASS\\n## Must-fix\\nnone\\n## ROOT CAUSE\\nrecorded\\n## FAILING TEST\\ntest_fix\\n')"
        review = self.call_job_without_viewer(live, '--ids', 'ISSUE-01', '--phase', 'plan_review',
                                               '--result', result, 'job', '--', sys.executable, '-c', command)
        self.assertEqual(review.returncode, 0, review.stderr)
        retried = self.call_job_without_viewer(live, '--ids', 'ISSUE-01', '--phase', 'fix', 'job', '--',
                                                sys.executable, '-c', 'pass')
        self.assertEqual(retried.returncode, 0, retried.stderr)

    def test_broken_verifier_is_inconclusive_not_an_issue_failure(self):
        live, command = self._lane_ready()
        check = Path(command.split('"')[3])
        check.write_text("raise SystemExit(7)\n")
        failed = self.call_without_viewer("verify", "--ids", "ISSUE-01", "--then", command)
        self.assertEqual(failed.returncode, 7, failed.stderr)
        tracking = json.loads(live.read_text())["tracking"]
        self.assertEqual(tracking["plan"]["groups"][0]["status"], "failed")
        self.assertEqual(tracking["plan"]["groups"][0]["failure_history"], [])

    def test_fix_job_does_not_run_command_without_current_introspect(self):
        self.assertEqual(self.call('start').returncode, 0)
        live = Path(json.loads((self.state / 'registry.json').read_text())['example/project/123']['live'])
        document = json.loads(live.read_text())
        document['findings'][0]['status'] = 'in_progress'
        tracking = document['tracking']
        tracking['plan'] = {'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'status':'failed','phase':'verify',
            'failure_history': {'ISSUE-01': [{'attempt_id':'failed-job-1','source':'fix','cycle':1,'round':1}]}}]}
        tracking['workflow'] = {'stage':'verify'}
        tracking.pop('introspect', None); tracking.pop('introspect_history', None)
        live.write_text(json.dumps(document)); self.data.write_text(json.dumps(document))
        marker = self.root / 'command-ran'
        command = f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"
        evidence = self.root / 'observed.jsonl'; evidence.write_text('{}\n')
        (Path(json.loads((self.state / 'registry.json').read_text())['example/project/123']['home']) / 'test_x.py').write_text('def test_x(): pass\n')
        result = self.call_job_without_viewer(live,'--ids','ISSUE-01','--phase','fix','job','--',sys.executable,'-c',command)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires a passed plan_review after failure", result.stderr)
        self.assertFalse(marker.exists())

    def test_fix_job_still_refuses_blocked_lane(self):
        self.assertEqual(self.call('start').returncode, 0)
        live = Path(json.loads((self.state / 'registry.json').read_text())['example/project/123']['live'])
        document = json.loads(live.read_text())
        document['findings'][0]['status'] = 'in_progress'
        document['tracking']['plan'] = {'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'status':'blocked'}]}
        live.write_text(json.dumps(document)); self.data.write_text(json.dumps(document))
        result = self.call_job_without_viewer(live,'--ids','ISSUE-01','--phase','fix','job','--',sys.executable,'-c','pass')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('fix job cannot start a blocked, parked, or deferred lane', result.stderr)

    def test_failed_command_does_not_overwrite_newer_plan(self):
        self.assertEqual(self.call_without_viewer('start').returncode, 0)
        live = Path(json.loads((self.state / 'registry.json').read_text())['example/project/123']['live'])
        fail = self.root / 'replan.py'
        fail.write_text(f"import json\nfrom pathlib import Path\np=Path({str(live)!r})\nd=json.loads(p.read_text())\nd['tracking']['plan']['groups'][0]['status']='review'\np.write_text(json.dumps(d))\nraise SystemExit(7)\n")
        command = f'"{sys.executable}" "{fail}"'
        plan = {'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'status':'pending',
            'verify':{'command':command}, 'codex': {'implementer_session':'fixture-implementer',
            'reviewer_session':'fixture-reviewer', 'review_status':'pending'}}]}
        path = self.root / 'plan.json'
        path.write_text(json.dumps(plan))
        self.assertEqual(self.call_without_viewer('plan','--plan',str(path),'--ids','ISSUE-01').returncode, 0)
        self.prepare_review(['ISSUE-01'])
        self.assertEqual(self.call_without_viewer('install','--ids','ISSUE-01').returncode, 0)
        result = self.call_without_viewer('verify','--ids','ISSUE-01','--then',command)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Could not record failed lanes', result.stderr)
        self.assertEqual(json.loads(live.read_text())['tracking']['plan']['groups'][0]['status'], 'review')

    def test_failed_command_is_recorded_when_another_lane_changes(self):
        self.assertEqual(self.call('start').returncode, 0)
        live = Path(json.loads((self.state / 'registry.json').read_text())['example/project/123']['live'])
        fail = self.root / 'other_lane.py'
        fail.write_text(f"import json\nfrom pathlib import Path\np=Path({str(live)!r})\nd=json.loads(p.read_text())\nd['tracking']['plan']['groups'][1]['status']='review'\np.write_text(json.dumps(d))\nraise SystemExit(7)\n")
        command = f'"{sys.executable}" "{fail}"'
        plan = {'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'status':'pending','verify':{'command':command}},
                                    {'id':'B','finding_ids':['ISSUE-02'],'status':'pending','verify':{'command':'python3 test.py'}}]}
        path = self.root / 'plan.json'; path.write_text(json.dumps(plan))
        self.assertEqual(self.call('plan','--plan',str(path),'--ids','ISSUE-01').returncode, 0)
        self.assertEqual(self.install_after_review('--ids','ISSUE-01').returncode, 0)
        result = self.call('verify','--ids','ISSUE-01','--then',command)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('Could not record failed lanes', result.stderr)
        written = json.loads(live.read_text())
        self.assertEqual(written['tracking']['plan']['groups'][0]['status'], 'failed')
        self.assertIn('exited 7', written['tracking']['plan']['groups'][0]['verify']['failure'])
        self.assertEqual(written['tracking']['plan']['groups'][1]['status'], 'review')

    def test_failed_wave_records_unchanged_lane_when_other_selected_lane_changes(self):
        self.assertEqual(self.call('start').returncode, 0)
        live = Path(json.loads((self.state / 'registry.json').read_text())['example/project/123']['live'])
        document = json.loads(live.read_text())
        document['findings'][1]['status'] = 'untested'
        live.write_text(json.dumps(document)); self.data.write_bytes(live.read_bytes())
        fail = self.root / 'change_selected.py'
        fail.write_text(f"import json\nfrom pathlib import Path\np=Path({str(live)!r})\nd=json.loads(p.read_text())\nd['tracking']['plan']['groups'][0]['status']='review'\np.write_text(json.dumps(d))\nraise SystemExit(7)\n")
        command = f'"{sys.executable}" "{fail}"'
        plan = {'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'status':'pending','verify':{'command':command}},
                                    {'id':'B','finding_ids':['ISSUE-02'],'status':'pending','verify':{'command':command}}]}
        path = self.root / 'plan.json'; path.write_text(json.dumps(plan))
        self.assertEqual(self.call('plan','--plan',str(path),'--ids','ISSUE-01','--ids','ISSUE-02').returncode, 0)
        self.assertEqual(self.install_after_review('--ids','ISSUE-01','--ids','ISSUE-02').returncode, 0)
        result = self.call('verify','--ids','ISSUE-01','--ids','ISSUE-02','--then',command)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Could not record failed lanes (A)', result.stderr)
        written = json.loads(live.read_text())
        self.assertEqual(written['tracking']['plan']['groups'][0]['status'], 'review')
        self.assertEqual(written['tracking']['plan']['groups'][1]['status'], 'failed')
        self.assertIn('exited 7', written['tracking']['plan']['groups'][1]['verify']['failure'])
        self.assertEqual(next(item for item in written['findings'] if item['id'] == 'ISSUE-02')['status'], 'in_progress')

    def test_verify_rejects_plan_change_during_snapshot(self):
        self.assertEqual(self.call('start').returncode, 0)
        marker = self.root / 'ran'
        proof = self.root / 'proof.py'; proof.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n")
        command = f'"{sys.executable}" "{proof}"'
        plan = {'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'status':'pending','verify':{'command':command}}]}
        path = self.root / 'plan.json'; path.write_text(json.dumps(plan))
        self.assertEqual(self.call('plan','--plan',str(path),'--ids','ISSUE-01').returncode, 0)
        self.assertEqual(self.install_after_review('--ids','ISSUE-01').returncode, 0)
        live = Path(json.loads((self.state / 'registry.json').read_text())['example/project/123']['live'])
        manager = PLUGIN / 'skills/batched-change-list/scripts/manage_findings.py'
        operations = self.root / 'change-plan.json'
        operations.write_text(json.dumps([{'action':'update-plan-group','id':'A',
                                           'patch':{'verify_summary':'Changed after activity commit'}}]))
        def change_plan(record, live_path):
            result = subprocess.run([sys.executable, str(manager), 'batch', str(live_path), str(operations),
                                     '--render', str(self.page)], env=self.env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        with patch.dict(os.environ, self.env), patch.object(live_state, 'STATE', self.state), \
             patch.object(live_state, 'snapshot', side_effect=change_plan), \
             patch.object(sys, 'argv', [str(BCL),'--data',str(live),'--ids','ISSUE-01','--then',command,'verify']):
            with self.assertRaisesRegex(SystemExit, 'plan changed before verification command started'):
                bcl_entrypoint.main()
        self.assertFalse(marker.exists())
        self.assertEqual(json.loads(live.read_text())['tracking']['plan']['groups'][0]['verify_summary'],
                         'Changed after activity commit')

    def test_concurrent_verify_is_rejected_while_child_updates_tracker(self):
        self.assertEqual(self.call('start').returncode, 0)
        live = Path(json.loads((self.state / 'registry.json').read_text())['example/project/123']['live'])
        changed = self.root / 'child-updated'
        release = self.root / 'release-child'
        operations = self.root / 'child-update.json'
        operations.write_text(json.dumps([{'action':'update','id':'ISSUE-01',
                                           'patch':{'title':'Updated by verifier child'}}]))
        manager = PLUGIN / 'skills/batched-change-list/scripts/manage_findings.py'
        proof = self.root / 'long_verify.py'
        proof.write_text("\n".join((
            'import subprocess, sys, time',
            'from pathlib import Path',
            f"result = subprocess.run([{sys.executable!r}, {str(manager)!r}, 'batch', {str(live)!r}, {str(operations)!r}, '--render', {str(self.page)!r}], capture_output=True, text=True)",
            'if result.returncode: raise SystemExit(result.stderr)',
            f"Path({str(changed)!r}).write_text('yes')",
            f"deadline = time.monotonic() + 60; gate = Path({str(release)!r})",
            'while not gate.exists() and time.monotonic() < deadline: time.sleep(0.02)',
            'if not gate.exists(): raise SystemExit(9)',
        )) + '\n')
        command = f'"{sys.executable}" "{proof}"'
        plan = {'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'status':'pending',
                                     'verify':{'command':command}}]}
        path = self.root / 'concurrent-plan.json'; path.write_text(json.dumps(plan))
        self.assertEqual(self.call('plan','--plan',str(path),'--ids','ISSUE-01').returncode, 0)
        self.assertEqual(self.install_after_review('--ids','ISSUE-01').returncode, 0)

        original_argv = sys.argv[:]
        first_result = []
        def first_verify():
            sys.argv = [str(BCL),'--data',str(self.data),'--ids','ISSUE-01','--then',command,'verify']
            try:
                bcl_entrypoint.main()
                first_result.append(None)
            except BaseException as error:
                first_result.append(error)

        with patch.dict(os.environ, self.env), patch.object(live_state, 'STATE', self.state), \
             patch.object(live_state, 'ensure_viewer'):
            thread = threading.Thread(target=first_verify)
            thread.start()
            deadline = time.monotonic() + 30
            while not changed.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(changed.exists(), 'verifier child could not write while launch lock was held')
            sys.argv = [str(BCL),'--data',str(live),'--ids','ISSUE-01','--then',command,'verify']
            with self.assertRaisesRegex(SystemExit, 'verification already in progress for this tracker'):
                bcl_entrypoint.main()
            release.write_text('continue')
            thread.join(timeout=30)
            self.assertFalse(thread.is_alive(), 'first verification did not finish')
        sys.argv = original_argv
        self.assertEqual(first_result, [None])
        self.assertEqual(json.loads(live.read_text())['findings'][0]['title'], 'Updated by verifier child')

    def test_verify_reserves_plan_until_spawn_against_plan_and_finding_writers(self):
        self.assertEqual(self.call('start').returncode, 0)
        marker = self.root / 'spawned'
        proof = self.root / 'proof.py'
        proof.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('yes')\n")
        command = [sys.executable, str(proof)]
        stored_command = f'"{sys.executable}" "{proof}"'
        plan = {'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'status':'pending',
                                     'verify':{'command':stored_command}}]}
        path = self.root / 'plan.json'; path.write_text(json.dumps(plan))
        self.assertEqual(self.call('plan','--plan',str(path),'--ids','ISSUE-01').returncode, 0)
        self.assertEqual(self.install_after_review('--ids','ISSUE-01').returncode, 0)
        live = Path(json.loads((self.state / 'registry.json').read_text())['example/project/123']['live'])
        manager = PLUGIN / 'skills/batched-change-list/scripts/manage_findings.py'
        operations = [
            [{'action':'update-plan-group','id':'A','patch':{'verify_summary':'Writer waited'}}],
            [{'action':'update','id':'ISSUE-01','patch':{'title':'Finding writer waited'}}],
        ]
        threads, attempts, completed, results = [], [], [threading.Event() for _ in operations], [None for _ in operations]

        def spawn_writer(index):
            ops = self.root / f'writer-{index}.json'; ops.write_text(json.dumps(operations[index]))
            attempt = self.root / f'writer-{index}-blocked'
            attempts.append(attempt)
            wrapper = self.root / f'writer-{index}.py'
            wrapper.write_text("\n".join((
                'import sys',
                f"sys.path.insert(0, {str(manager.parent)!r})",
                'import manage_findings, tracker_lock',
                'from pathlib import Path',
                'real_lock = tracker_lock.exclusive_lock',
                f"target = tracker_lock.plan_lock_path(Path({str(live)!r}))",
                'def observed_lock(path, *args, **kwargs):',
                '    if Path(path) == target:',
                '        try:',
                '            with real_lock(path, blocking=False): pass',
                '        except tracker_lock.LockBusyError:',
                f"            Path({str(attempt)!r}).write_text('blocked')",
                '        else:',
                f"            Path({str(attempt)!r}).write_text('free')",
                '    return real_lock(path, *args, **kwargs)',
                'tracker_lock.exclusive_lock = observed_lock',
                f"raise SystemExit(manage_findings.main(['batch', {str(live)!r}, {str(ops)!r}, '--render', {str(self.page)!r}]))",
            )) + '\n')
            def write():
                results[index] = subprocess.run(
                    [sys.executable, str(wrapper)],
                    env=self.env, capture_output=True, text=True)
                completed[index].set()
            thread = threading.Thread(target=write)
            threads.append(thread)
            thread.start()

        real_popen = subprocess.Popen
        launched = False
        def check_writers_then_spawn(args, *popen_args, **kwargs):
            nonlocal launched
            if args == command and not launched:
                launched = True
                for index in range(len(operations)):
                    spawn_writer(index)
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline and not all(path.exists() for path in attempts):
                    time.sleep(0.02)
                self.assertTrue(all(path.exists() and path.read_text() == 'blocked' for path in attempts),
                                'managed writers did not confirm the verify lock was held')
                self.assertFalse(any(event.wait(0.15) for event in completed),
                                 'managed plan/finding writer crossed the launch reservation')
            return real_popen(args, *popen_args, **kwargs)

        with patch.dict(os.environ, self.env), patch.object(live_state, 'STATE', self.state), \
             patch.object(bcl_entrypoint.subprocess, 'Popen', check_writers_then_spawn), \
             patch.object(sys, 'argv', [str(BCL),'--data',str(live),'--ids','ISSUE-01','--then',stored_command,'verify']):
            bcl_entrypoint.main()
        for thread in threads:
            thread.join(timeout=30)
            self.assertFalse(thread.is_alive(), 'managed writer did not resume after spawn')
        self.assertTrue(launched)
        self.assertTrue(marker.exists())
        self.assertTrue(all(result and result.returncode == 0 for result in results),
                        [result.stderr if result else 'writer did not finish' for result in results])

    def test_verify_rechecks_prerequisite_evidence_before_spawn(self):
        document = json.loads(self.data.read_text())
        prerequisite = next(item for item in document['findings'] if item['id'] == 'ISSUE-02')
        prerequisite.update(status='verified_fixed', verification='Installed proof', verification_refs=['receipt'])
        live = Path(json.loads((self.state / 'registry.json').read_text())['example/project/123']['live'])
        serialized = json.dumps(document)
        live.write_text(serialized)
        self.data.write_text(serialized)
        def invoke(stage, *extra):
            with patch.dict(os.environ, self.env), patch.object(live_state, 'STATE', self.state), \
                 patch.object(live_state, 'ensure_viewer'), \
                 patch.object(sys, 'argv', [str(BCL),'--data',str(self.data),*map(str,extra),stage]):
                bcl_entrypoint.main()
        invoke('start')
        marker = self.root / 'should-not-run'
        proof = self.root / 'proof.py'
        proof.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n")
        command = f'"{sys.executable}" "{proof}"'
        plan = {'round':1,'groups':[
            {'id':'A','finding_ids':['ISSUE-02'],'status':'verified','phase':'verify','verify':{'command':'python3 proof-a.py'}},
            {'id':'B','finding_ids':['ISSUE-01'],'depends_on':['A'],'status':'pending',
             'codex':{'implementer_session':'fixture-implementer','reviewer_session':'fixture-reviewer'},
             'verify':{'command':command}},
        ]}
        path = self.root / 'dependency-plan.json'; path.write_text(json.dumps(plan))
        live_doc = json.loads(live.read_text())
        live_doc['tracking']['plan'] = plan
        next(item for item in live_doc['findings'] if item['id'] == 'ISSUE-02').update(status='verified_fixed')
        live.write_text(json.dumps(live_doc)); self.data.write_text(json.dumps(live_doc))
        self.prepare_review(["ISSUE-01"])
        invoke('install','--ids','ISSUE-01')
        manager = PLUGIN / 'skills/batched-change-list/scripts/manage_findings.py'
        def change_prerequisite_check(record, live_path):
            def apply(operation):
                ops = self.root / 'check-update.json'; ops.write_text(json.dumps([operation]))
                result = subprocess.run([sys.executable, str(manager), 'batch', str(live_path), str(ops),
                                         '--render', str(self.page)], env=self.env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            apply({'action':'start-verification','run':{
                'id':'new-run','build':'2','revision':'def','installed_at':'2026-11-15T00:00:00Z',
                'install_evidence':'new receipt'}})
            apply({'action':'start-check','run_id':'new-run','id':'ISSUE-02',
                   'observation':'Retesting prerequisite','observed_at':'2026-11-15T00:01:00Z'})
            apply({'action':'finish-check','run_id':'new-run','id':'ISSUE-02','outcome':'inconclusive',
                   'evidence':'Checked: prerequisite evidence; Expected: confirmation; Observed: prerequisite could not be confirmed'})
        with patch.dict(os.environ, self.env), patch.object(live_state, 'STATE', self.state), \
             patch.object(live_state, 'ensure_viewer'), \
             patch.object(live_state, 'snapshot', side_effect=change_prerequisite_check), \
             patch.object(sys, 'argv', [str(BCL),'--data',str(live),'--ids','ISSUE-01','--then',command,'verify']):
            with self.assertRaisesRegex(SystemExit, 'plan changed before verification command started'):
                bcl_entrypoint.main()
        current = json.loads(live.read_text())
        self.assertFalse(marker.exists())
        self.assertEqual(current['tracking']['verification_run']['checks']['ISSUE-02']['state'], 'inconclusive')
        self.assertEqual(next(item for item in current['findings'] if item['id'] == 'ISSUE-02')['status'],
                         'verified_fixed')

    def test_stage_guards_remain(self):
        self.assertNotEqual(self.call("install").returncode, 0)
        self.assertNotEqual(self.call("verify").returncode, 0)
        self.assertNotEqual(self.call("report").returncode, 0)

    def test_unblock_without_reason_refuses_without_changing_tracker(self):
        live = Path(json.loads((self.state / "registry.json").read_text())["example/project/123"]["live"])
        before_source, before_live = self.data.read_bytes(), live.read_bytes()
        result = self.call_without_viewer("unblock", "--ids", "ISSUE-01")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unblock override requires a one-line nonblank --reason", result.stderr)
        self.assertEqual(self.data.read_bytes(), before_source)
        self.assertEqual(live.read_bytes(), before_live)

    def test_unblock_cli_records_tool_fault(self):
        live = Path(json.loads((self.state / "registry.json").read_text())["example/project/123"]["live"])
        document = json.loads(live.read_text())
        document["findings"][0]["status"] = "blocked"
        document["tracking"].update(cycle=1, round=1, plan={"cycle": 1, "round": 1, "groups": [{
            "id": "A", "finding_ids": ["ISSUE-01"], "status": "blocked", "phase": "fix",
            "blocked_reason": "Second failure", "failure_block_reason": "Review incomplete",
            "failure_history": {"ISSUE-01": [
                {"attempt_id": "attempt-1", "source": "review", "reason": "Review incomplete"},
                {"attempt_id": "attempt-2", "source": "plan_review", "reason": "Changes requested"},
            ]},
        }]})
        payload = json.dumps(document)
        live.write_text(payload); self.data.write_text(payload)
        result = self.call_without_viewer("unblock", "--ids", "ISSUE-01", "--reason", "parser fault",
                                          "--tool-fault", "attempt-1")
        self.assertEqual(result.returncode, 0, result.stderr)
        updated = json.loads(live.read_text())
        group = updated["tracking"]["plan"]["groups"][0]
        self.assertEqual((group["status"], group["phase"]), ("running", "fix"))
        self.assertTrue(group["failure_history"][0]["tool_fault"])
        self.assertEqual(group["unblock_history"][-1]["tool_faults"], ["attempt-1"])

    def test_review_refuses_until_fix_job_finishes(self):
        self.assertEqual(self.call("start").returncode, 0)
        plan = self.root / "plan.json"
        plan.write_text(json.dumps({"round": 1, "groups": [{"id": "A", "finding_ids": ["ISSUE-01"],
            "status": "planned", "codex": {"implementer_session": "writer", "reviewer_session": "reviewer"}}]}))
        self.assertEqual(self.call("plan", "--plan", str(plan), "--ids", "ISSUE-01").returncode, 0)
        result = self.call("review", "--ids", "ISSUE-01")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("has not finished fix", result.stderr)

    def test_review_and_presuite_reject_other_lane_still_fixing(self):
        live = Path(json.loads((self.state / "registry.json").read_text())["example/project/123"]["live"])
        data = json.loads(live.read_text())
        data["tracking"]["workflow"] = {"stage": "review", "updated_at": "2026-11-18T00:00:00Z"}
        data["tracking"]["plan"] = {"cycle": 1, "groups": [
            {"id": "A", "finding_ids": ["ISSUE-01"], "status": "review", "phase": "review", "codex": {"fix_status": "passed"}},
            {"id": "B", "finding_ids": ["ISSUE-02"], "status": "running", "phase": "fix", "codex": {}}]}
        live.write_text(json.dumps(data))
        result_file = self.root / "review.txt"
        for phase in ("review", "presuite"):
            result = subprocess.run([sys.executable, str(BCL), "--data", str(live), "--ids", "ISSUE-01", "--ids", "ISSUE-02",
                "--result", str(result_file), "--phase", phase, "job", "--", sys.executable, "-c", "pass"], env=self.env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("each lane", result.stderr)

    def test_one_file_review_skip_and_presuite_gate_real_install(self):
        self.assertEqual(self.call("start").returncode, 0)
        verify_script = self.root / "verify.sh"
        verify_script.write_text(f"#!/bin/sh\n{sys.executable} -c 'pass'\n")
        verify_script.chmod(0o755)
        plan = self.root / "one-file.json"
        plan.write_text(json.dumps({"round": 1, "groups": [{"id": "A", "finding_ids": ["ISSUE-01"],
            "status": "planned", "codex": {"one_file": True}, "verify": {"command": str(verify_script)}}]}))
        self.assertEqual(self.call("plan", "--plan", str(plan), "--ids", "ISSUE-01").returncode, 0)
        self.assertNotEqual(self.call("review", "--ids", "ISSUE-01").returncode, 0)
        def job(phase, code):
            return subprocess.run([sys.executable, str(BCL), "--data", str(self.data), "--ids", "ISSUE-01",
                "--phase", phase, "job", "--", sys.executable, "-c", f"exit({code})"], env=self.env, capture_output=True, text=True)
        self.assertEqual(job("fix", 0).returncode, 0)
        self.assertEqual(self.call("review", "--ids", "ISSUE-01").returncode, 0)
        self.assertEqual(json.loads(self.data.read_text())["tracking"]["plan"]["groups"][0]["codex"]["review_status"], "skipped_one_file")
        self.assertNotEqual(self.call("install", "--ids", "ISSUE-01").returncode, 0)
        self.assertEqual(job("presuite", 1).returncode, 1)
        self.assertNotEqual(self.call("install", "--ids", "ISSUE-01").returncode, 0)
        self.assertEqual(job("presuite", 0).returncode, 0)
        result = self.call("install", "--ids", "ISSUE-01")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_install_refuses_before_review(self):
        self.assertEqual(self.call("start").returncode, 0)
        verify_script = self.root / "verify.sh"
        verify_script.write_text("#!/bin/sh\nexit 0\n")
        verify_script.chmod(0o755)
        plan = self.root / "plan.json"
        plan.write_text(json.dumps({"round": 1, "groups": [{"id": "A", "finding_ids": ["ISSUE-01"],
            "status": "planned", "codex": {"one_file": True}, "verify": {"command": str(verify_script)}}]}))
        self.assertEqual(self.call("plan", "--plan", str(plan), "--ids", "ISSUE-01").returncode, 0)
        result = self.call("install", "--ids", "ISSUE-01")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("install requires review", result.stderr)

    def test_stage_renders_before_then_command(self):
        live = Path(json.loads((self.state / "registry.json").read_text())["example/project/123"]["live"])
        tracker = json.loads(live.read_text())
        tracker["findings"][0]["status"] = "in_progress"
        tracker["tracking"]["stages_reached"] = ["plan_fix", "review"]
        verify_script = self.root / "verify.sh"
        verify_script.write_text("#!/bin/sh\nexit 0\n"); verify_script.chmod(0o755)
        tracker["tracking"]["plan"] = {"cycle": 1, "groups": [{"id": "A", "finding_ids": ["ISSUE-01"], "status": "review", "phase": "review", "codex": {"implementer_session": "fixture-implementer", "reviewer_session": "fixture-reviewer", "review_status": "passed"}, "verify": {"command": str(verify_script)}}]}
        live.write_text(json.dumps(tracker)); self.data.write_text(json.dumps(tracker))
        stub = self.root / "check_stage.py"
        stub.write_text("import json, sys\nfrom pathlib import Path\ndata, page = map(Path, sys.argv[1:])\nhtml = page.read_text()\nassert json.loads(data.read_text())['tracking']['workflow']['stage'] == 'install'\nassert 'Install' in html\nassert 'class=\"workflow-now\"' not in html\n")
        command = f'"{sys.executable}" "{stub}" "{live}" "{self.page}"'
        result = self.install_after_review(live, "--ids", "ISSUE-01", "--then", command)
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

    def test_hook_rejects_missing_report(self):
        report = self.home / "docs/triage/pr-123.html"
        self.assertFalse(report.exists())
        result = subprocess.run([sys.executable, str(PLUGIN / "bin/bcl-hook"), str(self.home)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing report", result.stderr)

    def test_hook_rejects_staged_tracker_without_staged_report(self):
        tracker = self.home / 'docs/triage/pr-123-findings.json'
        report = self.home / 'docs/triage/pr-123.html'
        render = PLUGIN / 'skills/batched-change-list/scripts/render.py'
        subprocess.run([sys.executable, str(render), str(tracker), str(report)], check=True, capture_output=True)
        tracker.write_text(tracker.read_text() + ' ')
        self.git(self.home, 'add', str(tracker))
        result = subprocess.run([sys.executable, str(PLUGIN / 'bin/bcl-hook'), str(self.home)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('in the index', result.stderr)

    def test_stale_snapshot_refuses_without_changing_live(self):
        live = Path(self.call_without_viewer("adopt", self.data).stdout.strip()); before = live.read_bytes()
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

    def test_rollover_preserves_old_list_and_defers_new_viewer_until_collect(self):
        self.git(self.home, "worktree", "add", "-qb", "feature-124", str(self.root / "feature-124"))
        old_live = Path(json.loads((self.state / "registry.json").read_text())["example/project/123"]["live"])
        doc = json.loads(old_live.read_text())
        doc["findings"][0]["status"] = "in_progress"
        doc["findings"][1]["status"] = "failing"
        doc["findings"].append({**doc["findings"][1], "id":"BLOCKED-01", "status":"blocked"})
        contents = (json.dumps(doc, indent=2) + "\n").encode()
        self.data.write_bytes(contents); old_live.write_bytes(contents)
        before = old_live.read_bytes()
        old_source = self.data.read_bytes(); old_page = self.page.read_bytes()
        result = self.call("rollover", "--new-pr", "124", "--worktree", "feature-124", "--force", "--reason", "test override")
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
        new_html = (Path(registry["example/project/124"]["home"]) / registry["example/project/124"]["html"]).read_text()
        self.assertNotIn("Working on it", new_html)
        self.assertNotIn('class="workflow-now"', new_html)
        self.assertTrue("ISSUE-01" in new_html)
        self.assertEqual(self.page.read_bytes(), old_page)

    def test_rollover_sets_new_home_from_worktree(self):
        self.git(self.home, "worktree", "add", "-qb", "feature-124", str(self.root / "feature-124"))
        result = self.call("rollover", "--new-pr", "124", "--worktree", "feature-124", "--force", "--reason", "test override")
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads((self.state / "registry.json").read_text())["example/project/124"]
        self.assertEqual(Path(record["home"]).resolve(), (self.home.parent / "feature-124").resolve())

    def test_rollover_clears_old_plan_lanes(self):
        self.git(self.home, "worktree", "add", "-qb", "feature-124", str(self.root / "feature-124"))
        live = Path(json.loads((self.state / "registry.json").read_text())["example/project/123"]["live"])
        doc = json.loads(live.read_text()); doc["tracking"]["plan"] = {"round": 3, "cycle": 1, "groups": [{"id": "old-lane"}]}
        live.write_text(json.dumps(doc)); self.data.write_text(json.dumps(doc))
        result = self.call("rollover", "--new-pr", "124", "--worktree", "feature-124", "--force", "--reason", "test override")
        self.assertEqual(result.returncode, 0, result.stderr)
        new_live = Path(json.loads((self.state / "registry.json").read_text())["example/project/124"]["live"])
        self.assertNotIn("plan", json.loads(new_live.read_text())["tracking"])

    def test_merged_records_terminal_state_and_retargets_stacked_prs(self):
        sha = "a" * 40; merged_at = "2026-11-14T12:00:00Z"
        live = Path(json.loads((self.state / "registry.json").read_text())["example/project/123"]["live"])
        doc = json.loads(self.data.read_text())
        doc = self.merge_ready(doc)
        doc["tracking"]["workflow"] = {"stage": "report_rollover", "updated_at": "2026-11-14T11:00:00Z"}
        contents = json.dumps(doc)
        self.data.write_text(contents)
        live.write_text(contents)
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

    def test_merged_force_records_reason_when_gate_is_short(self):
        live = Path(json.loads((self.state / "registry.json").read_text())["example/project/123"]["live"])
        doc = json.loads(self.data.read_text())
        doc["tracking"]["workflow"] = {"stage": "report_rollover", "updated_at": "2026-11-14T11:00:00Z"}
        doc["tracking"]["stages_reached"] = ["report_rollover"]
        contents = json.dumps(doc)
        self.data.write_text(contents); live.write_text(contents)
        refused = self.call("merged")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("pass --force with --reason", refused.stderr)
        no_reason = self.call("merged", "--force")
        self.assertNotEqual(no_reason.returncode, 0)
        self.assertIn("merged override requires a one-line nonblank --reason", no_reason.stderr)
        multiline = self.call("merged", "--force", "--reason", "two\nlines")
        self.assertNotEqual(multiline.returncode, 0)
        self.assertEqual(json.loads(live.read_text())["tracking"]["workflow"]["stage"], "report_rollover")
        result = self.call("merged", "--force", "--reason", "gate zeroed by a stopped run")
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = json.loads(live.read_text())
        self.assertEqual(doc["tracking"]["workflow"]["stage"], "merged")
        override = doc["tracking"]["rollover"]["forced_gate_overrides"][-1]
        self.assertEqual((override["action"], override["reason"]), ("merged", "gate zeroed by a stopped run"))
        self.assertEqual((override["threshold"], override["verified_fixed"] < 16), (16, True))

    def test_report_force_passes_through_to_the_gate(self):
        live = Path(json.loads((self.state / "registry.json").read_text())["example/project/123"]["live"])
        doc = json.loads(self.data.read_text())
        doc["tracking"]["workflow"] = {"stage": "gate", "updated_at": "2026-11-14T11:00:00Z"}
        doc["tracking"]["stages_reached"] = ["introspect"]
        contents = json.dumps(doc)
        self.data.write_text(contents); live.write_text(contents)
        refused = self.call("report")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("pass --force", refused.stderr + refused.stdout)
        result = self.call("report", "--force", "--reason", "test override")
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = json.loads(live.read_text())
        self.assertEqual(doc["tracking"]["workflow"]["stage"], "report_rollover")
        self.assertEqual(doc["tracking"]["rollover"]["forced_gate_overrides"][-1]["action"], "set-stage")

    def test_merged_stops_its_viewer_by_recorded_pid(self):
        live = Path(json.loads((self.state / "registry.json").read_text())["example/project/123"]["live"])
        doc = self.merge_ready(json.loads(live.read_text()))
        doc["tracking"]["workflow"] = {"stage": "report_rollover", "updated_at": "2026-11-14T11:00:00Z"}
        live.write_text(json.dumps(doc)); self.data.write_text(json.dumps(doc))
        key, record = "example/project/123", json.loads((self.state / "registry.json").read_text())["example/project/123"]
        record["home"] = str(self.second)
        records = json.loads((self.state / "registry.json").read_text()); records[key] = record
        (self.state / "registry.json").write_text(json.dumps(records))
        path = self.state / "viewers" / (live_state.safe_component(key.replace("/", "--")) + ".json")
        path.write_text(json.dumps({"pid": 87654, "data": str(live.resolve()), "report": str(self.page.resolve())}))
        with patch.object(live_state, "_pid_is_running", return_value=True), \
             patch.object(live_state, "_viewer_process", return_value=True) as verify_process, \
             patch.object(live_state, "_wait_for_pid", return_value=True), \
             patch.object(live_state.os, "kill") as signal_process:
            result = self.call_without_viewer("merged")
        self.assertEqual(result.returncode, 0, result.stderr)
        signal_process.assert_called_once_with(87654, 15)
        verify_process.assert_called_once_with(87654, str(self.page.resolve()))
        self.assertFalse(path.exists())
        self.assertTrue((self.state / "viewers" / "other-list.json").exists())

    def test_merged_leaves_other_list_viewers_running(self):
        other = self.state / "viewers" / "other-list.json"
        before = other.read_bytes()
        live = Path(json.loads((self.state / "registry.json").read_text())["example/project/123"]["live"])
        doc = self.merge_ready(json.loads(live.read_text()))
        doc["tracking"]["workflow"] = {"stage": "report_rollover", "updated_at": "2026-11-14T11:00:00Z"}
        live.write_text(json.dumps(doc)); self.data.write_text(json.dumps(doc))
        result = self.call_without_viewer("merged")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(other.read_bytes(), before)

    def test_stop_viewer_ignores_pid_serving_another_list(self):
        key, record = "example/project/123", json.loads((self.state / "registry.json").read_text())["example/project/123"]
        path = self.state / "viewers" / (live_state.safe_component(key.replace("/", "--")) + ".json")
        path.write_text(json.dumps({"pid": 87655, "data": record["live"], "report": str(self.page.resolve())}))
        with patch.object(live_state, "_pid_is_running", return_value=True), \
             patch.object(live_state, "_viewer_process", return_value=False), \
             patch.object(live_state.os, "kill") as signal_process:
            result = self.call_without_viewer("stop-viewer")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot verify viewer PID", result.stderr)
        signal_process.assert_not_called()
        self.assertTrue(path.exists())

    def test_viewer_process_matches_real_arguments_with_spaces(self):
        with tempfile.TemporaryDirectory() as directory:
            reports = Path(directory) / "reports with spaces"
            reports.mkdir()
            report = reports / "list page.html"
            other = reports / "other page.html"
            report.write_text("<html></html>")
            other.write_text("<html></html>")
            proc = subprocess.Popen([sys.executable, str(live_state.SCRIPTS / "serve.py"), str(report), "--port", str(self.viewer_port)],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and not live_state._viewer_process(proc.pid, report):
                    time.sleep(0.05)
                self.assertTrue(live_state._viewer_process(proc.pid, report))
                self.assertFalse(live_state._viewer_process(proc.pid, other))
            finally:
                viewer_test_guard.stop_viewers(directory, (proc.pid,))
                proc.wait(timeout=5)

    def test_merged_keeps_terminal_data_and_emits_todo_when_retarget_api_fails(self):
        doc = json.loads(self.data.read_text())
        doc["tracking"]["pull_request"]["url"] = "https://origin.example/example/project/pull/123"
        doc = self.merge_ready(doc)
        doc["tracking"]["workflow"] = {"stage": "report_rollover", "updated_at": "2026-11-14T11:00:00Z"}
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
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("snapshot home worktree missing", result.stderr)
        live = Path(record["live"])
        self.assertTrue(live.with_suffix(".unsnapshotted").exists())

    def test_missing_capture_fails_and_preserves_recovery_marker(self):
        import render
        record = next(iter(json.loads((self.state / "registry.json").read_text()).values()))
        live = Path(record["live"])
        missing = self.home / Path(record["snapshot"]).parent / "missing.png"
        error = FileNotFoundError(2, "missing capture", str(missing))
        with patch.object(render, "render", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "snapshot capture missing"):
                live_state.snapshot(record, live)
        self.assertTrue(live.with_suffix(".unsnapshotted").exists())

    def test_viewer_avoids_default_port_reserved_by_another_list(self):
        self.assertEqual(self.call("start").returncode, 0)
        path = self.viewer_state_path()
        state = json.loads(path.read_text())
        self.assertEqual(json.loads((self.state / "viewers/other-list.json").read_text())["port"], 53366)
        self.assertNotEqual(state["port"], 53366)
        self.assertEqual(state["port"], self.viewer_port)
        self.assertEqual(state["version"], json.loads((PLUGIN / "plugin.json").read_text())["version"])
        self.assertIn("started_at", state)
        self.assertTrue(state["data"].endswith("123.json"))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", state["port"]), timeout=.2): break
            except OSError: time.sleep(.05)
        else: self.fail("viewer did not bind its recorded loopback port")

    def test_port_probe_propagates_non_occupancy_socket_error(self):
        class FailedSocket:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def setsockopt(self, *args): pass
            def bind(self, *args): raise OSError(errno.EACCES, "permission denied")

        with patch.object(live_state.socket, "socket", return_value=FailedSocket()):
            with self.assertRaisesRegex(OSError, "permission denied"):
                live_state._port_is_free(53366)

    def test_corrupt_other_viewer_state_is_reported(self):
        state = self.state / "viewers" / "other-list.json"
        state.write_text("{")
        with self.assertRaisesRegex(ValueError, "invalid viewer state"):
            live_state._reserved_ports(self.state / "viewers" / "example--project--123.json")

    def test_viewer_restarts_for_version_and_data_path_mismatch(self):
        self.assertEqual(self.call("start").returncode, 0)
        path = self.viewer_state_path()
        state = json.loads(path.read_text()); first_pid = state["pid"]
        state["version"] = "0.1.16"; path.write_text(json.dumps(state))
        result = self.call("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        updated = json.loads(path.read_text()); self.assertNotEqual(updated["pid"], first_pid)
        updated["data"] = "/tmp/wrong.json"; path.write_text(json.dumps(updated))
        old_pid = updated["pid"]
        self.assertEqual(self.call("status").returncode, 0)
        self.assertNotEqual(json.loads(path.read_text())["pid"], old_pid)

    def test_viewer_restarts_when_report_path_moves(self):
        self.assertEqual(self.call("start").returncode, 0)
        path = self.viewer_state_path()
        before = json.loads(path.read_text())
        records_path = self.state / "registry.json"
        records = json.loads(records_path.read_text())
        record = records["example/project/123"]
        new_home = self.second
        (new_home / Path(record["html"]).parent).mkdir(parents=True, exist_ok=True)
        (new_home / Path(record["snapshot"]).parent).mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.page, new_home / record["html"])
        shutil.copy2(self.data, new_home / record["snapshot"])
        record["home"] = str(new_home)
        records_path.write_text(json.dumps(records))
        result = self.call("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        after = json.loads(path.read_text())
        self.assertNotEqual(after["pid"], before["pid"])
        self.assertEqual(Path(after["report"]).resolve(), (new_home / record["html"]).resolve())

    def test_collect_start_uses_default_port_when_free(self):
        self.git(self.home, "worktree", "add", "-qb", "feature-124", str(self.root / "feature-124"))
        self.call("rollover", "--new-pr", "124", "--worktree", "feature-124", "--force", "--reason", "test override")
        records = json.loads((self.state / "registry.json").read_text())
        new_record = records["example/project/124"]
        new_live = Path(new_record["live"])
        (self.state / "viewers" / "other-list.json").unlink()
        binds = []

        class FakeSocket:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def setsockopt(self, *args): pass
            def bind(self, address): binds.append(address)

        real_popen = subprocess.Popen
        def safe_popen(command, *args, **kwargs):
            if len(command) > 1 and str(command[1]).endswith("/serve.py"):
                return SimpleNamespace(pid=87006)
            return real_popen(command, *args, **kwargs)

        with patch.dict(os.environ, self.env), patch.object(live_state, "STATE", self.state), \
             patch.object(live_state.socket, "socket", FakeSocket), \
             patch.object(bcl_entrypoint.subprocess, "Popen", safe_popen), \
             patch.object(live_state, "_viewer_ready", return_value=True), \
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
        viewer_path.write_text(json.dumps({"port":53366, "pid":None, "version":"old", "data":str(live)}))
        (self.state / "viewers" / "other-list.json").unlink()
        binds = []
        class FakeSocket:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def setsockopt(self, *args): pass
            def bind(self, address): binds.append(address)
        with patch.object(live_state, "STATE", self.state), patch.object(live_state.socket, "socket", FakeSocket), \
             patch.object(live_state.subprocess, "Popen", return_value=SimpleNamespace(pid=87006)) as popen, \
             patch.object(live_state, "_viewer_ready", return_value=True):
            value = live_state.ensure_viewer(key, record, live.resolve(), "0.1.17")
        self.assertEqual(value["port"], 53366)
        self.assertEqual(binds, [("127.0.0.1", 53366)])
        self.assertEqual(popen.call_args.args[0][popen.call_args.args[0].index("--port") + 1], "53366")

    def test_outdated_running_viewer_releases_owned_default_port_before_restart(self):
        self.assertEqual(self.call("start").returncode, 0)
        records = json.loads((self.state / "registry.json").read_text())
        key, record = next(iter(records.items()))
        live = Path(record["live"]).resolve()
        viewer_path = self.viewer_state_path(live)
        current = json.loads(viewer_path.read_text())
        viewer_path.write_text(json.dumps({"port":53366, "pid":12345, "version":"old", "data":str(live), "instance":"test"}))
        (self.state / "viewers" / "other-list.json").unlink()
        signals, probes, binds = [], [], []

        class FakeSocket:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def bind(self, address): binds.append(address)
            def getsockname(self): return ("127.0.0.1", 54444)

        def fake_kill(pid, sig): signals.append((pid, sig))
        class FakeResponse:
            status = 204
            def __enter__(self): return self
            def __exit__(self, *args): return False
        def port_is_free(port):
            probes.append(port)
            return len(probes) > 1

        with patch.object(live_state, "STATE", self.state), patch.object(live_state.os, "kill", fake_kill), \
             patch.object(live_state, "_viewer_responds", return_value=True), \
             patch.object(live_state.urllib.request, "urlopen", return_value=FakeResponse()), \
             patch.object(live_state, "_port_is_free", side_effect=port_is_free), \
             patch.object(live_state.socket, "socket", FakeSocket), \
             patch.object(live_state.subprocess, "Popen", return_value=SimpleNamespace(pid=87006)), \
             patch.object(live_state, "_viewer_ready", return_value=True):
            value = live_state.ensure_viewer(key, record, live, "0.1.17")

        self.assertEqual(signals, [(12345, 0)])
        self.assertEqual(probes, [53366, 53366, 53366])
        self.assertEqual(value["port"], 53366)
        self.assertEqual(binds, [])

    def test_viewer_start_failure_is_reported_and_not_recorded(self):
        state = self.state / "viewers/test-1.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"pid": 87006}))
        previous = state.read_bytes()
        live = self.root / "live.json"
        live.write_text("{}")
        page = self.root / "page.html"
        page.write_text("viewer")
        process = SimpleNamespace(pid=99999, poll=lambda: 1)
        def fail_start(*args, **kwargs):
            kwargs["stderr"].write("Traceback (most recent call last):\nOSError: child startup cause\n")
            return process
        with patch.object(live_state, "STATE", self.state), \
             patch.object(live_state, "report_paths", return_value=(page, self.root)), \
             patch.object(live_state, "_reserved_ports", return_value=set()), \
             patch.object(live_state, "_port_is_free", return_value=True), \
             patch.object(live_state.subprocess, "Popen", side_effect=fail_start), \
             patch.object(live_state, "_viewer_ready", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "(?s)viewer failed to start.*OSError: child startup cause"):
                live_state.ensure_viewer("test/1", {"repo": "test", "id": "1"}, live, "new-version")
        self.assertEqual(state.read_bytes(), previous)

    def test_unresponsive_recorded_viewer_is_not_reported_as_healthy(self):
        records = json.loads((self.state / "registry.json").read_text())
        key, record = next(iter(records.items()))
        state_path = self.state / "viewers" / (live_state.safe_component(key.replace("/", "--")) + ".json")
        state = {"pid": 28743, "port": 53367, "version": "old", "instance": "instance", "data": record["live"]}
        state_path.write_text(json.dumps(state))
        previous = state_path.read_bytes()
        with patch.object(live_state, "STATE", self.state), \
             patch.object(live_state.os, "kill") as signal_process, \
             patch.object(live_state, "_viewer_responds", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "cannot verify viewer ownership"):
                live_state.ensure_viewer(key, record, Path(record["live"]), state["version"])
        signal_process.assert_called_once_with(state["pid"], 0)
        self.assertEqual(state_path.read_bytes(), previous)

    def test_unverified_viewer_is_never_signalled(self):
        records = json.loads((self.state / "registry.json").read_text())
        key, record = next(iter(records.items()))
        state_path = self.state / "viewers" / (live_state.safe_component(key.replace("/", "--")) + ".json")
        state = {"pid": 28743, "port": 53367, "version": "old", "instance": "instance", "data": record["live"]}
        state_path.write_text(json.dumps(state))
        previous = state_path.read_bytes()
        with patch.object(live_state, "STATE", self.state), \
             patch.object(live_state.os, "kill") as signal_process, \
             patch.object(live_state, "_viewer_responds", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "cannot verify viewer ownership"):
                live_state.ensure_viewer(key, record, Path(record["live"]), "new-version")
        signal_process.assert_called_once_with(state['pid'], 0)
        self.assertEqual(state_path.read_bytes(), previous)

    def test_legacy_viewer_is_not_stopped_without_identity(self):
        records = json.loads((self.state / "registry.json").read_text())
        key, record = next(iter(records.items()))
        path = self.state / "viewers" / (live_state.safe_component(key.replace("/", "--")) + ".json")
        state = {"pid": 28743, "port": 53367, "version": "old", "data": record["live"]}
        path.write_text(json.dumps(state))
        unchanged = path.read_bytes()
        with patch.object(live_state, "STATE", self.state), \
             patch.object(live_state.os, "kill") as signal_process, \
             patch.object(live_state.urllib.request, "urlopen") as request:
            with self.assertRaisesRegex(RuntimeError, "cannot verify viewer ownership"):
                live_state.ensure_viewer(key, record, Path(record["live"]), "new-version")
        signal_process.assert_called_once_with(state['pid'], 0)
        request.assert_called_once_with(f"http://127.0.0.1:{state['port']}/__bcl_health", timeout=0.5)
        self.assertEqual(path.read_bytes(), unchanged)

    def test_status_reconciles_legacy_viewer(self):
        self.assertEqual(self.call("start").returncode, 0)
        state = self.viewer_state_path()
        before = json.loads(state.read_text())
        before.pop('instance')
        state.write_text(json.dumps(before))
        result = self.call('status')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['adopted'])
        after = json.loads(state.read_text())
        self.assertNotEqual(after['pid'], before['pid'])
        self.assertTrue(after.get('instance'))

if __name__ == "__main__": unittest.main()
