from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from unittest.mock import Mock, patch

from voice_cua import SYSTEM_INSTRUCTIONS
from voice_cua import tools
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
        ('tool:gmail', 'account_select', 'Work', 'Please use Work for my email account for future choices'),
        ('tool:macos', 'default_browser', 'Safari', 'Please use Safari as my browser going forward'),
    ]:
        helper.note_user(quote)
        result = helper.run(operation='preference', scope=scope, rule=rule, value=value, owner_quote=quote)
        assert result['status'] == 'saved'
        args = transport.call.call_args.args[1]
        assert (args['scope'], args['rule'], args['decision']) == (scope, rule, value)
    replay = LedgerHelper(transport, decide, tmp_path / 'pending2')
    assert replay.run(operation='recall', scope='Gmail', rule='Account Select', need='email account',
                      situation='choose Gmail account')['data']['matches'][0]['decision'] == 'Work'
    decide.assert_not_called()
    assert 'ledger_task' in {tool['name'] for tool in tool_definitions()}
    assert 'skip routine dev, shell, and file turns' in SYSTEM_INSTRUCTIONS
    assert 'same scope and rule' in SYSTEM_INSTRUCTIONS
    assert 'Do not wait for the user to ask' in SYSTEM_INSTRUCTIONS
    ledger_tool = next(tool for tool in tool_definitions() if tool['name'] == 'ledger_task')
    assert 'For recall, always provide both a concrete need and the actual situation' in ledger_tool['description']
    assert 'non-retryable' in ledger_tool['description']
    skill = (Path(__file__).resolve().parents[3] / 'skills/macos-cua/SKILL.md').read_text()
    assert 'the helper searches both `need` and `situation`' in skill
    assert '`ledger_invalid_argument` is non-retryable' in skill
    assert 'If `has_more` is true, ask rather than select from an incomplete set' in skill


def test_casual_preference_mention_is_not_saved(tmp_path):
    transport = Mock()
    helper = LedgerHelper(transport, Mock(), tmp_path / 'pending')
    quote = 'My favourite colour is teal.'
    helper.note_user(quote)
    with patch('voice_cua.tools._LEDGER_HELPER', helper):
        result = tools._ledger_task({
            'operation': 'preference', 'scope': 'fleet', 'rule': 'favourite_colour',
            'value': 'teal', 'owner_quote': quote,
        })
    assert result['error_type'] == 'ledger_invalid_argument'
    assert 'owner_quote' in result['error']
    transport.call.assert_not_called()


def test_acu13_corrected_explicit_preference_request_is_accepted(tmp_path):
    transport = Mock()
    transport.call.return_value = {'ok': True, 'data': {'decision_id': 'saved'}}
    helper = LedgerHelper(transport, Mock(), tmp_path / 'pending')
    quote = 'Please remember my favourite colour is teal.'
    helper.note_user(quote)
    result = helper.run(operation='preference', scope='fleet', rule='favourite_colour',
                        value='teal', owner_quote=quote)
    assert result['status'] == 'saved'
    name, payload = transport.call.call_args.args
    assert name == 'save'
    assert (payload['type'], payload['scope'], payload['rule'], payload['decision']) == (
        'user_decision', 'fleet', 'favourite_colour', 'teal')


def test_followup_remember_that_saves_the_preceding_quoted_preference(tmp_path):
    transport = Mock()
    transport.call.return_value = {'ok': True, 'data': {'decision_id': 'followup-saved'}}
    helper = LedgerHelper(transport, Mock(side_effect=AssertionError('helper model called')), tmp_path / 'pending')
    helper.note_user('My favourite colour is teal.')
    helper.note_user('Please remember that.')
    result = helper.run(operation='preference', scope='fleet', rule='favourite_colour', value='teal',
                        owner_quote='my favourite colour is teal')
    assert result['status'] == 'saved'
    assert result['decision_id'] == 'followup-saved'
    assert transport.call.call_args.args[0] == 'save'


def test_followup_save_without_reference_does_not_save_preference(tmp_path):
    transport = Mock()
    helper = LedgerHelper(transport, Mock(side_effect=AssertionError('helper model called')), tmp_path / 'pending')
    helper.note_user('My favourite colour is teal.')
    helper.note_user('Please remember to call Mom.')
    result = helper.run(operation='preference', scope='fleet', rule='favourite_colour', value='teal',
                        owner_quote='my favourite colour is teal')
    assert result['error_type'] == 'ledger_invalid_argument'
    assert 'explicit save or future-use intent' in result['error']
    transport.call.assert_not_called()


