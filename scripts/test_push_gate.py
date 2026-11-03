"""Pre-push rules (scripts/push_gate.py) and the hygiene checks that back them.

Fixture repos live under ~/.agents/tmp (the machine scratch root), never /tmp.
"""
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hygiene as hy  # noqa: E402
import placement  # noqa: E402
import push_gate as pg  # noqa: E402

Z = pg.ZERO


def git(*args, cwd):
    return subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout.strip()


def line(branch, remote_sha='1' * 40, local_sha='2' * 40, ref='refs/heads/'):
    return f'{ref}{branch} {local_sha} {ref}{branch} {remote_sha}\n'


class Base(unittest.TestCase):
    def setUp(self):
        scratch = Path.home() / '.agents' / 'tmp'
        scratch.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=scratch, prefix='test-push-gate-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.apps, self.wt = self.base / 'apps', self.base / 'worktrees'
        self.canonical = self.apps / 'agent-plugins'
        self.canonical.mkdir(parents=True)
        git('init', '-q', '-b', 'main', cwd=self.canonical)
        git('commit', '-q', '--allow-empty', '-m', 'init', cwd=self.canonical)
        self.policy = self.base / 'machine.toml'
        self.write_policy()
        env = patch.dict(os.environ, {'CI': '', 'AGENT_PLUGINS_MACHINE_TOML': str(self.policy),
                                      placement.OVERRIDE_ENV: '', pg.BRANCH_ENV: '', pg.MAIN_ENV: ''})
        env.start()
        self.addCleanup(env.stop)

    def write_policy(self, extra=''):
        self.policy.write_text(f'[paths]\ncheckout_root = "{self.apps}"\nworktree_root = "{self.wt}"\n{extra}')

    def worktree(self, path, branch):
        git('worktree', 'add', '-q', str(path), '-b', branch, cwd=self.canonical)
        return path

    def run_gate(self, root, stdin):
        err = io.StringIO()
        return pg.gate(root, stdin, err), err.getvalue()


class PushGateTests(Base):
    def test_main_push_only_from_canonical(self):
        self.assertEqual(pg.problems(self.canonical, pg.parse_updates(line('main'))), [])
        task = self.worktree(self.wt / 'agent-plugins' / 'task', 'grokbot/task')
        code, out = self.run_gate(task, line('main'))
        self.assertEqual(code, 1)
        self.assertIn('FAIL main-push', out)
        self.assertIn(pg.MAIN_ENV, out)
        with patch.dict(os.environ, {pg.MAIN_ENV: '1'}):
            code, out = self.run_gate(task, line('main'))
        self.assertEqual((code, 'WARN main-push' in out), (0, True))

    def test_branch_names(self):
        rules = lambda b: [p[0] for p in pg.problems(self.canonical, pg.parse_updates(line(b)))]  # noqa: E731
        self.assertEqual(rules('grokbot/x'), [])
        self.assertEqual(rules('codex/cf01-wave1.CJKpAF'), [])
        self.assertEqual(rules('foo/x'), ['branch-name'])
        self.assertEqual(rules('grokbot'), ['branch-name'])
        self.assertEqual(rules('grokbot/a/b'), ['branch-name'])
        self.write_policy('[push_gate]\nbranch_owners = ["zed"]\n')
        self.assertEqual(rules('zed/x'), [])
        self.assertEqual(rules('grokbot/x'), ['branch-name'])
        with patch.dict(os.environ, {pg.BRANCH_ENV: '1'}):
            code, out = self.run_gate(self.canonical, line('grokbot/x'))
        self.assertEqual((code, 'WARN branch-name' in out), (0, True))

    def test_deletions_and_tags_ignored(self):
        stdin = line('foo', local_sha=Z) + line('v1', ref='refs/tags/') + line('main', local_sha=Z)
        task = self.worktree(self.wt / 'agent-plugins' / 'task', 'grokbot/task')
        self.assertEqual(pg.problems(task, pg.parse_updates(stdin)), [])

    def test_placement_via_gate_and_override(self):
        stray = self.worktree(self.base / 'elsewhere' / 'agent-plugins-old', 'grokbot/old')
        code, out = self.run_gate(stray, line('grokbot/old'))
        self.assertEqual(code, 1)
        self.assertIn('FAIL placement', out)
        with patch.dict(os.environ, {placement.OVERRIDE_ENV: '1'}):
            self.assertEqual(self.run_gate(stray, line('grokbot/old'))[0], 0)

    def test_new_branch_reminder_does_not_block(self):
        code, out = self.run_gate(self.canonical, line('grokbot/new', remote_sha=Z))
        self.assertEqual(code, 0)
        self.assertIn('pre-push note: new branch grokbot/new', out)
        self.assertEqual(self.run_gate(self.canonical, line('grokbot/new')), (0, ''))

    def test_ci_and_missing_policy_skip(self):
        with patch.dict(os.environ, {'CI': '1'}):
            self.assertEqual(self.run_gate(self.canonical, line('foo', remote_sha=Z)), (0, ''))
        with patch.dict(os.environ, {'AGENT_PLUGINS_MACHINE_TOML': str(self.base / 'none.toml')}):
            self.assertEqual(self.run_gate(self.canonical, line('foo')), (0, ''))


