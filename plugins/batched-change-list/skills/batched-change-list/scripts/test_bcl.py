import importlib.util
import contextlib
import io
from importlib.machinery import SourceFileLoader
import errno, json, os, shutil, signal, socket, subprocess, sys, tempfile, threading, time, unittest
import shlex
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
import check_introspect
loader = SourceFileLoader("bcl_entrypoint", str(BCL))
spec = importlib.util.spec_from_loader("bcl_entrypoint", loader)
bcl_entrypoint = importlib.util.module_from_spec(spec)
try:
    loader.exec_module(bcl_entrypoint)
finally:
    sys.path.remove(str(bcl_entrypoint.SCRIPTS))

CLEANED_VIEWER_PIDS = set()
TEMP_TEST_ROOTS = set()

def viewer_pids_under(roots):
    pids = set()
    rows = subprocess.run(["ps", "-ww", "-axo", "pid,command"], capture_output=True, text=True).stdout.splitlines()[1:]
    roots = [Path(root).resolve() for root in roots]
    for row in rows:
        try:
            pid, command = row.strip().split(None, 1)
            args = shlex.split(command)
            if "--data" not in args or not any(Path(arg).name == "serve.py" for arg in args): continue
            data = Path(args[args.index("--data") + 1]).resolve()
            if any(data.is_relative_to(root) for root in roots): pids.add(int(pid))
        except (ValueError, IndexError): pass
    return pids

