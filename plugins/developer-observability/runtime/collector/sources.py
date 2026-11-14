"""Source adapters. Each yields normalized events; nothing here stores raw lines, audio or transcripts."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from datetime import datetime, timezone
import xml.etree.ElementTree as ET

from .scrub import DENY_KEYS, TEXT_KINDS, scrub, scrub_detail

# Real step durations, in priority order. `step_ms` is a gap since the previous row, so it only
# counts for successful rows that are not an end, close, failure or idle phase (see END_PHASE).
DURATION_KEYS = ('speech_to_playback_ms', 'latency_ms', 'duration_ms', 'took_ms', 'backend_ms')
END_PHASE = re.compile(r'(close|closed|exit|done|error|fail|gave_up|updated|reconnect|stop|idle|expired)')
ERROR_STATUS = {'fail', 'failed', 'error', 'err'}
# Kinds that re-display errors already logged elsewhere (the island HUD): counted as mirrors, not failures.
MIRROR_KINDS = {'island_error_raw'}
# Row metadata: never part of a tool call's argument fingerprint.
ARG_META = {'ts', 'time', 'timestamp', 'kind', 'event', 'type', 'name', 'session_id', 'session', 'tool', 'phase',
            'step', 'stage', 'status', 'ok', 'source', 'call_id', 'response_id', 'item_id', 'delegation_id', 'id',
            'elapsed_ms', 'step_ms', 'provider', 'version', 'purpose', 'schema'}
# Environment conditions, not code failures: their own category.
CAUSES = (('no input device', 'no_input_device'),)
# Test fixtures: canned replies answered within a few ms, and explicit self-test sessions.
CANNED_REPLIES = {'RIGHT', 'WRONG'}
INSTANT_MS = 2
TEST_PURPOSES = {'self_test', 'test', 'fixture'}
SKIP_DIRS = {'.git', 'node_modules', '.build', '__pycache__', '.venv'}
_ISO = re.compile(r'^\[?(\d{4}-\d\d-\d\dT[\d:.]+(?:Z|[+-]\d\d:?\d\d)?)')
_LOG_ERROR = re.compile(r'(?i)\b(error|exception|traceback|fatal|failed|failure)\b')
_PHASE = re.compile(r'\bphase=(\S+)')
_WORD = re.compile(r'\b([a-z_]+):')
_NAMED_MS = re.compile(r'\b(\w+?)_ms=(\d+(?:\.\d+)?)')
_PLAIN_MS = re.compile(r'(\d+(?:\.\d+)?)\s?ms\b')
_CHECK_FAILED = re.compile(r'^failed: test (.+?) \(exit (\d+)\)')
_XCTEST = re.compile(r'error: -\[([\w.]+) (\w+)\] : (.*)')
_CHECK_TEST = re.compile(r'^test (.+?) \(([\d.]+)s, exit (\d+)\)')
_TASK_REF = re.compile(r'[A-Za-z0-9_.:/-]{1,128}\Z')
_CHECK_ID = re.compile(r'(?i)^(?:run_id|check_id)=([A-Za-z0-9_.:-]{1,128})$')
_TASK_FIELDS = {
    'request': ('request', 'task_request'), 'typed_text': ('typed_text',), 'error_message': ('error_message',),
    'typed_text_redacted_reason': ('typed_text_redacted_reason',),
    'typed_text_length': ('typed_text_length', 'typed_text_chars'),
    'query_terms': ('query_terms', 'ledger_query_terms'),
    'invalid_query_terms': ('invalid_query_terms',), 'selected_id': ('selected_id', 'ledger_selected_id'),
    'action': ('action',), 'target': ('target',), 'expected_target': ('expected_target', 'expected'),
    'found_target': ('found_target', 'found'), 'url': ('url',), 'lease': ('lease',),
    'cancel': ('cancel',), 'cancel_ack': ('cancel_ack',), 'cancel_state': ('cancel_state',),
    'target_run_ref': ('target_run_ref',), 'queued_action': ('queued_action',),
    'queued_action_suppressed': ('queued_action_suppressed',),
    'expected_outcome': ('expected_outcome', 'outcome_expected'),
    'hint': ('hint',), 'verified': ('verified',), 'completion': ('completion',),
    'match_count': ('match_count',), 'near_miss_count': ('near_miss_count',),
    'matching_targets': ('matching_targets',), 'near_miss_targets': ('near_miss_targets',),
    'app': ('app',), 'window': ('window',),
}
_NESTED_TASK_KEYS = {'app', 'window', 'name', 'title', 'role', 'id', 'status', 'state', 'active',
                     'released', 'requested', 'acknowledged', 'suppressed', 'started', 'count', 'url',
                     'target', 'expected', 'found', 'match_count', 'near_miss_count'}


def _task_value(value, limit=1024):
    if isinstance(value, str):
        return scrub(value, limit, preserve_emails=True) or ''
    if value is None or type(value) in (bool, int, float):
        return value
    if isinstance(value, (list, tuple)):
        return [_task_value(item, 240) for item in value[:20]
                if isinstance(item, (str, bool, int, float, dict, list, tuple))]
    if isinstance(value, dict):
        return {key: (_run_ref(item) if key.endswith('_run_ref') and isinstance(item, str)
                      else _task_value(item, 240)) for key, item in list(value.items())[:20]
                if isinstance(key, str) and (key in _NESTED_TASK_KEYS or key.endswith('_run_ref'))
                and isinstance(item, (str, bool, int, float, list, tuple, dict, type(None)))}
    return None


def _task_id(value):
    return value if isinstance(value, str) and _TASK_REF.fullmatch(value) else None


def _test_step_ref(value):
    if step_ref := _task_id(value):
        return step_ref
    return f'test-{hashlib.sha256(value.encode()).hexdigest()[:24]}' if isinstance(value, str) and value else None


def _run_ref(value):
    if isinstance(value, str) and re.fullmatch(r'[0-9a-fA-F]{24}', value):
        return value.lower()
    return hashlib.sha256(value.encode()).hexdigest()[:24] if isinstance(value, str) and 0 < len(value) <= 512 else None


def _task_fields(obj):
    fields = {}
    for output, keys in _TASK_FIELDS.items():
        key = next((key for key in keys if key in obj), None)
        if key is None:
            continue
        value = _task_value(obj[key], 4096 if output in ('request', 'typed_text', 'url') else 1024)
        if value is not None:
            fields[output] = value
    if fields.get('typed_text_redacted_reason') and isinstance(fields.get('typed_text_length'), int):
        fields['typed_text'] = f"redacted ({fields['typed_text_redacted_reason']}, {fields['typed_text_length']} chars)"
    arguments = obj.get('arguments')
    if isinstance(arguments, dict):
        allowed = {key for keys in _TASK_FIELDS.values() for key in keys}
        fields['arguments'] = {key: (_run_ref(value) if key.endswith('_run_ref') and isinstance(value, str)
                                     else _task_value(value)) for key, value in list(arguments.items())[:20]
                               if (key in allowed or isinstance(key, str) and key.endswith('_run_ref'))
                               and _task_value(value) is not None}
    for key, value in obj.items():
        if isinstance(key, str) and key.endswith('_run_ref') and isinstance(value, str):
            fields[key] = _run_ref(value)
    if 'error_type' in obj and isinstance(obj['error_type'], str):
        fields['error_type'] = scrub(obj['error_type'], 120)
    return fields


_HOME = str(Path.home())


def ref(path, line) -> str:
    text = str(path)
    if text.startswith(_HOME):
        text = '~' + text[len(_HOME):]
    return f'{text}:{line}'


def iso(ts):
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def parse_ts(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e12 else float(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _first_str(obj, keys):
    for key in keys:
        if isinstance(obj.get(key), str) and obj[key]:
            return obj[key]
    return None


def _stat(stats, source):
    return stats.setdefault(source, {'events': 0, 'errors': 0, 'durations': 0, 'skipped': 0,
                                     'last_event_at': None, 'read': False, 'missing': False})


def _event(stats, *, project, source, kind, step, error, ts, duration_ms, message, where, session=None,
           metric=None, condition=None, mirror=False, test=False, version=None, alias=False, turn=None,
           args=None, layout=None, session_attribution=None, version_attribution=None, ledger_operation=None,
           ledger_error_category=None, ledger_result_status=None, ledger_decision_count=None, ledger_scope_kind=None, ledger_rule_supplied=None,
           task_outcome=None, task_check_id=None, task_evidence_class=None, task_tools=None,
           call_ref=None, response_ref=None, run_ref=None, error_details=None, ledger_result_detail=None,
           outcome=None, ledger_detail_truncated=None, input_availability=None, input_device=None,
           input_rate=None, input_recovery=None, input_selection=None, mirror_ref=None, build=None,
           ledger_rule=None, ledger_selected_count=None, ledger_selected_id=None, ledger_supersedes=None,
           ledger_decision_id=None, ledger_revision=None, step_ref=None, retry_of_step_ref=None,
           retry_of_run_ref=None, mirror_of=None, caused_by=None, task_data=None):
    item = _stat(stats, source)
    item['events'] += 1
    item['errors'] += bool(error)
    item['durations'] += duration_ms is not None
    item['mirrors'] = item.get('mirrors', 0) + bool(mirror)
    item['test_events'] = item.get('test_events', 0) + bool(test)
    if ts is not None and (item['last_event_at'] is None or iso(ts) > item['last_event_at']):
        item['last_event_at'] = iso(ts)
    event = {'project': project, 'source': source, 'kind': kind, 'step': step, 'error': bool(error),
             'ts': ts, 'duration_ms': duration_ms, 'metric': metric, 'message': message, 'ref': where,
             'session': session, 'condition': condition, 'mirror': bool(mirror), 'test': bool(test),
             'version': version, 'alias': bool(alias),
             'session_attribution': session_attribution, 'version_attribution': version_attribution,
             'outcome': outcome}
    optional = {'turn': turn, 'args': args, 'layout': layout,
                'ledger_operation': ledger_operation, 'ledger_error_category': ledger_error_category,
                'ledger_result_status': ledger_result_status, 'ledger_decision_count': ledger_decision_count,
                'ledger_scope_kind': ledger_scope_kind, 'ledger_rule_supplied': ledger_rule_supplied,
                'task_outcome': task_outcome, 'task_check_id': task_check_id,
                'task_evidence_class': task_evidence_class, 'task_tools': task_tools,
                'call_ref': call_ref, 'response_ref': response_ref, 'run_ref': run_ref,
                'error_details': error_details, 'ledger_result_detail': ledger_result_detail,
                'ledger_detail_truncated': ledger_detail_truncated, 'input_availability': input_availability,
                'input_device': input_device, 'input_rate': input_rate, 'input_recovery': input_recovery,
                'input_selection': input_selection, 'mirror_ref': mirror_ref, 'build': build,
                'ledger_rule': ledger_rule, 'ledger_selected_count': ledger_selected_count,
                'ledger_selected_id': ledger_selected_id, 'ledger_supersedes': ledger_supersedes,
                'ledger_decision_id': ledger_decision_id, 'ledger_revision': ledger_revision,
                'step_ref': step_ref, 'retry_of_step_ref': retry_of_step_ref,
                'retry_of_run_ref': retry_of_run_ref, 'mirror_of': mirror_of}
    for key, value in optional.items():
        if value is not None:
            event[key] = value
    if caused_by:
        event['caused_by'] = caused_by
    if task_data:
        event['task_data'] = task_data
    return event


def _normalize(value):
    """Case- and whitespace-insensitive call arguments; structure is preserved."""
    if isinstance(value, str):
        return ' '.join(value.split()).lower()
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in value.items()}
    return value


def _args_fingerprint(obj):
    """A short, stable hash of the call's arguments; row metadata and denied keys never count."""
    payload = {key: value for key, value in obj.items() if key not in ARG_META and key not in DENY_KEYS}
    if not payload:
        return None
    normalized = _normalize(payload)
    return hashlib.sha256(json.dumps(normalized, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _coord(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _rect(value):
    if not isinstance(value, dict):
        return None
    x, y, w, h = (_coord(value.get(key)) for key in ('x', 'y', 'w', 'h'))
    if None in (x, y, w, h):
        return None
    return {'id': str(value.get('id', '')), 'x': x, 'y': y, 'w': w, 'h': h}


def _layout(obj):
    """Island HUD geometry (schema 1); None means the row cannot be trusted and is a parse failure."""
    raw_screen, raw_island, raw_overlays = obj.get('screen'), obj.get('island'), obj.get('overlays')
    screen, island = _rect(raw_screen), _rect(raw_island)
    if screen is None or island is None or not isinstance(raw_overlays, list):
        return None
    island['visible'] = bool(raw_island.get('visible'))
    overlays = []
    for raw in raw_overlays:
        rect = _rect(raw)
        if rect is None or not isinstance(raw.get('kind'), str):
            return None
        rect['kind'] = raw['kind'][:40]
        rect['visible'] = bool(raw.get('visible'))
        rect['expected_inside_island'] = bool(raw.get('expected_inside_island'))
        overlays.append(rect)
    trigger = obj.get('trigger') if isinstance(obj.get('trigger'), str) else ''
    result = {'trigger': trigger[:20], 'screen': screen, 'island': island, 'overlays': overlays}
    transition = obj.get('transition_id')
    if isinstance(transition, str) and re.fullmatch(r'[0-9a-fA-F-]{36}', transition):
        result['transition_ref'] = hashlib.sha256(transition.encode()).hexdigest()[:24]
        if obj.get('transition_phase') in ('start', 'change', 'settled'):
            result['transition_phase'] = obj['transition_phase']
        if isinstance(obj.get('transition_trigger'), str):
            result['transition_trigger'] = scrub(obj['transition_trigger'])[:40]
    return result


def _lines(path, max_bytes):
    path = Path(path)
    if path.suffix == '.gz':
        # A rotated Samantha log (agent-computer-use 0.2.86: <name>.N.<ext>.gz, at most 50 MB uncompressed).
        with gzip.open(path, 'rb') as handle:
            number = 0
            for raw in handle:
                number += 1
                yield number, raw.decode('utf-8', errors='replace')
        return
    size = path.stat().st_size
    with path.open('rb') as handle:
        start = 0
        if size > max_bytes:
            start = size - max_bytes
            handle.seek(start)
            handle.readline()
        number = 0
        for raw in handle:
            number += 1
            yield number, raw.decode('utf-8', errors='replace')


def _cause(*texts):
    for text in texts:
        low = str(text or '').lower()
        for needle, cause in CAUSES:
            if needle in low:
                return cause
    return None


def _jsonl_rows(path, max_bytes, stats, source):
    for number, line in _lines(path, max_bytes):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            obj = None
        if not isinstance(obj, dict):
            stats[source]['skipped'] += 1
            continue
        yield number, obj


def _session_flags_update(info, obj):
    session = obj.get('session_id') or obj.get('session')
    if not isinstance(session, str) or not session:
        return
    entry = info.setdefault(session, {'versions': set(), 'purpose_test': False, 'canned': False, 'instant': False})
    if isinstance(obj.get('version'), str) and obj['version']:
        entry['versions'].add(obj['version'][:40])
    if obj.get('purpose') in TEST_PURPOSES:
        entry['purpose_test'] = True
    # Replies are compared against two canned words only; their text is never stored.
    if obj.get('kind') == 'assistant_reply' and obj.get('text') in CANNED_REPLIES:
        entry['canned'] = True
    step = _number(obj.get('step_ms'))
    if obj.get('phase') == 'response_done' and step is not None and step <= INSTANT_MS:
        entry['instant'] = True


def _sessions(path, max_bytes):
    """Collect flags only from explicitly identified sessions; conflicting versions stay unknown."""
    info = {}
    for _line, obj in _jsonl_rows(path, max_bytes, {None: {'skipped': 0}}, None):
        _session_flags_update(info, obj)
    return info


def _apply_session_flags(event, info, stats, source):
    flags = info.get(event.get('session'), {})
    if event.get('version_attribution') != 'event':
        versions = flags.get('versions', set())
        if len(versions) == 1:
            event['version'] = next(iter(versions))
            event['version_attribution'] = 'session_inferred'
    test = bool(flags.get('purpose_test') or (flags.get('canned') and flags.get('instant')))
    if test and not event.get('test'):
        event['test'] = True
        item = stats.get(source)
        if item is not None:
            item['test_events'] = item.get('test_events', 0) + 1


def iter_jsonl(path, project, source, stats, max_bytes=64_000_000):
    _stat(stats, source)
    produced, info = [], {}
    for number, obj in _jsonl_rows(path, max_bytes, stats, source):
        _session_flags_update(info, obj)
        own = obj.get('session_id') or obj.get('session')
        current = own if isinstance(own, str) and own else None
        direct_version = obj.get('version')
        if isinstance(direct_version, str) and direct_version:
            version, version_attribution = direct_version[:40], 'event'
        else:
            version, version_attribution = None, 'unknown'
        test = False
        error_details = scrub_detail(obj.get('error_details'))
        ledger_result_detail = scrub_detail(obj.get('ledger_result_detail'))
        obj = {key: value for key, value in obj.items() if key not in DENY_KEYS}
        kind = _first_str(obj, ('kind', 'event', 'type', 'name')) or 'event'
        step = 'task_outcome' if kind == 'task_outcome' else (_first_str(obj, ('phase', 'tool', 'step', 'stage')) or '')
        status = str(obj.get('status', '')).lower()
        err = obj.get('error')
        error = (status in ERROR_STATUS or 'error' in kind or 'fail' in kind or bool(isinstance(err, (str, dict)) and err)
                 or bool(obj.get('error_type'))
                 or obj.get('ok') is False)
        if kind == 'tool_error' and str(obj.get('error_type', '')).startswith('needs_confirm'):
            kind, error = 'tool_needs_confirmation', False
        mirror = kind in MIRROR_KINDS
        error = error and not mirror
        text = err.get('message') if isinstance(err, dict) else None
        raw_message = text if isinstance(text, str) else _first_str(obj, ('error', 'error_message', 'detail', 'message', 'reason', 'msg'))
        cause = obj.get('cause') if isinstance(obj.get('cause'), str) else _cause(obj.get('detail'), obj.get('error'))
        condition = cause if cause and (error or status == 'skip') else None
        metric = next((key for key in DURATION_KEYS if _number(obj.get(key)) is not None), None)
        duration = obj[metric] if metric else None
        if duration is None and _number(obj.get('duration_s')) is not None:
            duration, metric = obj['duration_s'] * 1000, 'duration_s'
        if (duration is None and _number(obj.get('step_ms')) is not None and not error and status in ('ok', '')
                and not END_PHASE.search(step or kind)):
            duration, metric = obj['step_ms'], 'step_ms'
        ts = next((parse_ts(obj[key]) for key in ('ts', 'time', 'timestamp') if key in obj), None)
        message = '' if kind in TEXT_KINDS else scrub(raw_message)
        if condition:
            message, error = f'cause: {condition}', True
        turn_id = obj.get('turn_id')
        turn = str(turn_id)[:40] if ((isinstance(turn_id, int) and not isinstance(turn_id, bool))
                                    or (isinstance(turn_id, str) and turn_id)) else None
        args = _args_fingerprint(obj) if kind == 'tool_start' or kind.endswith('_call') else None
        layout = None
        if kind == 'island_layout':
            layout = _layout(obj)
            if layout is None:
                stats[source]['skipped'] += 1
                continue
            if status:
                layout['hud_status'] = status[:40]
            # Island HUD status (idle/listening/error/...) is presentation, not a collector failure.
            error = False
            condition = None
        operation = obj.get('ledger_operation') if step == 'ledger_task' or 'ledger' in kind.lower() else None
        operation = operation if operation in ('lookup', 'capture', 'recall', 'preference', 'unknown') else None
        scope_kind = obj.get('ledger_scope_kind') if step == 'ledger_task' else None
        scope_kind = scope_kind if scope_kind in ('missing', 'fleet', 'browser', 'tool', 'project', 'invalid') else None
        rule_supplied = obj.get('ledger_rule_supplied') if step == 'ledger_task' else None
        rule_supplied = rule_supplied if type(rule_supplied) is bool else None
        category = obj.get('ledger_error_category') if step == 'ledger_task' else None
        category = category if category in ('invalid_argument', 'invalid_query_terms', 'unavailable', 'outcome_unknown', 'other') else None
        ledger_status = obj.get('ledger_result_status') if step == 'ledger_task' or 'ledger' in kind.lower() else None
        ledger_status = ledger_status[:32] if isinstance(ledger_status, str) else None
        decision_count = obj.get('ledger_decision_count') if step == 'ledger_task' or 'ledger' in kind.lower() else None
        decision_count = decision_count if type(decision_count) is int and 0 <= decision_count <= 3 else None
        task_outcome = obj.get('outcome') if kind == 'task_outcome' else None
        task_outcome = {'success': 'success', 'failure': 'failure', 'cancelled': 'cancelled',
                        'unknown': 'unknown'}.get(task_outcome, task_outcome if task_outcome in
                                                   ('verified_success', 'verified_incomplete', 'unverified') else None)
        task_check_id = obj.get('check_id') if kind == 'task_outcome' else None
        task_check_id = task_check_id[:48] if isinstance(task_check_id, str) and re.fullmatch(r'[a-z][a-z0-9]*(?:_[a-z0-9]+)*', task_check_id) else None
        task_evidence_class = obj.get('evidence_class') if kind == 'task_outcome' else None
        task_evidence_class = task_evidence_class if task_evidence_class in ('app_readback', 'browser_inventory', 'local_login', 'combined', 'tool_reported') else None
        task_tools = obj.get('tools') if kind == 'task_outcome' else None
        task_tools = sorted({tool for tool in task_tools if isinstance(tool, str) and tool in ('cua_act', 'comet_act', 'system_volume')})[:3] if isinstance(task_tools, list) else None
        def opaque_id(key):
            value = obj.get(key)
            return hashlib.sha256(value.encode()).hexdigest()[:24] if isinstance(value, str) and 0 < len(value) <= 128 else None
        # Installed build identity recorded on the row by the producer (OBS-06); version-only rows
        # keep it unknown rather than guessing from the session.
        build = obj.get('build')
        build = build[:40] if isinstance(build, str) and re.fullmatch(r'[A-Za-z0-9_.+\-]{1,40}', build) else None
        ledger_rule = obj.get('ledger_rule')
        ledger_rule = ledger_rule[:64] if isinstance(ledger_rule, str) and re.fullmatch(r'[a-z0-9_]{1,64}', ledger_rule) else None
        selected_count = obj.get('ledger_selected_count')
        selected_count = selected_count if type(selected_count) is int and 0 <= selected_count <= 3 else None
        supersedes = obj.get('supersedes') if isinstance(obj.get('supersedes'), str) else None
        refs = obj.get('caused_by')
        caused_by = [_task_id(value) for value in refs] if isinstance(refs, list) else []
        caused_by = [value for value in caused_by if value]
        produced.append(_event(stats, project=project, source=source, kind=kind, step=step, error=error, ts=ts,
                     duration_ms=duration, metric=metric, message=message, where=ref(path, number), session=current,
                     condition=condition, mirror=mirror, test=test, version=version, turn=turn,
                     args=args, layout=layout, session_attribution='event' if current else 'unknown',
                     version_attribution=version_attribution, ledger_operation=operation,
                     ledger_scope_kind=scope_kind, ledger_rule_supplied=rule_supplied,
                     ledger_error_category=category, ledger_result_status=ledger_status,
                     ledger_decision_count=decision_count, task_outcome=task_outcome,
                     task_check_id=task_check_id, task_evidence_class=task_evidence_class,
                     task_tools=task_tools, error_details=error_details or None,
                     ledger_result_detail=ledger_result_detail or None,
                     ledger_detail_truncated=obj.get('ledger_detail_truncated') if type(obj.get('ledger_detail_truncated')) is bool else None,
                     input_availability=obj.get('input_availability') if obj.get('input_availability') in ('available','unavailable','unknown') else None,
                     input_device=scrub(obj.get('input_device'))[:120] if isinstance(obj.get('input_device'), str) else None,
                     input_rate=obj.get('input_rate') if type(obj.get('input_rate')) in (int,float) and 0 < obj['input_rate'] <= 768000 else None,
                     input_recovery=obj.get('input_recovery') if obj.get('input_recovery') in ('recovered','not_observed','unknown') else None,
                     input_selection=obj.get('input_selection') if obj.get('input_selection') == 'system_default_snapshot' else None,
                     call_ref=opaque_id('call_id'), response_ref=opaque_id('response_id'), run_ref=_run_ref(obj.get('run_id')),
                     mirror_ref=opaque_id('mirror_id'), build=build,
                     ledger_rule=ledger_rule, ledger_selected_count=selected_count,
                     ledger_selected_id=opaque_id('ledger_selected_id'), ledger_supersedes=opaque_id('supersedes')
                     if 'supersedes' in obj else opaque_id('ledger_supersedes'),
                     ledger_decision_id=opaque_id('ledger_decision_id') or opaque_id('decision_id'),
                     ledger_revision=obj.get('revision') if type(obj.get('revision')) is int and 0 <= obj['revision'] <= 100000 else None,
                     step_ref=_task_id(obj.get('step_ref')) or opaque_id('call_id'),
                     retry_of_step_ref=_task_id(obj.get('retry_of_step_ref')),
                     retry_of_run_ref=_run_ref(obj.get('retry_of_run_ref')),
                     mirror_of=_task_id(obj.get('mirror_of')), caused_by=caused_by,
                     task_data=_task_fields(obj),
                     outcome='failure' if error else ('success' if obj.get('ok') is True or status == 'ok' else 'unknown')))
    for event in produced:
        _apply_session_flags(event, info, stats, source)
        yield event


_SESSION = re.compile(r'\bsession_id=(\w+)')
_ERROR_TEXT = (re.compile(r"'message': '([^']*)'"), re.compile(r'\berror=(.+)$'), re.compile(r'(?i)\berror: (.+)$'))


def _known_phases(path, max_bytes):
    phases = set()
    for _line, text in _lines(path, max_bytes):
        if 'phase=' in text and (match := _PHASE.search(text)) and len(match.group(1)) >= 6:
            phases.add(match.group(1).lower())
    return sorted(phases, key=len, reverse=True)


def _prose_phase(line, phases):
    """'mic preflight failed: …' names the mic_preflight phase in words; return that phase, longest first."""
    words = '_' + re.sub(r'[^a-z0-9]+', '_', line.lower()) + '_'
    return next((phase for phase in phases if f'_{phase}_' in words), None)


def iter_textlog(path, project, source, stats, max_bytes=64_000_000):
    _stat(stats, source)
    phases = _known_phases(path, max_bytes)
    # Lines without their own timestamp or session inherit the previous one (else the file's mtime).
    last_ts, session = None, None
    mtime = Path(path).stat().st_mtime
    for number, line in _lines(path, max_bytes):
        line = line.rstrip('\n')
        error = bool(_LOG_ERROR.search(line)) and 'status=ok' not in line
        match = _ISO.match(line)
        stamp = parse_ts(match.group(1)) if match else None
        last_ts = stamp if stamp is not None else last_ts
        ts = last_ts if last_ts is not None else mtime
        own = _SESSION.search(line)
        if own:
            session = own.group(1)
        phase = _PHASE.search(line)
        word = _WORD.search(line)
        named = None if phase or not error else _prose_phase(line, phases)
        step = phase.group(1) if phase else named or (word.group(1) if word else 'log')
        duration = metric = None
        for name, value in _NAMED_MS.findall(line):
            if name == 'elapsed' or (name == 'step' and (error or END_PHASE.search(step))):
                continue
            duration, metric = float(value), f'{name}_ms'
            break
        if duration is None and not _NAMED_MS.search(line):
            plain = _PLAIN_MS.search(line)
            duration, metric = (float(plain.group(1)), 'ms') if plain else (None, None)
        if not error and duration is None:
            continue
        text = next((m.group(1) for pattern in _ERROR_TEXT if (m := pattern.search(line))), line) if error else ''
        cause = _cause(line) if error else None
        message = f'cause: {cause}' if cause else scrub(text)
        yield _event(stats, project=project, source=source, kind='log_error' if error else 'log', step=step,
                     error=error, ts=ts, duration_ms=duration, metric=metric, message=message, where=ref(path, number),
                     session=session, condition=cause, alias=named is not None,
                     session_attribution='event' if own else ('previous_line_inferred' if session else 'unknown'),
                     version_attribution='unknown',
                     mirror_ref=hashlib.sha256(m.group(1).encode()).hexdigest()[:24] if (m := re.search(r'\bmirror_id=([0-9a-f]{32})\b', line)) else None)


def iter_junit(path, project, source, stats):
    _stat(stats, source)
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        stats[source]['skipped'] += 1
        return
    ts = Path(path).stat().st_mtime
    run_id = root.get('run_id') or root.get('check_id') or next(
        (prop.get('value') for prop in root.iter('property') if prop.get('name') in ('run_id', 'check_id')), None)
    for case in root.iter('testcase'):
        bad = case.find('failure')
        if bad is None:
            bad = case.find('error')
        try:
            duration = float(case.get('time')) * 1000 if case.get('time') else None
        except ValueError:
            duration = None
        message = scrub((bad.get('message') or bad.text or '') if bad is not None else '')
        error_type = bad.get('type') if bad is not None else None
        case_name = f"{case.get('classname', '')}.{case.get('name', '')}".strip('.')
        yield _event(stats, project=project, source=source, kind='test',
                     step=case_name, error=bad is not None, ts=ts,
                     duration_ms=duration, metric='test_ms', message=message, where=ref(path, 0),
                     run_ref=_run_ref(run_id), step_ref=_test_step_ref(case.get('step_ref') or case.get('id') or case_name),
                     task_data={'error_type': scrub(error_type, 120)} if error_type else {},
                     outcome='failure' if bad is not None else 'success')


def iter_checklog(run_dir, project, source, stats):
    _stat(stats, source)
    log = Path(run_dir) / 'check.log'
    if not log.is_file():
        stats[source]['skipped'] += 1
        stats[source]['missing'] = True
        return
    ts = log.stat().st_mtime
    run_id = None
    context = {}
    try:
        summary = json.loads((Path(run_dir) / 'summary.json').read_text())
        attempt = summary.get('attempt') if isinstance(summary, dict) else None
        head, pr = (summary.get('head'), summary.get('pr')) if isinstance(summary, dict) else (None, None)
        if isinstance(attempt, str) and 0 < len(attempt) <= 512:
            run_id = attempt
            valid_head = isinstance(head, str) and re.fullmatch(r'[0-9a-fA-F]{40}', head)
            context = {'request': f'CI PR #{pr} @ {head[:12]}' if type(pr) is int and valid_head else None,
                       'build': head if valid_head else None,
                       'task_outcome': {'success': 'verified_success', 'failure': 'verified_incomplete'}.get(
                           summary.get('conclusion'))}
            phases = summary.get('phases')
            if isinstance(phases, dict):
                for index, (phase, duration) in enumerate(phases.items()):
                    if isinstance(phase, str) and type(duration) in (int, float) and duration >= 0:
                        yield _event(stats, project=project, source=source, kind='ci_phase', step=phase,
                                     error=False, ts=ts - len(phases) + index, duration_ms=duration,
                                     message='', where=ref(Path(run_dir) / 'summary.json', 0),
                                     run_ref=_run_ref(run_id), step_ref=_test_step_ref(f'phase-{phase}'),
                                     task_data={'request': context['request']} if context['request'] else {},
                                     build=context['build'], task_outcome=context['task_outcome'], outcome='success')
    except (OSError, ValueError, TypeError):
        pass
    for number, line in _lines(log, 64_000_000):
        line = line.strip()
        if match := _CHECK_ID.fullmatch(line):
            run_id = run_id or match.group(1)
            continue
        step_match = re.search(r'\bstep_ref=([A-Za-z0-9_.:/-]{1,128})(?:\s|$)', line)
        step_ref = _task_id(step_match.group(1)) if step_match else None
        where = ref(log, number)
        if match := _CHECK_FAILED.match(line):
            yield _event(stats, project=project, source=source, kind='check_failed', step=match.group(1), error=True,
                         ts=ts, duration_ms=None, message=scrub(line), where=where,
                         run_ref=_run_ref(run_id),
                         step_ref=step_ref or (_test_step_ref(match.group(1)) if run_id else None),
                         task_data={'request': context['request']} if context.get('request') else {},
                         build=context.get('build'), task_outcome=context.get('task_outcome'))
        elif match := _XCTEST.search(line):
            test_name = f'{match.group(1)}.{match.group(2)}'
            yield _event(stats, project=project, source=source, kind='test', step=f'{match.group(1)}.{match.group(2)}',
                         error=True, ts=ts, duration_ms=None, message=scrub(match.group(3)), where=where,
                         run_ref=_run_ref(run_id),
                         step_ref=step_ref or (_test_step_ref(test_name) if run_id else None),
                         task_data={'request': context['request']} if context.get('request') else {},
                         build=context.get('build'), task_outcome=context.get('task_outcome'))
        elif match := _CHECK_TEST.match(line):
            yield _event(stats, project=project, source=source, kind='check_test', step=match.group(1),
                         error=match.group(3) != '0', ts=ts, duration_ms=float(match.group(2)) * 1000, metric='test_ms',
                         message='', where=where, run_ref=_run_ref(run_id),
                         step_ref=step_ref or (_test_step_ref(match.group(1)) if run_id else None),
                         task_data={**({'request': context['request']} if context.get('request') else {}),
                                    **({'error_type': 'test_failure'} if match.group(3) != '0' else {})},
                         build=context.get('build'), task_outcome=context.get('task_outcome'),
                         outcome='failure' if match.group(3) != '0' else 'success')


def _git(repo, *args):
    try:
        result = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def iter_git(repo, project, source, stats):
    _stat(stats, source)
    now = time.time()
    status = _git(repo, 'status', '--porcelain')
    if status is None:
        stats[source]['skipped'] += 1
        return
    dirty = len([line for line in status.splitlines() if line.strip()])
    if dirty:
        yield _event(stats, project=project, source=source, kind='git_state', step='dirty_worktree', error=True, ts=now,
                     duration_ms=None, message=f'{dirty} uncommitted file(s)', where=ref(repo, 0))
    ahead = _git(repo, 'rev-list', '--count', '@{u}..HEAD')
    if ahead and ahead.strip().isdigit() and int(ahead) > 0:
        yield _event(stats, project=project, source=source, kind='git_state', step='unpushed_commits', error=True, ts=now,
                     duration_ms=None, message=f'{int(ahead)} local-only commit(s)', where=ref(repo, 0))


def iter_hygiene(json_path, project, source, stats):
    _stat(stats, source)
    try:
        data = json.loads(Path(json_path).read_text())
    except (OSError, ValueError):
        stats[source]['skipped'] += 1
        return
    items = (data.get('checks') or data.get('results') or []) if isinstance(data, dict) else data
    ts = Path(json_path).stat().st_mtime
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        state = str(item.get('status') or item.get('state') or '').upper()
        if state == 'FAIL':
            yield _event(stats, project=project, source=source, kind='hygiene', step=str(item.get('name') or item.get('id')),
                         error=True, ts=ts, duration_ms=None,
                         message=scrub(item.get('detail') or item.get('summary') or ''), where=ref(json_path, 0))


def _walk(root, depth=4):
    root = Path(root)
    for current, dirs, files in os.walk(root):
        rel = Path(current).relative_to(root)
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS) if len(rel.parts) < depth else []
        for name in sorted(files):
            yield Path(current) / name


def _is_junit(path):
    try:
        for _event_name, element in ET.iterparse(path, events=('start',)):
            return element.tag in ('testsuite', 'testsuites')
    except (ET.ParseError, OSError):
        return False
    return False


def discover_project(directory, stats, project=None):
    directory = Path(directory).expanduser()
    project = project or directory.name
    for path in _walk(directory):
        rel = path.relative_to(directory).as_posix()
        source = f'{project}:{rel}'
        if path.suffix == '.jsonl':
            yield from iter_jsonl(path, project, source, stats)
        elif path.suffix == '.log':
            yield from iter_textlog(path, project, source, stats)
        elif path.suffix == '.xml' and _is_junit(path):
            yield from iter_junit(path, project, source, stats)
        if source in stats:
            stats[source]['read'] = not bool(stats[source].get('io_errors') or stats[source].get('missing')
                                             or stats[source].get('skipped'))
    if (directory / '.git').exists():
        source = f'{project}:git'
        yield from iter_git(directory, project, source, stats)
        if source in stats:
            stats[source]['read'] = not bool(stats[source].get('io_errors') or stats[source].get('missing')
                                             or stats[source].get('skipped'))


ROTATED_KEEP = 5


def rotated_logs(path):
    """Existing rotated copies of ``path``, oldest first: ``stem.5.ext.gz`` … ``stem.1.ext.gz``."""
    path = Path(path)
    stem, ext = path.stem, path.suffix
    out = []
    for i in range(ROTATED_KEEP, 0, -1):
        for name in (f'{stem}.{i}{ext}.gz', f'{stem}.{i}{ext}'):
            candidate = path.with_name(name)
            if candidate.is_file():
                out.append(candidate)
    return out


def known_sources():
    home = Path.home()
    specs = []
    for name, kind in (('samantha-activity.jsonl', 'jsonl'), ('voice.log', 'textlog')):
        path = home / '.cache/macos-cua' / name
        # Rotated copies first, oldest to newest (agent-computer-use 0.2.86 keeps .1 … .5, gzipped; a plain
        # .N is a copy a crash left before its gzip), then the live file. One source id for all of them.
        for rotated in rotated_logs(path):
            specs.append({'type': kind, 'path': str(rotated), 'project': 'samantha', 'source': f'samantha:{name}'})
        if path.is_file():
            specs.append({'type': kind, 'path': str(path), 'project': 'samantha'})
    runs = home / 'Library/Logs/agent-plugins-origin-ci/runs'
    if runs.is_dir():
        newest = sorted((p for p in runs.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)[:20]
        specs.extend({'type': 'checklog', 'path': str(p), 'project': 'agent-plugins', 'source': 'agent-plugins:origin-ci'}
                     for p in newest)
    return specs


ADAPTERS = {'jsonl': iter_jsonl, 'textlog': iter_textlog, 'junit': iter_junit, 'checklog': iter_checklog,
            'git': iter_git, 'hygiene': iter_hygiene}


def _io_error(stats, source):
    item = _stat(stats, source)
    item['io_errors'] = item.get('io_errors', 0) + 1


def iter_spec(spec, stats):
    kind, path, project = spec.get('type'), Path(str(spec.get('path', ''))).expanduser(), spec.get('project')
    if kind == 'project_dir':
        yield from discover_project(path, stats, project)
        return
    if kind not in ADAPTERS or not project:
        raise ValueError(f'invalid source spec type {kind!r}')
    source = spec.get('source') or f'{project}:{path.name}'
    item = _stat(stats, source)
    stale_after = spec.get('stale_after_s', 86400)
    item['stale_after_s'] = stale_after if isinstance(stale_after, (int, float)) and not isinstance(stale_after, bool) and stale_after > 0 else 86400
    if not path.exists():
        item['missing'] = True
        _io_error(stats, source)
        return
    try:
        yield from ADAPTERS[kind](path, project, source, stats)
        item['read'] = not bool(item.get('io_errors') or item.get('missing') or item.get('skipped'))
    except OSError:
        _io_error(stats, source)
