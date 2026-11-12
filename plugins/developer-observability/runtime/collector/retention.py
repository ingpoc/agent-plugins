"""0.1.8 automatic clearing and storage report for developer-observability (stdlib only).

Pruning only touches files this plugin creates, under its own directories:
- collector outputs (`collector.out`, default ~/.local/share/developer-observability/collected): the live
  findings.json is always kept; snapshots and scratch older than `days` go;
- plugin caches (default ~/.cache/observability): files older than `days`; configs and backups
  (`*.bak*`, `*.json` configs) are kept for `backup_days`;
- trace data (`retention.trace_paths`, none by default): files older than `days`.
Removed files are MOVED to `trash_dir` (default ~/.Trash/developer-observability-pruned/<stamp>/), never rm'd,
and a run stops moving once the trash dir holds `max_trash_bytes`. Emptying the Trash is left to the user.

Hard exclusions (never moved, written or opened): every `[protected].paths` entry of ~/.agents/machine.toml
(read each run; skipped quietly when absent), ~/.cache/macos-cua and anything else ACU owns, Samantha's
logs (samantha-activity*.jsonl, voice.log; Samantha/ACU rotates them), the plugin's runtime venv and
config.json. Those paths are only measured (stat) for the storage report.
"""
from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path
import re
import shutil
import time

DAY = 86400
DEFAULTS = {'enabled': True, 'days': 7, 'backup_days': 30, 'trace_paths': [],
            'cache_dirs': ['~/.cache/observability'], 'trash_dir': '~/.Trash/developer-observability-pruned',
            'max_trash_bytes': 2 * 1024 ** 3, 'warn_bytes': 1024 ** 3}
SAMANTHA_LOGS = ('samantha-activity*.jsonl', 'voice.log')
BACKUP_PATTERNS = ('*.bak*', '*.json.bak*', 'config*.json', '*.toml')


def machine_protected(home):
    """Paths in `[protected].paths` of ~/.agents/machine.toml (Hygiene's list); missing file or list -> []."""
    try:
        import tomllib
        with open(Path(home) / '.agents/machine.toml', 'rb') as handle:
            paths = tomllib.load(handle).get('protected', {}).get('paths', [])
    except Exception:
        return []
    if not isinstance(paths, list):
        return []
    out = []
    for item in paths:
        if isinstance(item, str) and item.strip():
            out.append(Path(home) / item[2:] if item.startswith('~/') else Path(item))
    return out


def protected_roots(home=None):
    home = Path(home or Path.home())
    return machine_protected(home) + [home / '.cache/macos-cua', home / 'Library/Application Support/agent-computer-use',
            home / 'Library/Logs/agent-computer-use', home / '.config/developer-observability',
            home / '.local/share/developer-observability/runtime']


def settings(config):
    raw = config.get('retention') if isinstance(config.get('retention'), dict) else {}
    out = dict(DEFAULTS)
    for key, default in DEFAULTS.items():
        value = raw.get(key)
        if isinstance(default, bool):
            out[key] = value if isinstance(value, bool) else default
        elif isinstance(default, int):
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                out[key] = value
        elif isinstance(default, str):
            out[key] = value if isinstance(value, str) and value else default
        elif isinstance(value, list):
            out[key] = [v for v in value if isinstance(v, str) and v]
    return out


def _inside(path, root):
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def is_protected(path, home=None, roots=None):
    path = Path(path)
    if any(fnmatch.fnmatch(path.name, p) for p in SAMANTHA_LOGS):
        return True
    if roots is None:
        roots = protected_roots(home)
    return any(path == root or _inside(path, root) for root in roots)


def _size(path):
    total = 0
    try:
        if path.is_symlink():
            return 0
        if path.is_file():
            return path.stat().st_blocks * 512
        for base, dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.lstat(os.path.join(base, name)).st_blocks * 512
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _skip_dirs(root, keep):
    """Resolved directories the walker must not enter (kept snapshots and staging)."""
    skipped = set()
    for path in list(keep) + [Path(root) / '.trash']:
        try:
            if path.is_dir() and not path.is_symlink():
                skipped.add(path.resolve())
        except OSError:
            pass
    return skipped


