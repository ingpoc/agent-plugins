from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from unittest.mock import Mock

from voice_cua import SYSTEM_INSTRUCTIONS
from voice_cua.ledger_helper import LedgerHelper
from voice_cua.tools import tool_definitions


def test_preferences_and_recall_without_helper(tmp_path):
    values = {}
    def call(name, args):
        if name == 'save':
            values[(args['scope'], args['rule'])] = args['decision']
            return {'ok': True, 'data': {'decision_id': 'saved'}}
        return {'ok': True, 'data': {'matches': [{'decision': values[(args['scope'], args['rule'])]}]}}
    transport = Mock()
    transport.call.side_effect = call
    decide = Mock(side_effect=AssertionError('helper model called'))
    helper = LedgerHelper(transport, decide, tmp_path / 'pending')
    for scope, rule, value, quote in [
        ('tool:gmail', 'account_select', 'Work', 'Please use Work for my email account'),
        ('tool:macos', 'default_browser', 'Safari', 'Please use Safari as my browser'),
    ]:
        helper.note_user(quote)
        result = helper.run(operation='preference', scope=scope, rule=rule, value=value, owner_quote=quote)
        assert result['status'] == 'saved'
        args = transport.call.call_args.args[1]
        assert (args['scope'], args['rule'], args['decision']) == (scope, rule, value)
    replay = LedgerHelper(transport, decide, tmp_path / 'pending2')
    assert replay.run(operation='recall', scope='tool:gmail', rule='account_select')['data']['matches'][0]['decision'] == 'Work'
    decide.assert_not_called()
    assert 'ledger_task' in {tool['name'] for tool in tool_definitions()}
    assert 'apply the saved value without asking' in SYSTEM_INSTRUCTIONS


def test_preference_rejects_one_off_and_unquoted_value(tmp_path):
    transport = Mock()
    helper = LedgerHelper(transport, Mock(), tmp_path / 'pending')
    quote = 'Use Work just this once for the account'
    helper.note_user(quote)
    assert not helper.run(operation='preference', scope='tool:gmail', rule='account_select', value='Work', owner_quote=quote)['ok']
    helper = LedgerHelper(transport, Mock(), tmp_path / 'pending')
    quote = 'Please use my personal account'
    helper.note_user(quote)
    assert not helper.run(operation='preference', scope='tool:gmail', rule='account_select', value='Work', owner_quote=quote)['ok']
    transport.call.assert_not_called()
