"""Portable collector: adapters, analysis, privacy, atomic output, merge with a primary producer, clean sandbox."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime'))
from backend.observability_api import ObservabilityAPI  # noqa: E402
from collector.analyze import build, percentile  # noqa: E402
from collector.cli import collect, parse_source  # noqa: E402
from collector.scrub import scrub  # noqa: E402
from collector.sources import discover_project, iter_checklog, iter_git, iter_hygiene, iter_jsonl, iter_junit  # noqa: E402

FIXTURE = ROOT / 'tests/fixtures/sample-project'
NOW = 1793700000.0 + 1500
SECRETS = ('sk-live-abcdefghijklmnop', 'ghp_' + 'abcdefghijklmnopqrstuv', 'secret user words', 'hello private',
           'ops@example.com', 'eyJhbGciOi.eyJzdWIiOi.c2lnbmF0dXJl')


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='devobs-collector-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.project = self.tmp / 'sample-project'
        shutil.copytree(FIXTURE, self.project)
        with (self.project / 'logs/app.jsonl').open('a') as handle:
            for row in ({'kind': 'realtime_error', 'status': 'error', 'detail': 'auth failed token=' + SECRETS[0], 'ts': NOW - 10},
                        {'kind': 'assistant_reply', 'text': SECRETS[2], 'detail': SECRETS[2], 'ts': NOW - 9},
                        {'kind': 'user_transcript', 'detail': SECRETS[3], 'ts': NOW - 8},
                        {'kind': 'usage', 'token': SECRETS[1], 'detail': f'mail {SECRETS[4]} jwt {SECRETS[5]} Bearer abcdef123',
                         'ts': NOW - 7}):
                handle.write(json.dumps(row) + '\n')

    def findings(self):
        stats = {}
        return build(discover_project(self.project, stats), stats, NOW)

    def test_scoped_findings_do_not_mix_versions(self):
        path = self.tmp / 'versions.jsonl'
        rows = []
        for version, count in [('1.0', 3), ('2.0', 2)]:
            for index in range(count):
                rows.append({'kind': 'realtime_error', 'status': 'error', 'detail': 'same error',
                             'version': version, 'session_id': f'{version}-{index}', 'ts': NOW - index})
            for index in range(5):
                rows.append({'kind': 'startup', 'phase': 'connect', 'status': 'ok',
                             'step_ms': 8000 if version == '1.0' else 2000,
                             'version': version, 'session_id': f'{version}-latency-{index}', 'ts': NOW - index})
        rows.append({'kind': 'realtime_error', 'status': 'error', 'detail': 'unattributed error', 'ts': NOW})
        rows.extend([{'kind': 'startup', 'session_id': 'inferred', 'version': '2.0', 'ts': NOW},
                     {'kind': 'realtime_error', 'status': 'error', 'detail': 'inferred error',
                      'session_id': 'inferred', 'ts': NOW + 1},
                     {'kind': 'response_done', 'session_id': 'shared', 'version': '1.0', 'ts': NOW},
                     {'kind': 'response_done', 'session_id': 'shared', 'version': '2.0', 'ts': NOW + 1}])
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        stats = {}
        document = build(iter_jsonl(path, 'p', 'p:log', stats), stats, NOW)
        scopes = {(scope['version'], scope['attribution']): scope for scope in document['version_scopes']}
        first, second = scopes[('1.0', 'event')], scopes[('2.0', 'event')]
        first_failure = next(f for f in first['findings'] if f['kind'] == 'failure')
        second_failure = next(f for f in second['findings'] if f['kind'] == 'failure')
        self.assertEqual(first_failure['failed_attempt_count'], 3)
        self.assertEqual(second_failure['failed_attempt_count'], 2)
        self.assertNotEqual(first_failure['finding_key'], second_failure['finding_key'])
        self.assertEqual(next(f['latency']['p95_ms'] for f in first['findings'] if f['kind'] == 'latency_hotspot'), 8000)
        self.assertEqual(next(f['latency']['p95_ms'] for f in second['findings'] if f['kind'] == 'latency_hotspot'), 2000)
        self.assertIn((None, 'unknown'), scopes)
        self.assertIn(('2.0', 'session_inferred'), scopes)
        self.assertTrue(any(f['kind'] == 'failure' for f in scopes[('2.0', 'session_inferred')]['findings']))
        self.assertNotEqual(first['session_timelines'][0]['session_ref'], second['session_timelines'][0]['session_ref'])
        runtime, collected = self.tmp/'primary', self.tmp/'output'
        runtime.mkdir(); collected.mkdir()
        (runtime/'findings.json').write_text(json.dumps({'findings': [{'finding_key': 'legacy', 'owner_ref': 'owner:p'}]}))
        (collected/'findings.json').write_text(json.dumps(document))
        api = ObservabilityAPI(runtime, {'p': {'owners': ['owner:p']}}, None, collected=collected)
        catalog = api.version_scopes('p')['version_scopes']
        self.assertIn({'version': '2.0', 'attribution': 'event'}, catalog)
        self.assertIn({'version': None, 'attribution': 'unknown'}, catalog)
        listing = api.issues('p', version='2.0', attribution='event')
        failures = [row for row in listing['issues'] if row['kind'] == 'failure']
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]['failed_attempts'], 2)
        self.assertEqual(listing['slowest'][0]['latency']['p95_ms'], 2000)
        self.assertNotIn('legacy', [row['issue_id'] for row in listing['issues']])
        self.assertEqual(listing['version_scope']['legacy_primary'], 'excluded')
        brief = api.diagnose('p', version='2.0', attribution='event')
        issue_id = brief['brief']['top_failures'][0]['issue_id']
        result = api.diagnose('p', [issue_id], brief['snapshot_id'], version='2.0', attribution='event')
        self.assertEqual(result['issues'][0]['issue']['failed_attempts'], 2)
        sessions = api.sessions('p', version='2.0', attribution='event')['sessions']
        self.assertTrue(sessions)
        with self.assertRaisesRegex(ValueError, 'session_not_in_project'):
            api.sessions('p', first['session_timelines'][0]['session_ref'], version='2.0', attribution='event')
        with self.assertRaisesRegex(ValueError, 'snapshot_changed'):
            api.diagnose('p', [issue_id], brief['snapshot_id'], version='1.0', attribution='event')
        page = api.issues('p', limit=1, version='1.0', attribution='event')
        self.assertIsNotNone(page['next_cursor'])
        with self.assertRaisesRegex(ValueError, 'invalid_cursor|snapshot_changed'):
            api.issues('p', cursor=page['next_cursor'], limit=1, version='2.0', attribution='event')
        with self.assertRaisesRegex(ValueError, 'invalid_version_scope'):
            api.issues('p', version='2.0', attribution='unknown')
        self.assertFalse(api.issues('p', version='missing', attribution='event')['version_scope']['scope_present'])

    def test_scrub_removes_tokens_email_and_home(self):
        text = scrub(f'{SECRETS[0]} {SECRETS[1]} {SECRETS[4]} {SECRETS[5]} Bearer abc123def {Path.home()}/x')
        for secret in (SECRETS[0], SECRETS[1], SECRETS[4], SECRETS[5], 'abc123def', str(Path.home())):
            self.assertNotIn(secret, text)

    def test_jsonl_ignores_transcripts_denied_keys_and_elapsed(self):
        stats = {}
        events = list(iter_jsonl(self.project / 'logs/app.jsonl', 'p', 'p:app', stats))
        blob = json.dumps(events)
        for secret in SECRETS:
            self.assertNotIn(secret, blob)
        self.assertEqual(stats['p:app']['skipped'], 1)
        startup = [e for e in events if e['kind'] == 'startup']
        self.assertEqual([e['duration_ms'] for e in startup][:2], [1000, 1250])

    def test_jsonl_attribution_does_not_inherit_ambiguous_sessions_or_versions(self):
        path = self.tmp / 'interleaved.jsonl'
        rows = [
            {'kind':'startup', 'session_id':'a', 'version':'0.2.92'},
            {'kind':'startup', 'session_id':'b', 'version':'0.2.94'},
            {'kind':'tool_error', 'error':'failure'},
            {'kind':'tool_error', 'session_id':'a', 'error':'failure'},
            {'kind':'startup', 'session_id':'a', 'version':'0.2.94'},
            {'kind':'tool_error', 'session_id':'b', 'error':'failure'},
            {'kind':'tool_error', 'session_id':'a', 'version':'0.2.93', 'error':'failure'},
        ]
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        events = list(iter_jsonl(path, 'p', 'p:app', {}))
        self.assertEqual((events[2]['session'], events[2]['version']), (None, None))
        self.assertEqual((events[3]['session'], events[3]['version']), ('a', None))
        self.assertEqual((events[5]['version'], events[5]['version_attribution']), ('0.2.94', 'session_inferred'))
        self.assertEqual((events[6]['version'], events[6]['version_attribution']), ('0.2.93', 'event'))
        self.assertEqual(events[2]['session_attribution'], 'unknown')

    def test_percentile_nearest_rank(self):
        values = list(range(1, 21))
        self.assertEqual(percentile(values, 0.5), 10)
        self.assertEqual(percentile(values, 0.95), 19)
        self.assertEqual(percentile([7], 0.95), 7)

    def test_failures_latency_and_redundancy_on_fixture(self):
        doc = self.findings()
        kinds = {f['kind'] for f in doc['findings']}
        self.assertEqual(kinds, {'failure', 'latency_hotspot', 'redundancy'})
        heads = [f['headline'] for f in doc['findings']]
        self.assertTrue(any('tool_error/cua_act failed 5x' in h for h in heads), heads)
        hot = [f for f in doc['findings'] if f['kind'] == 'latency_hotspot']
        self.assertTrue(all(f['latency']['n'] >= 5 and f['latency']['p95_ms'] >= 1000 for f in hot))
        self.assertIn('ws_connect step slow', ' '.join(f['headline'] for f in hot))
        self.assertFalse(any('comet_act' in f['headline'] for f in hot), 'tool_result p95 < 1 s is not a hotspot')
        patterns = {f['redundancy']['pattern'] for f in doc['findings'] if f['kind'] == 'redundancy'}
        self.assertEqual(patterns, {'burst', 'retry', 'duplicate_call'})
        for finding in doc['findings']:
            self.assertTrue(finding['finding_key'].startswith('col-'))
            self.assertEqual(finding['owner_ref'], 'owner:sample-project')
            self.assertTrue(finding['evidence_event_ids'] and ':' in finding['evidence_event_ids'][0])
        blob = json.dumps(doc)
        for secret in SECRETS:
            self.assertNotIn(secret, blob)

    def test_junit_and_checklog(self):
        stats = {}
        junit = list(iter_junit(self.project / 'test-results/junit.xml', 'p', 'p:junit', stats))
        self.assertEqual(sorted(e['step'] for e in junit if e['error']), ['app.Net.test_timeout', 'app.Parser.test_bad_input'])
        check = list(iter_checklog(self.project / 'ci/run1', 'p', 'p:ci', stats))
        failed = {(e['kind'], e['step']) for e in check if e['error']}
        self.assertIn(('check_failed', 'scripts/test_b.py'), failed)
        self.assertIn(('test', 'AppTests.GateTests.testColdStart'), failed)
        self.assertEqual(sum(1 for e in check if e['kind'] == 'check_test'), 2)

    def test_failed_ci_summary_explains_missing_output(self):
        log = self.project / 'ci/run1/check.log'
        log.write_text('test tests/test_example.py (1.0s, exit 1)\nAssertionError: known failure token=' + SECRETS[0] + '\n')
        stats = {}
        events = list(iter_checklog(self.project / 'ci/run1', 'p', 'p:ci', stats))
        doc = build(events, stats, NOW)
        finding = next(f for f in doc['findings'] if f['latest_evidence']['error_type'] == 'check_test')
        self.assertEqual(finding['latest_evidence']['tool'], 'tests/test_example.py')
        self.assertIn('not retained', finding['what_happened'])
        self.assertIn('check log', finding['fix'])
        self.assertNotIn(SECRETS[0], json.dumps(doc))

    def test_git_state_and_hygiene(self):
        repo = self.tmp / 'repo'
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        (repo / 'dirty.txt').write_text('x')
        events = list(iter_git(repo, 'r', 'r:git', {}))
        self.assertEqual([e['step'] for e in events], ['dirty_worktree'])
        report = self.tmp / 'hygiene.json'
        report.write_text(json.dumps({'checks': [{'name': 'local-only-work', 'status': 'FAIL', 'detail': 'x'},
                                                 {'name': 'disk-floor', 'status': 'PASS'}]}))
        self.assertEqual([e['step'] for e in iter_hygiene(report, 'r', 'r:h', {})], ['local-only-work'])

    def test_cli_writes_only_out_atomically_0600(self):
        out = self.tmp / 'out'
        summary = collect([{'type': 'project_dir', 'path': str(self.project)}], out, NOW)
        path = out / 'findings.json'
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in out.iterdir()), ['findings.json'])
        self.assertGreater(summary['findings'], 0)
        self.assertGreater(summary['by_kind']['redundancy'], 0)
        with self.assertRaises(ValueError):
            parse_source('jsonl')
        self.assertEqual(parse_source('jsonl:/a:b.jsonl:proj'), {'type': 'jsonl', 'path': '/a:b.jsonl', 'project': 'proj'})

    def test_merge_with_primary_and_prefix_attribution(self):
        runtime, out = self.tmp / 'runtime', self.tmp / 'collected'
        runtime.mkdir()
        primary = {'generated_at': '2026-01-01T00:00:00Z', 'coverage': {'codex': 'partial'},
                   'findings': [{'finding_key': 'p1', 'owner_ref': 'owner:x', 'latest_evidence': {'project': 'a-wt1'}}]}
        (runtime / 'findings.json').write_text(json.dumps(primary))
        api = ObservabilityAPI(runtime, {'a': {'labels': []}, 'sample-project': {'owners': ['owner:sample-project']}},
                               None, collected=out)
        only_primary = api.projects()
        self.assertEqual({p['project_id']: p['issue_count'] for p in only_primary['projects']}['a'], 1)
        self.assertEqual(only_primary['unassigned_issue_count'], 0)
        collect([{'type': 'project_dir', 'path': str(self.project)}], out, NOW)
        merged = api.projects()
        counts = {p['project_id']: p['issue_count'] for p in merged['projects']}
        self.assertGreater(counts['sample-project'], 0)
        self.assertNotEqual(merged['snapshot_id'], only_primary['snapshot_id'])
        self.assertEqual(merged['optional_services']['collector'], 'present')
        self.assertIn('collected', merged['coverage'].values())
        issues = api.issues('sample-project', limit=25)
        keys = [i['issue_id'] for i in issues['issues']]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertTrue(issues['slowest'] and 'p95_ms' in issues['slowest'][0]['latency'])
        only_hot = api.issues('sample-project', kind='latency_hotspot')
        self.assertTrue(only_hot['issues'] and all(i['kind'] == 'latency_hotspot' for i in only_hot['issues']))
        with self.assertRaises(ValueError):
            api.issues('sample-project', kind='bogus')
        (runtime / 'findings.json').rename(self.tmp / 'gone.json')
        self.assertGreater(api.issues('sample-project')['total'], 0)

    def test_clean_sandbox_install_then_collect_is_non_empty(self):
        home = self.tmp / 'home'
        home.mkdir()
        cfg = home / '.config/developer-observability/config.json'
        env = {**os.environ, 'HOME': str(home)}
        env.pop('OBSERVABILITY_CONFIG', None)
        install = subprocess.run([sys.executable, str(ROOT / 'runtime/install.py'), '--skip-deps', '--config', str(cfg)],
                                 env=env, capture_output=True, text=True, check=False)
        self.assertEqual(install.returncode, 0, install.stderr)
        out = home / '.local/share/developer-observability/collected'
        run = subprocess.run([str(ROOT / 'bin/developer-observability'), 'collect', '--no-known', '--project-dir',
                              str(self.project), '--config', str(cfg), '--json'],
                             env={**env, 'OBSERVABILITY_PYTHON': sys.executable}, capture_output=True, text=True, check=False)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertGreater(json.loads(run.stdout)['findings'], 0)
        config = json.loads(cfg.read_text())
        api = ObservabilityAPI(Path(config['runtime'].replace('~', str(home), 1)), config['projects'], None,
                               discover=True, collected=out)
        projects = {p['project_id']: p['issue_count'] for p in api.projects()['projects']}
        self.assertGreater(projects.get('sample-project', 0), 0, projects)
        issues = api.issues('sample-project')
        self.assertTrue(all(issues['by_kind'].get(k, 0) > 0 for k in ('failure', 'latency_hotspot', 'redundancy')))
        self.assertTrue(issues['slowest'])
        self.assertTrue(issues['top_failures'] and issues['top_failures'][0]['failed_attempts'] >= issues['top_failures'][-1]['failed_attempts'])

    def test_four_megabyte_jsonl_under_ten_seconds(self):
        big = self.tmp / 'big.jsonl'
        row = {'kind': 'startup', 'phase': 'ws', 'status': 'ok', 'step_ms': 1500, 'detail': 'x' * 60, 'ts': NOW}
        line = json.dumps(row) + '\n'
        big.write_text(line * (4_000_000 // len(line)))
        started = time.perf_counter()
        summary = collect([{'type': 'jsonl', 'path': str(big), 'project': 'big'}], self.tmp / 'big-out', NOW)
        self.assertLess(time.perf_counter() - started, 10)
        self.assertGreater(summary['events'], 10_000)


if __name__ == '__main__':
    unittest.main()
