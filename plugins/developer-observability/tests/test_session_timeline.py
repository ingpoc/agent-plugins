import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from backend.observability_api import ObservabilityAPI
from collector.analyze import build
from collector.sources import iter_jsonl, iter_textlog


class SessionTimelineTests(unittest.TestCase):
    def test_ledger_start_scope_is_task_linked_and_unknown_scope_is_dropped(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / 'activity.jsonl'
            rows = [
                {'ts':'2026-11-06T10:00:00Z', 'session_id':'s', 'kind':'tool_start',
                 'tool':'ledger_task', 'run_id':'abc12345', 'ledger_operation':'recall',
                 'ledger_scope_kind':'tool', 'ledger_rule_supplied':False},
                {'ts':'2026-11-06T10:00:01Z', 'session_id':'s', 'kind':'tool_start',
                 'tool':'ledger_task', 'run_id':'def12345', 'ledger_scope_kind':'PRIVATE_SITE'},
            ]
            source.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
            stats = {}
            (root / 'findings.json').write_text(json.dumps(build(list(iter_jsonl(source, 'a', 'activity', stats)), stats)))
            api = ObservabilityAPI(root, {'a': {'owners': ['owner:a']}}, None)
            ref = api.sessions('a')['sessions'][0]['session_ref']
            events = api.sessions('a', ref)['timeline']
            self.assertEqual(events[0]['ledger_scope_kind'], 'tool')
            self.assertIs(events[0]['ledger_rule_supplied'], False)
            self.assertNotIn('ledger_scope_kind', events[1])
            self.assertNotIn('PRIVATE_SITE', json.dumps(events))
            self.assertEqual(api.sessions('a', ref, run_ref=events[0]['run_ref'])['total'], 1)

    def test_only_explicit_startup_mirrors_are_collapsed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            activity, voice = root / 'activity.jsonl', root / 'voice.log'
            mirror = 'a' * 32
            activity.write_text(json.dumps({'ts':'2026-11-06T10:00:00.001Z', 'session_id':'s',
                'kind':'startup', 'phase':'audio_ready', 'step_ms':42, 'mirror_id':mirror,
                'input_availability':'available'}) + '\n')
            voice.write_text('2026-11-06T10:00:00.000Z [startup] phase=audio_ready status=ok '
                f'mirror_id={mirror} session_id=s step_ms=42\n'
                '2026-11-06T10:00:01.000Z [startup] phase=audio_ready status=ok session_id=s step_ms=42\n')
            stats = {}
            data = build(list(iter_jsonl(activity, 'a', 'activity', stats)) +
                         list(iter_textlog(voice, 'a', 'voice', stats)), stats)
            (root / 'findings.json').write_text(json.dumps(data))
            api = ObservabilityAPI(root, {'a': {'owners': ['owner:a']}}, None)
            ref = api.sessions('a')['sessions'][0]['session_ref']
            timeline = api.sessions('a', ref, include_diagnostics=True)['timeline']
            self.assertEqual(len(timeline), 2)
            self.assertEqual(timeline[0]['input_availability'], 'available')
            self.assertEqual(timeline[0]['mirrored_occurrences'], 1)
            self.assertEqual(timeline[0]['diagnostics']['mirrors'][0]['source'], 'voice')
            self.assertNotIn('mirrored_occurrences', timeline[1])

    def test_explicit_run_filter_and_microphone_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / 'activity.jsonl'
            rows = [
                {'ts':'2026-11-06T10:00:00Z', 'session_id':'s', 'kind':'startup', 'phase':'audio_ready',
                 'input_availability':'available', 'input_device':'Built-in microphone', 'input_rate':48000,
                 'input_recovery':'recovered', 'input_selection':'system_default_snapshot'},
                {'ts':'2026-11-06T10:00:01Z', 'session_id':'s', 'kind':'tool_result', 'tool':'ledger_task',
                 'run_id':'abc12345', 'ok':True, 'ledger_result_detail':'{"value":"concise"}', 'ledger_detail_truncated':False},
                {'ts':'2026-11-06T10:00:02Z', 'session_id':'s', 'kind':'task_outcome', 'run_id':'abc12345',
                 'outcome':'unverified', 'evidence_class':'tool_reported', 'check_id':'calculator'},
                {'ts':'2026-11-06T10:00:03Z', 'session_id':'s', 'kind':'tool_result', 'tool':'cua_act', 'run_id':'def12345', 'ok':True},
                {'ts':'2026-11-06T10:00:04Z', 'session_id':'s', 'kind':'tool_result', 'tool':'cua_act', 'ok':True},
                {'ts':'2026-11-06T10:00:05Z', 'session_id':'s', 'event':'island_layout', 'trigger':'settled',
                 'transition_id':'12345678-1234-1234-1234-123456789abc', 'transition_phase':'settled', 'transition_trigger':'overlay_show',
                 'screen':{'x':100,'y':200,'w':1000,'h':800}, 'island':{'x':500,'y':200,'w':100,'h':30,'visible':True}, 'overlays':[]},
            ]
            source.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
            stats = {}
            data = build(list(iter_jsonl(source, 'a', 'activity', stats)), stats)
            (root / 'findings.json').write_text(json.dumps(data))
            api = ObservabilityAPI(root, {'a': {'owners': ['owner:a']}}, None)
            ref = api.sessions('a')['sessions'][0]['session_ref']
            events = api.sessions('a', ref)['timeline']
            self.assertEqual(events[0]['input_selection'], 'system_default_snapshot')
            self.assertEqual(events[0]['input_recovery'], 'recovered')
            self.assertEqual(events[5]['layout']['island']['x'], 400)
            self.assertEqual(events[5]['layout']['transition_phase'], 'settled')
            self.assertNotIn('x', events[5]['layout']['screen'])
            run = events[1]['run_ref']
            page = api.sessions('a', ref, run_ref=run, limit=1)
            self.assertEqual(page['total'], 2)
            self.assertFalse(page['timeline'][0]['ledger_detail_truncated'])
            self.assertNotIn('diagnostics', page['timeline'][0])
            second = api.sessions('a', ref, run_ref=run, limit=1, cursor=page['next_cursor'])
            self.assertEqual(second['timeline'][0]['task_evidence_class'], 'tool_reported')
            self.assertEqual(api.sessions('a', ref, run_ref=run, summary_only=True)['summary']['independent_task_result'], 'unknown')
            self.assertIn('concise', api.sessions('a', ref, run_ref=run, include_diagnostics=True)['timeline'][0]['diagnostics']['ledger_result_detail'])
            with self.assertRaises(ValueError):
                api.sessions('a', ref, run_ref=events[3]['run_ref'], cursor=page['next_cursor'])

    def test_explicit_session_timeline_is_scoped_and_privacy_safe(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / 'activity.jsonl'
            rows = [
                {'ts':'2026-11-06T10:00:00Z', 'session_id':'private-session', 'kind':'startup',
                 'phase':'audio_ready', 'step_ms':250, 'version':'0.2.94', 'detail':'private input'},
                {'ts':'2026-11-06T10:00:01Z', 'session_id':'private-session', 'kind':'tool_result',
                 'tool':'ledger_task', 'ledger_operation':'recall', 'ledger_result_status':'found',
                 'ledger_scope_kind':'browser', 'ledger_rule_supplied':False,
                 'ledger_decision_count':1, 'ledger_result_detail':'{"status":"found","decisions":[{"summary":"Use concise answers"}]}',
                 'call_id':'private-call', 'response_id':'private-response', 'ok':True},
                {'ts':'2026-11-06T10:00:02Z', 'session_id':'private-session', 'kind':'task_outcome',
                 'check_id':'browser_first_request', 'outcome':'verified_success', 'evidence_class':'browser_inventory',
                 'run_id':'a1234567', 'response_id':'resp_private', 'tools':['comet_act']},
                {'ts':'2026-11-06T10:00:03Z', 'session_id':'private-session', 'kind':'tool_error',
                 'tool':'cua_act', 'error_type':'RuntimeError', 'error':'fixture failure',
                 'error_details':'Traceback (most recent call last):\n  File "fixture.py", line 4\nRuntimeError: Bearer synthetic-secret-value',
                 'call_id':'private-call', 'response_id':'private-response', 'ok':False},
                {'ts':'2026-11-06T10:00:02Z', 'kind':'startup', 'phase':'audio_ready', 'step_ms':999},
            ]
            source.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')
            stats = {}
            data = build(list(iter_jsonl(source, 'a', 'activity', stats)), stats)
            (root / 'findings.json').write_text(json.dumps(data))
            api = ObservabilityAPI(root, {'a':{'owners':['owner:a']}, 'b':{'owners':['owner:b']}}, None)
            listing = api.sessions('a')['sessions']
            self.assertEqual(len(listing), 1)
            ref = listing[0]['session_ref']
            result = api.sessions('a', ref)
            self.assertEqual([e['stage'] for e in result['timeline']], ['audio_ready', 'ledger_task', 'task_outcome', 'cua_act'])
            self.assertEqual(result['timeline'][3]['outcome'], 'failure')
            self.assertEqual(result['timeline'][0]['duration_ms'], 250)
            self.assertEqual(result['timeline'][1]['ledger_operation'], 'recall')
            self.assertEqual(result['timeline'][1]['ledger_scope_kind'], 'browser')
            self.assertIs(result['timeline'][1]['ledger_rule_supplied'], False)
            self.assertEqual(result['timeline'][1]['ledger_result_status'], 'found')
            self.assertEqual(result['timeline'][1]['ledger_decision_count'], 1)
            self.assertTrue(result['timeline'][1]['diagnostics_available'])
            self.assertEqual(result['timeline'][2]['task_outcome'], 'verified_success')
            self.assertEqual(result['timeline'][2]['task_tools'], ['comet_act'])
            self.assertRegex(result['timeline'][2]['run_ref'], r'^[0-9a-f]{24}$')
            self.assertRegex(result['timeline'][2]['response_ref'], r'^[0-9a-f]{24}$')
            self.assertNotEqual(result['timeline'][2]['response_ref'], 'resp_private')
            self.assertIn('diagnostics', api.sessions('a', ref, include_diagnostics=True)['timeline'][1])
            issue_id = data['findings'][0]['finding_key']
            default_issue = api.inspect('a', issue_id)
            detail_issue = api.inspect('a', issue_id, include_diagnostics=True)
            default_event = default_issue['evidence']['timeline'][0]
            detail_event = detail_issue['evidence']['timeline'][0]
            self.assertTrue(default_event['diagnostics_available'])
            self.assertNotIn('diagnostics', default_event)
            self.assertIn('Traceback', detail_event['diagnostics']['error_details'])
            self.assertNotIn('synthetic-secret-value', json.dumps(detail_issue))
            serialized = json.dumps(result)
            for private in ('private-session', 'private-call', 'private-response', 'private input', 'Use concise answers', 'resp_private'):
                self.assertNotIn(private, serialized)
            with self.assertRaises(ValueError):
                api.sessions('b', ref)
            with self.assertRaises(ValueError):
                api.sessions('a', 'private-session')

    def test_build_identity_and_ledger_trail_are_attributed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / 'activity.jsonl'
            label = '0.2.121+1a2b3c4d5e6f'
            rows = [
                {'ts': '2026-11-06T10:00:00Z', 'session_id': 's', 'kind': 'comet_begin',
                 'run_id': 'run-abc', 'build': label, 'ok': True},
                {'ts': '2026-11-06T10:00:00.500Z', 'session_id': 's', 'kind': 'ledger_select',
                 'run_id': 'run-abc', 'build': label, 'ledger_rule': 'browser_preference',
                 'ledger_selected_count': 1, 'ledger_selected_id': 'decision-private-id'},
                {'ts': '2026-11-06T10:00:01Z', 'session_id': 's', 'kind': 'ledger_save_receipt',
                 'run_id': 'run-abc', 'build': label, 'decision_id': 'decision-private-id2',
                 'revision': 4, 'supersedes': 'decision-old-id'},
                {'ts': '2026-11-06T10:00:02Z', 'session_id': 's', 'kind': 'comet_act',
                 'run_id': 'run-abc', 'build': label, 'ok': False, 'duration_ms': 1700},
                {'ts': '2026-11-06T10:00:03Z', 'session_id': 's', 'kind': 'comet_end',
                 'run_id': 'run-abc', 'build': label, 'ok': True},
                {'ts': '2026-11-06T10:00:04Z', 'session_id': 's', 'kind': 'comet_act',
                 'run_id': 'run-other', 'ok': True},
            ]
            source.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
            stats = {}
            data = build(list(iter_jsonl(source, 'a', 'activity', stats)), stats)
            (root / 'findings.json').write_text(json.dumps(data))
            api = ObservabilityAPI(root, {'a': {'owners': ['owner:a']}}, None)
            ref = api.sessions('a')['sessions'][0]['session_ref']
            timeline = api.sessions('a', ref)['timeline']
            self.assertEqual([event['stage'] for event in timeline],
                             ['comet_begin', 'ledger_select', 'ledger_save_receipt', 'comet_act', 'comet_end',
                              'comet_act'])
            self.assertTrue(all(event['build'] == label for event in timeline[:5]))
            self.assertEqual(timeline[1]['ledger_rule'], 'browser_preference')
            self.assertEqual(timeline[1]['ledger_selected_count'], 1)
            self.assertRegex(timeline[1]['ledger_selected_id'], r'^[0-9a-f]{24}$')
            self.assertEqual(timeline[2]['ledger_revision'], 4)
            self.assertRegex(timeline[2]['ledger_decision_id'], r'^[0-9a-f]{24}$')
            self.assertRegex(timeline[2]['ledger_supersedes'], r'^[0-9a-f]{24}$')
            self.assertNotIn('decision-private-id', json.dumps(timeline))
            run = timeline[0]['run_ref']
            summary = api.sessions('a', ref, run_ref=run, summary_only=True)['summary']
            self.assertEqual(summary['builds'], [label])
            self.assertEqual(summary['build_attribution'], 'event')
            self.assertEqual(summary['ledger_trail_counts'],
                             {'ledger_select': 1, 'ledger_save_receipt': 1, 'ledger_supersede': 0})
            self.assertEqual(summary['first_failure'], {'stage': 'comet_act', 'timestamp': timeline[3]['timestamp']})
            identity = summary['task_identity']
            self.assertEqual(identity['run_ref'], run)
            self.assertEqual(identity['session_ref'], ref)
            self.assertEqual(identity['builds'], [label])
            self.assertEqual(identity['duration_ms'], 3000)
            self.assertEqual(summary['independent_task_result'], 'unknown')
            completeness = summary['evidence_completeness']
            self.assertEqual(completeness['event_count'], 5)
            self.assertEqual(completeness['events_with_duration'], 1)
            self.assertEqual(completeness['outcomes']['failure'], 1)
            self.assertEqual(completeness['independent_task_result'], 'unknown')
            unknown = api.sessions('a', ref, run_ref=timeline[5]['run_ref'], summary_only=True)['summary']
            self.assertEqual(unknown['builds'], [])
            self.assertEqual(unknown['build_attribution'], 'unknown')

    def test_long_session_can_be_paged_without_losing_native_actions(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / 'activity.jsonl'
            start = datetime(2026, 11, 6, tzinfo=timezone.utc)
            rows = [{'ts': (start + timedelta(seconds=i)).isoformat(), 'session_id': 'one',
                     'kind': 'tool_error' if i == 70 else 'tool_result', 'tool': 'cua_act',
                     'ok': i != 70, 'error': 'failed' if i == 70 else None} for i in range(75)]
            source.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
            stats = {}
            data = build(list(iter_jsonl(source, 'a', 'activity', stats)), stats)
            (root / 'findings.json').write_text(json.dumps(data))
            api = ObservabilityAPI(root, {'a': {'owners': ['owner:a']}}, None)
            ref = api.sessions('a')['sessions'][0]['session_ref']
            first = api.sessions('a', ref, limit=50)
            second = api.sessions('a', ref, cursor=first['next_cursor'], limit=50)
            events = first['timeline'] + second['timeline']
            self.assertEqual(len(events), 75)
            self.assertFalse(first['truncated'])
            self.assertIsNone(second['next_cursor'])
            self.assertEqual(events[70]['outcome'], 'failure')
            self.assertEqual(events[70]['stage'], 'cua_act')
            summary = api.sessions('a', ref, summary_only=True)
            self.assertEqual(summary['summary']['native_event_outcomes'], {'success':74, 'failure':1})
            self.assertEqual(summary['summary']['failure_events_by_stage'], {'cua_act':1})
            self.assertNotIn('timeline', summary)
            self.assertNotIn('coverage_details', summary)
            with self.assertRaises(ValueError):
                api.sessions('a', ref, cursor=first['next_cursor'], summary_only=True)
            with self.assertRaises(ValueError):
                api.sessions('a', ref, cursor=first['next_cursor'], include_diagnostics=True)


if __name__ == '__main__':
    unittest.main()
