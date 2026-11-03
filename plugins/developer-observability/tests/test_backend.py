import json
from pathlib import Path
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from backend.observability_api import NO_FINDINGS_HINT, ObservabilityAPI  # noqa: E402


class APITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.decider = self.root/'missing-decider.py'
        self.api = ObservabilityAPI(self.root, {'a': {'owners': ['owner:a']}, 'b': {'owners': ['owner:b']},
                                             'ledger': {'recommendation_target': 'context-ledger'}}, self.decider)
        self.data = {'generated_at': datetime.now(timezone.utc).isoformat(), 'coverage': {'codex': 'partial'},
                     'findings': [{'finding_key': f'id{i}', 'owner_ref': 'owner:a', 'headline': 'build failed',
                                   'status': 'active', 'evidence_event_ids': ['private-session-id'] * 12,
                                   'latest_evidence': {'first_error_line': 'token=sk-abcdefghijklmnopqrstuvwxyz',
                                                       'command': '/Users/test/private --password=hidden'},
                                   'raw_contents': 'NEVER_RETURN'} for i in range(3)] + [
                                  {'finding_key': 'other', 'owner_ref': 'owner:b'}]}
        self.write()

    def write(self):
        (self.root/'findings.json').write_text(json.dumps(self.data))

    def test_scoped_listing_pagination_and_cross_project_rejection(self):
        result = self.api.issues('a', limit=1)
        self.assertEqual(result['total'], 3)
        cursor = result['next_cursor']
        self.assertEqual(len(self.api.issues('a', cursor=cursor, limit=1)['issues']), 1)
        for kwargs in ({'project_id': 'b', 'cursor': cursor}, {'project_id': 'a', 'status': 'active', 'cursor': cursor}):
            with self.assertRaises(ValueError):
                self.api.issues(**kwargs)
        with self.assertRaises(ValueError):
            self.api.inspect('b', 'id0')
        with self.assertRaises(ValueError):
            self.api.issues('unknown')
        self.data['findings'].append({'finding_key': 'new'})
        self.write()
        with self.assertRaises(ValueError):
            self.api.issues('a', cursor=cursor)

    def test_redacted_bounded_evidence_no_raw_or_session_ids(self):
        result = self.api.inspect('a', 'id0')
        serialized = json.dumps(result)
        for secret in ['NEVER_RETURN', 'sk-abcdefghijklmnopqrstuvwxyz', 'private-session-id', 'hidden']:
            self.assertNotIn(secret, serialized)
        self.assertEqual(len(result['evidence']['event_refs']), 10)
        self.assertIsNotNone(result['next_cursor'])
        self.assertEqual(result['acceptance_status'], 'not_assessed')
        with self.assertRaises(ValueError):
            self.api.inspect('a', 'id0', limit=11)

    def test_inspect_projects_timestamped_occurrences_and_safe_connection_diagnostics(self):
        finding = self.data['findings'][0]
        event_at = '2026-10-24T11:30:00Z'
        finding.update({
            'first_seen': event_at,
            'failure_attempt_times': [event_at, '2026-10-24T11:35:00Z'],
            'failure_attempt_times_complete': True,
            'event_timeline_total': 1,
            'event_timeline_truncated': False,
            'event_timeline': [{
                'event_id': 'private-event-id', 'event_type': 'attempt_failed', 'event_at': event_at,
                'received_at': '2026-10-24T11:30:01Z', 'attempt_id': 'private-attempt-id',
                'invocation_id': 'private-invocation-id', 'outcome': 'failure',
                'safe_evidence': {'error_type': 'websocket', 'session_ref': 'sref_' + 'a' * 32,
                    'connection_diagnostics': {
                    'close_code': 1006, 'close_frame_received': False,
                    'last_server_event_type': 'session.updated', 'reason': 'PRIVATE-REASON'}}}],
            'mac_investigation': {'owner_locator': 'plugins/agent-computer-use'},
        })
        finding['event_timeline'].extend({
            'event_id': f'event-{index}', 'event_type': 'attempt_failed', 'event_at': event_at,
            'received_at': event_at, 'attempt_id': f'attempt-{index}', 'invocation_id': f'invocation-{index}',
            'outcome': 'failure', 'safe_evidence': {},
        } for index in range(64))
        finding['event_timeline'][1]['safe_evidence'] = {'session_ref': 'private-session-id'}
        finding['event_timeline_total'] = 65
        finding['event_timeline_truncated'] = True
        self.write()
        result = self.api.inspect('a', 'id0')
        self.assertEqual(result['owner']['owner_locator'], 'plugins/agent-computer-use')
        self.assertEqual(result['evidence']['occurrences']['failure_attempt_times'],
                         ['2026-10-24T11:30:00.000000Z', '2026-10-24T11:35:00.000000Z'])
        self.assertTrue(result['evidence']['occurrences']['failure_times_complete'])
        self.assertEqual(result['evidence']['timeline'][0]['connection_diagnostics']['close_code'], 1006)
        self.assertEqual(result['evidence']['timeline'][0]['evidence']['session_ref'], 'sref_' + 'a' * 32)
        self.assertEqual((len(result['evidence']['timeline']), result['evidence']['timeline_total'],
                          result['evidence']['timeline_truncated']), (64, 65, True))
        serialized = json.dumps(result)
        for private in ('private-event-id', 'private-attempt-id', 'private-invocation-id',
                        'private-session-id', 'PRIVATE-REASON'):
            self.assertNotIn(private, serialized)

    def test_projects_expose_source_health_and_scope_of_drop_counts(self):
        self.data.update({
            'coverage_details': {'samantha': {
                'status': 'partial', 'collector_heartbeat_at': '2026-10-24T11:40:00Z',
                'last_event_at': '2026-10-24T11:35:00Z', 'last_received_at': '2026-10-24T11:35:01Z',
                'dropped_rows': 'unknown'}},
            'coverage_gap_scope': 'Dropped-row detail currently comes from Cursor; other sources are unknown.',
        })
        self.write()
        result = self.api.projects()
        self.assertEqual(result['coverage_details']['samantha']['last_event_at'], '2026-10-24T11:35:00.000000Z')
        self.assertEqual(result['coverage_details']['samantha']['dropped_rows'], 'unknown')
        self.assertIn('other sources are unknown', result['coverage_gap_scope'])

    def test_invalid_cursor_limits(self):
        for cursor in ['garbage', 'e30=', 'WzFd']:
            with self.assertRaises(ValueError):
                self.api.issues('a', cursor=cursor)
        for limit in [0, 26, True]:
            with self.assertRaises(ValueError):
                self.api.issues('a', limit=limit)

    def test_read_no_writes_or_model_and_freshness(self):
        path = self.root/'findings.json'
        before = path.read_bytes()
        with patch('backend.recommendations.choose', side_effect=AssertionError('no model')):
            self.assertEqual(self.api.projects()['unassigned_issue_count'], 0)
            self.api.issues('a')
            self.api.inspect('a', 'id0')
            self.assertEqual(self.api.recommendation('ledger')['recommendations'], [])
        self.assertEqual(before, path.read_bytes())
        with self.assertRaises(ValueError):
            self.api.recommendation('a')
        self.data['generated_at'] = '2000-01-01T00:00:00Z'
        self.write()
        self.assertEqual(self.api.projects()['freshness'], 'stale')
        with self.assertRaises(ValueError):
            self.api.decide('a', 'state', {}, self.api.projects()['snapshot_id'])
        path.unlink()
        empty = self.api.projects()
        self.assertEqual((empty['freshness'], empty['data_source']['status']), ('unavailable', 'no_findings'))
        with self.assertRaises(ValueError):
            self.api.inspect('a', 'id0')

    def test_decision_requires_current_revision_and_typed_question(self):
        with self.assertRaises(ValueError):
            self.api.decide('a', 'context', {}, 'wrong')
        rev = self.api.projects()['snapshot_id']
        with self.assertRaises(ValueError):
            self.api.decide('a', 'context', {'type': 'text'}, rev)
        script = self.root/'decider.py'
        script.write_text("DEFAULT_URL='http://127.0.0.1:8080'\nDEFAULT_MODEL='fake'\n"
                          "def validate_questions(q): return q\n"
                          "def call(*a, **k): raise RuntimeError('unavailable')\n")
        self.api.decider = script
        with self.assertRaisesRegex(RuntimeError, 'unavailable'):
            self.api.decide('a', 'context', {'type':'choice', 'instructions':'select',
                                            'criteria':{'yes':'yes', 'no':'no'}}, rev)

    def test_quoted_secrets_and_multiline_context_are_preserved_safely(self):
        from backend.finding_text import redact
        for value in ('password="dummy value"', "token='dummy value'"):
            self.assertNotIn('dummy', redact(value))
            self.assertNotIn('value', redact(value))
        result = self.api._multiline('first observation\nsecond acceptance failure\npassword="dummy value"', 4000)
        self.assertIn('second acceptance failure', result)
        self.assertNotIn('dummy', result)
        self.assertNotIn('value', result)

    def test_explicit_owner_takes_precedence_over_other_label(self):
        self.api.project_map['b']['labels'] = ['same-label']
        self.data['findings'][0]['latest_evidence']['project'] = 'same-label'
        self.write()
        self.assertNotIn('id0', [f['issue_id'] for f in self.api.issues('b')['issues']])
        self.assertIn('id0', [f['issue_id'] for f in self.api.issues('a')['issues']])

    def test_ambiguous_attribution_remains_unassigned(self):
        self.api.project_map['b']['owners'] = ['owner:a']
        self.assertEqual(self.api.issues('a')['total'], 0)
        self.assertEqual(self.api.issues('b')['total'], 0)
        result = self.api.projects()
        self.assertEqual(result['unassigned_issue_count'], 4)
        self.assertEqual(result['unassigned_findings'][0]['attribution_reason'], 'ambiguous_owner_mapping')

    def test_new_and_retained_proposal_provenance(self):
        from backend.recommendations import propose
        from unittest.mock import patch
        evidence = {'sessions': [], 'cost_by_purpose': {}, 'limits': []}
        for retained in (False, True):
            record = propose([], evidence, time.time())
            record['retained'] = retained
            from subprocess import CompletedProcess
            completed = CompletedProcess([], 0, stdout=json.dumps(record), stderr='')
            with patch('backend.observability_api.subprocess.run', return_value=completed):
                result = self.api.recommendation('ledger', 'propose')['recommendations'][0]
            self.assertEqual(result['retained'], retained)

    def test_mocked_decider_receives_multiline_without_quoted_secret(self):
        script = self.root/'decider.py'
        capture = self.root/'payload.json'
        script.write_text("import json\nfrom pathlib import Path\nDEFAULT_URL='http://127.0.0.1:8080'\nDEFAULT_MODEL='fake'\n"
                          "def validate_questions(q): return q\n"
                          f"def call(url,path,payload,timeout):\n    Path({str(capture)!r}).write_text(json.dumps(payload))\n    return {{'answers':{{'decision':{{}}}}}}\n"
                          "def summarize(*args): return {'choice':'inspect','confidence':.5,'low_confidence':True}\n")
        self.api.decider = script
        self.api.decide('a', 'first line\nsecond line password="dummy value"',
                        {'type':'choice','instructions':'first instruction\nsecond instruction',
                         'criteria':{'inspect':'first option','test':'second option'}}, self.api.projects()['snapshot_id'])
        payload = json.loads(capture.read_text())
        self.assertIn('second line', payload['state'])
        self.assertNotIn('dummy', payload['state'])
        self.assertNotIn('value', payload['state'])
        self.assertIn('second instruction', payload['questions']['decision']['instructions'])

    def test_closed_recommendation_is_not_acceptance(self):
        from backend.recommendations import propose
        evidence = {'sessions': [], 'cost_by_purpose': {}, 'limits': []}
        record = propose([], evidence, time.time())
        record.update(status='closed', outcome='inconclusive')
        self.data['token_burn'] = {'recommendations':[record]}
        self.write()
        result = self.api.recommendation('ledger')['recommendations'][0]
        self.assertFalse(result['actionable'])
        self.assertEqual(result['acceptance_status'], 'not_assessed')
        self.assertNotIn('evidence', result)

    def test_empty_state_lists_configured_projects_with_hint(self):
        (self.root/'findings.json').unlink()
        result = self.api.projects()
        self.assertEqual(result['data_source']['status'], 'no_findings')
        self.assertIn('findings.json', result['data_source']['hint'])
        self.assertIn('follow-up', result['data_source']['hint'])
        self.assertEqual(result['data_source']['hint'], NO_FINDINGS_HINT)
        self.assertEqual({p['project_id']: p['issue_count'] for p in result['projects']}, {'a': 0, 'b': 0, 'ledger': 0})
        self.assertEqual(result['optional_services']['findings_producer'], 'absent')
        issues = self.api.issues('a')
        self.assertEqual((issues['issues'], issues['total'], issues['data_source']['status']), ([], 0, 'no_findings'))
        with self.assertRaises(ValueError):
            self.api.issues('unknown')

    def test_discovery_names_projects_from_findings_and_configured_ids_win(self):
        api = ObservabilityAPI(self.root, {'a': {'owners': ['owner:a']}}, None, discover=True)
        self.data['findings'] += [{'finding_key': 'repo1', 'latest_evidence': {'project': 'My Repo'}},
                                  {'finding_key': 'svc1', 'owner_ref': 'owner:svc'},
                                  {'finding_key': 'a-label', 'latest_evidence': {'project': 'a'}}]
        self.write()
        rows = {p['project_id']: p for p in api.projects()['projects']}
        self.assertEqual(rows['my-repo']['issue_count'], 1)
        self.assertTrue(rows['my-repo']['discovered'])
        self.assertIn('svc', rows)
        self.assertFalse(rows['a']['discovered'])
        self.assertEqual(rows['a']['issue_count'], 3)
        self.assertEqual([i['issue_id'] for i in api.issues('my-repo')['issues']], ['repo1'])
        self.assertEqual([i['issue_id'] for i in api.issues('svc')['issues']], ['svc1'])
        self.assertNotIn('my-repo', [p['project_id'] for p in self.api.projects()['projects']])

    def test_missing_decider_is_reported_not_faked(self):
        self.assertEqual(self.api.optional_services()['decider'], 'absent')
        rev = self.api.projects()['snapshot_id']
        with self.assertRaisesRegex(ValueError, '^decider_unavailable'):
            self.api.decide('a', 'context', {'type': 'choice', 'instructions': 'pick',
                                             'criteria': {'yes': 'yes', 'no': 'no'}}, rev)
