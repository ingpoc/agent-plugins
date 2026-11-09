"""Read a bounded sample of Codex session metadata without retaining message content."""
from datetime import datetime, timedelta
import json
from pathlib import Path
import re

MAX_FILES = 30
MAX_BYTES = 8 * 1024 * 1024
EFFORTS = {'none', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'}


def effective_config(thread_id, root=None, today=None):
    if not isinstance(thread_id, str) or not re.fullmatch(r'[0-9a-f-]{36}', thread_id):
        return None, None
    root = Path(root or '~/.codex/sessions').expanduser()
    today = today or datetime.now().astimezone().date()
    files = []
    for offset in range(2):
        directory = root / (today - timedelta(days=offset)).strftime('%Y/%m/%d')
        files.extend(directory.glob(f'*{thread_id}*.jsonl'))
    if len(files) != 1:
        return None, None
    configs = set()
    session_matches = False
    try:
        if files[0].stat().st_size > MAX_BYTES:
            return None, None
        with files[0].open(encoding='utf-8') as handle:
            for line in handle:
                item = json.loads(line)
                if not isinstance(item, dict) or not isinstance(item.get('payload'), dict):
                    continue
                payload = item['payload']
                if item.get('type') == 'session_meta':
                    session_matches = payload.get('id') == thread_id
                elif item.get('type') == 'turn_context':
                    model, effort = payload.get('model'), payload.get('effort')
                    if isinstance(model, str) and re.fullmatch(r'(?:gpt-[A-Za-z0-9._-]{1,76}|o[0-9][A-Za-z0-9._-]{0,77})', model):
                        configs.add((model, effort if isinstance(effort, str) and effort in EFFORTS else None))
                    else:
                        configs.add((None, None))
    except (OSError, ValueError, UnicodeError):
        return None, None
    return next(iter(configs)) if session_matches and len(configs) == 1 else (None, None)


def sample(root=None, today=None):
    root = Path(root or '~/.codex/sessions').expanduser()
    today = today or datetime.now().astimezone().date()
    files = []
    for offset in range(2):
        date = today - timedelta(days=offset)
        directory = root / date.strftime('%Y/%m/%d')
        files.extend(directory.glob('*.jsonl'))
    files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    total_files = len(files)
    groups = {}
    skipped = errors = examined = 0
    for path in files[:MAX_FILES]:
        turns = {}
        try:
            if path.stat().st_size > MAX_BYTES:
                skipped += 1
                continue
            with path.open(encoding='utf-8') as handle:
                for line in handle:
                    try:
                        item = json.loads(line)
                    except (ValueError, UnicodeError):
                        errors += 1
                        continue
                    if not isinstance(item, dict) or not isinstance(item.get('payload'), dict):
                        continue
                    payload = item['payload']
                    kind = item.get('type')
                    if kind not in ('turn_context', 'token_usage_record', 'event_msg'):
                        continue
                    if kind == 'event_msg' and payload.get('type') not in ('task_started', 'task_complete', 'task_aborted'):
                        continue
                    turn_id = payload.get('turn_id')
                    if not isinstance(turn_id, str) or not re.fullmatch(r'[0-9a-fA-F-]{36}', turn_id):
                        continue
                    turn = turns.setdefault(turn_id, {'configs': set(), 'usage': None, 'status': 'incomplete'})
                    if kind == 'turn_context':
                        model, effort = payload.get('model'), payload.get('effort')
                        if not isinstance(model, str) or not re.fullmatch(r'(?:gpt-[A-Za-z0-9._-]{1,76}|o[0-9][A-Za-z0-9._-]{0,77})', model):
                            model = None
                        turn['configs'].add((model, effort if isinstance(effort, str) and effort in EFFORTS else None))
                    elif kind == 'token_usage_record':
                        usage = payload.get('turn_token_usage')
                        if isinstance(usage, dict):
                            values = [usage.get(k) for k in ('input_tokens', 'cached_input_tokens', 'output_tokens')]
                            if all(type(v) is int and v >= 0 for v in values) and values[1] <= values[0]:
                                turn['usage'] = values
                            else:
                                turn['usage'] = None
                    elif payload.get('type') == 'task_complete':
                        turn['status'] = 'completed'
                    elif payload.get('type') == 'task_aborted':
                        turn['status'] = 'aborted'
            examined += 1
        except (OSError, UnicodeError):
            errors += 1
            continue
        for turn in turns.values():
            model, effort = next(iter(turn['configs'])) if len(turn['configs']) == 1 else (None, None)
            key = (model, effort)
            group = groups.setdefault(key, {'model': model, 'effort': effort, 'turns': 0, 'completed': 0,
                                             'usage_known': 0, 'input_tokens': 0, 'cached_input_tokens': 0, 'output_tokens': 0})
            group['turns'] += 1
            group['completed'] += turn['status'] == 'completed'
            if turn['usage'] is not None:
                group['usage_known'] += 1
                for field, value in zip(('input_tokens', 'cached_input_tokens', 'output_tokens'), turn['usage']):
                    group[field] += value
    return {'source': 'codex_session_metadata', 'scope': 'bounded_sample_not_total',
            'days': 2, 'file_limit': MAX_FILES, 'file_size_limit_bytes': MAX_BYTES,
            'candidate_files': total_files, 'files_examined': examined,
            'files_skipped_large': skipped, 'errors': errors, 'groups': list(groups.values()),
            'category': None, 'acceptance': 'unknown', 'cost': None}
