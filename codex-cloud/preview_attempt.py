#!/usr/bin/env python3
"""Record bounded preview attempts and inspect explicitly referenced evidence."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

import project

ID_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')
DEFAULT_DIR = Path.home() / '.local/state/codex-cloud/preview-attempts'


def identifier(value):
    if value is not None and not ID_PATTERN.fullmatch(value):
        raise ValueError('identifiers must contain only letters, numbers, dot, underscore, colon, or hyphen')
    return value


def record_attempt(site_root, project_id, issue_id=None, finding_id=None, record_dir=DEFAULT_DIR):
    identifier(project_id); identifier(issue_id); identifier(finding_id)
    executable = shutil.which('sites-preview')
    attempt = {'schema_version': 1, 'attempt_id': str(uuid.uuid4()),
               'timestamp_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
               'project_id': project_id, 'operation': 'preview_start', 'phase': 'unknown',
               'outcome': 'failed', 'error_code': 'unknown', 'exit_code': None,
               'issue_id': issue_id, 'finding_id': finding_id,
               'reference_note': 'User-supplied references are leads, not proof of attribution.',
               'service_diagnostics': 'not provided by preview CLI'}
    preview_url = None
    if not executable:
        attempt['error_code'] = 'preview_command_missing'
    else:
        try:
            result = subprocess.run([executable, 'start', str(site_root)], capture_output=True, text=True, timeout=45, check=False)
            attempt['exit_code'] = result.returncode
            if result.returncode == 0:
                attempt.update(outcome='succeeded', phase='ready', error_code=None)
                # Return the exact URL in the command response only; never save command output.
                urls = re.findall(r'https?://[^\s<>"\']+', result.stdout)
                if len(urls) == 1:
                    preview_url = urls[0]
            elif 'timed out waiting for relay readiness' in result.stderr:
                attempt.update(phase='relay_readiness', error_code='relay_readiness_timeout')
            else:
                attempt['error_code'] = 'preview_start_failed'
        except subprocess.TimeoutExpired:
            attempt['error_code'] = 'command_timeout'
        except OSError:
            attempt['error_code'] = 'preview_command_failed_to_start'
    record_dir = Path(record_dir)
    record_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = record_dir / (attempt['attempt_id'] + '.json')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(attempt, f, indent=2); f.write('\n')
    return attempt, path, preview_url


def diagnose_attempt(path, tracker=None):
    record = json.loads(Path(path).read_text())
    if record.get('schema_version') != 1 or record.get('operation') != 'preview_start':
        raise ValueError('unsupported attempt record')
    if not isinstance(record.get('project_id'), str): raise ValueError('missing project ID')
    identifier(record['project_id']); identifier(record.get('issue_id')); identifier(record.get('finding_id'))
    # Read only allowlisted fields; records and external findings are untrusted.
    report = {'attempt_id': record.get('attempt_id'), 'timestamp_utc': record.get('timestamp_utc'),
              'project_id': record.get('project_id'), 'phase': record.get('phase'),
              'outcome': record.get('outcome'), 'error_code': record.get('error_code'),
              'tracker_reference': {'id': record.get('issue_id'), 'state': 'not_checked'},
              'observability_reference': {'id': record.get('finding_id'), 'state': 'not_checked'},
              'interpretation': 'References are unverified leads; reproduce and inspect evidence before determining cause.'}
    issue = record.get('issue_id')
    if tracker and issue:
        try:
            data = json.loads(Path(tracker).read_text())
            matches = [x for x in data.get('findings', []) if isinstance(x, dict) and x.get('id') == issue]
            report['tracker_reference']['state'] = 'found' if len(matches) == 1 else 'not_found'
            if len(matches) == 1: report['tracker_reference']['status'] = matches[0].get('status')
        except (OSError, ValueError, TypeError):
            report['tracker_reference']['state'] = 'unavailable'
    finding = record.get('finding_id')
    if finding:
        try:
            discovery = project.bridge_call('observability_diagnose', {'project_id': record['project_id'], 'include_coverage': True})
            snapshot = discovery.get('snapshot_id')
            if not snapshot:
                report['observability_reference']['state'] = 'unavailable'
            else:
                data = project.bridge_call('observability_diagnose', {'project_id': record['project_id'], 'snapshot_id': snapshot, 'issue_ids': [finding]})
                matches = [x for x in data.get('issues', []) if x.get('issue', {}).get('issue_id') == finding]
                report['observability_reference'].update(state='found' if len(matches) == 1 else 'not_found', snapshot_id=snapshot, freshness=discovery.get('freshness'), coverage=discovery.get('coverage'))
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
            report['observability_reference']['state'] = 'unavailable'
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    start = sub.add_parser('start', help='Start managed preview and save a bounded attempt record')
    start.add_argument('site_root', type=Path); start.add_argument('--project', required=True)
    start.add_argument('--issue'); start.add_argument('--finding'); start.add_argument('--record-dir', type=Path, default=DEFAULT_DIR)
    inspect = sub.add_parser('diagnose', help='Inspect an attempt and explicitly referenced findings')
    inspect.add_argument('record', type=Path); inspect.add_argument('--tracker', type=Path)
    args = parser.parse_args()
    if args.command == 'start':
        root = args.site_root.resolve(strict=True)
        if not root.is_dir(): raise ValueError('site_root must be a directory')
        record, path, url = record_attempt(root, args.project, args.issue, args.finding, args.record_dir)
        print(json.dumps({'record': str(path), 'attempt': record, 'preview_url': url}, indent=2))
        return 0 if record['outcome'] == 'succeeded' else 1
    print(json.dumps(diagnose_attempt(args.record, args.tracker), indent=2)); return 0

if __name__ == '__main__':
    try: sys.exit(main())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f'preview_attempt: {type(exc).__name__}: unable to complete request', file=sys.stderr); sys.exit(2)
