"""0.1.5/0.1.6 accuracy (0.1.7: tests/test_accuracy_017.py): one cluster per root cause, mirrors and tests excluded, real latency only, version-aware status, no cap."""
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime'))
from backend.observability_api import ObservabilityAPI  # noqa: E402
from collector.analyze import build  # noqa: E402
from collector.cli import collect  # noqa: E402
from collector.sources import iter_jsonl, iter_textlog  # noqa: E402

FIXTURE = ROOT / 'tests/fixtures/accuracy'
NOW = 1793800000.0
SPECS = [{'type': 'jsonl', 'path': str(FIXTURE / 'activity.jsonl'), 'project': 'voice', 'source': 'voice:activity'},
         {'type': 'textlog', 'path': str(FIXTURE / 'voice.log'), 'project': 'voice', 'source': 'voice:voice.log'}]


def events():
    stats = {}
    out = list(iter_jsonl(FIXTURE / 'activity.jsonl', 'voice', 'voice:activity', stats))
    out += list(iter_textlog(FIXTURE / 'voice.log', 'voice', 'voice:voice.log', stats))
    return out, stats


def run(include_tests=False):
    found, stats = events()
    return build(found, stats, NOW, include_tests)['findings'], stats


def failures(findings, needle):
    return [f for f in findings if f['kind'] == 'failure' and needle in f['headline']]