def test_grok_2035_capture_fixture_saves_the_followup_preference_through_both_provider_tools(tmp_path):
    from voice_cua.providers import LiveProvider, XAIProvider

    transport = Mock()
    transport.call.return_value = {'ok': True, 'data': {'decision_id': 'grok-2035-saved'}}
    request = {
        'operation': 'capture', 'scope': 'fleet', 'rule': 'favourite_colour', 'value': 'teal',
        'owner_quote': 'my favourite colour is teal',
        'capture_gate': {
            'owner_decision': 'User stated their favourite colour is teal',
            'future_task': 'Any future preference involving colour choices',
            'changed_choice': 'No prior colour preference on record', 'unique_value': 'teal',
            'lifetime': 'durable', 'sensitivity': 'model_safe',
        },
    }
    ledger_tool = next(tool for tool in tools.tool_definitions(groups={'ledger'})
                       if tool['name'] == 'ledger_task')
    for index, provider in enumerate((XAIProvider('grok-voice-latest'), LiveProvider())):
        wire = provider.outbound({'type': 'session.update', 'session': {'tools': [ledger_tool]}})
        assert 'ledger_task' in str(wire)
        helper = LedgerHelper(transport, Mock(side_effect=AssertionError('helper model called')),
                              tmp_path / f'pending-{index}')
        with patch.object(tools, '_LEDGER_HELPER', helper):
            tools.note_user_turn('My favourite colour is teal.')
            tools.note_user_turn('Please remember that.')
            result = tools._ledger_task(request)
        assert result['status'] == 'saved'
        assert result['decision_id'] == 'grok-2035-saved'
    assert transport.call.call_count == 2
    assert all(call.args[0] == 'save' for call in transport.call.call_args_list)


def test_oversized_context_fields_return_the_offending_field(tmp_path):
    helper = LedgerHelper(Mock(), Mock(), tmp_path / 'pending')
    for field, limit in (('situation', 1000), ('need', 500), ('project', 80)):
        request = {'operation': 'lookup', 'situation': 's', 'need': 'n'}
        request[field] = 'x' * (limit + 1)
        result = helper.run(**request)
        assert result['error_type'] == 'ledger_invalid_argument'
        assert field in result['error']


def test_ledger_schema_separates_preference_and_capture_fields():
    tool = next(tool for tool in tool_definitions(groups={'ledger'}) if tool['name'] == 'ledger_task')
    fields = tool['parameters']['properties']
    assert 'Use preference for user preferences' in fields['operation']['description']
    assert 'do not use capture for a preference' in fields['operation']['description']
    assert 'immediate' in fields['operation']['description']
    assert 'save intent may be in an immediate follow-up' in fields['owner_quote']['description']
    assert 'required for lookup/capture' in fields['situation']['description']
    assert 'required for lookup/capture' in fields['need']['description']


def test_preference_instructions_cover_agreement_and_conflict():
    ledger_tool = next(tool for tool in tool_definitions() if tool['name'] == 'ledger_task')
    skill = (Path(__file__).resolve().parents[3] / 'skills/macos-cua/SKILL.md').read_text()
    for instructions in (SYSTEM_INSTRUCTIONS, ledger_tool['description'], skill):
        assert 'rule names differ' in instructions
        assert 'conflict' in instructions
        assert 'has_more' in instructions
        assert 'genuinely global preferences' in instructions
        assert 'self-reported record' in instructions
        assert 'do not claim owner attestation' in instructions


def test_preference_normalizes_bare_app_scope(tmp_path):
    transport = Mock()
    transport.call.return_value = {'ok': True, 'data': {'decision_id': 'saved'}}
    helper = LedgerHelper(transport, Mock(), tmp_path / 'pending')
    quote = 'Please use Work for my email account for future choices'
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
    quote = 'Please use Work for my email account going forward'
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
    quote = 'Please use Work for my email account going forward'
    helper.note_user(quote)
    assert helper.run(operation='preference', scope='tool:gmail', rule='account_select', value='Work', owner_quote=quote)['status'] == 'saved'
    assert helper.run(**lookup)['decisions'][0]['decision'] == 'Work'
    quote = 'Please use Personal for my email account going forward'
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


def test_snake_case_value_matches_spoken_words(tmp_path):
    transport = Mock()
    transport.call.return_value = {'ok': True, 'data': {'decision_id': 'saved'}}
    helper = LedgerHelper(transport, Mock(), tmp_path / 'pending')
    quote = 'I prefer dark mode in every app'
    helper.note_user('Remember that ' + quote)
    result = helper.run(operation='preference', scope='fleet', rule='app_appearance_mode', value='dark_mode', owner_quote=quote)
    assert result['status'] == 'saved', result
