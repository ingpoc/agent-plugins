"""Cursor CLI run capture: Cursor hook rows plus the CLI's own JSON result, never task content.

Sources, all written by Cursor itself (nothing here is hand-entered by a caller):
  hook     Cursor runs `developer-observability cursor-hook` for sessionStart, stop and sessionEnd
           (hooks/cursor-hooks.json). Rows carry model, model_id, status, duration and errors.
  capture  `developer-observability cursor-capture --format json -- -p --model <model> "prompt"` finds
           `agent` on PATH, runs it with `--output-format`, and keeps the terminal result envelope
           (usage, duration_ms, request_id) plus `agent --version` as cursor_version.
  ingest   `developer-observability cursor-usage --ingest FILE` stores a saved json/stream-json output.
Tokens are official only when Cursor's own `usage` object is present. Cursor reports no cost and no
model version, so both stay null with a *_source of not_reported; nothing is estimated.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from collector.scrub import scrub  # noqa: E402

DATABASE = '~/.local/share/developer-observability/task-usage.sqlite3'
HOOK_EVENTS = {'sessionStart', 'stop', 'sessionEnd', 'subagentStop'}
STATUSES = {'completed', 'aborted', 'error', 'window_close', 'user_close', 'success'}
USAGE_KEYS = {'inputTokens': 'uncached_input', 'outputTokens': 'output',
              'cacheReadTokens': 'cache_read', 'cacheWriteTokens': 'cache_write'}
_ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')
_MODEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9 ._()/+-]{0,79}')
_ID = re.compile(r'[A-Za-z0-9._:-]{1,80}')
_CLI_VERSION = re.compile(r'\d{4}\.\d{2}\.\d{2}-[0-9a-f]{5,}')
MAX_LINE = 4 * 1024 * 1024
MAX_HOOK_INPUT = 1024 * 1024


def _now():
    return datetime.now(timezone.utc).isoformat()


def _id(value):
    return value if isinstance(value, str) and _ID.fullmatch(value) else None


def _model(value):
    return value.strip() if isinstance(value, str) and _MODEL.fullmatch(value.strip()) else None


def _int(value):
    return value if type(value) is int and value >= 0 else None


def classify_error(text):
    """Error kind from a CLI-owned error line. ActionRequiredError covers plan and usage refusals."""
    line = _ANSI.sub('', text or '').strip()
    low = line.lower()
    if not line:
        return None
    if 'out of usage' in low or 'usage limit' in low or 'spend limit' in low:
        return 'usage_limit'
    if 'named models unavailable' in low or 'free plans can only' in low or 'upgrade' in low and 'plan' in low:
        return 'plan_refusal'
    if 'model blocked' in low:
        return 'model_blocked'
    if 'cannot use this model' in low:
        return 'unknown_model'
    if line.startswith('ActionRequiredError'):
        return 'action_required'
    if 'not logged in' in low or 'authentication' in low or 'unauthorized' in low:
        return 'auth'
    return 'error'


def requested_model(args):
    for index, value in enumerate(args):
        if value in ('-m', '--model') and index + 1 < len(args):
            return _model(args[index + 1])
        if value.startswith('--model='):
            return _model(value.partition('=')[2])
    return None


def summarize(lines, stderr_lines=(), exit_code=None):
    """Reduce Cursor CLI json/stream-json output to usage and outcome fields. Result text is dropped."""
    out = {'session_id': None, 'request_id': None, 'model_display': None, 'envelope': False,
           'result_subtype': None, 'is_error': None, 'duration_ms': None, 'duration_api_ms': None,
           'usage': None, 'error_kind': None, 'error_message': None, 'invalid_lines': 0,
           'tool_calls': 0, 'process_exit_code': exit_code}
    error_lines = []
    for raw in lines:
        if len(raw) > MAX_LINE:
            out['invalid_lines'] += 1
            continue
        line = _ANSI.sub('', raw).strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            # Agent text only ever arrives inside JSON events, so a plain line is CLI-owned.
            if not out['envelope'] and line.startswith('ActionRequiredError'):
                error_lines.append(line)
            else:
                out['invalid_lines'] += 1
            continue
        if not isinstance(event, dict):
            out['invalid_lines'] += 1
            continue
        kind = event.get('type')
        out['session_id'] = out['session_id'] or _id(event.get('session_id'))
        if kind == 'system' and event.get('subtype') == 'init':
            out['model_display'] = _model(event.get('model'))
        elif kind == 'tool_call' and event.get('subtype') == 'started':
            out['tool_calls'] += 1
        elif kind == 'result':
            out['envelope'] = True
            out['result_subtype'] = event.get('subtype') if isinstance(event.get('subtype'), str) else None
            out['is_error'] = event.get('is_error') if isinstance(event.get('is_error'), bool) else None
            out['duration_ms'] = _int(event.get('duration_ms'))
            out['duration_api_ms'] = _int(event.get('duration_api_ms'))
            out['request_id'] = _id(event.get('request_id'))
            usage = event.get('usage')
            if isinstance(usage, dict):
                counts = {name: _int(usage.get(key)) for key, name in USAGE_KEYS.items()}
                out['usage'] = counts if any(value is not None for value in counts.values()) else None
            if out['is_error'] and isinstance(event.get('result'), str):
                error_lines.append(event['result'].splitlines()[0] if event['result'] else '')
    for raw in stderr_lines:
        line = _ANSI.sub('', raw).strip()
        if line:
            error_lines.append(line)
    failed = exit_code not in (None, 0) or out['is_error'] or (out['envelope'] and out['result_subtype'] != 'success')
    if failed or (not out['envelope'] and error_lines):
        picked = next((line for line in error_lines if line.startswith('ActionRequiredError')), None) \
            or next((line for line in error_lines if re.search(r'(?i)error', line)), None) \
            or (error_lines[-1] if error_lines else None)
        out['error_kind'] = classify_error(picked) if picked else ('error' if failed else None)
        out['error_message'] = scrub(picked, 160) or None if picked else None
    return out


def _connect(path):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.exists():
        os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
    db = sqlite3.connect(path, timeout=2)
    db.execute('CREATE TABLE IF NOT EXISTS cursor_runs (run_id TEXT PRIMARY KEY, session_id TEXT, source TEXT NOT NULL, '
               'started_at TEXT, finished_at TEXT, summary TEXT NOT NULL, recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
    db.execute('CREATE TABLE IF NOT EXISTS cursor_hook_events (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, '
               'event TEXT NOT NULL, at TEXT NOT NULL, payload TEXT NOT NULL)')
    return db


def record_run(path, run_id, source, summary, started_at=None, finished_at=None):
    run_id = str(uuid.UUID(run_id))
    if source not in ('capture', 'ingest'):
        raise ValueError('unknown source')
    allowed = {'session_id', 'request_id', 'model_display', 'envelope', 'result_subtype', 'is_error', 'duration_ms',
               'duration_api_ms', 'usage', 'error_kind', 'error_message', 'invalid_lines', 'tool_calls',
               'process_exit_code', 'requested_model', 'elapsed_seconds', 'output_format', 'agent_version'}
    if set(summary) - allowed:
        raise ValueError('invalid cursor run fields')
    with _connect(path) as db:
        db.execute('INSERT OR REPLACE INTO cursor_runs (run_id, session_id, source, started_at, finished_at, summary) '
                   'VALUES (?, ?, ?, ?, ?, ?)', (run_id, summary.get('session_id'), source, started_at, finished_at,
                                                 json.dumps(summary, sort_keys=True, separators=(',', ':'))))
    return run_id


def hook_row(payload, environ=None):
    """Keep only metadata from a Cursor hook payload: no prompt, transcript, email or tool data."""
    environ = environ if environ is not None else os.environ
    event = payload.get('hook_event_name')
    if event not in HOOK_EVENTS:
        return None
    params = payload.get('model_params') if isinstance(payload.get('model_params'), list) else []
    version = payload.get('cursor_version') or environ.get('CURSOR_VERSION')
    version = version if isinstance(version, str) and re.fullmatch(r'[0-9A-Za-z._-]{1,40}', version) else None
    status = payload.get('status') or payload.get('reason')
    error = payload.get('error_message')
    roots = payload.get('workspace_roots') if isinstance(payload.get('workspace_roots'), list) else []
    return {'event': event, 'session_id': _id(payload.get('session_id') or payload.get('conversation_id')),
            'generation_id': _id(payload.get('generation_id')),
            'model': _model(payload.get('model')), 'model_id': _model(payload.get('model_id')),
            'model_params': [{'id': str(p.get('id'))[:40], 'value': str(p.get('value'))[:40]} for p in params[:8]
                             if isinstance(p, dict) and p.get('id') is not None],
            'cursor_version': version,
            'client': 'cli' if version and _CLI_VERSION.fullmatch(version) else ('ide' if version else 'unknown'),
            'status': status if status in STATUSES else None,
            'final_status': scrub(payload.get('final_status'), 40) or None,
            'duration_ms': _int(payload.get('duration_ms')),
            'is_background_agent': payload.get('is_background_agent') if isinstance(payload.get('is_background_agent'), bool) else None,
            'composer_mode': payload.get('composer_mode') if payload.get('composer_mode') in ('agent', 'ask', 'edit', 'plan') else None,
            'error_kind': classify_error(error) if isinstance(error, str) and error.strip() else None,
            'error_message': scrub(error, 160) or None if isinstance(error, str) else None,
            'workspace': scrub(roots[0], 120) or None if roots and isinstance(roots[0], str) else None}


def record_hook(path, payload, environ=None, at=None):
    row = hook_row(payload, environ)
    if row is None:
        return None
    with _connect(path) as db:
        db.execute('INSERT INTO cursor_hook_events (session_id, event, at, payload) VALUES (?, ?, ?, ?)',
                   (row['session_id'], row['event'], at or _now(), json.dumps(row, sort_keys=True, separators=(',', ':'))))
    return row


def _instant(value):
    try:
        stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _merge(session_id, runs, hooks):
    """One read-back row per session (or per run when Cursor gave no session id)."""
    run = runs[-1] if runs else None
    summary = json.loads(run[5]) if run else {}
    start_hook = next((h for h in hooks if h['event'] == 'sessionStart'), None)
    end_hooks = [h for h in hooks if h['event'] in ('stop', 'sessionEnd')]
    last = end_hooks[-1] if end_hooks else None
    hook_model = next((h for h in reversed(hooks) if h.get('model_id') or h.get('model')), None)
    usage = summary.get('usage') if isinstance(summary.get('usage'), dict) else None
    tokens = None
    if usage:
        parts = [usage.get(key) for key in ('uncached_input', 'cache_read', 'cache_write')]
        tokens_in = sum(parts) if all(type(part) is int for part in parts) else None
        out = usage.get('output')
        tokens = {'input': tokens_in, 'output': out, 'total': tokens_in + out if tokens_in is not None and type(out) is int else None,
                  'uncached_input': usage.get('uncached_input'), 'cache_read': usage.get('cache_read'),
                  'cache_write': usage.get('cache_write')}
    exit_code = summary.get('process_exit_code')
    hook_error = next((h for h in reversed(hooks) if h.get('error_kind')), None)
    error_kind = summary.get('error_kind') or (hook_error or {}).get('error_kind')
    if run:
        status = 'success' if exit_code == 0 and summary.get('envelope') and not summary.get('is_error') else 'error'
    elif last:
        status = {'completed': 'success', 'error': 'error', 'aborted': 'aborted'}.get(last.get('status'), last.get('status') or 'unknown')
    else:
        status = 'unknown'
    if summary.get('duration_ms') is not None:
        wall_ms, wall_source = summary['duration_ms'], 'cli_result'
    elif last and last.get('duration_ms') is not None:
        wall_ms, wall_source = last['duration_ms'], 'hook'
    elif summary.get('elapsed_seconds') is not None:
        wall_ms, wall_source = int(summary['elapsed_seconds'] * 1000), 'wrapper'
    elif start_hook and last and _instant(start_hook['at']) and _instant(last['at']):
        wall_ms, wall_source = int((_instant(last['at']) - _instant(start_hook['at'])).total_seconds() * 1000), 'hook_times'
    else:
        wall_ms, wall_source = None, None
    sources = sorted({run[2]} if run else set()) + (['hook'] if hooks else [])
    model_id = (hook_model or {}).get('model_id') or (hook_model or {}).get('model') or summary.get('requested_model')
    return {'run_id': run[0] if run else None, 'session_id': session_id, 'request_id': summary.get('request_id'),
            'source': '+'.join(sources), 'client': next((h['client'] for h in hooks if h.get('client') != 'unknown'), 'cli' if run else 'unknown'),
            'cursor_version': next((h['cursor_version'] for h in hooks if h.get('cursor_version')), None) or summary.get('agent_version'),
            'started_at': (run[3] if run else None) or (start_hook or (hooks[0] if hooks else {})).get('at'),
            'finished_at': (run[4] if run else None) or (last or {}).get('at'),
            'model': model_id, 'model_display': summary.get('model_display'),
            'model_params': (hook_model or {}).get('model_params') or [],
            'requested_model': summary.get('requested_model'),
            'model_version': None, 'model_version_source': 'not_reported',
            'tokens': tokens, 'tokens_in': (tokens or {}).get('input'), 'tokens_out': (tokens or {}).get('output'),
            'tokens_total': (tokens or {}).get('total'),
            'tokens_source': 'official' if tokens else 'none', 'tokens_method': 'harness_usage' if tokens else None,
            'cost': None, 'cost_source': 'not_reported',
            'wall_ms': wall_ms, 'wall_source': wall_source, 'duration_api_ms': summary.get('duration_api_ms'),
            'exit_code': exit_code, 'status': status, 'error_kind': error_kind,
            'error_message': summary.get('error_message') or (hook_error or {}).get('error_message'),
            'tool_calls': summary.get('tool_calls'), 'hook_events': [h['event'] for h in hooks]}


def read_runs(path, run_id=None, session_id=None, since=None, until=None, limit=20, client=None):
    path = Path(path).expanduser()
    if not path.is_file():
        return {'status': 'not_captured', 'reason': 'database_missing', 'runs': []}
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        runs = list(db.execute('SELECT run_id, session_id, source, started_at, finished_at, summary FROM cursor_runs '
                               'ORDER BY COALESCE(started_at, recorded_at)')) if 'cursor_runs' in tables else []
        hooks = [{**json.loads(payload), 'at': at} for at, payload in db.execute(
            'SELECT at, payload FROM cursor_hook_events ORDER BY id')] if 'cursor_hook_events' in tables else []
    if run_id:
        try:
            run_id = str(uuid.UUID(run_id))
        except ValueError:
            return {'status': 'invalid', 'reason': 'run_id must be a UUID', 'runs': []}
    groups, order = {}, []
    for row in runs:
        key = row[1] or f'run:{row[0]}'
        if key not in groups:
            groups[key] = ([], [])
            order.append(key)
        groups[key][0].append(row)
    for hook in hooks:
        key = hook.get('session_id') or None
        if not key:
            continue
        if key not in groups:
            groups[key] = ([], [])
            order.append(key)
        groups[key][1].append(hook)
    rows = [_merge(None if key.startswith('run:') else key, *groups[key]) for key in order]
    lo, hi = _instant(since) if since else None, _instant(until) if until else None
    out = []
    for row in rows:
        if run_id and row['run_id'] != run_id:
            continue
        if session_id and row['session_id'] != session_id:
            continue
        if client and row['client'] != client:
            continue
        stamp = _instant(row['started_at']) if row['started_at'] else None
        if (lo or hi) and (stamp is None or (lo and stamp < lo) or (hi and stamp > hi)):
            continue
        out.append(row)
    out.sort(key=lambda row: row['started_at'] or '', reverse=True)
    out = out[:max(1, min(int(limit), 500))]
    official = [row for row in out if row['tokens_source'] == 'official' and row['tokens_total'] is not None]
    kinds = {}
    for row in out:
        if row['error_kind']:
            kinds[row['error_kind']] = kinds.get(row['error_kind'], 0) + 1
    return {'status': 'captured' if out else 'not_captured', 'runs': out,
            'totals': {'runs': len(out), 'with_official_tokens': len(official),
                       'official_tokens_in': sum(row['tokens_in'] for row in official),
                       'official_tokens_out': sum(row['tokens_out'] for row in official),
                       'official_tokens_total': sum(row['tokens_total'] for row in official),
                       'cost': None, 'errors_by_kind': kinds}}


def _agent_binary():
    explicit = os.environ.get('CURSOR_AGENT_BIN')
    if explicit:
        return explicit
    return shutil.which('agent') or shutil.which('cursor-agent')


VERSION_TIMEOUT = 5
_VERSION = re.compile(r'^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$')


def agent_version(binary, timeout=VERSION_TIMEOUT):
    """`agent --version` (documented as -v, --version) prints a dated build such as 2026.11.19-f005e9d.
    One short lookup per run; any failure leaves the version null and never touches the run."""
    try:
        done = subprocess.run([binary, '--version'], capture_output=True, text=True, errors='replace',
                              timeout=timeout, stdin=subprocess.DEVNULL)
    except Exception:
        return None
    line = (done.stdout or '').strip().splitlines()[:1]
    if done.returncode != 0 or not line:
        return None
    value = line[0].strip()
    return value if _VERSION.match(value) else None


def capture(argv=None):
    parser = argparse.ArgumentParser(prog='developer-observability cursor-capture',
                                     description='Run Cursor CLI headless and record its own usage and outcome.')
    parser.add_argument('--run-id', default=None)
    parser.add_argument('--format', choices=('stream-json', 'json'), default='stream-json')
    parser.add_argument('--database', default=DATABASE)
    parser.add_argument('--quiet-events', action='store_true', help='do not forward Cursor output (it can carry task content)')
    parser.add_argument('agent_args', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        run_id = str(uuid.UUID(args.run_id)) if args.run_id else str(uuid.uuid4())
    except ValueError:
        parser.error('run ID must be a UUID')
    agent_args = args.agent_args[1:] if args.agent_args[:1] == ['--'] else args.agent_args
    # The wrapper finds the CLI itself; a leading `agent` or `cursor-agent` would reach Cursor as an extra word.
    if agent_args and os.path.basename(agent_args[0]) in ('agent', 'cursor-agent'):
        agent_args = agent_args[1:]
    if not agent_args:
        parser.error('provide Cursor CLI arguments after --, for example: -- -p --model <model> "prompt"')
    if any(value == '--output-format' or value.startswith('--output-format=') for value in agent_args):
        parser.error('cursor-capture sets --output-format; use --format json|stream-json')
    binary = _agent_binary()
    if not binary:
        print('developer-observability: Cursor CLI not found (agent or cursor-agent on PATH, or CURSOR_AGENT_BIN)', file=sys.stderr)
        return 127
    print_flag = [] if any(value in ('-p', '--print') for value in agent_args) else ['-p']
    version = {}
    lookup = threading.Thread(target=lambda: version.update(value=agent_version(binary)), daemon=True)
    lookup.start()
    started_at, started = _now(), time.monotonic()
    proc = subprocess.Popen([binary, *print_flag, '--output-format', args.format, *agent_args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors='replace')
    tail = []

    def pump_stderr():
        for line in proc.stderr:
            sys.stderr.write(line)
            tail.append(line)
            del tail[:-50]

    reader = threading.Thread(target=pump_stderr, daemon=True)
    reader.start()

    def events():
        for line in proc.stdout:
            if not args.quiet_events:
                sys.stdout.write(line)
                sys.stdout.flush()
            yield line

    lines = list(events())
    code = proc.wait()
    reader.join(timeout=5)
    summary = summarize(lines, tail, code)
    lookup.join(timeout=VERSION_TIMEOUT + 1)
    summary.update(requested_model=requested_model(agent_args), elapsed_seconds=round(time.monotonic() - started, 3),
                   output_format=args.format, agent_version=version.get('value'))
    try:
        record_run(args.database, run_id, 'capture', summary, started_at=started_at, finished_at=_now())
    except Exception as error:
        print(f'developer-observability: cursor capture failed ({type(error).__name__})', file=sys.stderr)
        return code or 1
    print(json.dumps({'run_id': run_id, 'capture': 'recorded', 'tokens_source': 'official' if summary['usage'] else 'none',
                      'error_kind': summary['error_kind'], 'cursor_version': summary['agent_version']}), file=sys.stderr)
    return code


def hook_main(stdin=None, database=None):
    """Cursor hook entry. Always exits 0 and prints {} so it can never block or alter a session."""
    try:
        raw = (stdin or sys.stdin).read(MAX_HOOK_INPUT + 1)
        if len(raw) <= MAX_HOOK_INPUT:
            payload = json.loads(raw)
            if isinstance(payload, dict) and os.environ.get('DEVELOPER_OBSERVABILITY_CURSOR_HOOK') != '0':
                record_hook(database or DATABASE, payload)
    except Exception:
        pass
    sys.stdout.write('{}\n')
    return 0


def usage_main(argv=None):
    parser = argparse.ArgumentParser(prog='developer-observability cursor-usage',
                                     description='Read back captured Cursor CLI runs, or ingest a saved Cursor JSON output.')
    parser.add_argument('--run', dest='run_id')
    parser.add_argument('--session')
    parser.add_argument('--since', help='ISO time, inclusive')
    parser.add_argument('--until', help='ISO time, inclusive')
    parser.add_argument('--last', type=int, default=20)
    parser.add_argument('--client', choices=('cli', 'ide', 'unknown'))
    parser.add_argument('--database', default=DATABASE)
    parser.add_argument('--ingest', metavar='FILE', help='saved json/stream-json output ("-" for stdin)')
    parser.add_argument('--exit-code', type=int, default=None, help='exit status of the ingested run')
    parser.add_argument('--stderr-file', help='saved stderr of the ingested run, read for the error line only')
    parser.add_argument('--requested-model')
    args = parser.parse_args(argv)
    if args.ingest:
        handle = sys.stdin if args.ingest == '-' else open(args.ingest, encoding='utf-8', errors='replace')
        with handle:
            lines = list(handle)
        stderr_lines = []
        if args.stderr_file:
            with open(args.stderr_file, encoding='utf-8', errors='replace') as handle:
                stderr_lines = handle.readlines()[-50:]
        summary = summarize(lines, stderr_lines, args.exit_code)
        summary['requested_model'] = _model(args.requested_model)
        run_id = record_run(args.database, args.run_id or str(uuid.uuid4()), 'ingest', summary, started_at=_now())
        result = read_runs(args.database, run_id=run_id)
    else:
        result = read_runs(args.database, run_id=args.run_id, session_id=args.session, since=args.since,
                           until=args.until, limit=args.last, client=args.client)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write('\n')
    return 0 if result.get('status') != 'invalid' else 2


if __name__ == '__main__':
    command = sys.argv[1] if len(sys.argv) > 1 else ''
    rest = sys.argv[2:]
    if command == 'capture':
        sys.exit(capture(rest))
    if command == 'hook':
        sys.exit(hook_main())
    if command == 'usage':
        sys.exit(usage_main(rest))
    print('usage: cursor_runs.py capture|hook|usage ...', file=sys.stderr)
    sys.exit(2)
