"""Reduce a Codex JSONL event stream to usage counts without retaining task content."""
import argparse
import json
import math
import os
import re
from pathlib import Path
import sqlite3
import sys
import uuid

FIELDS = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
          'output_tokens', 'reasoning_output_tokens')


def summarize(lines):
    totals = {field: 0 for field in FIELDS}
    completed = failed = invalid = 0
    present = {field: 0 for field in FIELDS}
    threads = set()
    turn_ids = set()
    ambiguous = False
    missing_turn_id = False
    for line in lines:
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            invalid += 1
            continue
        if not isinstance(event, dict):
            invalid += 1
            continue
        if event.get('type') == 'thread.started':
            try:
                threads.add(str(uuid.UUID(event.get('thread_id'))))
            except (ValueError, TypeError, AttributeError):
                invalid += 1
            continue
        if event.get('type') == 'turn.failed':
            failed += 1
        if event.get('type') != 'turn.completed':
            continue
        turn_id = event.get('turn_id')
        if isinstance(turn_id, str) and turn_id:
            if turn_id in turn_ids or missing_turn_id:
                ambiguous = True
            turn_ids.add(turn_id)
        else:
            missing_turn_id = True
            if completed:
                ambiguous = True
        completed += 1
        usage = event.get('usage')
        if not isinstance(usage, dict):
            invalid += 1
            continue
        input_tokens = usage.get('input_tokens')
        cached_tokens = usage.get('cached_input_tokens')
        if (type(input_tokens) is int and type(cached_tokens) is int
                and cached_tokens > input_tokens):
            invalid += 1
            continue
        for field in FIELDS:
            value = usage.get(field)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                totals[field] += value
                present[field] += 1
    return {'completed_turns': completed, 'failed_turns': failed, 'invalid_events': invalid,
            'usage': {field: totals[field] if completed and not failed and not ambiguous and present[field] == completed else None for field in FIELDS},
            'thread_id': next(iter(threads)) if len(threads) == 1 else None,
            'model': None, 'effort': None,
            'acceptance': 'unknown', 'cost': None}


