import json
import sys
import tempfile
import unittest
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
                {'ts':'2026-11-06T10:00:01Z', 'session_id':'private-session', 'kind':'tool_error',
                 'tool':'ledger_task', 'ledger_operation':'recall', 'ledger_error_category':'invalid_argument',
                 'call_id':'private-call', 'response_id':'private-response', 'error':'private preference'},
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
            self.assertEqual([e['stage'] for e in result['timeline']], ['audio_ready', 'ledger_task'])
            self.assertEqual(result['timeline'][0]['duration_ms'], 250)
            self.assertEqual(result['timeline'][1]['ledger_operation'], 'recall')
            self.assertEqual(result['timeline'][1]['ledger_error_category'], 'invalid_argument')
            serialized = json.dumps(result)
            for private in ('private-session', 'private-call', 'private-response', 'private preference', 'private input'):
                self.assertNotIn(private, serialized)
            with self.assertRaises(ValueError):
                api.sessions('b', ref)
            with self.assertRaises(ValueError):
                api.sessions('a', 'private-session')


if __name__ == '__main__':
    unittest.main()
