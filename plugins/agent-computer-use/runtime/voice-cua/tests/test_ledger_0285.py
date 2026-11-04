"""0.2.85: a ledger rejection is reported as invalid arguments, never as 'unavailable'."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'python'))
# Test hygiene (0.2.86): these tests must never write rows into the real ~/.cache/macos-cua activity log.
_ACTIVITY_DIR = tempfile.TemporaryDirectory(prefix="voice-cua-test-ledger-")
os.environ["VOICE_CUA_ACTIVITY_LOG"] = str(Path(_ACTIVITY_DIR.name) / "activity.jsonl")
from voice_cua.ledger_helper import LedgerHelper, normalize_project  # noqa: E402
from voice_cua.ledger_transport import LedgerInvalidArgument, LedgerTransport  # noqa: E402

REJECTING_SERVER = '''#!/usr/bin/python3
import sys,json
for line in sys.stdin:
 req=json.loads(line)
 if "id" not in req: continue
 if req["method"]=="initialize":
  result={}
 else:
  args=req["params"]["arguments"]
  proj=(args.get("caller_scope") or {}).get("project")
  if proj and proj!=proj.lower():
   result={"isError":True,"content":[{"type":"text","text":json.dumps({"ok":False,"error":{"code":"INVALID_ARGUMENT","retryable":False}})}]}
  else:
   result={"content":[{"type":"text","text":json.dumps({"ok":True,"data":{"matches":[]}})}]}
 print(json.dumps({"jsonrpc":"2.0","id":req["id"],"result":result}),flush=True)
'''


def gate(quote, choice='Use the Guru profile on Netflix'):
    return {'owner_decision': quote, 'future_task': 'open Netflix', 'changed_choice': choice,
            'unique_value': 'owner preference', 'lifetime': 'durable', 'sensitivity': 'model_safe'}


class TransportRejectionTests(unittest.TestCase):
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
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.package, self.data = root / 'plugin', root / 'data'
        (self.package / 'bin').mkdir(parents=True)
        self.data.mkdir()
        (self.package / 'plugin.json').write_text(json.dumps({'name': 'context-ledger'}))
        (self.data / 'binding.json').write_text('{}')
        launcher = self.package / 'bin/context-ledger-mcp'
        launcher.write_text(REJECTING_SERVER)
        launcher.chmod(0o700)
        self.transport = LedgerTransport(self.package, self.data)
        self.addCleanup(self.transport.close)

    def test_invalid_argument_keeps_the_server_and_is_not_unavailable(self):
        with self.assertRaises(LedgerInvalidArgument) as caught:
            self.transport.call('find', {'query': 'x', 'caller_scope': {'project': 'Netflix', 'tools': []}})
        self.assertEqual(caught.exception.code, 'INVALID_ARGUMENT')
        process = self.transport._process
        self.assertIsNotNone(process, 'a validation error must not kill the ledger process')
        self.assertTrue(self.transport.call('find', {'query': 'x', 'caller_scope': {'project': 'netflix', 'tools': []}})['ok'])
        self.assertIs(process, self.transport._process)

    def test_helper_lowercases_project_before_the_server_sees_it(self):
        helper = LedgerHelper(self.transport, Mock(), Path(self.temp.name) / 'pending')
        with patch('voice_cua.activity_log.log_event'):
            result = helper.run(operation='lookup', situation='which profile', need='profile', project='Netflix')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['status'], 'none')


class HelperErrorTypeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.transport = Mock()
        self.decide = Mock()
        self.helper = LedgerHelper(self.transport, self.decide, Path(self.temp.name) / 'pending')
        self.log = patch('voice_cua.activity_log.log_event').start()
        self.addCleanup(patch.stopall)

    def test_normalize_project(self):
        self.assertEqual(normalize_project(' Netflix '), 'netflix')
        self.assertEqual(normalize_project('Agent Plugins'), 'agent-plugins')
        self.assertEqual(normalize_project('my_App!'), 'my-app')

    def test_server_rejection_is_invalid_argument_not_unavailable(self):
        self.transport.call.side_effect = LedgerInvalidArgument('INVALID_ARGUMENT', 'scope must be lowercase')
        result = self.helper.run(operation='lookup', situation='which profile', need='profile', project='netflix')
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_type'], 'ledger_invalid_argument')
        self.assertNotIn('unavailable', result['error'].lower())
        row = self.log.call_args
        self.assertEqual(row.args[0], 'ledger_helper_error')
        self.assertEqual(row.kwargs['error_class'], 'LedgerInvalidArgument')
        self.assertEqual(row.kwargs['error_code'], 'INVALID_ARGUMENT')

    def test_real_transport_failure_is_unavailable(self):
        from voice_cua.ledger_transport import LedgerUnavailable
        self.transport.call.side_effect = LedgerUnavailable('ledger unavailable')
        result = self.helper.run(operation='lookup', situation='which profile', need='profile')
        self.assertEqual(result['error_type'], 'ledger_unavailable')

    def test_owner_quote_matches_ignoring_case_spacing_and_punctuation(self):
        self.helper.note_user('Uh, you can use Guru profile. Always use the Guru profile on Netflix, okay?')
        self.transport.call.side_effect = [{'ok': True, 'data': {'matches': []}},
                                           {'ok': True, 'data': {'decision_id': 'd1', 'revision': 1}}]
        self.decide.return_value = {'status': 'save', 'decision': 'Use the Guru profile on Netflix', 'why': 'Owner preference'}
        result = self.helper.run(operation='capture', situation='Netflix profile', need='remember',
                                 project='Netflix', capture_gate=gate('always use the  Guru profile on netflix'))
        self.assertEqual(result.get('status'), 'saved', result)
        record = self.transport.call.call_args.args[1]
        self.assertEqual(record['body']['scope'], 'project:netflix')

    def test_personal_preference_needs_no_project(self):
        self.helper.note_user('Remember that I always want dark mode in every app')
        self.transport.call.side_effect = [{'ok': True, 'data': {'matches': []}},
                                           {'ok': True, 'data': {'decision_id': 'd2', 'revision': 1}}]
        self.decide.return_value = {'status': 'save', 'decision': 'Use dark mode in every app', 'why': 'Owner preference'}
        result = self.helper.run(operation='capture', situation='app theme', need='remember',
                                 capture_gate=gate('I always want dark mode in every app', 'Use dark mode in every app'))
        self.assertEqual(result.get('status'), 'saved', result)
        find_args = self.transport.call.call_args_list[0].args[1]
        self.assertEqual(find_args['caller_scope'], {'project': 'personal', 'tools': []})
        self.assertEqual(self.transport.call.call_args.args[1]['body']['scope'], 'project:personal')

    def test_unrelated_quote_still_refused(self):
        self.helper.note_user('Open Netflix for me please')
        result = self.helper.run(operation='capture', situation='x', need='y',
                                 capture_gate=gate('Always use the Guru profile on Netflix'))
        self.assertEqual(result['error_type'], 'ledger_invalid_argument')
        self.transport.call.assert_not_called()


if __name__ == '__main__':
    unittest.main()
