"""Pre-push placement rule: canonical checkout or a worktree under worktree_root."""
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import placement


def git(*args, cwd):
    subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True)


class PlacementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.apps, self.wt, self.t3 = base/'apps', base/'worktrees', base/'t3'
        self.canonical = self.apps/'agent-plugins'
        self.canonical.mkdir(parents=True)
        git('init', '-q', '-b', 'main', cwd=self.canonical)
        git('-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-q', '--allow-empty', '-m', 'init', cwd=self.canonical)
        self.policy = base/'machine.toml'
        self.policy.write_text(f'[paths]\ncheckout_root = "{self.apps}"\nworktree_root = "{self.wt}"\n'
                               f'[exceptions]\nworktree_roots = ["{self.t3}"]\n')
        env = patch.dict(os.environ, {'CI': '', 'AGENT_PLUGINS_MACHINE_TOML': str(self.policy),
                                      placement.OVERRIDE_ENV: ''})
        env.start()
        self.addCleanup(env.stop)

    def worktree(self, path, branch):
        git('worktree', 'add', '-q', str(path), '-b', branch, cwd=self.canonical)
        return path

    def test_canonical_and_worktree_root_and_harness_root_pass(self):
        self.assertEqual(placement.problems(self.canonical), [])
        self.assertEqual(placement.problems(self.worktree(self.wt/'agent-plugins'/'task', 'a/task')), [])
        self.assertEqual(placement.problems(self.worktree(self.t3/'agent-plugins'/'x', 't3/x')), [])

    def test_worktree_outside_root_is_blocked(self):
        stray = self.worktree(Path(self.tmp.name).resolve()/'wt'/'agent-plugins-old', 'a/old')
        found = placement.problems(stray)
        self.assertEqual(len(found), 1)
        self.assertIn('outside', found[0])
        self.assertEqual(placement.gate(stray, io.StringIO()), 1)

    def test_second_clone_is_blocked_and_override_warns(self):
        clone = Path(self.tmp.name).resolve()/'code'/'agent-plugins'
        git('clone', '-q', str(self.canonical), str(clone), cwd=self.tmp.name)
        self.assertIn('second clone', placement.problems(clone)[0])
        with patch.dict(os.environ, {placement.OVERRIDE_ENV: '1'}):
            err = io.StringIO()
            self.assertEqual(placement.gate(clone, err), 0)
            self.assertIn('WARN', err.getvalue())

    def test_ci_or_missing_policy_skips(self):
        clone = Path(self.tmp.name).resolve()/'other'
        git('clone', '-q', str(self.canonical), str(clone), cwd=self.tmp.name)
        with patch.dict(os.environ, {'CI': 'true'}):
            self.assertEqual(placement.problems(clone), [])
        with patch.dict(os.environ, {'AGENT_PLUGINS_MACHINE_TOML': str(self.policy) + '.missing'}):
            self.assertEqual(placement.problems(clone), [])


if __name__ == '__main__':
    unittest.main()
