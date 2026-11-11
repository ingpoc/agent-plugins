#!/usr/bin/env python3
"""Deterministic recovery checks for the experimental rollover wrapper.

Runs the candidate CLI as a subprocess against throwaway trackers. No agent is
involved: every assertion is a filesystem or process-exit contract.
"""
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURE = json.loads((HERE / 'fixture.json').read_text())
SKILL = Path(os.environ.get(
    'BCL_ROLLOVER_SKILL',
    Path.home() / 'code/worktrees/.agents/bcl-rollover-experiment/skills/batched-change-list'))
SCRIPT = SKILL / 'scripts/rollover_experiment.py'
URL2 = 'https://example.com/pr/2'


def sha(path):
    path = Path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def tracker(path):
    return json.loads(Path(path).read_text())


def embedded_ids(path):
    text = Path(path).read_text()
    marker = '<script type="application/json" id="findings-data">'
    return [item['id'] for item in json.loads(text.split(marker, 1)[1].split('</script>', 1)[0])]


class Case:
    def __init__(self, name, report_dir=True):
        self.name = name
        self.root = Path(tempfile.mkdtemp(prefix=f'bcl-rollover-{name}-'))
        self.tracker = self.root / 'tracker' / 'findings.json'
        self.tracker.parent.mkdir()
        self.tracker.write_text(json.dumps(FIXTURE, indent=2) + '\n')
        if report_dir:
            (self.root / 'report').mkdir()

    def run(self, *extra, url=URL2, worktree='new-worktree', label='PR 2'):
        args = [sys.executable, str(SCRIPT), str(self.tracker), str(self.root / 'report' / 'pr-2.html'),
                '--label', label, '--url', url, '--worktree', worktree, *extra]
        done = subprocess.run(args, capture_output=True, text=True)
        return done.returncode, done.stdout, done.stderr

    def close(self):
        for folder in self.root.rglob('*'):
            if folder.is_dir():
                try:
                    folder.chmod(0o700)
                except OSError:
                    pass
        shutil.rmtree(self.root, ignore_errors=True)


def check(results, name, condition, detail):
    results.append({'check': name, 'pass': bool(condition), 'detail': detail})


