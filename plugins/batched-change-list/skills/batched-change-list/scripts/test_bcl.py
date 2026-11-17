import importlib.util
from importlib.machinery import SourceFileLoader
import json, os, signal, socket, subprocess, sys, tempfile, threading, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[5]
PLUGIN = ROOT / "plugins/batched-change-list"
BCL = PLUGIN / "bin/bcl"
EXAMPLE = PLUGIN / "skills/batched-change-list/examples/findings.json"
sys.path.insert(0, str(PLUGIN / "skills/batched-change-list/scripts"))
import live_state
import check_introspect
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

    def test_repo_id_uses_cursor_origin_over_github_mirror(self):
        self.git(self.home, "remote", "add", "origin", "https://origin.cursor.com/gurusharan/agent-plugins")
        self.assertEqual(live_state.repo_id(self.home), "example/project")
        self.assertEqual(live_state.repo_id(self.home, "origin"), "gurusharan/agent-plugins")
        self.assertEqual(bcl_entrypoint._provider("https://github.com/ingpoc/agent-plugins/pull/110"), "gh")
        self.assertEqual(bcl_entrypoint._provider("https://cursor.com/codebase/gurusharan/agent-plugins/pull/110"), "origin")

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
                codex.setdefault("review_status", "passed")
            plan_path.write_text(json.dumps(plan))
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
        marker = self.root / 'proof-ran'
        proof = self.root / 'proof.py'; proof.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('yes')\n")
        command = f'"{sys.executable}" "{proof}"'
        plan = {"round":1,"groups":[{"id":"A","finding_ids":["ISSUE-01"],"reason":"Same root cause","depends_on":[],"codex":{"model":"gpt-6-luna","effort":"medium","status":"running","started_at":"2026-11-14T09:00:00Z","duration_s":120,"implementer_session":"fixture-implementer","reviewer_session":"fixture-reviewer","review_status":"passed"},"verify":{"command":command,"method":"Unit tests and a live page check","status":"pending"},"status":"running"}]}
        planfile = self.root / "plan.json"; planfile.write_text(json.dumps(plan))
        sections = {"delete":{"none":"No deletions."},"simplify":{"none":"No simplifications."},"optimize":{"none":"No optimizations."},"automate":{"none":"No automations."}}
        steps = check_introspect.performed_round_steps({
            "stages_reached": ["plan_fix", "install", "verify"], "plan": plan,
        })
        sectionfile = self.root / "sections.json"; sectionfile.write_text(json.dumps({"steps": steps, "none": steps, "sections": sections}))
        self.assertNotEqual(self.call("introspect", "--result", "no issues").returncode, 0)
        for stage, extra in (("plan", ["--plan", str(planfile), "--ids", "ISSUE-01"]), ("fix", ["--ids", "ISSUE-01"]), ("install", ["--ids", "ISSUE-01"]), ("verify", ["--ids", "ISSUE-01", "--then", command]), ("introspect", ["--sections", str(sectionfile)])):
            result = self.call(stage, *extra); self.assertEqual(result.returncode, 0, result.stderr)
            if stage == "plan": self.assertIn('class="plan"', self.page.read_text())
        status = self.call("status")
        self.assertEqual(marker.read_text(), 'yes')
        self.assertEqual(status.returncode, 0, status.stderr)
        data = json.loads(self.data.read_text())
        self.assertEqual(data["tracking"]["stages_reached"], ["collect", "plan_fix", "install", "verify", "introspect"])
        self.assertEqual(data["tracking"]["plan"]["groups"][0]["id"], "A")
        self.assertEqual(data["tracking"]["plan"]["status"], "closed")
        self.assertEqual(json.loads(status.stdout)["plan"][0]["status"], "running")

    def test_verify_rejects_placeholder_and_mismatched_command(self):
        self.assertEqual(self.call('start').returncode, 0)
        plan = {'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'status':'pending','verify':{'command':'echo verify'}}]}
        path = self.root / 'plan.json'; path.write_text(json.dumps(plan))
        self.assertEqual(self.call('plan','--plan',str(path),'--ids','ISSUE-01').returncode, 0)
        self.assertEqual(self.call('install','--ids','ISSUE-01').returncode, 0)
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
        self.assertEqual(self.call('install','--ids','ISSUE-01').returncode, 0)
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
        self.assertEqual(self.call('install','--ids','ISSUE-01').returncode, 0)
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
        self.assertEqual(self.call('install','--ids','ISSUE-01').returncode, 0)
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
        self.assertEqual(self.call('install','--ids','ISSUE-01','--ids','ISSUE-02').returncode, 0)
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
        self.assertEqual(self.call('install','--ids','ISSUE-01').returncode, 0)
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
        self.assertEqual(self.call('install','--ids','ISSUE-01').returncode, 0)

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
        self.assertEqual(self.call('install','--ids','ISSUE-01').returncode, 0)
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
                   'evidence':'Prerequisite could not be confirmed'})
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

    def test_stage_renders_before_then_command(self):
        live = Path(json.loads((self.state / "registry.json").read_text())["example/project/123"]["live"])
        tracker = json.loads(live.read_text())
        tracker["findings"][0]["status"] = "in_progress"
        tracker["tracking"]["plan"] = {"cycle": 1, "groups": [{"id": "A", "finding_ids": ["ISSUE-01"], "status": "running", "codex": {"implementer_session": "fixture-implementer", "reviewer_session": "fixture-reviewer", "review_status": "passed"}}]}
        live.write_text(json.dumps(tracker)); self.data.write_text(json.dumps(tracker))
        stub = self.root / "check_stage.py"
        stub.write_text("import json, sys\nfrom pathlib import Path\ndata, page = map(Path, sys.argv[1:])\nhtml = page.read_text()\nassert json.loads(data.read_text())['tracking']['workflow']['stage'] == 'install'\nassert 'Install' in html\nassert 'class=\"workflow-now\"' not in html\n")
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
        doc["tracking"].update(stages_reached=["report_rollover"], rollover_threshold=1)
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
        doc["tracking"].update(stages_reached=["report_rollover"], rollover_threshold=1)
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
        records = json.loads((self.state / "registry.json").read_text())
        key, record = next(iter(records.items()))
        state = self.viewer_state_path()
        previous = state.read_bytes()
        process = SimpleNamespace(pid=99999, poll=lambda: 1)
        with patch.object(live_state, "STATE", self.state), \
             patch.object(live_state.os, "kill", side_effect=ProcessLookupError), \
             patch.object(live_state.subprocess, "Popen", return_value=process), \
             patch.object(live_state, "_viewer_ready", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "viewer failed to start"):
                live_state.ensure_viewer(key, record, Path(record["live"]), "new-version")
        self.assertEqual(state.read_bytes(), previous)

    def test_unresponsive_recorded_viewer_is_not_reported_as_healthy(self):
        records = json.loads((self.state / "registry.json").read_text())
        key, record = next(iter(records.items()))
        state = json.loads(self.viewer_state_path().read_text())
        with patch.object(live_state, "STATE", self.state), \
             patch.object(live_state.os, "kill"), \
             patch.object(live_state, "_viewer_responds", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "cannot verify viewer ownership"):
                live_state.ensure_viewer(key, record, Path(record["live"]), state["version"])

    def test_unverified_viewer_is_never_signalled(self):
        records = json.loads((self.state / "registry.json").read_text())
        key, record = next(iter(records.items()))
        state = json.loads(self.viewer_state_path().read_text())
        with patch.object(live_state, "STATE", self.state), \
             patch.object(live_state.os, "kill") as signal_process, \
             patch.object(live_state, "_viewer_responds", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "cannot verify viewer ownership"):
                live_state.ensure_viewer(key, record, Path(record["live"]), "new-version")
        signal_process.assert_called_once_with(state['pid'], 0)

    def test_legacy_viewer_is_not_stopped_without_identity(self):
        records = json.loads((self.state / "registry.json").read_text())
        key, record = next(iter(records.items()))
        path = self.viewer_state_path()
        state = json.loads(path.read_text())
        state.pop('instance')
        path.write_text(json.dumps(state))
        with patch.object(live_state, "STATE", self.state), \
             patch.object(live_state.os, "kill") as signal_process, \
             patch.object(live_state.urllib.request, "urlopen") as request:
            with self.assertRaisesRegex(RuntimeError, "cannot verify viewer ownership"):
                live_state.ensure_viewer(key, record, Path(record["live"]), "new-version")
        signal_process.assert_called_once_with(state['pid'], 0)
        request.assert_not_called()

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
