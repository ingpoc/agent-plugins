import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))
from voice_cua import activity_log, tools
from voice_cua.ledger_helper import LedgerHelper
from voice_cua.ledger_transport import LedgerUnavailable


class LedgerObservabilityTests(unittest.TestCase):
    def test_helper_error_has_correlation_without_exception_text(self):
        rows = []
        transport = Mock()
        transport.call.side_effect = LedgerUnavailable('PRIVATE_EXCEPTION')
        helper = LedgerHelper(transport=transport)
        with patch.object(activity_log, '_append_row', side_effect=rows.append), \
             activity_log.tool_context(call_id='call-2', response_id='response-2'):
            helper.run(operation='lookup', situation='find profile', need='profile')
        self.assertEqual(rows[0]['kind'], 'ledger_helper_error')
        self.assertEqual((rows[0]['call_id'], rows[0]['response_id']), ('call-2', 'response-2'))
        self.assertNotIn('PRIVATE_', json.dumps(rows))

    def test_dispatch_logs_correlated_allowlisted_metadata_without_arguments(self):
        rows = []
        result = {'ok': False, 'error_type': 'ledger_invalid_argument',
                  'error': 'find needs query words PRIVATE_QUERY', 'hint': 'PRIVATE_HINT'}
        with patch.object(activity_log, '_append_row', side_effect=rows.append), \
             patch.object(tools, '_ledger_task', return_value=result), \
             patch.object(tools.workflow_store, 'mutation_guard', return_value=None), \
             patch.object(tools.workflow_store, 'note_failure'), \
             activity_log.tool_context(call_id='call-1', response_id='response-1'):
            self.assertEqual(tools.dispatch('ledger_task', {'operation':'lookup', 'situation':'PRIVATE_QUERY',
                'value':'PRIVATE_VALUE', 'label':'PRIVATE_LABEL', 'app':'PRIVATE_APP'}), result)
        self.assertEqual([r['kind'] for r in rows], ['tool_start', 'tool_error'])
        for row in rows:
            self.assertEqual((row['call_id'], row['response_id'], row['ledger_operation']),
                             ('call-1', 'response-1', 'lookup'))
        self.assertEqual(rows[1]['ledger_error_category'], 'invalid_query_terms')
        self.assertIn('PRIVATE_QUERY', rows[1]['error_details'])
        for argument in ('PRIVATE_VALUE', 'PRIVATE_LABEL', 'PRIVATE_APP', 'PRIVATE_HINT'):
            self.assertNotIn(argument, json.dumps(rows))

    def test_unexpected_operation_is_suppressed_but_error_context_is_recorded(self):
        rows = []
        with patch.object(activity_log, '_append_row', side_effect=rows.append):
            activity_log.log_tool_start('ledger_task', {'operation':'PRIVATE_OPERATION'})
            activity_log.log_tool_result('ledger_task', {'ok':False, 'error':'PRIVATE_ERROR'}, operation='PRIVATE_OPERATION')
            activity_log.log_exception('ledger_task', ValueError('PRIVATE_EXCEPTION'), app='PRIVATE_APP')
        self.assertEqual(rows[0]['ledger_operation'], 'unknown')
        self.assertEqual(rows[1]['ledger_error_category'], 'other')
        self.assertIn('PRIVATE_ERROR', rows[1]['error_details'])
        self.assertIn('PRIVATE_EXCEPTION', rows[2]['error_details'])
        self.assertNotIn('PRIVATE_OPERATION', json.dumps(rows))
        self.assertNotIn('PRIVATE_APP', json.dumps(rows))


if __name__ == '__main__':
    unittest.main()
