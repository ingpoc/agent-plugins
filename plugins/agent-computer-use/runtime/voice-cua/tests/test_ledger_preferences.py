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
    assert replay.run(operation='recall', scope='Gmail', rule='Account Select')['data']['matches'][0]['decision'] == 'Work'
    decide.assert_not_called()
    assert 'ledger_task' in {tool['name'] for tool in tool_definitions()}
    assert 'proactively' in SYSTEM_INSTRUCTIONS
    assert 'same scope and rule' in SYSTEM_INSTRUCTIONS
    assert 'do not wait for the user to ask' in SYSTEM_INSTRUCTIONS
    ledger_tool = next(tool for tool in tool_definitions() if tool['name'] == 'ledger_task')
    assert 'search uses both need and situation' in ledger_tool['description']
    assert 'non-retryable' in ledger_tool['description']
    skill = (Path(__file__).resolve().parents[3] / 'skills/macos-cua/SKILL.md').read_text()
    assert 'the helper searches both `need` and `situation`' in skill
    assert '`ledger_invalid_argument` is non-retryable' in skill
    assert 'If `has_more` is true, ask rather than select from an incomplete set' in skill


def test_preference_instructions_cover_agreement_and_conflict():
    ledger_tool = next(tool for tool in tool_definitions() if tool['name'] == 'ledger_task')
    skill = (Path(__file__).resolve().parents[3] / 'skills/macos-cua/SKILL.md').read_text()
    for instructions in (SYSTEM_INSTRUCTIONS, ledger_tool['description'], skill):
        assert 'rule names differ' in instructions
        assert 'conflict' in instructions
        assert 'has_more' in instructions
        assert 'genuinely global preferences' in instructions


def test_preference_normalizes_bare_app_scope(tmp_path):
    transport = Mock()
    transport.call.return_value = {'ok': True, 'data': {'decision_id': 'saved'}}
    helper = LedgerHelper(transport, Mock(), tmp_path / 'pending')
    quote = 'Please use Work for my email account'
    helper.note_user(quote)

    result = helper.run(operation='preference', scope='Gmail', rule='Account Select',
                        value='Work', owner_quote=quote)

    assert result['status'] == 'saved'
    args = transport.call.call_args.args[1]
    assert (args['scope'], args['rule']) == ('tool:gmail', 'account_select')


def test_preference_normalizes_scope_and_rule_before_lookup_or_save(tmp_path):
    transport = Mock()
    transport.call.return_value = {'ok': True, 'data': {'decision_id': 'saved'}}
    helper = LedgerHelper(transport, Mock(), tmp_path / 'pending')
    quote = 'Please use Work for my email account'
    helper.note_user(quote)
    result = helper.run(operation='preference', scope=' Tool:Gmail ', rule='Account Select',
                        value='Work', owner_quote=quote)
    assert result['status'] == 'saved'
    args = transport.call.call_args.args[1]
    assert (args['scope'], args['rule']) == ('tool:gmail', 'account_select')


def test_lookup_normalizes_bare_site_scope_without_widening(tmp_path):
    match = {'decision_id': 'saved', 'decision': 'Work'}
    transport = Mock()
    transport.call.return_value = {'ok': True, 'data': {'matches': [match]}}
    helper = LedgerHelper(transport, lambda _: {'status': 'found', 'decision_ids': ['saved']}, tmp_path / 'pending')

    result = helper.run(operation='lookup', scope='Video Service', rule='Profile Selection',
                        situation='choose a profile', need='profile')

    assert result['decisions'] == [match]
    args = transport.call.call_args.args[1]
    assert args['scope'] == 'tool:video-service'
    assert args['rule'] == 'profile_selection'
    assert args['caller_scope'] == {'project': None, 'tools': ['video-service']}
    transport.reset_mock()
    invalid = helper.run(operation='lookup', scope='site:video-service', rule='Profile Selection',
                         situation='choose a profile', need='profile')
    assert invalid['error_type'] == 'ledger_invalid_argument'
    transport.call.assert_not_called()


def test_lookup_observes_preference_saved_after_empty_lookup(tmp_path):
    stored = []
    def call(name, args):
        if name == 'find':
            return {'ok': True, 'data': {'matches': list(stored)}}
        stored[:] = [{'decision_id': 'saved', 'decision': args['decision']}]
        return {'ok': True, 'data': {'decision_id': 'saved'}}
    helper = LedgerHelper(Mock(call=call), lambda _: {'status': 'found', 'decision_ids': ['saved']}, tmp_path / 'pending')
    lookup = {'operation': 'lookup', 'scope': 'tool:gmail', 'situation': 'choose account', 'need': 'account'}
    assert helper.run(**lookup)['status'] == 'none'
    quote = 'Please use Work for my email account'
    helper.note_user(quote)
    assert helper.run(operation='preference', scope='tool:gmail', rule='account_select', value='Work', owner_quote=quote)['status'] == 'saved'
    assert helper.run(**lookup)['decisions'][0]['decision'] == 'Work'
    quote = 'Please use Personal for my email account'
    helper.note_user(quote)
    assert helper.run(operation='preference', scope='tool:gmail', rule='account_select', value='Personal', owner_quote=quote)['status'] == 'saved'
    assert helper.run(**lookup)['decisions'][0]['decision'] == 'Personal'
    stored[:] = [{'decision_id': 'saved', 'decision': 'Family'}]
    assert helper.run(**lookup)['decisions'][0]['decision'] == 'Family'


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