def main():
    results = []
    cases = []

    # 1. Happy path.
    case = Case('happy'); cases.append(case)
    code, out, err = case.run()
    report = case.root / 'report' / 'pr-2.html'
    doc = tracker(case.tracker)
    payload = json.loads(out) if code == 0 and out.strip() else {}
    check(results, 'happy.exit', code == 0, {'exit': code, 'stderr': err[-400:]})
    check(results, 'happy.tracker', doc['tracking'].get('pull_request', {}).get('url') == URL2
          and doc['tracking'].get('worktree') == 'new-worktree'
          and doc['tracking'].get('rollover', {}).get('excluded_ids') == ['ISSUE-02'], doc['tracking'])
    check(results, 'happy.report', report.is_file() and embedded_ids(report) == ['ISSUE-01'],
          {'ids': embedded_ids(report) if report.is_file() else None})
    check(results, 'happy.recovered_flag', payload.get('recovered') is False, payload)
    happy_report_sha = sha(report)

    # 2. Report write denied after tracker commit.
    case = Case('denied'); cases.append(case)
    (case.root / 'report').chmod(0o500)
    code, out, err = case.run()
    report = case.root / 'report' / 'pr-2.html'
    after_failure = case.tracker.read_bytes()
    doc = tracker(case.tracker)
    check(results, 'denied.exit', code == 2, {'exit': code, 'stdout': out[-200:]})
    check(results, 'denied.clean_error', 'tracker updated' in err and 'rerun the same command' in err
          and 'Traceback' not in err, err[-500:])
    check(results, 'denied.tracker_committed', doc['tracking'].get('pull_request', {}).get('url') == URL2, doc['tracking'])
    check(results, 'denied.report_absent', not report.exists(), str(report))

    # 3. Retry after fixing the path completes without a manual rerender.
    (case.root / 'report').chmod(0o700)
    code, out, err = case.run()
    payload = json.loads(out) if code == 0 and out.strip() else {}
    check(results, 'retry.exit', code == 0, {'exit': code, 'stderr': err[-400:]})
    check(results, 'retry.no_double_mutation', case.tracker.read_bytes() == after_failure,
          {'before': sha(case.tracker), 'same_bytes': case.tracker.read_bytes() == after_failure})
    check(results, 'retry.report', report.is_file() and embedded_ids(report) == ['ISSUE-01'],
          {'ids': embedded_ids(report) if report.is_file() else None})
    check(results, 'retry.recovered_flag', payload.get('recovered') is True, payload)
    check(results, 'retry.same_report_bytes', report.is_file() and sha(report) == happy_report_sha,
          {'retry': sha(report) if report.is_file() else None, 'happy': happy_report_sha})

    # 4. Repeating a completed rollover is a no-op.
    before = (case.tracker.read_bytes(), sha(report))
    code, out, err = case.run()
    payload = json.loads(out) if code == 0 and out.strip() else {}
    check(results, 'repeat.exit', code == 0 and payload.get('recovered') is False, {'exit': code, 'out': out[-200:], 'stderr': err[-200:]})
    check(results, 'repeat.unchanged', (case.tracker.read_bytes(), sha(report)) == before, {})

    # 5. Same-PR target is rejected before any mutation.
    case = Case('same-pr'); cases.append(case)
    before = case.tracker.read_bytes()
    code, out, err = case.run(url='https://example.com/pr/1')
    check(results, 'same_pr.exit', code == 2 and 'must differ from current PR' in err, {'exit': code, 'stderr': err[-300:]})
    check(results, 'same_pr.unchanged', case.tracker.read_bytes() == before and not (case.root / 'report' / 'pr-2.html').exists(), {})

    # 6. Recovery refuses conflicting metadata.
    case = Case('metadata'); cases.append(case)
    (case.root / 'report').chmod(0o500)
    case.run()
    after_failure = case.tracker.read_bytes()
    (case.root / 'report').chmod(0o700)
    code, out, err = case.run(worktree='different-worktree')
    check(results, 'metadata.exit', code == 2 and 'different label or worktree' in err, {'exit': code, 'stderr': err[-300:]})
    check(results, 'metadata.unchanged', case.tracker.read_bytes() == after_failure and not (case.root / 'report' / 'pr-2.html').exists(), {})
    code, out, err = case.run()
    check(results, 'metadata.correct_retry', code == 0 and (case.root / 'report' / 'pr-2.html').is_file(), {'exit': code, 'stderr': err[-300:]})

    # 7. An existing report is never overwritten on the first attempt.
    case = Case('existing'); cases.append(case)
    (case.root / 'report' / 'pr-2.html').write_text('not a report')
    before = case.tracker.read_bytes()
    code, out, err = case.run()
    check(results, 'existing.exit', code == 2 and 'new path' in err, {'exit': code, 'stderr': err[-300:]})
    check(results, 'existing.unchanged', case.tracker.read_bytes() == before
          and (case.root / 'report' / 'pr-2.html').read_text() == 'not a report', {})

    # 8. A render failure leaves the tracker untouched.
    case = Case('render'); cases.append(case)
    doc = tracker(case.tracker)
    doc['findings'][0]['status'] = 'not-a-status'
    case.tracker.write_text(json.dumps(doc, indent=2) + '\n')
    before = case.tracker.read_bytes()
    code, out, err = case.run()
    check(results, 'render.exit', code == 2 and 'Traceback' not in err, {'exit': code, 'stderr': err[-300:]})
    check(results, 'render.unchanged', case.tracker.read_bytes() == before
          and not (case.root / 'report' / 'pr-2.html').exists(), {})

    # 9. A missing report directory recovers after it is created.
    case = Case('missing-dir', report_dir=False); cases.append(case)
    code, out, err = case.run()
    check(results, 'missing_dir.first', code == 2 and 'tracker updated' in err, {'exit': code, 'stderr': err[-300:]})
    (case.root / 'report').mkdir()
    code, out, err = case.run()
    payload = json.loads(out) if code == 0 and out.strip() else {}
    check(results, 'missing_dir.retry', code == 0 and payload.get('recovered') is True
          and embedded_ids(case.root / 'report' / 'pr-2.html') == ['ISSUE-01'], {'exit': code, 'out': out[-200:], 'stderr': err[-300:]})

    for case in cases:
        case.close()

    summary = {'skill': str(SKILL), 'script_sha256': sha(SCRIPT), 'fixture_sha256': sha(HERE / 'fixture.json'),
               'checks': results, 'passed': sum(x['pass'] for x in results),
               'total': len(results), 'ok': all(x['pass'] for x in results)}
    (HERE / 'results.json').write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n')
    print(json.dumps({k: summary[k] for k in ('passed', 'total', 'ok')}))
    for item in results:
        print(('PASS ' if item['pass'] else 'FAIL ') + item['check'])
    return 0 if summary['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