def tearDownModule():
    pids = viewer_pids_under(TEMP_TEST_ROOTS)
    for pid in pids:
        try: os.kill(pid, signal.SIGTERM)
        except OSError: pass
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and any(live_state._pid_is_running(pid) for pid in pids): time.sleep(.05)
    CLEANED_VIEWER_PIDS.update(pids)
    leaked = [pid for pid in CLEANED_VIEWER_PIDS if live_state._pid_is_running(pid)]
    if leaked:
        raise AssertionError(f"viewer processes survived suite cleanup: {leaked}")

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
        self.assertIn('Checked: python3 check.py', reason)
        self.assertIn('Expected: verification command exits 0', reason)
        self.assertIn('Observed: Verification command exited 7', reason)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.state = self.root / "state"
        TEMP_TEST_ROOTS.add(self.root.resolve())
        self.extra_pids = set(); self.addCleanup(self.cleanup_viewers)
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

    def cleanup_viewers(self):
        pids = set(self.extra_pids)
        for path in (self.state / 'viewers').glob('*.json'):
            try:
                record = json.loads(path.read_text())
                if record.get('pid') and Path(record.get('data', '')).resolve().is_relative_to(self.root.resolve()):
                    pids.add(int(record['pid']))
            except (OSError, ValueError, TypeError): pass
        pids.update(viewer_pids_under([self.root]))
        owned = set()
        for pid in pids - {87005, 28743}:
            result = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(pid)], capture_output=True, text=True)
            try: command = shlex.split(result.stdout.strip()) if result.returncode == 0 else []
            except ValueError: command = []
            if "--data" in command and any(Path(arg).name == "serve.py" for arg in command):
                data = Path(command[command.index("--data") + 1]).resolve()
                if data.is_relative_to(self.root.resolve()): owned.add(pid)
        for pid in owned:
            try: os.kill(pid, signal.SIGTERM)
            except OSError: pass
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and any(live_state._pid_is_running(pid) for pid in owned):
            time.sleep(.05)
        CLEANED_VIEWER_PIDS.update(owned)

    def test_suite_leaves_no_viewer_processes(self):
        result = self.call("start")
        self.assertEqual(result.returncode, 0, result.stderr)
        viewers = [json.loads(path.read_text()) for path in (self.state / "viewers").glob("*.json")]
        self.assertTrue(any(value.get("pid") for value in viewers))
        pid = next(int(value["pid"]) for value in viewers if value.get("pid"))
        self.cleanup_viewers()
        self.assertFalse(live_state._pid_is_running(pid))
        self.assertTrue(all(not live_state._pid_is_running(pid) for pid in CLEANED_VIEWER_PIDS))

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
        with patch.object(bcl_entrypoint, "HERE", checkout), patch.dict(os.environ, {"HOME": str(home)}), \
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
    def sync_main(self):
        return subprocess.run([sys.executable, str(BCL), "sync-main"], cwd=self.home, env=self.env, capture_output=True, text=True)
    def branch_for_sync(self):
        self.git(self.home, "add", "docs/triage/pr-123-findings.html")
        self.git(self.home, "commit", "-qm", "baseline rendered report")
        self.git(self.home, "branch", "-M", "main")
        self.git(self.home, "switch", "-c", "feature")

    def test_sync_main_merges_renders_and_stashes_hook_dirt(self):
        self.branch_for_sync()
        (self.home / ".githooks").mkdir()
        (self.home / ".githooks/pre-commit").write_text("leftover hook\n")
        self.git(self.home, "add", ".githooks/pre-commit"); self.git(self.home, "commit", "-qm", "feature hook")
        self.git(self.home, "switch", "main")
        (self.home / "main-change.txt").write_text("from main\n")
        self.git(self.home, "add", "main-change.txt"); self.git(self.home, "commit", "-qm", "main change")
        self.git(self.home, "switch", "feature")
        (self.home / ".githooks/pre-commit").write_text("leftover hook dirt\n")
        result = self.sync_main()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.home / "main-change.txt").exists())
        self.assertEqual((self.home / "docs/triage/pr-123-findings.html").read_text()[:15], "<!doctype html>")
        self.assertEqual((self.home / ".githooks/pre-commit").read_text(), "leftover hook\n")
        self.assertIn("bcl sync-main hook/check dirt", self.git(self.home, "stash", "list").stdout)

    def test_sync_main_stashes_hook_dirt_and_preserves_unrelated_work(self):
        self.branch_for_sync()
        (self.home / ".githooks").mkdir()
        (self.home / ".githooks/pre-commit").write_text("leftover hook\n")
        self.git(self.home, "add", ".githooks/pre-commit"); self.git(self.home, "commit", "-qm", "feature hook")
        self.git(self.home, "switch", "main")
        (self.home / "main-change.txt").write_text("from main\n")
        self.git(self.home, "add", "main-change.txt"); self.git(self.home, "commit", "-qm", "main change")
        self.git(self.home, "switch", "feature")
        (self.home / ".githooks/pre-commit").write_text("leftover hook dirt\n")
        (self.home / "wip.txt").write_text("unrelated work\n")
        result = self.sync_main()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.home / "wip.txt").read_text(), "unrelated work\n")
        self.assertEqual((self.home / ".githooks/pre-commit").read_text(), "leftover hook\n")
        stashed = self.git(self.home, "stash", "show", "--include-untracked", "--name-only").stdout
        self.assertIn(".githooks/pre-commit", stashed)
        self.assertNotIn("wip.txt", stashed)

    def test_sync_main_refuses_dirty_tracker_without_stashing_or_merging(self):
        self.branch_for_sync()
        self.git(self.home, "switch", "main")
        (self.home / "main-change.txt").write_text("from main\n")
        self.git(self.home, "add", "main-change.txt"); self.git(self.home, "commit", "-qm", "main change")
        self.git(self.home, "switch", "feature")
        (self.home / "docs/triage/pr-123-findings.json").write_text("dirty tracker")
        result = self.sync_main()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("commit them or clean them", result.stderr)
        self.assertFalse((self.home / "main-change.txt").exists())
        self.assertEqual(self.git(self.home, "stash", "list").stdout, "")

    def test_sync_main_stashes_dirt_created_by_post_merge_hook(self):
        self.branch_for_sync()
        (self.home / "feature-change.txt").write_text("feature\n")
        self.git(self.home, "add", "feature-change.txt"); self.git(self.home, "commit", "-qm", "feature change")
        self.git(self.home, "switch", "main")
        (self.home / "main-change.txt").write_text("main\n")
        self.git(self.home, "add", "main-change.txt"); self.git(self.home, "commit", "-qm", "main change")
        self.git(self.home, "switch", "feature")
        hook = self.home / ".git/hooks/post-merge"
        hook.write_text("#!/bin/sh\nprintf 'hook dirt\\n' > hook-output.txt\n"); hook.chmod(0o755)
        result = self.sync_main()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.home / "hook-output.txt").exists())
        self.assertIn("hook-output.txt", self.git(self.home, "stash", "show", "--include-untracked", "--name-only").stdout)

    def test_sync_main_stashes_post_merge_dirt_recreated_at_pre_stashed_path(self):
        self.branch_for_sync()
        (self.home / ".githooks").mkdir()
        (self.home / ".githooks/pre-commit").write_text("committed hook\n")
        self.git(self.home, "add", ".githooks/pre-commit"); self.git(self.home, "commit", "-qm", "feature hook")
        self.git(self.home, "switch", "main")
        (self.home / "main-change.txt").write_text("main\n")
        self.git(self.home, "add", "main-change.txt"); self.git(self.home, "commit", "-qm", "main change")
        self.git(self.home, "switch", "feature")
        (self.home / ".githooks/pre-commit").write_text("leftover hook dirt\n")
        hook = self.home / ".git/hooks/post-merge"
        hook.write_text("#!/bin/sh\nprintf 'recreated hook dirt\\n' > .githooks/pre-commit\n")
        hook.chmod(0o755)
        result = self.sync_main()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.home / ".githooks/pre-commit").read_text(), "committed hook\n")
        self.assertIn(".githooks/pre-commit", self.git(self.home, "stash", "show", "--include-untracked", "--name-only").stdout)

    def test_sync_main_preserves_merge_failure_without_unmerged_paths(self):
        self.branch_for_sync()
        original_run = subprocess.run
        calls = []
        def run(command, *args, **kwargs):
            if command[-2:] == ["merge", "main"]:
                return subprocess.CompletedProcess(command, 1, "", "merge failed without conflicts")
            calls.append(command)
            return original_run(command, *args, **kwargs)
        with (patch.object(live_state, "STATE", self.state), patch.object(Path, "cwd", return_value=self.home),
              patch.object(subprocess, "run", side_effect=run), self.assertRaises(subprocess.CalledProcessError) as failure):
            bcl_entrypoint._sync_main()
        self.assertEqual(failure.exception.stderr, "merge failed without conflicts")
        self.assertFalse(any(command[-2:] == ["commit", "--no-edit"] for command in calls))

    def test_sync_main_resolves_deleted_skill_reference_conflict(self):
        self.branch_for_sync()
        reference = self.home / "plugins/example/skills/demo/references/old.md"
        reference.parent.mkdir(parents=True); reference.write_text("base\n")
        self.git(self.home, "add", "plugins"); self.git(self.home, "commit", "-qm", "add reference")
        self.git(self.home, "branch", "-f", "main")
        self.git(self.home, "switch", "main"); reference.unlink()
        self.git(self.home, "add", "-u"); self.git(self.home, "commit", "-qm", "delete reference")
        self.git(self.home, "switch", "feature"); reference.write_text("feature edit\n")
        self.git(self.home, "add", "plugins"); self.git(self.home, "commit", "-qm", "edit reference")
        result = self.sync_main()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(reference.exists())
        self.assertEqual(self.git(self.home, "status", "--porcelain").stdout, "")

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
                         review_status="passed", job={"phase":"fix", "exit_code":0,
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

    def call_without_viewer(self, stage, *args):
        output, error = io.StringIO(), io.StringIO()
        argv = [str(BCL), "--data", str(self.data), *map(str, args), stage]
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
        self.assertEqual(state["live"], self.call("adopt", self.second / "docs/triage/pr-123-findings.json").stdout.strip())

    def test_adopt_accepts_tracker_as_positional_argument(self):
        result = subprocess.run([sys.executable, str(BCL), "adopt", str(self.data)], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

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
        result = subprocess.run([sys.executable, str(BCL), "--data", str(self.data), "--ids", "ISSUE-01",
                                 "--phase", "review", "job", "--", *job], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run([sys.executable, str(BCL), "--data", str(self.data), "--ids", "ISSUE-01", "--phase", "presuite", "job", "--", *job], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.call_without_viewer("install", "--ids", "ISSUE-01").returncode, 0)
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
        self.assertEqual(json.loads(status.stdout)["plan"][0]["status"], "installing")

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
        plan = {'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'status':'pending','verify':{'command':'echo verify'}}]}
        path = self.root / 'plan.json'; path.write_text(json.dumps(plan))
        self.assertEqual(self.call('plan','--plan',str(path),'--ids','ISSUE-01').returncode, 0)
        self.assertEqual(self.install_after_review('--ids','ISSUE-01').returncode, 0)
        result = self.call('verify','--ids','ISSUE-01','--then','echo verify')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('actual check', result.stderr)
        plan['groups'][0]['verify']['command'] = 'python3 -m unittest test_example'
        path.write_text(json.dumps(plan))
        self.assertEqual(self.call('plan','--plan',str(path),'--ids','ISSUE-01').returncode, 0)
        result = self.call('verify','--ids','ISSUE-01','--then','python3 -m unittest other')
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
        self.assertEqual(next(item for item in written['findings'] if item['id'] == 'ISSUE-01')['status'], 'blocked')
        self.assertIn('exited 7', self.page.read_text())

        masked = command + ' ; true'
        plan['groups'][0]['verify']['command'] = masked
        path.write_text(json.dumps(plan))
        self.assertEqual(self.call('plan','--plan',str(path),'--ids','ISSUE-01').returncode, 0)
        before = live.read_bytes()
        result = self.call('verify','--ids','ISSUE-01','--then',masked)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('shell operators', result.stderr)
        self.assertEqual(live.read_bytes(), before)

        plan['groups'][0]['verify']['command'] = "sh -c 'exit 7; true'"
        path.write_text(json.dumps(plan))
        self.assertEqual(self.call('plan','--plan',str(path),'--ids','ISSUE-01').returncode, 0)
        result = self.call('verify','--ids','ISSUE-01','--then',plan['groups'][0]['verify']['command'])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('cannot run a shell', result.stderr)

        changed = self.root / 'changed.py'
        changed.write_text(f"import json\nfrom pathlib import Path\np=Path({str(live)!r})\nd=json.loads(p.read_text())\nd['findings'][0]['status']='failing'\np.write_text(json.dumps(d))\nraise SystemExit(7)\n")
        command = f'"{sys.executable}" "{changed}"'
        plan['groups'][0]['verify']['command'] = command
        path.write_text(json.dumps(plan))
        self.assertEqual(self.call('plan','--plan',str(path),'--ids','ISSUE-01').returncode, 0)
        result = self.call('verify','--ids','ISSUE-01','--then',command)
        self.assertNotEqual(result.returncode, 0)
        written = json.loads(live.read_text())
        self.assertEqual(written['findings'][0]['status'], 'failing')
        self.assertEqual(written['tracking']['plan']['groups'][0]['status'], 'failed')

    def test_failed_command_blocks_active_check_without_losing_observation(self):
        self.assertEqual(self.call('start').returncode, 0)
        fail = self.root / 'fail.py'; fail.write_text('raise SystemExit(7)\n')
        command = f'"{sys.executable}" "{fail}"'
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
        result = self.call('verify','--ids','ISSUE-01','--then',command)
        self.assertNotEqual(result.returncode, 0)
        written = json.loads(live.read_text())
        check = written['tracking']['verification_run']['checks']['ISSUE-01']
        self.assertEqual(check['state'], 'blocked', result.stderr)
        self.assertEqual(check['observation'], 'Visible before check')
        self.assertIn('exited 7', check['evidence'])
        self.assertEqual(next(item for item in written['findings'] if item['id'] == 'ISSUE-01')['status'], 'blocked')

    def test_failed_command_does_not_overwrite_newer_plan(self):
        self.assertEqual(self.call('start').returncode, 0)
        live = Path(json.loads((self.state / 'registry.json').read_text())['example/project/123']['live'])
        fail = self.root / 'replan.py'
        fail.write_text(f"import json\nfrom pathlib import Path\np=Path({str(live)!r})\nd=json.loads(p.read_text())\nd['tracking']['plan']['groups'][0]['status']='review'\np.write_text(json.dumps(d))\nraise SystemExit(7)\n")
        command = f'"{sys.executable}" "{fail}"'
        plan = {'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'status':'pending','verify':{'command':command}}]}
        path = self.root / 'plan.json'; path.write_text(json.dumps(plan))
        self.assertEqual(self.call('plan','--plan',str(path),'--ids','ISSUE-01').returncode, 0)
        self.assertEqual(self.install_after_review('--ids','ISSUE-01').returncode, 0)
        result = self.call('verify','--ids','ISSUE-01','--then',command)
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
        self.assertEqual(next(item for item in written['findings'] if item['id'] == 'ISSUE-02')['status'], 'blocked')

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
            f"deadline = time.monotonic() + 10; gate = Path({str(release)!r})",
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
            deadline = time.monotonic() + 10
            while not changed.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(changed.exists(), 'verifier child could not write while launch lock was held')
            sys.argv = [str(BCL),'--data',str(live),'--ids','ISSUE-01','--then',command,'verify']
            with self.assertRaisesRegex(SystemExit, 'verification already in progress for this tracker'):
                bcl_entrypoint.main()
            release.write_text('continue')
            thread.join(timeout=10)
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
        threads, started, completed, results = [], [threading.Event() for _ in operations], \
            [threading.Event() for _ in operations], [None for _ in operations]

        def spawn_writer(index):
            ops = self.root / f'writer-{index}.json'; ops.write_text(json.dumps(operations[index]))
            def write():
                started[index].set()
                results[index] = subprocess.run(
                    [sys.executable, str(manager), 'batch', str(live), str(ops), '--render', str(self.page)],
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
                self.assertTrue(all(event.wait(5) for event in started))
                self.assertFalse(any(event.wait(0.15) for event in completed),
                                 'managed plan/finding writer crossed the launch reservation')
            return real_popen(args, *popen_args, **kwargs)

        with patch.dict(os.environ, self.env), patch.object(live_state, 'STATE', self.state), \
             patch.object(bcl_entrypoint.subprocess, 'Popen', check_writers_then_spawn), \
             patch.object(sys, 'argv', [str(BCL),'--data',str(live),'--ids','ISSUE-01','--then',stored_command,'verify']):
            bcl_entrypoint.main()
        for thread in threads:
            thread.join(timeout=5)
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
            {'id':'A','finding_ids':['ISSUE-02'],'status':'verified','verify':{'command':'python3 proof-a.py'}},
            {'id':'B','finding_ids':['ISSUE-01'],'depends_on':['A'],'status':'pending',
             'codex':{'implementer_session':'fixture-implementer','reviewer_session':'fixture-reviewer','review_status':'passed'},
             'verify':{'command':command}},
        ]}
        path = self.root / 'dependency-plan.json'; path.write_text(json.dumps(plan))
        invoke('plan','--plan',str(path),'--ids','ISSUE-01')
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
        for phase in ("review", "presuite"):
            result = subprocess.run([sys.executable, str(BCL), "--data", str(live), "--ids", "ISSUE-01", "--ids", "ISSUE-02",
                "--phase", phase, "job", "--", sys.executable, "-c", "pass"], env=self.env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("each lane", result.stderr)

    def test_one_file_review_skip_and_presuite_gate_real_install(self):
        self.assertEqual(self.call("start").returncode, 0)
        plan = self.root / "one-file.json"
        plan.write_text(json.dumps({"round": 1, "groups": [{"id": "A", "finding_ids": ["ISSUE-01"],
            "status": "planned", "codex": {"one_file": True}}]}))
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
        plan = self.root / "plan.json"
        plan.write_text(json.dumps({"round": 1, "groups": [{"id": "A", "finding_ids": ["ISSUE-01"],
            "status": "planned", "codex": {"one_file": True}}]}))
        self.assertEqual(self.call("plan", "--plan", str(plan), "--ids", "ISSUE-01").returncode, 0)
        result = self.call("install", "--ids", "ISSUE-01")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("install requires review", result.stderr)

    def test_stage_renders_before_then_command(self):
        live = Path(json.loads((self.state / "registry.json").read_text())["example/project/123"]["live"])
        tracker = json.loads(live.read_text())
        tracker["findings"][0]["status"] = "in_progress"
        tracker["tracking"]["stages_reached"] = ["plan_fix", "review"]
        tracker["tracking"]["plan"] = {"cycle": 1, "groups": [{"id": "A", "finding_ids": ["ISSUE-01"], "status": "review", "phase": "review", "codex": {"implementer_session": "fixture-implementer", "reviewer_session": "fixture-reviewer", "review_status": "passed"}}]}
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
        new_html = (self.home / registry["example/project/124"]["html"]).read_text()
        self.assertNotIn("Working on it", new_html)
        self.assertNotIn('class="workflow-now"', new_html)
        self.assertTrue("ISSUE-01" in new_html)
        self.assertEqual(self.page.read_bytes(), old_page)

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
        self.assertNotEqual(self.call("merge", live).returncode, 0)

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
            live_state._reserved_ports(self.viewer_state_path())

    def test_viewer_restarts_for_version_and_data_path_mismatch(self):
        path = self.viewer_state_path()
        state = json.loads(path.read_text()); first_pid = state["pid"]
        state["version"] = "0.1.16"; path.write_text(json.dumps(state))
        result = self.call("merge")
        self.assertEqual(result.returncode, 0, result.stderr)
        updated = json.loads(path.read_text()); self.assertNotEqual(updated["pid"], first_pid)
        updated["data"] = "/tmp/wrong.json"; path.write_text(json.dumps(updated))
        old_pid = updated["pid"]
        self.extra_pids.add(old_pid)
        self.assertEqual(self.call("merge").returncode, 0)
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
            def setsockopt(self, *args): pass
            def bind(self, address): binds.append(address)

        real_popen = subprocess.Popen
        def safe_popen(command, *args, **kwargs):
            if len(command) > 1 and str(command[1]).endswith("/serve.py"):
                return SimpleNamespace(pid=87005)
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
        self.extra_pids.add(int(current["pid"]))
        viewer_path.write_text(json.dumps({"port":53366, "pid":None, "version":"old", "data":str(live)}))
        (self.state / "viewers" / "other-list.json").unlink()
        binds = []
        class FakeSocket:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def setsockopt(self, *args): pass
            def bind(self, address): binds.append(address)
        with patch.object(live_state, "STATE", self.state), patch.object(live_state.socket, "socket", FakeSocket), \
             patch.object(live_state.subprocess, "Popen", return_value=SimpleNamespace(pid=87005)) as popen, \
             patch.object(live_state, "_viewer_ready", return_value=True):
            value = live_state.ensure_viewer(key, record, live.resolve(), "0.1.17")
        self.assertEqual(value["port"], 53366)
        self.assertEqual(binds, [("127.0.0.1", 53366)])
        self.assertEqual(popen.call_args.args[0][popen.call_args.args[0].index("--port") + 1], "53366")

    def test_outdated_running_viewer_releases_owned_default_port_before_restart(self):
        records = json.loads((self.state / "registry.json").read_text())
        key, record = next(iter(records.items()))
        live = Path(record["live"]).resolve()
        viewer_path = self.viewer_state_path(live)
        current = json.loads(viewer_path.read_text())
        self.extra_pids.add(int(current["pid"]))
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
             patch.object(live_state.subprocess, "Popen", return_value=SimpleNamespace(pid=87005)), \
             patch.object(live_state, "_viewer_ready", return_value=True):
            value = live_state.ensure_viewer(key, record, live, "0.1.17")

        self.assertEqual(signals, [(12345, 0)])
        self.assertEqual(probes, [53366, 53366, 53366])
        self.assertEqual(value["port"], 53366)
        self.assertEqual(binds, [])

    def test_viewer_start_failure_is_reported_and_not_recorded(self):
        state = self.state / "viewers/test-1.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"pid": 87005}))
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

    def test_status_reads_tracker_without_touching_legacy_viewer(self):
        state = self.viewer_state_path()
        before = json.loads(state.read_text())
        before.pop('instance')
        state.write_text(json.dumps(before))
        result = self.call('status')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['adopted'])
        self.assertEqual(json.loads(state.read_text()), before)

if __name__ == "__main__": unittest.main()
