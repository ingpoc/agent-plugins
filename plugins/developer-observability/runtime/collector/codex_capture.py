"""Run Codex and record its usage without retaining the event stream."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

from codex_usage import record, summarize
from codex_sessions import effective_config


def requested_config(args):
    model = effort = None
    for index, value in enumerate(args):
        if value in ('-m', '--model') and index + 1 < len(args):
            candidate = args[index + 1]
            if re.fullmatch(r'(?:gpt-[A-Za-z0-9._-]{1,76}|o[0-9][A-Za-z0-9._-]{0,77})', candidate):
                model = candidate
        elif value.startswith('--model='):
            candidate = value.partition('=')[2]
            if re.fullmatch(r'(?:gpt-[A-Za-z0-9._-]{1,76}|o[0-9][A-Za-z0-9._-]{0,77})', candidate):
                model = candidate
        elif value in ('-c', '--config') and index + 1 < len(args):
            match = re.fullmatch(r'''model_reasoning_effort\s*=\s*["']?(none|low|medium|high|xhigh|max|ultra)["']?''', args[index + 1])
            if match:
                effort = match.group(1)
    return {'requested_model': model, 'requested_effort': effort}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task-id', default=None)
    parser.add_argument('--category', choices=('coding', 'research', 'computer_use', 'writing', 'analysis', 'other', 'unknown'), default='unknown')
    parser.add_argument('--database', default='~/.local/share/developer-observability/task-usage.sqlite3')
    parser.add_argument('--verify', help='caller-supplied executable check, run after a successful task')
    parser.add_argument('codex_args', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        task_id = str(uuid.UUID(args.task_id)) if args.task_id else str(uuid.uuid4())
    except ValueError:
        parser.error('task ID must be a UUID')
    codex_args = args.codex_args[1:] if args.codex_args[:1] == ['--'] else args.codex_args
    if not codex_args:
        parser.error('provide Codex arguments after --')
    if args.verify and (not Path(args.verify).is_file() or not os.access(args.verify, os.X_OK)):
        parser.error('verification check must be an executable file')
    started = time.monotonic()
    proc = subprocess.Popen(['codex', 'exec', '--json', *codex_args], stdout=subprocess.PIPE, text=True)

    def events():
        for line in proc.stdout:
            sys.stdout.write(line)
            yield line

    summary = summarize(events())
    summary.update(requested_config(codex_args))
    proc.stdout.close()
    summary['process_exit_code'] = proc.wait()
    summary['model'], summary['effort'] = effective_config(summary.get('thread_id'))
    summary['elapsed_seconds'] = round(time.monotonic() - started, 3)
    summary['check_status'] = 'not_run'
    if args.verify and summary['process_exit_code'] == 0 and summary['completed_turns'] and not summary['failed_turns']:
        check_started = time.monotonic()
        try:
            check = subprocess.run([str(Path(args.verify).resolve())], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=300)
            summary['check_status'] = 'passed' if check.returncode == 0 else 'failed'
        except (OSError, subprocess.TimeoutExpired):
            summary['check_status'] = 'error'
        summary['check_seconds'] = round(time.monotonic() - check_started, 3)
    try:
        record(args.database, task_id, args.category, summary)
    except Exception as error:
        print(f'developer-observability: usage capture failed ({type(error).__name__})', file=sys.stderr)
        return summary['process_exit_code'] or 1
    else:
        print(json.dumps({'task_id': task_id, 'capture': 'recorded'}), file=sys.stderr)
    return summary['process_exit_code'] or (1 if args.verify and summary['check_status'] != 'passed' else 0)


if __name__ == '__main__':
    sys.exit(main())
