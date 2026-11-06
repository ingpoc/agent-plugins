import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from backend.observability_api import ObservabilityAPI
from collector.analyze import build
from collector.sources import iter_jsonl


class SessionTimelineTests(unittest.TestCase):
    def test_explicit_session_timeline_is_scoped_and_privacy_safe(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / 'activity.jsonl'
            rows = [
                {'ts':'2026-11-06T10:00:00Z', 'session_id':'private-session', 'kind':'startup',
                 'phase':'audio_ready', 'step_ms':250, 'version':'0.2.94', 'detail':'private input'},
                {'ts':'2026-11-06T10:00:01Z', 'session_id':'private-session', 'kind':'tool_result',
                 'tool':'ledger_task', 'ledger_operation':'recall', 'ledger_result_status':'found',
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
