"""Scoped and fail-closed Ledger reads; capture remains disabled."""
import os
import sys
import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))
# Test hygiene (0.2.86): these tests must never write rows into the real ~/.cache/macos-cua activity log.
_ACTIVITY_DIR = tempfile.TemporaryDirectory(prefix="voice-cua-test-ledger-")
os.environ["VOICE_CUA_ACTIVITY_LOG"] = str(Path(_ACTIVITY_DIR.name) / "activity.jsonl")
from voice_cua.ledger_helper import LedgerHelper

class LedgerHelperTests(unittest.TestCase):
    def test_activity_log_is_redirected_to_a_temp_path(self):
        from voice_cua.activity_log import log_path
        self.assertFalse(str(log_path()).startswith(str(Path.home() / ".cache/macos-cua")))
        original = os.environ.get("VOICE_CUA_ACTIVITY_LOG")
        try:
            os.environ["VOICE_CUA_ACTIVITY_LOG"] = str(Path(_ACTIVITY_DIR.name) / "activity.jsonl")
            self.assertTrue(str(log_path()).startswith(_ACTIVITY_DIR.name))
        finally:
            if original is None:
                os.environ.pop("VOICE_CUA_ACTIVITY_LOG", None)
            else:
                os.environ["VOICE_CUA_ACTIVITY_LOG"] = original

    def setUp(self):
        self.transport = Mock()
        self.match = {'decision_id': 'known', 'decision': 'A', 'why': 'B'}
        self.transport.call.return_value = {'ok': True, 'data': {'matches': [self.match]}}
        self.decide = Mock(return_value={'status': 'found', 'decision_ids': ['known']})
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.helper = LedgerHelper(self.transport, self.decide, Path(self.temp.name) / "pending")

    def test_lookup_only_returns_found_ids_and_reuses_unchanged_find(self):
        for _ in range(2):
            r = self.helper.run(operation='lookup', situation='sites', need='where', project='agent-plugins')
            self.assertEqual(r['decisions'], [self.match])
        self.assertEqual(self.transport.call.call_count, 1)
        self.assertEqual(self.transport.call.call_args.args[1]['caller_scope'], {'project': 'agent-plugins', 'tools': []})

    def test_empty_find_never_invents_a_match(self):
        self.transport.call.return_value = {'ok': True, 'data': {'matches': []}}
        r = self.helper.run(operation='lookup', situation='sites', need='where')
        self.assertEqual(r['status'], 'none')
        self.decide.assert_not_called()

    def test_fabricated_id_is_rejected(self):
        self.decide.return_value = {'status': 'found', 'decision_ids': ['fabricated']}
        self.assertFalse(self.helper.run(operation='lookup', situation='sites', need='where')['ok'])

    def test_capture_fails_closed(self):
        r = self.helper.run(operation='capture', situation='decision', need='remember', project='agent-plugins')
        self.assertFalse(r['ok'])
        self.transport.call.assert_not_called()
        self.decide.assert_not_called()

    def test_verified_owner_choice_produces_receipt(self):
        quote = 'For agent-plugins use separate product sites'
        self.helper.note_user(quote)
        gate = {'owner_decision': quote, 'future_task': 'design sites', 'changed_choice': 'Use separate product sites', 'unique_value': 'owner preference', 'lifetime': 'durable', 'sensitivity': 'model_safe'}
        self.decide.return_value = {'status': 'save', 'decision': 'Use separate product sites', 'why': 'Distinct audiences'}
        self.transport.call.side_effect = [{'ok': True, 'data': {'matches': []}}, {'ok': True, 'data': {'decision_id': 'receipt', 'revision': 1}}]
        result = self.helper.run(operation='capture', situation='site design', need='remember owner preference', project='agent-plugins', capture_gate=gate)
        self.assertEqual(result['status'], 'saved')
        self.assertEqual(self.transport.call.call_args.args[0], 'record')
        collection = Path(__file__).resolve().parents[4]
        ledger = collection / 'context-ledger'
        if ledger.is_dir():
            sys.path.insert(0, str(ledger))
            try:
                from context_ledger.contracts import validate_record_input
                validate_record_input(self.transport.call.call_args.args[1], allow_owner_attested=False, require_scope=True)
            finally:
                sys.path.remove(str(ledger))

    def test_existing_decision_is_not_written_twice(self):
        quote = 'For agent-plugins use separate product sites'
        self.helper.note_user(quote)
        gate = {'owner_decision': quote, 'future_task': 'design sites', 'changed_choice': 'Use separate product sites', 'unique_value': 'owner preference', 'lifetime': 'durable', 'sensitivity': 'model_safe'}
        self.decide.return_value = {'status': 'skip', 'reason': 'duplicate'}
        r = self.helper.run(operation='capture', situation='site design', need='remember', project='agent-plugins', capture_gate=gate)
        self.assertEqual(r['status'], 'skipped')
        self.assertEqual(self.transport.call.call_count, 1)

    def test_unknown_owner_quote_cannot_write(self):
        gate = {'owner_decision': 'An invented user statement', 'future_task': 'design sites', 'changed_choice': 'choice', 'unique_value': 'value', 'lifetime': 'durable', 'sensitivity': 'model_safe'}
        r = self.helper.run(operation='capture', situation='decision', need='save', project='agent-plugins', capture_gate=gate)
        self.assertFalse(r['ok'])
        self.transport.call.assert_not_called()

    def test_failed_record_has_unknown_outcome(self):
        quote = 'For agent-plugins use separate product sites'
        self.helper.note_user(quote)
        gate = {'owner_decision': quote, 'future_task': 'design sites', 'changed_choice': 'Use separate product sites', 'unique_value': 'owner preference', 'lifetime': 'durable', 'sensitivity': 'model_safe'}
        self.decide.return_value = {'status': 'save', 'decision': 'Use separate product sites', 'why': 'Distinct audiences'}
        self.transport.call.side_effect = [{'ok': True, 'data': {'matches': []}}, RuntimeError('lost reply')]
        r = self.helper.run(operation='capture', situation='site design', need='remember', project='agent-plugins', capture_gate=gate)
        self.assertIn('outcome unknown', r['error'])

    def test_rejected_partial_quote_cannot_authorize_save(self):
        self.helper.note_user('For agent-plugins do not save use separate sites; I rejected that.')
        gate = {'owner_decision': 'use separate sites', 'future_task': 'site design', 'changed_choice': 'separate sites', 'unique_value': 'preference', 'lifetime': 'durable', 'sensitivity': 'model_safe'}
        r = self.helper.run(operation='capture', situation='site', need='save', project='agent-plugins', capture_gate=gate)
        self.assertFalse(r['ok'])
        self.transport.call.assert_not_called()

    def test_temporary_decision_is_rejected(self):
        quote = 'For agent-plugins use separate sites until Friday'
        self.helper.note_user(quote)
        gate = {'owner_decision': quote, 'future_task': 'site design', 'changed_choice': 'separate sites', 'unique_value': 'preference', 'lifetime': 'until Friday', 'sensitivity': 'model_safe'}
        r = self.helper.run(operation='capture', situation='site', need='save', project='agent-plugins', capture_gate=gate)
        self.assertFalse(r['ok'])
        self.transport.call.assert_not_called()

    def test_revoked_decision_cannot_be_captured(self):
        quote = 'For agent-plugins remember separate sites'
        self.helper.note_user(quote)
        self.helper.note_user('Cancel that decision; do not store it')
        gate = {'owner_decision': quote, 'future_task': 'site design', 'changed_choice': 'separate sites', 'unique_value': 'preference', 'lifetime': 'durable', 'sensitivity': 'model_safe'}
        self.assertFalse(self.helper.run(operation='capture', situation='site', need='save', project='agent-plugins', capture_gate=gate)['ok'])
        self.transport.call.assert_not_called()

    def test_substring_project_cannot_authorize_scope(self):
        quote = 'For other-agent-plugins remember separate sites'
        self.helper.note_user(quote)
        gate = {'owner_decision': quote, 'future_task': 'site design', 'changed_choice': 'separate sites', 'unique_value': 'preference', 'lifetime': 'durable', 'sensitivity': 'model_safe'}
        self.assertFalse(self.helper.run(operation='capture', situation='site', need='save', project='agent-plugins', capture_gate=gate)['ok'])
        self.transport.call.assert_not_called()

    def test_uncertain_write_survives_helper_restart(self):
        quote = 'For agent-plugins use separate product sites'
        gate = {'owner_decision': quote, 'future_task': 'design sites', 'changed_choice': 'Use separate product sites', 'unique_value': 'owner preference', 'lifetime': 'durable', 'sensitivity': 'model_safe'}
        self.helper.note_user(quote)
        self.decide.return_value = {'status': 'save', 'decision': 'Use separate product sites', 'why': 'Distinct audiences'}
        self.transport.call.side_effect = [{'ok': True, 'data': {'matches': []}}, RuntimeError('lost reply')]
        result = self.helper.run(operation='capture', situation='site design', need='remember', project='agent-plugins', capture_gate=gate)
        self.assertIn('outcome unknown', result['error'])
        replacement_transport = Mock()
        replacement_transport.call.return_value = {'ok': True, 'data': {'matches': []}}
        replacement = LedgerHelper(replacement_transport, self.decide, Path(self.temp.name) / 'pending')
        replacement.note_user(quote)
        result = replacement.run(operation='capture', situation='site design', need='remember', project='agent-plugins', capture_gate=gate)
        self.assertIn('outcome unknown', result['error'])
        self.assertEqual(replacement_transport.call.call_count, 1)

    def test_closed_helper_cannot_capture(self):
        self.helper.close()
        self.assertFalse(self.helper.run(operation='lookup', situation='x', need='y')['ok'])

    def test_find_failure_returns_unavailable(self):
        self.transport.call.return_value = {'ok': False}
        self.assertFalse(self.helper.run(operation='lookup', situation='decision', need='find')['ok'])
        self.decide.assert_not_called()

if __name__ == '__main__':
    unittest.main()
