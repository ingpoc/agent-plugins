#!/usr/bin/env python3
import json
import unittest
from unittest import mock
import cleanup_merged_pr as cleanup

HEAD = 'a' * 40
MAIN = 'b' * 40


def outputs(*args):
    if args[:3] == ('origin', 'pr', 'view'):
        return json.dumps({'status': 'merged', 'number': 7, 'headRef': 'done', 'headSha': HEAD, 'mergeCommitSha': MAIN})
    if args[:3] == ('origin', 'pr', 'list'):
        return '[]'
    if args[:3] == ('git', 'check-ref-format', '--branch'):
        return 'done'
    if args[:2] == ('git', 'rev-parse'):
        return MAIN
    if args[:3] == ('git', 'ls-remote', '--heads'):
        return MAIN + '\trefs/heads/main\n' + HEAD + '\trefs/heads/done'
    if args[:3] == ('git', 'merge-base', '--is-ancestor'):
        return ''
    if args[:2] == ('git', 'for-each-ref'):
        return 'refs/heads/done ' + HEAD
    if args[:3] == ('git', 'worktree', 'list'):
        return 'worktree /repo\nbranch refs/heads/main'
    raise AssertionError(args)


class CleanupTest(unittest.TestCase):
    def test_review_exact_branch(self):
        with mock.patch.object(cleanup, 'run', side_effect=outputs):
            result = cleanup.inspect(7)
        self.assertTrue(result['local_branch'])
        self.assertTrue(result['origin_branch'])
        self.assertFalse(result['worktrees'])

    def test_branch_used_as_active_pr_base_is_blocked(self):
        def run(*args):
            if args[:3] == ('origin', 'pr', 'list'):
                return json.dumps([{'status': 'draft', 'headRef': 'next', 'baseRef': 'done'}])
            return outputs(*args)
        with mock.patch.object(cleanup, 'run', side_effect=run):
            with self.assertRaisesRegex(ValueError, 'head or base'):
                cleanup.inspect(7)

    def test_moved_origin_branch_is_blocked(self):
        def run(*args):
            if args[:4] == ('git', 'ls-remote', '--heads', 'origin'):
                return MAIN + '\trefs/heads/main\n' + 'c' * 40 + '\trefs/heads/done'
            return outputs(*args)
        with mock.patch.object(cleanup, 'run', side_effect=run):
            with self.assertRaisesRegex(ValueError, 'moved'):
                cleanup.inspect(7)


if __name__ == '__main__':
    unittest.main()
