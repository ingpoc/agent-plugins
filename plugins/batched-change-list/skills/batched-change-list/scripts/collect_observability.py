#!/usr/bin/env python3
"""Refresh stale developer-observability data and print BCL-ready project findings."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import glob
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def _observability_root(explicit: str | None) -> Path:
    candidates = [Path(explicit).expanduser()] if explicit else []
    candidates.extend([
        Path.home() / '.agents/plugins/developer-observability',
    ])
    for pattern in (
        '~/.cursor/plugins/cache/*/developer-observability/*',
        '~/.agents/plugins/cache/*/developer-observability/*',
    ):
        candidates.extend(Path(item).expanduser() for item in glob.glob(str(Path(pattern).expanduser())))
    for candidate in candidates:
        if (candidate / 'runtime/server.py').is_file():
            return candidate
    raise FileNotFoundError('developer-observability plugin root not found; pass --observability-root')


def _load_backend(root: Path):
    runtime = root / 'runtime'
    sys.path.insert(0, str(runtime))
    from backend.observability_api import ObservabilityAPI
    from configure import default_config

    config_path = Path(os.environ.get('OBSERVABILITY_CONFIG', '~/.config/developer-observability/config.json')).expanduser()
    try:
        config = json.loads(config_path.read_text())
    except FileNotFoundError:
        config = default_config()
    collected = (config.get('collector') or {}).get('out', '~/.local/share/developer-observability/collected')
    api = ObservabilityAPI(Path(config.get('runtime') or '~/.local/share/developer-observability/data').expanduser(),
                           config.get('projects') or {},
                           Path(config['decider_script']).expanduser() if config.get('decider_script') else None,
                           bool(config.get('discover_projects', False)), Path(collected).expanduser())
    return config, api


def _run_collector(root: Path, config: dict, project_dir: str | None) -> None:
    command = shutil.which('developer-observability')
    if command:
        argv = [command, 'collect']
    else:
        argv = [sys.executable, str(root / 'runtime/collector/cli.py')]
    config_path = os.environ.get('OBSERVABILITY_CONFIG')
    if config_path:
        argv += ['--config', config_path]
    if project_dir:
        argv += ['--project-dir', project_dir]
    subprocess.run(argv, check=True, stdout=sys.stderr)


def _collector_is_fresh(api) -> bool:
    """Check the collector file itself; merged diagnosis freshness can mask it."""
    if not api.collected:
        return False
    path = Path(api.collected) / 'findings.json'
    try:
        snapshot = json.loads(path.read_text())
        stamp = snapshot.get('generated_at')
        generated = datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp()
        age = datetime.now(timezone.utc).timestamp() - generated
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    return 0 <= age <= 300


def _resolved_dir(path: str) -> str:
    return str(Path(path).expanduser().resolve())


def _collector_covers_project_dir(api, project_dir: str) -> bool:
    """A requested directory is covered only by a non-missing source for its full path."""
    if not api.collected:
        return False
    try:
        snapshot = json.loads((Path(api.collected) / 'findings.json').read_text())
    except (OSError, ValueError, TypeError):
        return False
    try:
        requested = _resolved_dir(project_dir)
    except (OSError, RuntimeError):
        return False
    coverage = snapshot.get('coverage')
    if not isinstance(coverage, dict):
        coverage = {}
    stats = snapshot.get('collector_stats')
    if not isinstance(stats, dict):
        return False
    for source, item in stats.items():
        if not isinstance(item, dict):
            continue
        recorded = item.get('project_dir')
        if not isinstance(recorded, str) or not recorded:
            continue
        try:
            path = _resolved_dir(recorded)
        except (OSError, RuntimeError):
            path = recorded
        if path == requested and coverage.get(source) not in (None, 'missing'):
            return True
    return False


def _primary_finding_keys(api) -> set[str]:
    try:
        primary = json.loads((Path(api.runtime) / 'findings.json').read_text())
    except (OSError, ValueError, TypeError):
        return set()
    findings = primary.get('findings', []) if isinstance(primary, dict) else []
    return {item['finding_key'] for item in findings
            if isinstance(item, dict) and isinstance(item.get('finding_key'), str)}


def collect(project_id: str, observability_root: str | None = None, project_dir: str | None = None,
            *, backend_loader=None, collector_runner=None) -> list[dict]:
    root = _observability_root(observability_root)
    config, api = (backend_loader or _load_backend)(root)
    fresh = _collector_is_fresh(api)
    if fresh and project_dir and not _collector_covers_project_dir(api, project_dir):
        raise ValueError(f'fresh collector snapshot does not cover requested --project-dir: {project_dir}')
    if not fresh:
        (collector_runner or _run_collector)(root, config, project_dir)
    diagnosis = api.diagnose(project_id=project_id)
    snapshot_id = diagnosis.get('snapshot_id')
    primary_keys = _primary_finding_keys(api)
    primary_path = Path(api.runtime) / 'findings.json'
    collector_path = Path(api.collected) / 'findings.json' if api.collected else None
    rows = []
    cursor = None
    while True:
        listed = api.issues(project_id=project_id, status='all', limit=100, cursor=cursor)
        for issue in listed.get('issues', []):
            key = issue.get('issue_id')
            if key:
                evidence_path = primary_path if key in primary_keys else collector_path
                rows.append({'finding_key': key, 'evidence_path': str(evidence_path) if evidence_path else '',
                             'project_id': project_id, 'snapshot_id': snapshot_id,
                             'finding': issue})
        cursor = listed.get('next_cursor')
        if not cursor:
            break
    return rows


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-id', required=True)
    parser.add_argument('--project-dir')
    parser.add_argument('--observability-root')
    args = parser.parse_args(argv)
    try:
        rows = collect(args.project_id, args.observability_root, args.project_dir)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f'collect_observability: {error}', file=sys.stderr)
        return 1
    print(json.dumps(rows, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