class AccuracyTests(unittest.TestCase):
    def test_1_one_root_cause_cluster_across_sources_and_kinds(self):
        findings, _ = run()
        hits = [f for f in findings if f['kind'] == 'failure' and '403' in f['latest_evidence']['first_error_line']]
        self.assertEqual(len(hits), 1, [f['headline'] for f in hits])
        self.assertEqual(hits[0]['failed_attempt_count'], 3)
        self.assertEqual(len(hits[0]['logged_as']), 4, hits[0]['logged_as'])
        self.assertIn('startup/realtime_ws_error', hits[0]['headline'])

    def test_2_island_mirror_is_not_a_failure(self):
        findings, stats = run()
        self.assertFalse([f for f in findings if 'island_error_raw' in f['headline']])
        self.assertEqual(stats['voice:activity']['mirrors'], 4)
        self.assertFalse(failures(findings, 'confirmation'), 'needs_confirmation is not an error')
        self.assertEqual(failures(findings, 'tool_error/cua_act')[0]['failed_attempt_count'], 3)

    def test_3_latency_only_from_real_step_durations(self):
        findings, _ = run()
        hot = [f for f in findings if f['kind'] == 'latency_hotspot']
        self.assertEqual([f['latency']['metric'] for f in hot], ['speech_to_playback_ms'], [f['headline'] for f in hot])
        for idle in ('realtime_ws_close', 'response_done', 'stack_exit', 'realtime_ws_error'):
            self.assertFalse([f for f in hot if idle in f['headline']], idle)

    def test_4_latency_hotspot_reports_p50_p95_against_threshold(self):
        findings, _ = run()
        hot = next(f for f in findings if f['kind'] == 'latency_hotspot')
        self.assertEqual(hot['latency'] | {}, hot['latency'])
        self.assertEqual((hot['latency']['p50_ms'], hot['latency']['p95_ms'], hot['latency']['threshold_ms'], hot['latency']['n']),
                         (2100, 7000, 1000, 10))
        self.assertIn('p95 7.0 s (p50 2.1 s, n=10, threshold 1.0 s)', hot['headline'])
        found, stats = events()
        strict = build(found, stats, NOW, threshold_ms=8000)['findings']
        self.assertFalse([f for f in strict if f['kind'] == 'latency_hotspot'])

    def test_5_status_uses_window_and_version_split(self):
        findings, _ = run()
        mic = failures(findings, 'startup/mic_preflight failed')[0]
        self.assertEqual((mic['status'], mic['before_after']), ('recovered', {'version': '1.1', 'before': 2, 'after': 0}))
        cua = failures(findings, 'tool_error/cua_act')[0]
        self.assertEqual((cua['status'], cua['by_version']), ('active', {'1.0': 2, '1.1': 1}))
        self.assertEqual(cua['before_after'], {'version': '1.1', 'before': 2, 'after': 1})
        device = failures(findings, '(no_input_device)')[0]
        self.assertEqual((device['error_class'], device['condition'], device['failed_attempt_count']),
                         ('environment_condition', 'no_input_device', 1))
        old = {'project': 'p', 'source': 's', 'kind': 'tool_error', 'step': 'x', 'error': True, 'duration_ms': None,
               'metric': None, 'message': 'boom', 'ref': 'f:1', 'session': None, 'condition': None, 'mirror': False,
               'test': False, 'version': None}
        unversioned = build([dict(old, ts=NOW - 48 * 3600), dict(old, ts=NOW - 47 * 3600, message='fresh boom', step='y'),
                             dict(old, ts=NOW - 60, message='fresh boom', step='y')], {}, NOW)['findings']
        self.assertEqual(sorted(f['status'] for f in unversioned), ['active', 'stale'])

    def test_6_no_silent_cap_and_consistent_totals(self):
        tmp = Path(tempfile.mkdtemp(prefix='devobs-acc-'))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        log = tmp / 'many.jsonl'
        log.write_text(''.join(json.dumps({'kind': 'tool_error', 'tool': f'tool{chr(97 + i % 26)}{chr(97 + i // 26)}',
                                           'error': 'broken', 'ts': NOW - i}) + '\n' for i in range(130)))
        summary = collect([{'type': 'jsonl', 'path': str(log), 'project': 'many'}], tmp / 'out', NOW)
        self.assertEqual((summary['total'], summary['active']), (130, 130))
        doc = json.loads((tmp / 'out/findings.json').read_text())
        self.assertEqual([f['rank'] for f in doc['findings']], list(range(1, 131)))
        api = ObservabilityAPI(tmp / 'runtime', {'many': {'owners': ['owner:many']}}, None, collected=tmp / 'out')
        page, seen = api.issues('many', limit=25), []
        while True:
            seen += [i['issue_id'] for i in page['issues']]
            self.assertEqual((page['total'], page['active'], page['by_source']), (130, 130, {'collector': 130}))
            if not page['next_cursor']:
                break
            page = api.issues('many', cursor=page['next_cursor'], limit=25)
        self.assertEqual(len(set(seen)), 130)

    def test_6_filtered_counts_match_the_rows_listed(self):
        tmp = Path(tempfile.mkdtemp(prefix='devobs-acc-'))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        collect(SPECS, tmp / 'out', NOW)
        api = ObservabilityAPI(tmp / 'runtime', {'voice': {'owners': ['owner:voice']}}, None, collected=tmp / 'out')
        everything = api.issues('voice', limit=25)
        self.assertGreater(everything['by_kind'].get('latency_hotspot', 0), 0)
        for query in ({'kind': 'latency_hotspot'}, {'kind': 'failure'}, {'status': 'recovered'}, {'status': 'active', 'kind': 'failure'}):
            page = api.issues('voice', limit=25, **query)
            listed = page['issues']
            self.assertEqual(page['total'], len(listed), query)
            self.assertEqual(sum(page['by_status'].values()), page['total'], query)
            self.assertEqual(sum(page['by_source'].values()), page['total'], query)
            self.assertEqual(page['active'], sum(i['status'] == 'active' for i in listed), query)
        self.assertLess(api.issues('voice', kind='latency_hotspot')['total'], everything['total'])

    def test_1_voice_log_prose_joins_the_phase_cluster(self):
        findings, _ = run()
        self.assertFalse([f for f in findings if f['kind'] == 'failure' and re.search(
            r'querying device|voice-cua stack', f['latest_evidence']['first_error_line'], re.I)], [f['headline'] for f in findings])
        mic = failures(findings, 'startup/mic_preflight failed')[0]
        self.assertEqual(mic['failed_attempt_count'], 2)
        self.assertTrue(any(k.startswith('voice.log') for k in mic['logged_as']), mic['logged_as'])

    def test_7_test_sessions_excluded_by_default(self):
        default, stats = run()
        self.assertFalse(failures(default, 'self test') or failures(default, 'tool_error/comet_act'))
        self.assertFalse([f for f in default if 'fixture probe' in f['latest_evidence']['first_error_line']])
        self.assertEqual(failures(default, 'startup/mic_preflight failed')[0]['failed_attempt_count'], 2)
        self.assertGreater(stats['voice:activity']['excluded_test'] + stats['voice:voice.log']['excluded_test'], 0)
        with_tests, _ = run(include_tests=True)
        self.assertEqual(failures(with_tests, 'tool_error/comet_act')[0]['failed_attempt_count'], 1)
        self.assertEqual(failures(with_tests, 'startup/mic_preflight failed')[0]['failed_attempt_count'], 3)

    def test_7_cli_include_tests_flag(self):
        tmp = Path(tempfile.mkdtemp(prefix='devobs-acc-'))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        base = [sys.executable, str(ROOT / 'runtime/collector/cli.py'), '--no-known', '--config', str(tmp / 'none.json'), '--json',
                '--source', f"jsonl:{FIXTURE / 'activity.jsonl'}:voice"]
        env = {**os.environ, 'HOME': str(tmp)}
        default = json.loads(subprocess.run(base + ['--out', str(tmp / 'a')], env=env, capture_output=True, text=True, check=True).stdout)
        tests = json.loads(subprocess.run(base + ['--out', str(tmp / 'b'), '--include-tests'], env=env, capture_output=True,
                                          text=True, check=True).stdout)
        self.assertEqual((default['include_tests'], tests['include_tests']), (False, True))
        self.assertGreater(default['excluded_test_events'], 0)
        self.assertEqual(tests['excluded_test_events'], 0)
        self.assertGreater(tests['total'], default['total'])


if __name__ == '__main__':
    unittest.main()