class HygieneRuleTests(Base):
    def manifest(self):
        return {'paths': {'checkout_root': str(self.apps), 'worktree_root': str(self.wt)},
                'scan': {'roots': [str(self.apps), str(self.wt)], 'depth': 4}, 'exceptions': {}}

    def rows(self, fn, *args, **kw):
        r = hy.Report()
        fn(self.manifest(), r, *args, **kw)
        return r.rows

    def test_hooks_path(self):
        git('remote', 'add', 'origin-cursor', 'https://origin.cursor.com/x/agent-plugins.git', cwd=self.canonical)
        task = self.worktree(self.wt / 'agent-plugins' / 'task', 'grokbot/task')
        other = self.apps / 'other-repo'
        other.mkdir()
        git('init', '-q', cwd=other)
        git('remote', 'add', 'origin', 'https://example.com/x/other-repo.git', cwd=other)
        [row] = self.rows(hy.check_hooks_path)
        self.assertEqual(row['status'], 'FAIL')
        self.assertEqual(len(row['items']), 2)
        self.assertTrue(any(str(task) in i for i in row['items']))
        self.assertFalse(any('other-repo' in i for i in row['items']))
        git('config', 'core.hooksPath', '.githooks', cwd=self.canonical)
        [row] = self.rows(hy.check_hooks_path)
        self.assertEqual(row['status'], 'PASS')

    def test_branch_without_pr(self):
        git('remote', 'add', 'origin-cursor', 'https://origin.cursor.com/x/agent-plugins.git', cwd=self.canonical)
        sha = git('rev-parse', 'HEAD', cwd=self.canonical)
        ts = int(git('log', '-1', '--format=%ct', cwd=self.canonical))
        for ref in ('grokbot/old', 'main'):
            git('update-ref', f'refs/remotes/origin-cursor/{ref}', sha, cwd=self.canonical)
        later = ts + 2 * 86400
        with patch.object(hy, 'open_pr_heads', return_value=set()) as heads:
            [row] = self.rows(hy.check_unpr_branches, now=later)
        heads.assert_called_once_with('x/agent-plugins')
        self.assertEqual(row['status'], 'WARN')
        self.assertEqual(len(row['items']), 1)
        self.assertIn('grokbot/old', row['items'][0])
        with patch.object(hy, 'open_pr_heads', return_value={'grokbot/old'}):
            self.assertEqual(self.rows(hy.check_unpr_branches, now=later)[0]['status'], 'PASS')
        with patch.object(hy, 'open_pr_heads', return_value=None):
            self.assertEqual(self.rows(hy.check_unpr_branches, now=later)[0]['status'], 'UNKNOWN')
        with patch.object(hy, 'open_pr_heads') as heads:
            self.assertEqual(self.rows(hy.check_unpr_branches, now=ts + 3600)[0]['status'], 'PASS')
        heads.assert_not_called()


if __name__ == '__main__':
    unittest.main()
