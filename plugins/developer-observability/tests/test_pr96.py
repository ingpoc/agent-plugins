"""Synthetic acceptance coverage for the 0.1.29 read-side contracts."""
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime'))
from backend.observability_api import ObservabilityAPI  # noqa: E402
from collector import retention  # noqa: E402
from collector.analyze import _test_fix  # noqa: E402
from collector.cli import collect  # noqa: E402

NOW = datetime.now(timezone.utc).timestamp()
STAMP = datetime.fromtimestamp(NOW, timezone.utc).isoformat().replace('+00:00', 'Z')


class PR96Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT, prefix='.pr96-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def api_for(self, out):
        return ObservabilityAPI(self.root / 'runtime', {'p': {'owners': ['owner:p']}}, None, collected=out)

    def write_rows(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows))

    def collect_rows(self, rows, name='rows'):
        path, out = self.root / f'{name}.jsonl', self.root / f'{name}-out'
        self.write_rows(path, rows)
        collect([{'type': 'jsonl', 'path': str(path), 'project': 'p', 'source': 'p:synthetic'}], out, NOW)
        return self.api_for(out), out

    def test_test_fixes_follow_finding_kind_for_ci_and_junit(self):
        for source in ('origin-ci', 'junit'):
            events = [{'source': source, 'metric': 'test_ms', 'step': 'Pkg.test_slow', 'error': False}]
            failure = _test_fix('failure', {}, [{**events[0], 'error': True}])
            latency = _test_fix('latency_hotspot', {'latency': {'p95_ms': 90000, 'n': 6}}, events)
            redundancy = _test_fix('redundancy', {}, events)
            self.assertIn('CI check log', failure)
            self.assertIn('Pkg.test_slow', failure)
            self.assertIn('p95 90000 ms', latency)
            self.assertIn('6 runs', latency)
            self.assertIn('deduplicate', redundancy.lower())
            self.assertIn('retry', redundancy.lower())

    def test_compact_coverage_is_current_run_derived_and_opt_in(self):
        out = self.root / 'coverage'
        out.mkdir()
        old = '2010-01-01T00:00:00Z'
        data = {'generated_at': STAMP, 'findings': [],
                'coverage': {'p:codex': 'partial', 'p:ci': 'stale', 'p:missing-source': 'missing', 'p:silent': 'stale'},
                'coverage_details': {
                    'p:codex': {'status': 'partial', 'collector_heartbeat_at': old, 'last_event_at': STAMP,
                              'read_in_collect': True, 'stale_after_s': 86400},
                    'p:ci': {'status': 'stale', 'collector_heartbeat_at': old,
                           'last_event_at': datetime.fromtimestamp(NOW - 6 * 86400, timezone.utc).isoformat().replace('+00:00', 'Z'),
                           'read_in_collect': True, 'stale_after_s': 86400},
                    'p:missing-source': {'status': 'missing', 'missing': True, 'collector_heartbeat_at': old},
                    'p:silent': {'status': 'stale', 'collector_heartbeat_at': old, 'last_event_at': 'unknown',
                               'read_in_collect': True, 'stale_after_s': 86400}},
                'coverage_gaps': [], 'limits': ['static non-clipping note']}
        (out / 'findings.json').write_text(json.dumps(data))
        api = self.api_for(out)

        compact = api.issues('p')
        self.assertIsInstance(compact['coverage'], str)
        self.assertLess(len(json.dumps(compact['coverage']).encode()), 300)
        self.assertNotIn('coverage_details', compact)
        self.assertNotIn('limits', compact)
        self.assertNotIn('stale 6d', compact['coverage'])
        self.assertIn('missing', compact['coverage'])
        self.assertNotIn('silent stale', compact['coverage'])

        detailed = api.issues('p', include_coverage=True)
        self.assertEqual(detailed['coverage_details']['p:codex']['status'], 'collected')
        self.assertEqual(detailed['coverage_details']['p:ci']['status'], 'collected')
        self.assertEqual(detailed['coverage_details']['p:silent']['status'], 'collected')
        self.assertIn('coverage_gap_scope', detailed)
        self.assertNotIn('limits', detailed)  # static notes are not clipped limits

    def test_compact_sidecars_keep_reads_and_retention_snapshot_scoped(self):
        rows = []
        for version in ('1.0', '2.0'):
            for session in range(30):
                rows.append({'kind': 'realtime_error', 'status': 'error', 'detail': 'synthetic failure',
                             'phase': 'response_done',
                             'version': version, 'session_id': f'{version}-session-{session}',
                             'ts': NOW - session})
        api, out = self.collect_rows(rows, 'scopes')
        doc = json.loads((out / 'findings.json').read_text())
        self.assertLess((out / 'findings.json').stat().st_size, 2_000_000)
        self.assertNotIn('session_timelines', doc)
        self.assertTrue(all('session_timelines' not in scope and 'file' in scope for scope in doc['version_scopes']))
        self.assertEqual(len(doc['version_scopes']), 2)
        self.assertEqual(len(doc['session_index']), 50)
        snapshot = doc['snapshot_id']
        self.assertTrue((out / 'snapshots' / snapshot / 'sessions').is_dir())
        self.assertTrue((out / 'snapshots' / snapshot / 'scopes').is_dir())
        session_row = doc['session_index'][0]
        detail = api.sessions('p', session_row['session_ref'])
        self.assertEqual(detail['total'], 1)
        scoped = api.sessions('p', version='1.0', attribution='event')['sessions'][0]
        self.assertEqual(api.sessions('p', scoped['session_ref'], version='1.0', attribution='event')['total'], 1)

        # The reader still accepts pre-sidecar snapshots.
        legacy_out = self.root / 'legacy'
        legacy_out.mkdir()
        legacy_ref = 'sref_' + 'a' * 32
        legacy = {'generated_at': STAMP, 'findings': [],
                  'session_timelines': [{'project': 'p', 'session_ref': legacy_ref, 'last_seen': STAMP,
                                         'total': 1, 'raw_total': 1, 'truncated': False, 'events': []}],
                  'version_scopes': [{'project': 'p', 'version': '1.0', 'attribution': 'event',
                                      'findings': [], 'session_timelines': []}]}
        (legacy_out / 'findings.json').write_text(json.dumps(legacy))
        legacy_api = self.api_for(legacy_out)
        self.assertEqual(legacy_api.sessions('p')['sessions'][0]['session_ref'], legacy_ref)
        self.assertEqual(legacy_api.version_scopes('p')['version_scopes'],
                         [{'version': '1.0', 'attribution': 'event'}])

        # Mismatched sidecars are reported as snapshot changes, and retention sees old sidecars.
        pointer = session_row['file']
        sidecar = out / pointer
        bad = json.loads(sidecar.read_text())
        bad['snapshot_id'] = 'f' * 32
        sidecar.write_text(json.dumps(bad))
        gap = api.sessions('p', session_row['session_ref'])
        self.assertEqual(gap['gap']['code'], 'snapshot_changed')
        old_sidecar = out / 'snapshots' / ('e' * 32) / 'sessions' / 'stale.json'
        old_sidecar.parent.mkdir(parents=True)
        old_sidecar.write_text('{}')
        old_time = NOW - 10 * 86400
        os.utime(old_sidecar, (old_time, old_time))
        config = {'retention': {'enabled': True, 'days': 7, 'backup_days': 30,
                                'cache_dirs': [], 'trace_paths': [],
                                'trash_dir': str(self.root / 'trash'), 'max_trash_bytes': 1_000_000}}
        result = retention.prune(config, out, now=NOW, home=self.root)
        self.assertGreaterEqual(result['moved'], 1)
        self.assertFalse(old_sidecar.exists())
        self.assertTrue((out / 'snapshots' / snapshot).exists())

    def test_task_views_cover_ledger_cua_cancel_comet_and_mic_gap(self):
        rows = [
            {'run_id': 'agent02', 'session_id': 'shared-session', 'request': 'Find the preference sentinel',
             'expected_outcome': 'record found', 'kind': 'ledger_task', 'phase': 'ledger_task',
             'status': 'error', 'error_type': 'InvalidQueryTerms', 'error_message': 'invalid query terms',
             'step_ref': 'lookup-first', 'retry_of_step_ref': None, 'ledger_operation': 'lookup',
             'ledger_result_status': 'invalid_query_terms', 'query_terms': ['preference sentinel'],
             'invalid_query_terms': ['bad-term-sentinel'], 'selected_id': 'none', 'ts': NOW - 20},
            {'run_id': 'agent02', 'session_id': 'shared-session', 'kind': 'ledger_task', 'phase': 'ledger_task',
             'status': 'ok', 'step_ref': 'lookup-retry', 'retry_of_step_ref': 'lookup-first',
             'ledger_operation': 'lookup', 'ledger_result_status': 'no_match',
             'query_terms': ['preference sentinel'], 'selected_id': 'none', 'ts': NOW - 19},
            {'run_id': 'agent03', 'session_id': 'shared-session', 'request': 'Click the target sentinel',
             'kind': 'cua_act', 'phase': 'cua_act', 'status': 'error', 'error_type': 'TargetNotFound',
             'error_message': 'target sentinel was not found', 'step_ref': 'cua-fail', 'target': 'Save sentinel',
             'expected_target': 'Save sentinel', 'found_target': 'Save as sentinel', 'match_count': 0,
             'near_miss_count': 2, 'matching_targets': [], 'near_miss_targets': ['Save as sentinel', 'Save copy sentinel'],
             'ax_tree': 'AX-DUMP-SENTINEL', 'page_text': 'PAGE-DUMP-SENTINEL', 'ts': NOW - 18},
            {'run_id': 'agent07', 'session_id': 'shared-session', 'request': 'Cancel queued task sentinel',
             'expected_outcome': 'cancelled', 'kind': 'cancel_request', 'phase': 'cancel', 'status': 'ok',
             'step_ref': 'cancel-request', 'cancel': {'requested': True, 'target_run_ref': 'target-run-1'}, 'ts': NOW - 17},
            {'run_id': 'agent07', 'session_id': 'shared-session', 'kind': 'cancel_ack', 'phase': 'cancel',
             'status': 'ok', 'step_ref': 'cancel-ack', 'cancel_ack': {'acknowledged': True},
             'cancel_state': 'accepted', 'queued_action': 'save sentinel', 'queued_action_suppressed': True,
             'ts': NOW - 16},
            {'run_id': 'agent07', 'session_id': 'shared-session', 'kind': 'task_outcome', 'outcome': 'cancelled',
             'step_ref': 'cancel-outcome', 'ts': NOW - 15},
            {'run_id': 'agent09', 'session_id': 'shared-session', 'request': 'Read the loaded URL sentinel',
             'kind': 'comet_begin', 'phase': 'comet_begin', 'status': 'ok', 'step_ref': 'comet-lease',
             'lease': {'id': 'lease-sentinel', 'status': 'active', 'url': 'https://example.test/loaded-sentinel'},
             'url': 'https://example.test/loaded-sentinel', 'ts': NOW - 14},
            {'run_id': 'agent09', 'session_id': 'shared-session', 'kind': 'comet_act', 'phase': 'comet_act',
             'status': 'error', 'error_type': 'PageReadTimeout', 'error_message': 'typed page read timeout',
             'step_ref': 'comet-page-read', 'caused_by': ['comet-lease'], 'action': 'page-read sentinel',
             'url': 'https://example.test/loaded-sentinel', 'lease': {'id': 'lease-sentinel'},
             'ts': NOW - 13},
            {'run_id': 'agent09', 'session_id': 'shared-session', 'kind': 'comet_end', 'phase': 'comet_end',
             'status': 'ok', 'step_ref': 'comet-close', 'action': 'close sentinel', 'ts': NOW - 12},
            {'run_id': 'deferred', 'session_id': 'shared-session', 'turn_id': 'turn-1', 'kind': 'task_step',
             'phase': 'work', 'status': 'ok', 'step_ref': 'deferred-start', 'ts': NOW - 11},
            {'run_id': 'deferred', 'session_id': 'shared-session', 'turn_id': 'turn-1', 'kind': 'task_step',
             'phase': 'settled', 'status': 'ok', 'step_ref': 'deferred-settled', 'ts': NOW - 10},
            {'run_id': 'separate-run', 'session_id': 'shared-session', 'kind': 'task_step', 'phase': 'work',
             'status': 'ok', 'step_ref': 'separate-step', 'ts': NOW - 9},
            {'run_id': 'mic-run', 'session_id': 'shared-session', 'kind': 'mic_preflight', 'phase': 'mic_preflight',
             'status': 'ok', 'step_ref': 'mic-check', 'ts': NOW - 8},
            {'run_id': 'mic-run', 'session_id': 'shared-session', 'kind': 'voice_latency', 'phase': 'voice',
             'status': 'ok', 'step_ref': 'voice-check', 'ts': NOW - 7},
            # Live CI shape: test paths contain 'voice' and 'mic' (voice-cua/tests/test_mic_preflight.py).
            {'run_id': 'ci-mic-name', 'session_id': 'shared-session', 'kind': 'check_test',
             'phase': 'plugins/x/runtime/voice-cua/tests/test_mic_preflight.py', 'status': 'ok',
             'step_ref': 'ci-mic-check', 'ts': NOW - 6},
            {'run_id': 'mirror-run', 'session_id': 'shared-session', 'kind': 'tool_start', 'phase': 'cua_act',
             'status': 'ok', 'step_ref': 'primary-action', 'target': 'Mirror target', 'ts': NOW - 6},
            {'run_id': 'mirror-run', 'session_id': 'shared-session', 'kind': 'tool_start', 'phase': 'cua_act',
             'status': 'ok', 'step_ref': 'mirrored-action', 'mirror_of': 'primary-action',
             'target': 'Mirror target', 'ts': NOW - 5},
            {'session_id': 'shared-session', 'kind': 'orphan_step', 'phase': 'orphan', 'status': 'ok',
             'step_ref': 'orphan-step', 'ts': NOW - 7},
        ]
        api, _out = self.collect_rows(rows, 'tasks')
        tasks = api.tasks('p')
        refs = {item['run_ref'] for item in tasks['tasks']}
        self.assertIn(hashlib.sha256(b'agent02').hexdigest()[:24], refs)
        self.assertIn(hashlib.sha256(b'agent07').hexdigest()[:24], refs)
        self.assertIn(hashlib.sha256(b'deferred').hexdigest()[:24], refs)
        self.assertEqual(sum(1 for item in tasks['tasks'] if item['run_ref'] == hashlib.sha256(b'deferred').hexdigest()[:24]), 1)
        self.assertTrue(any(gap['code'] == 'run_ref_missing' for gap in tasks['gaps']))

        ledger = api.task('p', hashlib.sha256(b'agent02').hexdigest()[:24])
        self.assertLess(len(json.dumps(ledger, ensure_ascii=False, separators=(',', ':')).encode()), 4096)
        self.assertEqual(next(iter(ledger)), 'status')
        self.assertEqual(ledger['first_failure']['error_type'], 'InvalidQueryTerms')
        self.assertEqual(ledger['first_failure']['invalid_query_terms'], ['bad-term-sentinel'])
        self.assertEqual(ledger['first_failure']['selected_id'], 'none')
        self.assertIn('retry_of=lookup-first', ' '.join(step['line'] for step in ledger['steps']))

        cua = api.task('p', hashlib.sha256(b'agent03').hexdigest()[:24])
        self.assertLess(len(json.dumps(cua, ensure_ascii=False, separators=(',', ':')).encode()), 4096)
        self.assertEqual(cua['first_failure']['error_type'], 'TargetNotFound')
        self.assertEqual(cua['first_failure']['expected_target'], 'Save sentinel')
        self.assertEqual(cua['first_failure']['found_target'], 'Save as sentinel')
        self.assertEqual(cua['first_failure']['near_miss_count'], 2)
        self.assertNotIn('AX-DUMP-SENTINEL', json.dumps(cua))
        self.assertNotIn('PAGE-DUMP-SENTINEL', json.dumps(cua))

        cancel = api.task('p', hashlib.sha256(b'agent07').hexdigest()[:24])
        self.assertLess(len(json.dumps(cancel, ensure_ascii=False, separators=(',', ':')).encode()), 4096)
        self.assertEqual(cancel['status'], 'cancelled')
        self.assertTrue(cancel['cancellation']['cancel']['requested'])
        self.assertTrue(cancel['cancellation']['cancel_ack']['acknowledged'])
        self.assertTrue(cancel['cancellation']['queued_action_suppressed'])

        comet = api.task('p', hashlib.sha256(b'agent09').hexdigest()[:24])
        self.assertLess(len(json.dumps(comet, ensure_ascii=False, separators=(',', ':')).encode()), 4096)
        self.assertEqual(comet['first_failure']['error_type'], 'PageReadTimeout')
        self.assertEqual(comet['first_failure']['url'], 'https://example.test/loaded-sentinel')
        self.assertEqual(comet['first_failure']['lease']['id'], 'lease-sentinel')
        self.assertEqual(comet['first_failure']['direct_inputs']['comet-lease'][0]['url'],
                         'https://example.test/loaded-sentinel')
        mic = api.task('p', hashlib.sha256(b'mic-run').hexdigest()[:24])
        self.assertTrue(any(gap['code'] == 'mic_state_not_in_scope' for gap in mic['gaps']))
        ci_mic = api.task('p', hashlib.sha256(b'ci-mic-name').hexdigest()[:24])
        self.assertFalse(any(gap['code'] == 'mic_state_not_in_scope' for gap in ci_mic['gaps']))
        mirror = api.task('p', hashlib.sha256(b'mirror-run').hexdigest()[:24])
        self.assertEqual(mirror['total_steps'], 1)
        self.assertEqual(mirror['steps'][0]['mirror_refs'], ['mirrored-action'])
        deferred = api.task('p', hashlib.sha256(b'deferred').hexdigest()[:24])
        self.assertEqual(deferred['outcome']['observed'], 'unknown')
        self.assertIn('next_action', deferred['outcome'])

    def test_run_links_redacted_typed_text_and_ledger_decision_gap(self):
        rows = [
            {'run_id': 'link-target', 'kind': 'task_step', 'step_ref': 'target-step', 'status': 'ok', 'ts': NOW - 5},
            {'run_id': 'link-retry', 'kind': 'task_step', 'step_ref': 'retry-step',
             'retry_of_run_ref': 'link-target', 'status': 'ok', 'ts': NOW - 4},
            {'run_id': 'link-projected', 'kind': 'task_step', 'step_ref': 'projected-step',
             'retry_of_run_ref': hashlib.sha256(b'link-target').hexdigest()[:24], 'status': 'ok', 'ts': NOW - 3.5},
            {'run_id': 'link-cancel', 'kind': 'cancel_request', 'step_ref': 'cancel-step',
             'cancel': {'requested': True, 'target_run_ref': 'link-target'}, 'status': 'ok', 'ts': NOW - 3},
            {'run_id': 'typed-redacted', 'kind': 'cua_act', 'step_ref': 'typed-step', 'status': 'error',
             'error_type': 'Rejected', 'typed_text_redacted_reason': 'security_unknown',
             'typed_text_length': 17, 'ts': NOW - 2},
            {'run_id': 'typed-redacted-chars', 'kind': 'cua_act', 'step_ref': 'typed-step-chars', 'status': 'error',
             'error_type': 'Rejected', 'typed_text_redacted_reason': 'security_unknown',
             'typed_text_chars': 19, 'ts': NOW - 1},
            {'run_id': 'ledger-gap', 'kind': 'ledger_lookup', 'step_ref': 'ledger-lookup',
             'ledger_operation': 'lookup', 'ledger_result_status': 'selected', 'selected_id': 'record-1',
             'ts': NOW - 2},
            {'run_id': 'ledger-gap', 'kind': 'cua_act', 'step_ref': 'apply-action', 'action': 'apply decision',
             'status': 'ok', 'ts': NOW - 1},
            {'run_id': 'ledger-none', 'kind': 'ledger_lookup', 'step_ref': 'none-lookup',
             'ledger_operation': 'lookup', 'ledger_result_status': 'none', 'ledger_decision_count': 0,
             'ts': NOW - 2},
            {'run_id': 'ledger-none', 'kind': 'cua_act', 'step_ref': 'none-action', 'action': 'click',
             'status': 'ok', 'ts': NOW - 1},
        ]
        api, _out = self.collect_rows(rows, 'followup-contracts')
        target_ref = hashlib.sha256(b'link-target').hexdigest()[:24]
        listing = api.tasks('p')
        self.assertIn(target_ref, {row['run_ref'] for row in listing['tasks']})
        retry = api.task('p', hashlib.sha256(b'link-retry').hexdigest()[:24])
        self.assertEqual(retry['steps'][0]['retry_of_run_ref'], target_ref)
        projected = api.task('p', hashlib.sha256(b'link-projected').hexdigest()[:24])
        self.assertEqual(projected['steps'][0]['retry_of_run_ref'], target_ref)
        cancel = api.task('p', hashlib.sha256(b'link-cancel').hexdigest()[:24])
        self.assertEqual(cancel['cancellation']['cancel']['target_run_ref'], target_ref)

        typed_ref = hashlib.sha256(b'typed-redacted').hexdigest()[:24]
        typed = api.task('p', typed_ref)
        rendered = json.dumps(typed, ensure_ascii=False)
        self.assertIn('redacted (security_unknown, 17 chars)', rendered)
        typed_chars = api.task('p', hashlib.sha256(b'typed-redacted-chars').hexdigest()[:24])
        self.assertIn('redacted (security_unknown, 19 chars)', json.dumps(typed_chars, ensure_ascii=False))

        ledger = api.task('p', hashlib.sha256(b'ledger-gap').hexdigest()[:24])
        gap = next(gap for gap in ledger['gaps'] if gap['code'] == 'ledger_decision_id_missing')
        self.assertIn('ledger_use only appears', gap['reason'])
        self.assertEqual(gap['next_action'], 'Emit ledger_decision_id on the action that applied the decision.')
        empty = api.task('p', hashlib.sha256(b'ledger-none').hexdigest()[:24])
        self.assertFalse(any(g['code'] == 'ledger_decision_id_missing' for g in empty['gaps']))

    def test_ci_junit_run_becomes_bounded_task_with_test_steps(self):
        junit = self.root / 'ci.xml'
        tests = ['<testcase id="test_%02d" classname="Suite" name="case_%02d" time="0.01" />' % (i, i)
                 for i in range(40)]
        tests.append('<testcase id="test_40" classname="Suite" name="case_40" time="0.02">'
                     '<failure type="AssertionError" message="CI failure sentinel">assertion failed</failure>'
                     '</testcase>')
        junit.write_text('<testsuite run_id="ci-run-96">' + ''.join(tests) + '</testsuite>')
        out = self.root / 'ci-out'
        collect([{'type': 'junit', 'path': str(junit), 'project': 'p', 'source': 'p:origin-ci:junit'}], out, NOW)
        api = self.api_for(out)
        run_ref = hashlib.sha256(b'ci-run-96').hexdigest()[:24]
        indexed = next(row for row in api.tasks('p')['tasks'] if row['run_ref'] == run_ref)
        self.assertEqual(indexed['step_count'], 41)
        page = api.task('p', run_ref)
        self.assertLessEqual(len(json.dumps(page, ensure_ascii=False, separators=(',', ':')).encode()), 4096)
        self.assertEqual(page['status'], 'failure')
        steps = list(page['steps'])
        cursor = page['next_cursor']
        while cursor:
            page = api.task('p', run_ref, cursor)
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False, separators=(',', ':')).encode()), 4096)
            steps.extend(page['steps'])
            cursor = page['next_cursor']
        self.assertEqual(sum(step.get('status', 'success') == 'success' for step in steps), 40)
        self.assertEqual(len(steps), 41)
        self.assertEqual(page['first_failure']['step_ref'], 'test_40')
        self.assertEqual(page['first_failure']['error_type'], 'AssertionError')
        self.assertIn('CI failure sentinel', json.dumps(page))
        self.assertEqual(next(step['line'] for step in steps if step['step_ref'] == 'test_00').count('Suite.case_00'), 1)

        run_dir = self.root / 'checklog-run'
        run_dir.mkdir()
        (run_dir / 'check.log').write_text('run_id=ci-check-96\n'
                                           'test Suite.Pass (0.01s, exit 0)\n'
                                           'failed: test Suite.Fail (exit 1)\n')
        check_out = self.root / 'checklog-out'
        collect([{'type': 'checklog', 'path': str(run_dir), 'project': 'p', 'source': 'p:origin-ci'}],
                check_out, NOW)
        check_api = self.api_for(check_out)
        check_ref = hashlib.sha256(b'ci-check-96').hexdigest()[:24]
        check_task = check_api.task('p', check_ref)
        self.assertEqual(check_task['status'], 'failure')
        self.assertEqual(check_task['first_failure']['step_ref'], 'Suite.Fail')

    def test_task_and_facet_cursors_are_snapshot_bound_and_lossless(self):
        rows = []
        for index in range(450):
            rows.append({'run_id': 'cursor-run', 'session_id': 'cursor-session', 'kind': 'task_step',
                         'phase': 'work', 'status': 'ok', 'step_ref': f'step-{index:03d}',
                         'duration_ms': index, 'ts': NOW - 100 + index,
                         **({'request': 'cursor request sentinel'} if index == 0 else {})})
        large = 'typed text sentinel ' + 'x' * 2800
        for index in range(8):
            rows.append({'run_id': 'facet-run', 'session_id': 'facet-session', 'kind': 'task_step',
                         'phase': 'work', 'status': 'ok', 'step_ref': 'facet-step', 'typed_text': large,
                         'url': 'https://example.test/path/URL-sentinel', 'target': 'Target-sentinel',
                         'ax_tree': 'AX-DUMP-SENTINEL', 'ts': NOW + index})
        api, out = self.collect_rows(rows, 'cursor')
        run_ref = hashlib.sha256(b'cursor-run').hexdigest()[:24]
        first = api.task('p', run_ref)
        self.assertLessEqual(len(json.dumps(first, ensure_ascii=False, separators=(',', ':')).encode()), 16_384)
        self.assertIsNotNone(first['next_cursor'])
        self.assertEqual(api.task('p', run_ref, first['next_cursor']),
                         api.task('p', run_ref, first['next_cursor']))
        refs = [step['step_ref'] for step in first['steps']]
        cursor = first['next_cursor']
        while cursor:
            page = api.task('p', run_ref, cursor)
            refs.extend(step['step_ref'] for step in page['steps'])
            cursor = page['next_cursor']
        self.assertEqual(refs, [f'step-{index:03d}' for index in range(450)])

        facet_ref = hashlib.sha256(b'facet-run').hexdigest()[:24]
        args_first = api.task_detail('p', facet_ref, 'facet-step', 'arguments')
        self.assertLessEqual(len(json.dumps(args_first, ensure_ascii=False, separators=(',', ':')).encode()), 4096)
        self.assertTrue(args_first['truncated'])
        self.assertEqual(api.task_detail('p', facet_ref, 'facet-step', 'arguments', args_first['next_cursor']),
                         api.task_detail('p', facet_ref, 'facet-step', 'arguments', args_first['next_cursor']))
        arguments = list(args_first['values'])
        cursor = args_first['next_cursor']
        while cursor:
            page = api.task_detail('p', facet_ref, 'facet-step', 'arguments', cursor)
            arguments.extend(page['values'])
            cursor = page['next_cursor']
        self.assertEqual(len(arguments), 8)
        self.assertTrue(all(value['typed_text'] == large for value in arguments))
        raw = api.task_detail('p', facet_ref, 'facet-step', 'raw')
        self.assertLessEqual(len(json.dumps(raw, ensure_ascii=False, separators=(',', ':')).encode()), 16_384)
        self.assertNotIn('AX-DUMP-SENTINEL', json.dumps(raw))

        snapshot_path = out / 'findings.json'
        doc = json.loads(snapshot_path.read_text())
        doc['snapshot_id'] = 'f' * 32
        snapshot_path.write_text(json.dumps(doc))
        changed = api.task('p', run_ref, first['next_cursor'])
        self.assertEqual(changed['gap']['code'], 'snapshot_changed')

    def test_oversized_facet_value_cursor_stays_on_blocked_value(self):
        large = 'x' * 4096
        for position, values in enumerate((['large', 'small'], ['small', 'large', 'small'],
                                           ['small', 'small', 'large'])):
            with self.subTest(position=position):
                run_id = f'oversized-{position}'
                rows = [{'run_id': run_id, 'step_ref': 'oversized-step', 'kind': 'task_step',
                         'status': 'ok', 'request': large if value == 'large' else value,
                         'ts': NOW + index} for index, value in enumerate(values)]
                api, _out = self.collect_rows(rows, f'oversized-{position}')
                run_ref = hashlib.sha256(run_id.encode()).hexdigest()[:24]
                page = api.task_detail('p', run_ref, 'oversized-step', 'arguments')
                if position:
                    self.assertTrue(page['values'])
                    page = api.task_detail('p', run_ref, 'oversized-step', 'arguments', page['next_cursor'])
                self.assertEqual(page['gap']['code'], 'facet_value_exceeds_page_cap')
                cursor = page['next_cursor']
                offset = json.loads(base64.urlsafe_b64decode(cursor).decode())[-1]
                self.assertEqual(offset, values.index('large'))
                self.assertEqual(api.task_detail('p', run_ref, 'oversized-step', 'arguments', cursor), page)

    def test_task_redaction_preserves_nonsecret_sentinels(self):
        secrets = ('sk-live-abcdefghijklmnop', 'Bearer abcdef0123456789', 'password=private-sentinel',
                   'api_key=private-api-key', 'token=private-url-token')
        email = 'person@example.test'
        request = f'request sentinel {secrets[0]} {email} {secrets[2]}'
        typed = f'typed text sentinel {secrets[1]} {email} {secrets[3]}'
        query = 'query term sentinel'
        target = 'target sentinel'
        url = f'https://example.test/nonsecret-url-sentinel?{secrets[4]}&keep={email}'
        rows = [{'run_id': 'redact-run', 'request': request, 'kind': 'cua_act', 'phase': 'cua_act',
                 'status': 'error', 'error_type': 'TargetError', 'error_message': secrets[2],
                 'step_ref': 'redact-fail', 'typed_text': typed, 'query_terms': [query], 'target': target,
                 'url': url, 'api_key': secrets[0], 'password': secrets[2], 'ts': NOW}]
        api, _out = self.collect_rows(rows, 'redact')
        view = api.task('p', hashlib.sha256(b'redact-run').hexdigest()[:24])
        blob = json.dumps(view, ensure_ascii=False)
        for secret in secrets:
            self.assertNotIn(secret, blob)
        for sentinel in ('request sentinel', 'typed text sentinel', query, target, 'nonsecret-url-sentinel', email):
            self.assertIn(sentinel, blob)
        detail = api.task_detail('p', hashlib.sha256(b'redact-run').hexdigest()[:24],
                                 'redact-fail', 'arguments')
        detail_blob = json.dumps(detail, ensure_ascii=False)
        for secret in secrets:
            self.assertNotIn(secret, detail_blob)
        self.assertIn(email, detail_blob)
        self.assertIn(query, json.dumps(detail))
        self.assertIn(typed.split(' ')[0] + ' ' + typed.split(' ')[1] + ' ' + typed.split(' ')[2], json.dumps(detail))

    def test_coverage_scopes_sources_and_groups_legacy_snapshot(self):
        out = self.root / 'scoped-coverage'
        out.mkdir()
        old = '2026-11-04T00:00:00Z'
        details = {'agent-plugins:origin-ci': {'read_in_collect': True, 'last_event_at': old,
                                                'collector_heartbeat_at': old},
                   'samantha:voice.log': {'read_in_collect': True, 'last_event_at': old,
                                           'collector_heartbeat_at': old}}
        for name in ('codex', 'cursor', 'grokbot', 'opencode', 'samantha', 't3'):
            details[name] = {'read_in_collect': False, 'last_event_at': old, 'collector_heartbeat_at': old}
        legacy_gaps = [{'source': name, 'status': 'partial' if name != 'opencode' else 'unknown'}
                       for name in ('codex', 'cursor', 'grokbot', 'opencode', 'samantha', 't3')]
        (out / 'findings.json').write_text(json.dumps({'generated_at': STAMP,
            'findings': [
                {'finding_key': 'legacy', 'owner_ref': 'owner:samantha', 'source': 'samantha:voice.log',
                 'latest_evidence': {'project': 'samantha'}},
                {'finding_key': 'ci', 'owner_ref': 'owner:agent-plugins', 'source': 'agent-plugins:origin-ci',
                 'latest_evidence': {'project': 'agent-plugins'}}],
            'task_index': [{'project': 'samantha', 'source': 'samantha:voice.log'},
                           {'project': 'agent-plugins', 'source': 'agent-plugins:origin-ci'}],
            'coverage': {'agent-plugins:origin-ci': 'collected', 'samantha:voice.log': 'collected',
                         **{name: item['status'] for name, item in zip(
                             ('codex', 'cursor', 'grokbot', 'opencode', 'samantha', 't3'), legacy_gaps)}},
            'coverage_details': details, 'coverage_gaps': legacy_gaps}))
        api = ObservabilityAPI(self.root / 'runtime', {'agent-plugins': {'owners': ['owner:agent-plugins']},
                               'samantha': {'owners': ['owner:samantha']}},
                               None, collected=out)
        coverage = api.issues('samantha', include_coverage=True)
        self.assertRegex(coverage['coverage'], r'^legacy snapshot [67]d old \(6 sources\)$')
        self.assertLess(len(coverage['coverage'].encode()), 300)
        self.assertEqual(set(coverage['coverage_details']),
                         {'samantha:voice.log', 'codex', 'cursor', 'grokbot', 'opencode', 'samantha', 't3'})
        agent_plugins_coverage = api.issues('agent-plugins')['coverage']
        self.assertEqual(agent_plugins_coverage, 'ok 1/1 sources')
        self.assertNotIn('legacy snapshot', agent_plugins_coverage)

    def test_coverage_gap_fallback_never_crosses_project(self):
        out = self.root / 'coverage-gap-scope'
        out.mkdir()
        (out / 'findings.json').write_text(json.dumps({'generated_at': STAMP, 'findings': [],
            'coverage_details': {'p:present': {'read_in_collect': True, 'last_event_at': STAMP}},
            'coverage_gaps': [{'source': 'q:origin-ci'}, {'source': 'p:voice'}]}))
        api = ObservabilityAPI(self.root / 'runtime',
            {'p': {'owners': ['owner:p']}, 'q': {'owners': ['owner:q']}}, None, collected=out)
        self.assertIn('voice', api.issues('p')['coverage'])
        self.assertNotIn('origin-ci', api.issues('p')['coverage'])
        self.assertIn('origin-ci', api.issues('q')['coverage'])
        self.assertNotIn('voice', api.issues('q')['coverage'])

    def test_ci_summary_creates_bounded_task_with_failing_test(self):
        run = self.root / 'ci-run'
        run.mkdir()
        head = 'a' * 40
        (run / 'summary.json').write_text(json.dumps({'pr': 95, 'head': head, 'attempt': 'ci-a-123',
            'conclusion': 'failure', 'phases': {'setup': 10, 'deps': 20, 'check': 30}}))
        (run / 'check.log').write_text(''.join(f'test suite.test_{i:02d} (0.01s, exit 0)\n' for i in range(40)) +
                                       'test suite.test_broken (0.02s, exit 1)\n')
        out = self.root / 'ci-collected'
        collect([{'type': 'checklog', 'path': str(run), 'project': 'agent-plugins',
                  'source': 'agent-plugins:origin-ci'}], out)
        api = ObservabilityAPI(self.root / 'runtime', {'agent-plugins': {'owners': ['owner:agent-plugins']}},
                               None, collected=out)
        run_ref = hashlib.sha256(b'ci-a-123').hexdigest()[:24]
        tasks = api.tasks('agent-plugins')
        self.assertEqual(tasks['total'], 1)
        task = api.task('agent-plugins', run_ref)
        blob = json.dumps(task, ensure_ascii=False, separators=(',', ':'))
        self.assertLessEqual(len(blob.encode()), 4096)
        self.assertIn('suite.test_broken', blob)
        self.assertIn('test_failure', blob)
        self.assertIn('CI PR #95', blob)

    def test_second_collect_moves_previous_snapshot_to_retention_trash(self):
        out = self.root / 'repeat'
        source = self.root / 'repeat.jsonl'
        self.write_rows(source, [{'run_id': 'repeat-run', 'step_ref': 'repeat-step', 'kind': 'task', 'ts': NOW}])
        spec = [{'type': 'jsonl', 'path': str(source), 'project': 'p', 'source': 'p:test'}]
        collect(spec, out)
        previous = json.loads((out / 'findings.json').read_text())['snapshot_id']
        old_dir = out / 'snapshots' / previous
        self.assertTrue(old_dir.is_dir())
        collect(spec, out)
        current = json.loads((out / 'findings.json').read_text())['snapshot_id']
        self.assertNotEqual(previous, current)
        self.assertFalse(old_dir.exists())
        self.assertTrue((out / '.trash' / previous).is_dir())
        self.assertTrue((out / 'snapshots' / current).is_dir())

    def test_live_tool_call_ids_join_steps_and_task_outcome(self):
        failed_run = 'samantha-run-failed'
        successful_run = 'samantha-run-success'
        rows = [
            {'run_id': failed_run, 'call_id': 'call-ok', 'kind': 'tool_start', 'tool': 'cua_act',
             'app': 'Calendar', 'ts': NOW},
            {'run_id': failed_run, 'call_id': 'call-ok', 'kind': 'tool_result', 'tool': 'cua_act',
             'app': 'Calendar', 'ok': True, 'ts': NOW + 0.25},
            {'run_id': failed_run, 'call_id': 'call-fail', 'kind': 'tool_start', 'tool': 'cua_act',
             'app': 'Calendar', 'ts': NOW + 1},
            {'run_id': failed_run, 'call_id': 'call-fail', 'kind': 'tool_error', 'tool': 'cua_act',
             'app': 'Calendar', 'error_type': 'TargetNotFound', 'error': 'target missed',
             'hint': 'check the visible page', 'ts': NOW + 1.5},
            {'run_id': failed_run, 'call_id': 'call-ledger', 'kind': 'tool_start', 'tool': 'ledger_task',
             'ledger_operation': 'lookup', 'query_terms': ['a query'], 'ts': NOW + 1.9},
            {'run_id': failed_run, 'kind': 'ledger_select', 'ledger_operation': 'lookup',
             'ledger_result_status': 'invalid_query_terms', 'ledger_decision_count': 0,
             'query_terms': ['a query'], 'invalid_query_terms': ['bad'], 'ts': NOW + 2},
            {'run_id': failed_run, 'kind': 'task_outcome', 'outcome': 'failure', 'verified': False,
             'evidence_class': 'tool_reported', 'completion': 'failed', 'check_id': 'task_outcome',
             'build': 'build-abc', 'ts': NOW + 3},
            {'run_id': successful_run, 'kind': 'task_outcome', 'outcome': 'success', 'ts': NOW + 4},
        ]
        api, _out = self.collect_rows(rows, 'samantha-live-shape')
        failed_ref = hashlib.sha256(failed_run.encode()).hexdigest()[:24]
        task = api.task('p', failed_ref)
        self.assertEqual(api.task('p', failed_run)['run_ref'], failed_ref)
        blob = json.dumps(task, ensure_ascii=False, separators=(',', ':'))
        self.assertLessEqual(len(blob.encode()), 4096)
        self.assertEqual(task['outcome']['observed'], 'failure')
        self.assertEqual(task['outcome']['check_id'], 'task_outcome')
        self.assertEqual(task['build']['id'], 'build-abc')
        self.assertEqual(task['ledger']['lookup_status'], 'invalid_query_terms')
        self.assertEqual(task['ledger']['decision_count'], 0)
        self.assertEqual(task['ledger']['decision_count'], 0)
        self.assertEqual(len(task['steps']), 3)
        success_step = next(step for step in task['steps'] if '250ms' in step['line'])
        self.assertEqual(success_step.get('status', 'success'), 'success')
        failed_step = task['first_failure']
        self.assertEqual(failed_step['error_type'], 'TargetNotFound')
        self.assertIn('check the visible page', blob)
        self.assertEqual(blob.count('target missed'), 1)
        self.assertTrue(any(gap['code'] == 'step_ref_missing' for gap in task['gaps']))
        listed = api.tasks('p')['tasks']
        stamps = [row['last_seen'] for row in listed]
        self.assertEqual(stamps, sorted(stamps, reverse=True))
        failed_rows = api.tasks('p', status='failure')['tasks']
        self.assertEqual(failed_rows[0]['run_ref'], failed_ref)
        self.assertEqual(failed_rows[0]['first_error_type'], 'TargetNotFound')


if __name__ == '__main__':
    unittest.main()