def record(path, task_id, category, summary):
    task_id = str(uuid.UUID(task_id))
    if category not in ('coding', 'research', 'computer_use', 'writing', 'analysis', 'other', 'unknown'):
        raise ValueError('unknown task category')
    allowed = {'completed_turns', 'failed_turns', 'invalid_events', 'usage', 'thread_id', 'model', 'effort', 'acceptance', 'cost', 'requested_model', 'requested_effort', 'process_exit_code', 'elapsed_seconds', 'check_status', 'check_seconds'}
    if not isinstance(summary, dict) or set(summary) - allowed or not isinstance(summary.get('usage'), dict) or set(summary['usage']) - set(FIELDS):
        raise ValueError('invalid usage summary fields')
    if summary.get('acceptance', 'unknown') != 'unknown' or summary.get('cost') is not None:
        raise ValueError('acceptance and cost must remain unknown')
    for field in ('model', 'requested_model'):
        value = summary.get(field)
        if value is not None and (not isinstance(value, str) or not re.fullmatch(r'(?:gpt-[A-Za-z0-9._-]{1,76}|o[0-9][A-Za-z0-9._-]{0,77})', value)):
            raise ValueError('invalid model')
    for field in ('effort', 'requested_effort'):
        if summary.get(field) not in (None, 'none', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'):
            raise ValueError('invalid effort')
    if any(value is not None and (type(value) is not int or value < 0) for value in summary['usage'].values()):
        raise ValueError('invalid usage')
    if summary.get('thread_id') is not None:
        try:
            uuid.UUID(summary['thread_id'])
        except (ValueError, TypeError, AttributeError):
            raise ValueError('invalid thread id') from None
    if summary.get('check_status') not in (None, 'not_run', 'passed', 'failed', 'error'):
        raise ValueError('invalid check status')
    for field in ('completed_turns', 'failed_turns', 'invalid_events'):
        value = summary.get(field)
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError('invalid event count')
    for field in ('elapsed_seconds', 'check_seconds'):
        value = summary.get(field)
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
            raise ValueError('invalid duration')
    value = summary.get('process_exit_code')
    if value is not None and type(value) is not int:
        raise ValueError('invalid process exit code')
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    payload = json.dumps(summary, sort_keys=True, separators=(',', ':'))
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS task_usage (task_id TEXT PRIMARY KEY, category TEXT NOT NULL, summary TEXT NOT NULL, recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
        previous = db.execute('SELECT category, summary FROM task_usage WHERE task_id = ?', (task_id,)).fetchone()
        if previous and previous != (category, payload):
            raise ValueError('task ID already exists with different data')
        if not previous:
            db.execute('INSERT INTO task_usage (task_id, category, summary) VALUES (?, ?, ?)', (task_id, category, payload))


def task_detail(path, task_id):
    task_id = str(uuid.UUID(task_id))
    path = Path(path).expanduser()
    if not path.is_file():
        return {'status': 'not_captured', 'reason': 'task_not_found'}
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        row = db.execute('SELECT category, summary FROM task_usage WHERE task_id = ?', (task_id,)).fetchone()
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_verification'").fetchone()
        verification = db.execute('SELECT receipt FROM task_verification WHERE task_id = ?', (task_id,)).fetchone() if exists else None
    if row is None:
        return {'status': 'not_captured', 'reason': 'task_not_found'}
    summary = json.loads(row[1])
    usage = summary.get('usage', {})
    reasons = []
    if not summary.get('thread_id'):
        reasons.append('binding_unavailable')
    if summary.get('process_exit_code') != 0 or summary.get('failed_turns') or summary.get('invalid_events') or not summary.get('completed_turns'):
        reasons.append('execution_incomplete')
    if any(usage.get(k) is None for k in ('input_tokens', 'cached_input_tokens', 'output_tokens')):
        reasons.append('usage_partial')
    reasons.extend(('child_coverage_unknown', 'acceptance_unverified'))
    return {'status': 'incomplete', 'task_id': task_id, 'category': row[0],
            'source': 'explicit_codex_cli_capture', 'usage': {k: usage.get(k) for k in FIELDS},
            'model': summary.get('model'), 'effort': summary.get('effort'),
            'thread_id': summary.get('thread_id'), 'elapsed_seconds': summary.get('elapsed_seconds'),
            'caller_check': summary.get('check_status', 'not_run'),
            'verification': json.loads(verification[0]) if verification else None,
            'acceptance': 'unknown', 'reasons': reasons}


def task_overview(path):
    path = Path(path).expanduser()
    if not path.exists():
        return {'status': 'not_captured', 'tasks': 0, 'categories': []}
    categories = {}
    requested = {}
    effective = {}
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        for category, raw in db.execute('SELECT category, summary FROM task_usage ORDER BY category'):
            summary = json.loads(raw)
            row = categories.setdefault(category, {'category': category, 'tasks': 0, 'known_input_tasks': 0, 'input_tokens': 0,
                                                    'known_output_tasks': 0, 'output_tokens': 0,
                                                    'checks_passed': 0, 'checks_failed': 0, 'checks_unknown': 0})
            row['tasks'] += 1
            check = summary.get('check_status')
            row['checks_passed' if check == 'passed' else 'checks_failed' if check == 'failed' else 'checks_unknown'] += 1
            key = (category, summary.get('requested_model'), summary.get('requested_effort'))
            requested[key] = requested.get(key, 0) + 1
            model, effort = summary.get('model'), summary.get('effort')
            if not isinstance(model, str) or len(model) > 80:
                model = None
            if not isinstance(effort, str) or effort not in ('none', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'):
                effort = None
            key = (category, model, effort)
            group = effective.setdefault(key, {'category': category, 'model': model, 'effort': effort,
                                                'tasks': 0, 'checks_passed': 0, 'checks_failed': 0,
                                                'checks_unknown': 0, 'known_input_tasks': 0, 'input_tokens': 0,
                                                'known_output_tasks': 0, 'output_tokens': 0,
                                                'known_duration_tasks': 0, 'elapsed_seconds': 0.0})
            group['tasks'] += 1
            group['checks_passed' if check == 'passed' else 'checks_failed' if check == 'failed' else 'checks_unknown'] += 1
            value = summary.get('usage', {}).get('input_tokens')
            if type(value) is int and value >= 0:
                row['known_input_tasks'] += 1
                row['input_tokens'] += value
                group['known_input_tasks'] += 1
                group['input_tokens'] += value
            value = summary.get('usage', {}).get('output_tokens')
            if type(value) is int and value >= 0:
                row['known_output_tasks'] += 1
                row['output_tokens'] += value
                group['known_output_tasks'] += 1
                group['output_tokens'] += value
            value = summary.get('elapsed_seconds')
            if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                group['known_duration_tasks'] += 1
                group['elapsed_seconds'] += value
    return {'status': 'captured', 'tasks': sum(row['tasks'] for row in categories.values()),
            'categories': list(categories.values()),
            'requested_configurations': [{'category': category, 'model': model, 'effort': effort, 'tasks': count}
                                         for (category, model, effort), count in requested.items()],
            'effective_configurations': list(effective.values()),
            'model': None, 'effort': None, 'verified_outcome': None, 'cost': None}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task-id', help='UUID; enables recording')
    parser.add_argument('--category', default='unknown')
    parser.add_argument('--database', default='~/.local/share/developer-observability/task-usage.sqlite3')
    args = parser.parse_args()
    result = summarize(sys.stdin)
    if args.task_id:
        record(args.database, args.task_id, args.category, result)
    json.dump(result, sys.stdout)
    sys.stdout.write('\n')
