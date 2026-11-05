import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from collector.analyze import Context, _finding


class OccurrenceEvidenceTests(unittest.TestCase):
    def test_latest_empty_error_does_not_borrow_old_message(self):
        def event(ts, message, version):
            return {'project':'samantha', 'source':'activity', 'kind':'tool_error', 'step':'ledger_task',
                    'error':True, 'ts':ts, 'message':message, 'version':version, 'ref':str(ts)}
        events = [event(1, 'old error', '0.2.92'), event(2, '', '0.2.94')]
        result = _finding(Context(events, 3, 24, 1000), 'failure', 'app_error', ('samantha',), events, 'failed', 2)
        self.assertEqual(result['latest_evidence']['first_error_line'], '')
        self.assertIsNone(result['event_timeline'][0]['safe_evidence']['first_error_line'])
        self.assertEqual(result['event_timeline'][0]['safe_evidence']['version'], '0.2.94')
        self.assertEqual(result['event_timeline'][1]['safe_evidence']['first_error_line'], 'old error')


if __name__ == '__main__':
    unittest.main()
