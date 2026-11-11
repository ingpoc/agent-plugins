#!/usr/bin/env python3
"""Two-run OpenCode adoption probe for the fixed rollover helper.

Not a matched efficiency comparison: it answers whether an agent on the current
provider uses the helper and leaves a correct tracker/report pair.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = Path(os.environ.get(
    'BCL_ROLLOVER_SKILL',
    Path.home() / 'code/worktrees/.agents/bcl-rollover-experiment/skills/batched-change-list'))
ROOT = Path(os.environ.get('BCL_ROLLOVER_PROBE_ROOT', '/tmp/bcl-rollover-oc'))
MODEL = os.environ.get('BCL_MODEL', 'opencode/deepseek-v4.1-flash-free#low')
URL2 = 'https://example.com/pr/2'
HELPER = re.compile(r'rollover_experiment\.py')


def sha(path):
    path = Path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def embedded_ids(path):
    text = Path(path).read_text()
    marker = '<script type="application/json" id="findings-data">'
    return [item['id'] for item in json.loads(text.split(marker, 1)[1].split('</script>', 1)[0])]


def run(rep):
    folder = ROOT / f'rep-{rep}'
    folder.mkdir(parents=True, exist_ok=False)
    fixture = json.loads((HERE / 'fixture.json').read_text())
    (folder / 'findings.json').write_text(json.dumps(fixture, indent=2) + '\n')
    prompt = (
        f'Use only the batched-change-list skill at {SKILL}; read SKILL.md and '
        'references/collection-and-reporting.md. For the disposable tracker '
        f'{folder}/findings.json, perform the local rollover to PR 2: label "PR 2", '
        f'url "{URL2}", worktree "new-worktree", and render the report to {folder}/pr-2.html. '
        'Preserve all finding records and history; do not create subagents or change skill files.'
    )
    (folder / 'prompt.txt').write_text(prompt)
    started = time.monotonic()
    done = subprocess.run(
        ['opencode', 'run', '--model', MODEL, '--format', 'json', '--auto',
         '--title', f'bcl-rollover-{rep}-{uuid.uuid4()}', prompt],
        cwd=folder, capture_output=True, text=True, timeout=300)
    elapsed = round(time.monotonic() - started, 3)
    (folder / 'events.jsonl').write_text(done.stdout)
    (folder / 'stderr').write_text(done.stderr)
    events = []
    for line in done.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            pass
    commands = []
    tokens = {'input_uncached': 0, 'cache_read': 0, 'output': 0, 'reasoning': 0, 'steps': 0}
    for event in events:
        part = event.get('part') or {}
        if part.get('type') == 'tool' and part.get('tool') == 'shell':
            commands.append((part.get('state') or {}).get('input', {}).get('command', ''))
        if event.get('type') == 'step_finish':
            tokens['steps'] += 1
            values = part.get('tokens') or {}
            for key in ('input', 'output', 'reasoning'):
                tokens[key if key != 'input' else 'input_uncached'] += values.get(key, 0) or 0
            cache = values.get('cache') or {}
            tokens['cache_read'] += cache.get('read', 0) or 0
    result = {
        'rep': rep,
        'model': MODEL,
        'exit': done.returncode,
        'seconds': elapsed,
        'shell_calls': len(commands),
        'helper_used': any(HELPER.search(c or '') for c in commands),
        'helper_commands': [c for c in commands if HELPER.search(c or '')],
        'tokens': tokens,
        'tracker_sha256': sha(folder / 'findings.json'),
        'report_sha256': sha(folder / 'pr-2.html'),
    }
    try:
        tracker = json.loads((folder / 'findings.json').read_text())
        tracking = tracker.get('tracking', {})
        result['tracker_ok'] = (
            tracking.get('pull_request', {}).get('label') == 'PR 2'
            and tracking.get('pull_request', {}).get('url') == URL2
            and tracking.get('worktree') == 'new-worktree'
            and tracking.get('rollover', {}).get('excluded_ids') == ['ISSUE-02']
            and [item['id'] for item in tracker['findings']] == ['ISSUE-01', 'ISSUE-02'])
        result['report_ok'] = (folder / 'pr-2.html').is_file() and embedded_ids(folder / 'pr-2.html') == ['ISSUE-01']
    except Exception as error:
        result['tracker_ok'] = False
        result['report_ok'] = False
        result['error'] = f'{type(error).__name__}: {error}'
    result['accepted'] = bool(done.returncode == 0 and result['helper_used']
                              and result['tracker_ok'] and result['report_ok'])
    (folder / 'manifest.json').write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
    print(json.dumps({k: result[k] for k in ('rep', 'exit', 'accepted', 'helper_used', 'shell_calls',
                                             'seconds', 'tracker_ok', 'report_ok', 'tokens')}), flush=True)
    return result


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    rows = [run(rep) for rep in (1, 2)]
    summary = {'skill': str(SKILL), 'fixture_sha256': sha(HERE / 'fixture.json'),
               'probe_sha256': sha(__file__), 'rows': rows,
               'accepted': sum(row['accepted'] for row in rows), 'total': len(rows)}
    (HERE / 'probe-results.json').write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n')
    print(json.dumps({'accepted': summary['accepted'], 'total': summary['total']}))
    return 0 if summary['accepted'] == summary['total'] else 1


if __name__ == '__main__':
    sys.exit(main())
