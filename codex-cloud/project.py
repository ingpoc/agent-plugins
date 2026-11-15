#!/usr/bin/env python3
"""Read-only cloud project preflight, tracker summary, and observability triage."""
import argparse
import glob
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import cloud_health

HOME = Path.home()
CACHE = HOME / '.codex/plugins/cache/ingpoc-agent-plugins'
STATE = HOME / '.agents/state/batched-change-list'
BRIDGE = Path(__file__).resolve().with_name('observability.py')
PYTHON = HOME / '.local/share/developer-observability/runtime/bin/python3'


def installed(name):
    paths = [Path(p) for p in glob.glob(str(CACHE / name / '*' / 'plugin.json'))]
    versions = []
    for path in paths:
        try:
            data = json.loads(path.read_text())
            versions.append((data.get('version', 'unknown'), path.parent))
        except (OSError, ValueError):
            continue
    return sorted(versions, key=lambda x: x[0])


def bridge_call(name, args):
    proc = subprocess.run([str(PYTHON), str(BRIDGE), name, json.dumps(args)], capture_output=True, text=True, timeout=30)
    if proc.returncode:
        raise RuntimeError(f'{name}: {proc.stderr.strip() or proc.stdout.strip()}')
    result = json.loads(proc.stdout)
    if result.get('isError'):
        raise RuntimeError(f'{name}: {result.get("content")}')
    return result.get('structuredContent', result)


def preflight():
    for name in ('batched-change-list', 'developer-observability', 'context-ledger'):
        found = installed(name)
        print(f'{name}: ' + (', '.join(v for v, _ in found) if found else 'not found'))
    print('observability bridge:', 'available' if PYTHON.is_file() and BRIDGE.is_file() else 'unavailable')
    print('Sites preview:', shutil.which('sites-preview') or 'unavailable')
    print('Preview health:', json.dumps(cloud_health.health(), ensure_ascii=False))
    print('Native tool availability: check the host tool inventory separately.')


def summary(path):
    data = json.loads(path.read_text())
    print(f"# {data.get('title', 'Change list')}\n")
    print('| ID | Item | Status |')
    print('|---|---|---|')
    for item in data.get('findings', []):
        cells = [item.get('id', ''), item.get('title') or item.get('summary', ''), item.get('status', '')]
        print('| ' + ' | '.join(str(c).replace('|', '\\|').replace('\n', ' ') for c in cells) + ' |')
    print('\nSource:', path, '(confirm this is the authoritative live file)')


def diagnose(project, directory):
    args = {'project_id': project, 'include_coverage': True}
    try:
        result = bridge_call('observability_diagnose', args)
    except RuntimeError as exc:
        if 'unknown_project' not in str(exc) or not directory:
            raise
        collected = bridge_call('observability_collect', {'project_dir': str(directory)})
        print('Collection:', json.dumps(collected, ensure_ascii=False))
        try:
            result = bridge_call('observability_diagnose', args)
        except RuntimeError as retry_error:
            if 'unknown_project' not in str(retry_error):
                raise
            print(json.dumps({'project_id': project, 'state': 'no_project_findings',
                              'coverage': 'unknown', 'note': 'No project findings after collection; health is not established.'}, indent=2))
            return
        source = result.get('data_source') or {}
        if source.get('status') == 'no_findings' or result.get('freshness') in ('unavailable', 'stale'):
            raise RuntimeError('project diagnosis remains unavailable after collection')
        print_diagnosis(project, result)
        return
    source = result.get('data_source') or {}
    freshness = result.get('freshness')
    if source.get('status') == 'no_findings' or freshness in ('unavailable', 'stale'):
        collect_args = {'project_dir': str(directory)} if directory else {}
        collected = bridge_call('observability_collect', collect_args)
        print('Collection:', json.dumps(collected, ensure_ascii=False))
        result = bridge_call('observability_diagnose', args)
    print_diagnosis(project, result)

def print_diagnosis(project, result):
    brief = result.get('brief') or {}
    print(json.dumps({'project_id': project, 'snapshot_id': result.get('snapshot_id'), 'freshness': result.get('freshness'), 'coverage': result.get('coverage'), 'sources': result.get('sources'), 'unassigned_issue_count': result.get('unassigned_issue_count'), 'digest': brief.get('digest', []), 'top_failures': brief.get('top_failures', [])}, indent=2, ensure_ascii=False))
    print('Empty findings do not establish successful behavior; run acceptance checks separately.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('preflight')
    p = sub.add_parser('summary', help='Render a compact tracker table from the authoritative live JSON')
    p.add_argument('live_json', type=Path)
    p = sub.add_parser('diagnose', help='Diagnose once; collect only if absent or stale')
    p.add_argument('project_id'); p.add_argument('--project-dir', type=Path)
    args = parser.parse_args()
    if args.command == 'preflight': preflight()
    elif args.command == 'summary': summary(args.live_json)
    else: diagnose(args.project_id, args.project_dir)

if __name__ == '__main__':
    try: main()
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f'project.py: {exc}', file=sys.stderr); sys.exit(1)
