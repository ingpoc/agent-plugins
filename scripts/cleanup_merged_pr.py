#!/usr/bin/env python3
"""Review or retire a merged Origin PR's branches after its worktree is removed."""
from __future__ import annotations
import argparse
import json
import subprocess
import sys


def run(*args: str) -> str:
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=15).stdout.strip()


def inspect(pr: int) -> dict:
    change = json.loads(run('origin', 'pr', 'view', str(pr), '--json',
                            'status,number,headRef,headSha,mergeCommitSha'))
    if change.get('status') != 'merged' or change.get('number') != pr:
        raise ValueError('PR is not confirmed merged')
    branch, head, merge = (change.get(k) for k in ('headRef', 'headSha', 'mergeCommitSha'))
    if not isinstance(branch, str) or not branch or branch in ('main', 'master') or any(
        not isinstance(v, str) or len(v) != 40 or any(c not in '0123456789abcdef' for c in v)
        for v in (head, merge)
    ):
        raise ValueError('PR identity is incomplete')
    run('git', 'check-ref-format', '--branch', branch)
    active = json.loads(run('origin', 'pr', 'list', '--state', 'all', '--limit', '200',
                            '--json', 'status,headRef,baseRef'))
    if not isinstance(active, list) or len(active) >= 200 or any(
        not isinstance(p, dict) or p.get('status') not in ('open', 'draft', 'closed', 'merged')
        or not isinstance(p.get('headRef'), str) or not isinstance(p.get('baseRef'), str) for p in active
    ):
        raise ValueError('active PR inventory is incomplete')
    if any(branch in (p['headRef'], p['baseRef']) and p['status'] in ('open', 'draft') for p in active):
        raise ValueError('branch is used as head or base by an open or draft PR')
    local_main = run('git', 'rev-parse', 'refs/remotes/origin/main')
    lines = run('git', 'ls-remote', '--heads', 'origin', 'main', branch).splitlines()
    refs = dict((line.split('\t', 1)[1].removeprefix('refs/heads/'), line.split('\t', 1)[0])
                for line in lines if '\t' in line)
    if refs.get('main') != local_main:
        raise ValueError('Origin main does not match cached main; fetch and retry')
    run('git', 'merge-base', '--is-ancestor', merge, local_main)
    remote = refs.get(branch)
    if remote is not None and remote != head:
        raise ValueError('Origin branch moved since the PR was merged')
    local_refs = run('git', 'for-each-ref', '--format=%(refname) %(objectname)', f'refs/heads/{branch}')
    local = next((line.split(' ', 1)[1] for line in local_refs.splitlines()
                  if line.startswith(f'refs/heads/{branch} ')), '')
    if local and local != head:
        raise ValueError('local branch moved since the PR was merged')
    trees = run('git', 'worktree', 'list', '--porcelain').split('\n\n')
    checked = [entry.splitlines()[0].removeprefix('worktree ') for entry in trees
               if f'branch refs/heads/{branch}' in entry.splitlines()]
    mirror_lines = run('git', 'ls-remote', '--heads', 'github', 'main', branch).splitlines()
    mirror = dict((line.split('\t', 1)[1].removeprefix('refs/heads/'), line.split('\t', 1)[0])
                  for line in mirror_lines if '\t' in line)
    return {'pr': pr, 'branch': branch, 'head': head, 'worktrees': checked,
            'local_branch': bool(local), 'origin_branch': remote is not None,
            'mirror_branch': mirror.get(branch), 'mirror_main_matches': mirror.get('main') == local_main}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pr', type=int)
    parser.add_argument('--apply', action='store_true', help='delete eligible branches after rechecking')
    args = parser.parse_args()
    try:
        result = inspect(args.pr)
        if args.apply:
            if result['worktrees']:
                raise ValueError('worktree exists; inspect and remove it separately with safe-delete')
            result = inspect(args.pr)
            if result['worktrees']:
                raise ValueError('worktree appeared during recheck; removal refused')
            if result['local_branch']:
                run('git', 'branch', '-d', result['branch'])
            if result['origin_branch']:
                ref = 'refs/heads/' + result['branch']
                run('git', 'push', '--force-with-lease=' + ref + ':' + result['head'], 'origin', ':' + ref)
            result = inspect(args.pr)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        print('cleanup refused: ' + str(error).split('\n')[0], file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
