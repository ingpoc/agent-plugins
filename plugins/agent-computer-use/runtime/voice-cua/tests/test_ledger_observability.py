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
    def test_scope_diagnostics_expose_only_bounded_categories(self):
        rows = []
        with patch.object(activity_log, '_append_row', side_effect=rows.append):
            for scope in ('', 'fleet', 'tool:comet', 'tool:PRIVATE_SITE', 'project:PRIVATE_PROJECT', 'bad:PRIVATE'):
                activity_log.log_tool_start('ledger_task', {'operation': 'recall', 'scope': scope, 'rule': 'PRIVATE_RULE'})
        self.assertEqual([r['ledger_scope_kind'] for r in rows], ['missing', 'fleet', 'browser', 'tool', 'project', 'invalid'])
        self.assertTrue(all(r['ledger_rule_supplied'] for r in rows))
        self.assertTrue(all(row['arguments']['rule'] == 'PRIVATE_RULE' for row in rows))
        self.assertIn('PRIVATE_SITE', json.dumps(rows))

    def test_partial_result_is_not_logged_as_found(self):
        rows = []
        result = {'ok': True, 'status': 'needs_disambiguation', 'has_more': True,
                  'data': {'matches': [{'decision_id': 'fixture', 'decision': 'PRIVATE_VALUE'}]}}
        with patch.object(activity_log, '_append_row', side_effect=rows.append):
            activity_log.log_tool_result('ledger_task', result, operation='lookup')
        self.assertEqual(rows[0]['ledger_result_status'], 'needs_disambiguation')
        self.assertIn('PRIVATE_VALUE', json.dumps(rows[0]['result']))

    def test_recall_detail_is_correlated_and_marks_truncation(self):
        rows = []
        result = {'ok': True, 'data': {'matches': [
            {'decision_id': str(i), 'summary': 'Prefer concise answers', 'token': 'secret'}
            for i in range(4)]}}
        with patch.object(activity_log, '_append_row', side_effect=rows.append), \
             activity_log.tool_context(call_id='call-1', run_id='abcdef123456'):
            activity_log.log_tool_result('ledger_task', result, operation='recall')
        row = rows[0]
        self.assertEqual(row['run_id'], 'abcdef123456')
        self.assertEqual(row['ledger_result_status'], 'found')
        self.assertEqual(row['ledger_decision_count'], 3)
        self.assertTrue(row['ledger_detail_truncated'])
        self.assertEqual(len(json.loads(row['ledger_result_detail'])['data']['matches']), 3)
        self.assertNotIn('secret', row['ledger_result_detail'])
        with activity_log.tool_context(run_id='not-hex'):
            self.assertNotIn('run_id', activity_log._current_tool_ids())

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
        self.assertIn('PRIVATE_EXCEPTION', json.dumps(rows))

    def test_dispatch_logs_correlated_metadata_with_full_task_arguments(self):
        rows = []
        result = {'ok': False, 'error_type': 'ledger_invalid_argument',
                  'error': 'find needs query words PRIVATE_QUERY', 'hint': 'PRIVATE_HINT'}
        with patch.object(activity_log, '_append_row', side_effect=rows.append), \
             patch.object(tools, '_ledger_task', return_value=result), \
             patch.object(tools.workflow_store, 'mutation_guard', return_value=None), \
             patch.object(tools.workflow_store, 'note_failure'), \
             activity_log.tool_context(call_id='call-1', response_id='response-1',
                                       run_id='1234567890abcdef1234567890abcdef',
                                       step_ref='ledger-step', caused_by=['request-step']):
            self.assertEqual(tools.dispatch('ledger_task', {'operation':'lookup', 'situation':'PRIVATE_QUERY',
                'value':'PRIVATE_VALUE', 'label':'PRIVATE_LABEL', 'app':'PRIVATE_APP'}), result)
        self.assertEqual([r['kind'] for r in rows], ['tool_start', 'tool_error', 'ledger_select'])
        for row in rows:
            self.assertEqual((row['call_id'], row['response_id'], row['ledger_operation']),
                             ('call-1', 'response-1', 'lookup'))
        self.assertEqual(rows[1]['ledger_error_category'], 'invalid_query_terms')
        self.assertIn('PRIVATE_QUERY', rows[1]['error_details'])
        selection = next(row for row in rows if row['kind'] == 'ledger_select')
        self.assertEqual(selection['ledger_selection_status'], 'none')
        self.assertEqual(selection['ledger_selected_count'], 0)
        self.assertEqual(selection['step_ref'], 'ledger-step')
        self.assertEqual(selection['caused_by'], ['request-step'])
        self.assertEqual(rows[0]['arguments'], {'operation': 'lookup', 'situation': 'PRIVATE_QUERY',
                         'value': 'PRIVATE_VALUE', 'label': 'PRIVATE_LABEL', 'app': 'PRIVATE_APP'})
        self.assertIn('PRIVATE_HINT', json.dumps(rows[1]['result']))

    def test_selection_save_and_supersede_are_trailed_with_record_values(self):
        rows = []
        select = {'ok': True, 'status': 'found', 'rule': 'browser_preference',
                  'decisions': [{'decision_id': 'dec_123456', 'summary': 'PRIVATE_VALUE'}],
                  'data': {'matches': [{'decision_id': 'dec_123456', 'summary': 'PRIVATE_VALUE'}]}}
        save = {'ok': True, 'status': 'saved', 'decision_id': 'dec_654321', 'revision': 3,
                'supersedes': 'dec_123456', 'value': 'PRIVATE_VALUE'}
        with patch.object(activity_log, '_append_row', side_effect=rows.append):
            activity_log.log_tool_result('ledger_task', select, operation='lookup', duration_ms=123)
            activity_log.log_tool_result('ledger_task', save, operation='capture', duration_ms=45)
        self.assertEqual([row['kind'] for row in rows],
                         ['tool_result', 'ledger_select', 'tool_result', 'ledger_save_receipt', 'ledger_supersede'])
        self.assertEqual((rows[0]['duration_ms'], rows[2]['duration_ms']), (123, 45))
        self.assertEqual(rows[1]['ledger_rule'], 'browser_preference')
        self.assertEqual(rows[1]['ledger_selected_count'], 1)
        self.assertNotIn('selected_id', rows[1])
        self.assertEqual((rows[3]['decision_id'], rows[3]['revision']), ('dec_654321', 3))
        self.assertEqual(rows[4]['supersedes'], 'dec_123456')
        self.assertEqual(rows[0]['result']['decisions'][0]['summary'], 'PRIVATE_VALUE')
        self.assertEqual(rows[2]['result']['value'], 'PRIVATE_VALUE')

    def test_save_receipt_reports_each_committed_supersession(self):
        rows = []
        result = {'ok': True, 'status': 'saved', 'decision_id': 'dec_654321', 'revision': 1,
                  'superseded_ids': ['dec_123456', 'dec_abcdef'], 'value': 'PRIVATE_VALUE'}
        with patch.object(activity_log, '_append_row', side_effect=rows.append):
            activity_log.log_tool_result('ledger_task', result, operation='preference', duration_ms=1)
        self.assertEqual([row['supersedes'] for row in rows if row['kind'] == 'ledger_supersede'],
                         ['dec_123456', 'dec_abcdef'])
        self.assertIn('PRIVATE_VALUE', json.dumps(rows[0]['result']))
        self.assertTrue(all(row['build'] for row in rows))
        self.assertNotIn('PRIVATE_VALUE', json.dumps(rows[1::2]))

    def test_only_explicit_action_decision_id_emits_ledger_use(self):
        rows = []
        with patch.object(activity_log, '_append_row', side_effect=rows.append), \
             activity_log.tool_context(step_ref='apply-step'):
            activity_log.log_tool_result('ledger_task', {
                'ok': True, 'status': 'found',
                'decisions': [{'decision_id': 'dec_123456'}],
            }, operation='lookup')
            activity_log.log_tool_result('cua_act', {'ok': True}, arguments={
                'app': 'Editor', 'op': 'click', 'label': 'Save',
            })
            activity_log.log_tool_result('cua_act', {'ok': True}, arguments={
                'app': 'Editor', 'op': 'click', 'label': 'Save',
                'ledger_decision_id': 'dec_123456',
            })
        lookup = next(row for row in rows if row.get('tool') == 'ledger_task')
        actions = [row for row in rows if row.get('tool') == 'cua_act']
        self.assertNotIn('selected_id', lookup)
        self.assertEqual(len(actions), 2)
        self.assertNotIn('selected_id', actions[0])
        self.assertEqual(actions[1]['selected_id'], 'dec_123456')
        use = next(row for row in rows if row['kind'] == 'ledger_use')
        self.assertEqual((use['decision_id'], use['step_ref']), ('dec_123456', 'apply-step'))
        self.assertEqual(use['action'], {'tool': 'cua_act', 'op': 'click', 'label': 'Save'})

    def test_build_label_is_version_plus_package_digest_when_installed(self):
        import re
        from voice_cua import __version__, build_info, build_label
        self.assertEqual(build_info()['version'], __version__)
        self.assertRegex(build_label(), r'^' + re.escape(__version__) + r'(?:\+[0-9a-f]{12})?$')

    def test_unexpected_operation_keeps_full_arguments_and_error_context(self):
        rows = []
        with patch.object(activity_log, '_append_row', side_effect=rows.append):
            activity_log.log_tool_start('ledger_task', {'operation':'PRIVATE_OPERATION'})
            activity_log.log_tool_result('ledger_task', {'ok':False, 'error':'PRIVATE_ERROR'}, operation='PRIVATE_OPERATION')
            activity_log.log_exception('ledger_task', ValueError('PRIVATE_EXCEPTION'), app='PRIVATE_APP')
        self.assertEqual(rows[0]['ledger_operation'], 'unknown')
        self.assertEqual(rows[1]['ledger_error_category'], 'other')
        self.assertIn('PRIVATE_ERROR', rows[1]['error_details'])
        self.assertIn('PRIVATE_EXCEPTION', rows[2]['error_details'])
        self.assertEqual(rows[0]['arguments']['operation'], 'PRIVATE_OPERATION')
        self.assertEqual(rows[2]['app'], 'PRIVATE_APP')


if __name__ == '__main__':
    unittest.main()