def _candidates(root, keep, now, days, backup_days, home):
    if not root.is_dir() or is_protected(root, home):
        return
    roots = protected_roots(home)
    skip = _skip_dirs(root, keep)
    keep_files = set()
    for path in keep:
        try:
            if path.is_file():
                keep_files.add(path.resolve())
        except OSError:
            pass
    for base, dirs, files in os.walk(root):
        base_path = Path(base)
        remaining = []
        for name in dirs:
            child = base_path / name
            try:
                if child.is_symlink() or child.resolve() in skip or is_protected(child, home, roots=roots):
                    continue
            except OSError:
                continue
            remaining.append(name)
        dirs[:] = remaining
        for name in files:
            path = base_path / name
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if resolved in keep_files or path.is_symlink() or is_protected(path, home, roots=roots):
                continue
            limit = backup_days if any(fnmatch.fnmatch(name, p) for p in BACKUP_PATTERNS) else days
            try:
                age = now - path.stat().st_mtime
            except OSError:
                continue
            if age > limit * DAY:
                yield path, root, int(age // DAY)


def prune(config, out_dir, dry_run=False, now=None, home=None):
    """Move expired plugin-owned files to the trash dir. Returns a bounded summary."""
    opts = settings(config)
    home = Path(home or Path.home())
    now = time.time() if now is None else now
    out_dir = Path(out_dir).expanduser()
    keep = {out_dir / 'findings.json'}
    try:
        snapshot_id = json.loads((out_dir / 'findings.json').read_text()).get('snapshot_id')
    except (OSError, ValueError, AttributeError):
        snapshot_id = None
    if isinstance(snapshot_id, str) and len(snapshot_id) == 32 and all(c in '0123456789abcdef' for c in snapshot_id):
        keep.add(out_dir / 'snapshots' / snapshot_id)
    staged = [path for path in (out_dir / '.trash').iterdir()
              if path.is_dir() and not path.is_symlink() and re.fullmatch(r'[0-9a-f]{32}', path.name)] \
        if (out_dir / '.trash').is_dir() else []
    keep.update(staged)
    roots = [('outputs', out_dir, keep)]
    roots += [('cache', Path(p).expanduser(), set()) for p in opts['cache_dirs']]
    roots += [('traces', Path(p).expanduser(), set()) for p in opts['trace_paths']]
    trash = Path(opts['trash_dir']).expanduser() / time.strftime('%Y%m%d-%H%M%S', time.localtime(now))
    budget = opts['max_trash_bytes'] - _size(Path(opts['trash_dir']).expanduser())
    moved, freed, capped, items = 0, 0, False, []
    if not opts['enabled']:
        return {'enabled': False, 'dry_run': dry_run, 'moved': 0, 'bytes': 0}
    for path in staged:
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue
        if age <= opts['days'] * DAY:
            continue
        size = _size(path)
        if size > budget:
            capped = True
            continue
        if len(items) < 50:
            items.append({'root': 'outputs', 'name': path.name, 'age_days': int(age // DAY), 'bytes': size})
        if not dry_run:
            target = trash / 'outputs' / path.relative_to(out_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(target))
        budget -= size
        moved += 1
        freed += size
    for label, root, keep in roots:
        for path, base, age in _candidates(root, keep, now, opts['days'], opts['backup_days'], home):
            size = _size(path)
            if size > budget:
                capped = True
                continue
            if len(items) < 50:
                items.append({'root': label, 'name': path.name, 'age_days': age, 'bytes': size})
            if not dry_run:
                target = trash / label / path.relative_to(base)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(target))
            budget -= size
            moved += 1
            freed += size
    return {'enabled': True, 'dry_run': dry_run, 'moved': moved, 'bytes': freed, 'capped': capped,
            'days': opts['days'], 'backup_days': opts['backup_days'], 'trash_dir': '~/.Trash/' + Path(opts['trash_dir']).name,
            'items': items}


def known_paths(config, out_dir, home=None):
    opts = settings(config)
    home = Path(home or Path.home())
    paths = [('collector_outputs', Path(out_dir).expanduser()),
             ('runtime_venv', home / '.local/share/developer-observability/runtime')]
    paths += [(f'cache:{Path(p).name}', Path(p).expanduser()) for p in opts['cache_dirs']]
    paths += [(f'traces:{Path(p).name}', Path(p).expanduser()) for p in opts['trace_paths']]
    paths.append(('pruned_trash', Path(opts['trash_dir']).expanduser()))
    acu = home / '.cache/macos-cua'
    for pattern in SAMANTHA_LOGS:  # size only (stat); never opened or written here
        for path in sorted(acu.glob(pattern)) if acu.is_dir() else []:
            paths.append((f'samantha_log:{path.name}', path))
    machines = home / 'Library/Application Support/com.apple.container/plugin-state/machine-apiserver/machines'
    for path in sorted(machines.glob('*signoz*')) if machines.is_dir() else []:
        paths.append((f'container_vm:{path.name}', path))
    return paths


def report(config, out_dir, home=None):
    opts = settings(config)
    rows = []
    for label, path in known_paths(config, out_dir, home):
        if path.exists():
            size = _size(path)
            rows.append({'path': label, 'bytes': size, 'over_threshold': size >= opts['warn_bytes']})
    return {'warn_bytes': opts['warn_bytes'], 'paths': rows, 'total_bytes': sum(r['bytes'] for r in rows)}


def storage_findings(storage, stamp):
    """One `storage` finding per path over `warn_bytes`, so agents see growth in observability_issues."""
    import hashlib
    out = []
    for row in storage['paths']:
        if not row['over_threshold']:
            continue
        project = 'samantha' if row['path'].startswith('samantha_log') else 'developer-observability'
        gib = row['bytes'] / 1024 ** 3
        out.append({
            'finding_key': 'sto-' + hashlib.sha256(f"storage|{row['path']}".encode()).hexdigest()[:16],
            'kind': 'storage', 'error_class': 'storage_over_threshold',
            'headline': f"{row['path']} uses {gib:.1f} GiB (warn at {storage['warn_bytes'] / 1024 ** 3:.1f} GiB)",
            'source': 'developer-observability:storage', 'owner_ref': f'owner:{project}', 'status': 'active',
            'first_seen': stamp, 'last_seen': stamp, 'failed_attempt_count': 1, 'confirmed_recovery_count': 0,
            'evidence_event_ids': [], 'failure_attempt_times': [stamp], 'failure_attempt_times_complete': True,
            'recovery_times': [], 'recovery_times_complete': True, 'workflow_id': 'wf:developer-observability:storage',
            'step_id': f'step:{project}-storage',
            'latest_evidence': {'project': project, 'tool': 'storage', 'first_error_line': row['path'], 'error_type': 'storage'},
            'storage': {'path': row['path'], 'bytes': row['bytes'], 'warn_bytes': storage['warn_bytes']}})
    return out
