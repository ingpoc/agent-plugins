import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from backend.observability_api import ObservabilityAPI
from collector.analyze import build
from collector.sources import iter_jsonl

NOW = 1795089000.0


class SamanthaAnomalyTests(unittest.TestCase):
    def build_rows(self, root, rows, now=NOW):
        source = root / 'activity.jsonl'
        source.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        stats = {}
        return build(list(iter_jsonl(source, 'samantha', 'samantha:activity', stats)), stats, now)

    def api(self, root, document):
        (root / 'findings.json').write_text(json.dumps(document))
        return ObservabilityAPI(root, {'samantha': {'owners': ['owner:samantha']}}, None)

    def test_logged_realtime_exception_type_and_excerpt_reach_issue_timeline(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            document = self.build_rows(root, [{
                'ts': NOW - 1, 'session_id': 'failed-start', 'kind': 'realtime_error',
                'error_type': 'invalid_request_error',
                'error': 'RuntimeError: request rejected with Bearer synthetic-secret-value',
            }])
            finding = next(f for f in document['findings'] if f.get('kind') == 'failure')
            api = self.api(root, document)
            inspected = api.inspect('samantha', finding['finding_key'])
            excerpts = inspected['evidence']['excerpts']
            timeline = inspected['evidence']['timeline'][0]['evidence']
            self.assertEqual(excerpts['error_type'], 'invalid_request_error')
            self.assertIn('request rejected', excerpts['first_error_line'])
            self.assertEqual(timeline['error_type'], 'invalid_request_error')
            self.assertIn('request rejected', timeline['first_error_line'])
            self.assertNotIn('synthetic-secret-value', json.dumps(inspected))

    def test_explicit_test_purpose_survives_user_request_and_session_stays_visible(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            document = self.build_rows(root, [
                {'ts': NOW - 12, 'session_id': 'grok-session', 'kind': 'comet_call', 'purpose': 'self_test'},
                {'ts': NOW - 10, 'session_id': 'grok-session', 'kind': 'user_request',
                 'provider': 'xai', 'run_id': 'request-1', 'request': 'synthetic request'},
                {'ts': NOW - 9, 'session_id': 'grok-session', 'kind': 'model_response_start',
                 'provider': 'xai', 'run_id': 'request-1', 'response_id': 'response-1'},
                {'ts': NOW - 8, 'session_id': 'grok-session', 'kind': 'tool_start',
                 'response_id': 'response-1', 'call_id': 'call-1', 'tool': 'comet_act'},
                {'ts': NOW - 7, 'session_id': 'grok-session', 'kind': 'tool_result',
                 'response_id': 'response-1', 'call_id': 'call-1', 'ok': True},
                {'ts': NOW - 5, 'session_id': 'grok-session', 'kind': 'assistant_reply',
                 'response_id': 'response-1', 'text': 'synthetic reply'},
                {'ts': NOW - 4, 'session_id': 'grok-session', 'kind': 'model_response_end',
                 'response_id': 'response-1', 'status': 'completed'},
                {'ts': NOW - 3, 'session_id': 'grok-session', 'kind': 'usage',
                 'response_id': 'response-1', 'input_tokens': 4, 'output_tokens': 3, 'total_tokens': 7},
                {'ts': NOW - 2, 'session_id': 'grok-session', 'kind': 'usage',
                 'response_id': 'self-test-zero', 'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0},
            ])
            self.assertEqual(document['collector_stats']['samantha:activity']['excluded_test'], 9)
            self.assertFalse(any(f.get('anomaly') == 'zero_token_usage' for f in document['findings']))
            api = self.api(root, document)
            sessions = api.sessions('samantha')['sessions']
            self.assertEqual(len(sessions), 1)
            timeline = api.sessions('samantha', sessions[0]['session_ref'])['timeline']
            stages = {event['stage'] for event in timeline}
            self.assertTrue({'user_request', 'model_response_start', 'comet_act',
                             'assistant_reply', 'model_response_end'} <= stages)

    def test_request_without_reply_is_reported_only_after_three_minutes(self):
        with tempfile.TemporaryDirectory() as td:
            rows = [
                {'ts': NOW - 181, 'session_id': 'timed-out', 'kind': 'user_request', 'run_id': 'old-request'},
                {'ts': NOW - 179, 'session_id': 'still-waiting', 'kind': 'user_request', 'run_id': 'recent-request'},
                {'ts': NOW - 240, 'session_id': 'answered', 'kind': 'user_request', 'run_id': 'answered-request'},
                {'ts': NOW - 239, 'session_id': 'answered', 'kind': 'model_response_start',
                 'run_id': 'answered-request', 'response_id': 'answered-response'},
                {'ts': NOW - 220, 'session_id': 'answered', 'kind': 'assistant_reply',
                 'response_id': 'answered-response', 'text': 'synthetic reply'},
            ]
            document = self.build_rows(Path(td), rows)
            finding = next(f for f in document['findings'] if f.get('anomaly') == 'no_reply')
            self.assertEqual(finding['failed_attempt_count'], 1)
            self.assertEqual(finding['latest_evidence']['error_type'], 'no_reply')

    def test_run_request_correlates_reply_without_response_id_by_turn_order(self):
        with tempfile.TemporaryDirectory() as td:
            rows = [
                {'ts': NOW - 240, 'session_id': 'ordered-reply', 'kind': 'user_request',
                 'run_id': 'ordered-request'},
                {'ts': NOW - 239, 'session_id': 'ordered-reply', 'kind': 'model_response_start',
                 'run_id': 'ordered-request', 'response_id': 'ordered-response'},
                {'ts': NOW - 220, 'session_id': 'ordered-reply', 'kind': 'assistant_reply',
                 'text': 'synthetic reply without correlation ids'},
            ]
            document = self.build_rows(Path(td), rows)
            self.assertFalse(any(f.get('anomaly') == 'no_reply' for f in document['findings']))

    def test_redirect_with_overlapping_responses_creates_an_issue(self):
        with tempfile.TemporaryDirectory() as td:
            rows = [
                {'ts': NOW - 10, 'session_id': 'overlap', 'kind': 'model_response_start',
                 'run_id': 'old-request', 'response_id': 'old-response'},
                {'ts': NOW - 8, 'session_id': 'overlap', 'kind': 'live_backend_input_queued', 'run_id': 'new-request'},
                {'ts': NOW - 7, 'session_id': 'overlap', 'kind': 'model_response_start',
                 'run_id': 'new-request', 'response_id': 'new-response'},
                {'ts': NOW - 6, 'session_id': 'overlap', 'kind': 'model_response_end',
                 'response_id': 'old-response', 'status': 'completed'},
                {'ts': NOW - 5, 'session_id': 'overlap', 'kind': 'model_response_end',
                 'response_id': 'new-response', 'status': 'completed'},
                {'ts': NOW - 10, 'session_id': 'cut-off', 'kind': 'model_response_start',
                 'run_id': 'old-request-2', 'response_id': 'old-response-2'},
                {'ts': NOW - 8, 'session_id': 'cut-off', 'kind': 'live_backend_input_queued', 'run_id': 'new-request-2'},
                {'ts': NOW - 9, 'session_id': 'cut-off', 'kind': 'model_response_end',
                 'response_id': 'old-response-2', 'status': 'completed'},
                {'ts': NOW - 6, 'session_id': 'cut-off', 'kind': 'model_response_start',
                 'run_id': 'new-request-2', 'response_id': 'new-response-2'},
                {'ts': NOW - 5, 'session_id': 'cut-off', 'kind': 'model_response_end',
                 'response_id': 'new-response-2', 'status': 'completed'},
            ]
            document = self.build_rows(Path(td), rows)
            finding = next(f for f in document['findings'] if f.get('anomaly') == 'response_overlap')
            self.assertEqual(finding['failed_attempt_count'], 1)
            self.assertEqual(finding['latest_evidence']['error_type'], 'response_overlap')

    def test_overlapping_start_without_end_counts_only_within_timeout(self):
        with tempfile.TemporaryDirectory() as td:
            rows = [
                {'ts': NOW - 20, 'session_id': 'recent-overlap', 'kind': 'model_response_start',
                 'run_id': 'old-request', 'response_id': 'old-response'},
                {'ts': NOW - 5, 'session_id': 'recent-overlap', 'kind': 'model_response_start',
                 'run_id': 'new-request', 'response_id': 'new-response'},
                {'ts': NOW - 300, 'session_id': 'stale-start', 'kind': 'model_response_start',
                 'run_id': 'stale-request', 'response_id': 'stale-response'},
                {'ts': NOW - 5, 'session_id': 'stale-start', 'kind': 'model_response_start',
                 'run_id': 'fresh-request', 'response_id': 'fresh-response'},
            ]
            document = self.build_rows(Path(td), rows)
            finding = next(f for f in document['findings'] if f.get('anomaly') == 'response_overlap')
            self.assertEqual(finding['failed_attempt_count'], 1)

    def test_all_zero_token_usage_creates_issue_with_counts_in_timeline(self):
        with tempfile.TemporaryDirectory() as td:
            rows = [
                {'ts': NOW - 3, 'session_id': 'zero', 'kind': 'usage', 'response_id': 'zero-1',
                 'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0},
                {'ts': NOW - 2, 'session_id': 'zero', 'kind': 'usage', 'response_id': 'zero-2',
                 'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0},
                {'ts': NOW - 1, 'session_id': 'nonzero', 'kind': 'usage', 'response_id': 'response-3',
                 'input_tokens': 1, 'output_tokens': 0, 'total_tokens': 1},
            ]
            document = self.build_rows(Path(td), rows)
            finding = next(f for f in document['findings'] if f.get('anomaly') == 'zero_token_usage')
            self.assertEqual(finding['failed_attempt_count'], 2)
            self.assertEqual(finding['event_timeline'][0]['safe_evidence']['total_tokens'], 0)
            api = self.api(Path(td), document)
            inspected = api.inspect('samantha', finding['finding_key'])
            self.assertEqual(inspected['evidence']['timeline'][0]['evidence']['input_tokens'], 0)
            self.assertEqual(inspected['evidence']['timeline'][0]['evidence']['output_tokens'], 0)
            self.assertEqual(inspected['evidence']['timeline'][0]['evidence']['total_tokens'], 0)


if __name__ == '__main__':
    unittest.main()
