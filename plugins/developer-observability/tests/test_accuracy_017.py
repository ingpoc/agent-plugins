"""0.1.7 accuracy: root-cause rollup, duplicate vs retry, failure ownership, stable ids, slow turns, limit, island layout."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime'))
from backend.observability_api import NEXT_STEPS, ObservabilityAPI  # noqa: E402
from collector.cli import collect  # noqa: E402

NOW = 1793800000.0
T = NOW - 600


def ledger_turn(session='s1', start=T):
    """The replayed ca44bfcb ledger turn: three different ledger_task errors, the helper error, one mirror, a 17.4 s turn."""
    return [
        {'kind': 'voice_latency', 'session_id': session, 'turn_id': 26, 'speech_to_playback_ms': 2950, 'ts': start - 5},
        {'kind': 'tool_start', 'session_id': session, 'tool': 'ledger_task', 'ts': start},
        {'kind': 'tool_error', 'session_id': session, 'tool': 'ledger_task', 'ok': False, 'error_type': 'act_miss',
         'error': 'Owner decision not found in recent user statements', 'ts': start + 0.001},
        {'kind': 'island_error_raw', 'session_id': session, 'raw': 'Failed', 'ts': start + 0.002},
        {'kind': 'tool_start', 'session_id': session, 'tool': 'ledger_task', 'ts': start + 4.2},
        {'kind': 'tool_error', 'session_id': session, 'tool': 'ledger_task', 'ok': False, 'error_type': 'act_miss',
         'error': 'Capture requires an explicit project scope', 'ts': start + 4.201},
        {'kind': 'tool_start', 'session_id': session, 'tool': 'ledger_task', 'ts': start + 8.1},
        {'kind': 'ledger_helper_error', 'session_id': session, 'stage': 'lookup', 'error_class': 'LedgerUnavailable', 'ts': start + 9.0},
        {'kind': 'tool_error', 'session_id': session, 'tool': 'ledger_task', 'ok': False, 'error_type': 'act_miss',
         'error': 'Context Ledger helper unavailable; nothing was saved', 'ts': start + 9.001},
        {'kind': 'voice_latency', 'session_id': session, 'turn_id': 28, 'speech_to_playback_ms': 17411, 'ts': start + 13.6},
    ]


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='devobs-017-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.runs = 0

    def collect(self, rows, now=NOW, raw_lines=()):
        self.runs += 1
        log = self.tmp / f'activity{self.runs}.jsonl'
        log.write_text(''.join(json.dumps(r) + '\n' for r in rows) + ''.join(line + '\n' for line in raw_lines))
        out = self.tmp / f'out{self.runs}'
        collect([{'type': 'jsonl', 'path': str(log), 'project': 'voice', 'source': 'voice:activity'}], out, now)
        return json.loads((out / 'findings.json').read_text())['findings'], out

    @staticmethod
    def ledger(findings):
        return [f for f in findings if f['kind'] in ('failure', 'redundancy') and 'ledger' in f['headline']]


class RollupTests(Case):
    def test_1_one_ledger_save_failure_is_one_root_cause_finding(self):
        findings, _ = self.collect(ledger_turn())
        hits = self.ledger(findings)
        self.assertEqual(len(hits), 1, [f['headline'] for f in hits])
        root = hits[0]
        self.assertEqual(root['kind'], 'failure')
        self.assertIn('ledger_helper_error/lookup', root['headline'])
        self.assertEqual(root['failed_attempt_count'], 1)
        effects = root['effects']
        self.assertEqual((effects['count'], effects['incidents'], effects['calls'], effects['retries']), (3, 1, 3, 2))
        self.assertEqual(effects['kinds'], {'tool_error/ledger_task': 3})
        self.assertFalse([f for f in findings if 'island_error_raw' in f['headline']])

    def test_1_rollup_needs_same_session_turn_and_window(self):
        rows = ledger_turn('s1') + ledger_turn('s2', T + 120)
        far = [dict(r) for r in ledger_turn('s3', T + 300)]
        for r in far:
            if r['kind'] in ('ledger_helper_error',) or (r['kind'] == 'tool_error' and 'helper' in r.get('error', '')):
                r['ts'] += 60  # beyond the 30 s rollup window
        findings, _ = self.collect(rows + far)
        root = next(f for f in findings if 'ledger_helper_error/lookup' in f['headline'])
        self.assertEqual(root['failed_attempt_count'], 3)
        self.assertEqual(root['effects']['incidents'], 3)
        # s3: the two early errors are more than 30 s before the helper error, so they stand on their own.
        alone = [f for f in findings if f['kind'] == 'failure' and f is not root and 'ledger_task' in f['headline']]
        self.assertTrue(alone, [f['headline'] for f in findings])
        self.assertTrue(all(f['failed_attempt_count'] == 1 for f in alone))

    def test_1_same_signature_repeats_are_not_rolled_up(self):
        rows = [{'kind': 'tool_error', 'session_id': 's', 'tool': 'cua_act', 'error': 'element not found 12', 'ts': T + i}
                for i in range(3)]
        findings, _ = self.collect(rows)
        hit = [f for f in findings if f['kind'] == 'failure']
        self.assertEqual(len(hit), 1)
        self.assertNotIn('effects', hit[0])


class DuplicateRetryTests(Case):
    def calls(self, session, args_list, start):
        rows = []
        for i, args in enumerate(args_list):
            rows.append({'kind': 'tool_start', 'session_id': session, 'tool': 'comet_act', 'ts': start + i * 2})
            if args is not None:
                rows.append({'kind': 'comet_call', 'session_id': session, 'tool': 'comet_act', 'ts': start + i * 2 + 0.001, **args})
        return rows

    def test_2_duplicate_only_when_tool_and_normalized_args_match(self):
        same = self.calls('d', [{'actions': ['click'], 'url': 'https://a.example/x'},
                                {'actions': ['click'], 'url': 'https://a.example/x '},
                                {'actions': ['CLICK'], 'url': 'https://a.example/x'}], T)
        different = self.calls('r', [{'actions': ['click'], 'url': 'https://a.example/1'},
                                     {'actions': ['click'], 'url': 'https://a.example/2'},
                                     {'actions': ['type'], 'url': 'https://a.example/2'}], T + 100)
        findings, _ = self.collect(same + different)
        red = {f['redundancy']['pattern']: f for f in findings if f['kind'] == 'redundancy'}
        self.assertEqual(red['duplicate_call']['redundancy']['occurrences'], 2, red)
        self.assertEqual(red['retry']['redundancy']['occurrences'], 2, red)
        self.assertIn('retr', red['retry']['headline'])

    def test_2_unknown_arguments_are_a_retry_not_a_duplicate(self):
        findings, _ = self.collect(self.calls('u', [None, None, None], T))
        patterns = [f['redundancy']['pattern'] for f in findings if f['kind'] == 'redundancy']
        self.assertEqual(patterns, ['retry'])


class OwnershipTests(Case):
    def test_3_app_errors_are_app_error_and_parse_errors_collector_failure(self):
        findings, _ = self.collect(ledger_turn(), raw_lines=['{not json', '[1, 2]'])
        classes = {f['error_class'] for f in findings if f['kind'] == 'failure'}
        self.assertIn('app_error', classes)
        own = [f for f in findings if f['error_class'] == 'collector_failure']
        self.assertEqual(len(own), 1, [f['headline'] for f in findings])
        self.assertEqual(own[0]['failed_attempt_count'], 2)
        self.assertIn('voice:activity', own[0]['headline'])
        app = [f for f in findings if f['kind'] == 'failure' and f['error_class'] != 'collector_failure']
        self.assertTrue(all(f['error_class'] in ('app_error', 'environment_condition') for f in app))

    def test_3_missing_source_is_a_collector_io_failure(self):
        out = self.tmp / 'missing-out'
        collect([{'type': 'jsonl', 'path': str(self.tmp / 'nope.jsonl'), 'project': 'voice', 'source': 'voice:nope'}], out, NOW)
        findings = json.loads((out / 'findings.json').read_text())['findings']
        self.assertEqual([f['error_class'] for f in findings], ['collector_failure'])


class StableIdTests(Case):
    def test_4_ids_identical_across_two_runs_and_growing_counts(self):
        rows = ledger_turn() + DuplicateRetryTests.calls(None, 'd', [{'url': 'u'}] * 3, T + 50)
        first, _ = self.collect(rows, NOW)
        second, _ = self.collect(rows, NOW + 3600)
        self.assertEqual(sorted(f['finding_key'] for f in first), sorted(f['finding_key'] for f in second))
        grown, _ = self.collect(rows + ledger_turn('s9', T + 200), NOW)
        self.assertTrue({f['finding_key'] for f in first if f['kind'] != 'turn_latency'}
                        <= {f['finding_key'] for f in grown})
        for f in first:
            self.assertTrue(f['finding_key'].startswith('col-'))


class TurnLatencyTests(Case):
    def test_5_slowest_turns_per_session(self):
        fast = [{'kind': 'voice_latency', 'session_id': 'f', 'turn_id': i, 'speech_to_playback_ms': 2000, 'ts': T + 300 + i}
                for i in range(4)]
        findings, _ = self.collect(ledger_turn() + fast)
        turns = [f for f in findings if f['kind'] == 'turn_latency']
        self.assertEqual(len(turns), 1, [f['headline'] for f in turns])
        slow = turns[0]
        self.assertIn('17.4 s', slow['headline'])
        info = slow['turn_latency']
        self.assertEqual((info['session'], info['max_ms'], info['slow_turns']), ('s1', 17411, 1))
        self.assertEqual(info['turns'][0]['turn'], '28')
        self.assertIn('ledger_task', info['turns'][0]['tools'])
        self.assertEqual(info['turns'][0]['errors'], 4)
        self.assertIsNotNone(info['turns'][0]['timestamp'])
        self.assertEqual(info['turns'][0]['metric'], 'speech_to_playback_ms')


class LimitTests(Case):
    def test_6_limit_up_to_100_and_clamped_above(self):
        rows = [{'kind': 'tool_error', 'session_id': f's{i}', 'tool': f'tool{i}', 'error': 'broken', 'ts': T + i}
                for i in range(120)]
        _, out = self.collect(rows)
        api = ObservabilityAPI(self.tmp / 'runtime', {'voice': {'owners': ['owner:voice']}}, None, collected=out)
        self.assertEqual(len(api.issues('voice', limit=50)['issues']), 50)
        self.assertNotIn('limit_note', api.issues('voice', limit=50))
        self.assertEqual(len(api.issues('voice', limit=100)['issues']), 100)
        clamped = api.issues('voice', limit=150)
        self.assertEqual((len(clamped['issues']), clamped['limit']), (100, 100))
        self.assertIn('100', clamped['limit_note'])
        for bad in (0, -1, '5', True):
            with self.assertRaisesRegex(ValueError, r'^invalid_limit: .*1.100'):
                api.issues('voice', limit=bad)
        self.assertIn('100', NEXT_STEPS['invalid_limit'])
        self.assertNotIn('Decider', NEXT_STEPS['invalid_limit'])


def layout(overlays, island=None, screen=None, trigger='overlay_show', ts=T, status=None, session_id='L'):
    row = {'ts': ts, 'event': 'island_layout', 'schema': 1, 'session_id': session_id, 'turn_id': 3, 'version': '0.2.85',
            'trigger': trigger, 'screen': screen or {'id': 'main', 'x': 0, 'y': 0, 'w': 1512, 'h': 982, 'scale': 2},
            'island': island or {'x': 656, 'y': 0, 'w': 200, 'h': 40, 'visible': True}, 'overlays': overlays}
    if status is not None:
        row['status'] = status
    return row


def overlay(kind, x, y, w, h, visible=True, inside=True):
    return {'kind': kind, 'id': f'{kind}-1', 'x': x, 'y': y, 'w': w, 'h': h, 'visible': visible, 'expected_inside_island': inside}


class IslandLayoutTests(Case):
    def test_layout_evidence_is_bounded_to_ten_most_recent(self):
        rows = [layout([overlay('permission_card', 640, 4, 100, 30)], ts=T + i) for i in range(12)]
        findings, _ = self.collect(rows)
        card = next(f for f in findings if f['kind'] == 'island_layout')
        occurrences = card['island_layout']['occurrences']
        self.assertEqual(len(occurrences), 10)
        self.assertEqual(occurrences[0]['overflow_pt'], 16.0)
        self.assertGreater(occurrences[0]['timestamp'], occurrences[-1]['timestamp'])

    def test_7_island_layout_violations_from_synthetic_events(self):
        rows = [
            layout([overlay('status_pill', 660, 4, 100, 30)]),                                   # inside: fine
            layout([overlay('permission_card', 646, 4, 100, 30)], trigger='overlay_move', ts=T + 1),  # 10 pt left of island
            layout([overlay('permission_card', 654.5, 4, 100, 30)], ts=T + 2),                    # 1.5 pt: within tolerance
            layout([overlay('toast', 0, 500, 80, 30, inside=False)], ts=T + 3),                  # not expected inside: fine
            layout([overlay('ghost', 2000, 0, 50, 50, visible=False)], ts=T + 4),                 # hidden: ignored
            layout([], island={'x': 1480, 'y': 0, 'w': 200, 'h': 40, 'visible': True}, trigger='move', ts=T + 5),  # off screen
            layout([overlay('permission_card', 640, 4, 100, 30)], trigger='overlay_show', ts=T + 6),  # 16 pt left
        ]
        findings, _ = self.collect(rows)
        found = {(f['island_layout']['overlay_kind'], f['island_layout']['violation']): f
                 for f in findings if f['kind'] == 'island_layout'}
        self.assertEqual(set(found), {('permission_card', 'outside_island'), ('island', 'off_screen')}, list(found))
        card = found[('permission_card', 'outside_island')]
        self.assertEqual(card['failed_attempt_count'], 2)
        self.assertEqual(card['island_layout']['max_overflow_pt'], 16.0)
        self.assertEqual(card['island_layout']['triggers'], {'overlay_move': 1, 'overlay_show': 1})
        latest = card['island_layout']['occurrences'][0]
        self.assertEqual(latest['overflow_pt'], 16.0)
        self.assertEqual(latest['version'], '0.2.85')
        self.assertEqual(latest['bounds'], {'w': 200.0, 'h': 40.0})
        self.assertEqual(latest['rect'], {'x': -16.0, 'y': 4.0, 'w': 100.0, 'h': 30.0})
        self.assertNotIn('id', latest['rect'])
        self.assertEqual(found[('island', 'off_screen')]['island_layout']['max_overflow_pt'], 168.0)
        again, _ = self.collect(rows[:2])
        self.assertEqual(next(f['finding_key'] for f in again if f['kind'] == 'island_layout'), card['finding_key'])
        api = ObservabilityAPI(self.tmp / 'runtime', {'voice': {'owners': ['owner:voice']}}, None, collected=self.tmp / 'out1')
        page = api.issues('voice', kind='island_layout')
        self.assertEqual(page['total'], 2)
        self.assertEqual({i['island_layout']['violation'] for i in page['issues']}, {'outside_island', 'off_screen'})
        self.assertEqual(api.issues('voice', kind='turn_latency')['total'], 0)

    def test_error_state_hud_is_not_a_layout_failure(self):
        # Overflowing error HUD must not become island_layout or a standalone "island_layout failed Nx".
        overflow = overlay('status_pill', 640, 4, 100, 30)
        rows = [
            {'kind': 'mic_preflight', 'session_id': 'voice1', 'status': 'error', 'error': "Can't hear you, check mic",
             'ts': T, 'version': '0.2.85'},
            layout([overflow], trigger='settled', ts=T + 1, status='error', session_id='supervisor'),
            layout([overflow], trigger='settled', ts=T + 2, status='error', session_id='supervisor'),
        ]
        findings, _ = self.collect(rows)
        layout_hits = [f for f in findings if f['kind'] == 'island_layout']
        self.assertEqual(layout_hits, [], [f['headline'] for f in layout_hits])
        standalone = [f for f in findings if f['kind'] == 'failure' and 'island_layout' in f['headline']]
        self.assertEqual(standalone, [], [f['headline'] for f in standalone])
        mic = next(f for f in findings if f['kind'] == 'failure' and 'mic_preflight' in f['headline'])
        self.assertEqual(mic['failed_attempt_count'], 1)
        self.assertIn("Can't hear you, check mic", mic['latest_evidence']['first_error_line'])
        self.assertEqual(mic['latest_evidence']['error_type'], 'mic_preflight')
        self.assertEqual(mic['island_error_snapshots']['count'], 2)
        self.assertEqual(mic['island_error_snapshots']['status'], 'error')

    def test_error_state_hud_does_not_rank_above_real_errors(self):
        rows = [
            {'kind': 'tool_error', 'session_id': 'voice1', 'tool': 'cua_act', 'error': 'Something went wrong',
             'ts': T, 'version': '0.2.85'},
        ] + [layout([overlay('status_pill', 640, 4, 100, 30)], ts=T + i, status='error', session_id='sup')
             for i in range(8)]
        findings, _ = self.collect(rows)
        failures = [f for f in findings if f['kind'] == 'failure']
        self.assertEqual(len(failures), 1, [f['headline'] for f in failures])
        self.assertIn('cua_act', failures[0]['headline'])
        self.assertNotIn('island_layout', failures[0]['headline'])
        self.assertEqual(failures[0]['island_error_snapshots']['count'], 8)



if __name__ == '__main__':
    unittest.main()
