"""Write the local binding: data dir, optional Decider and project mappings. Paths and metadata only."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys

DEFAULT_CONFIG = Path.home() / '.config/developer-observability/config.json'
DEFAULT_DATA = '~/.local/share/developer-observability/data'
# Optional producers detected when present (home-relative). Absent ones are simply left out.
KNOWN_RUNTIMES = ['~/Library/Application Support/KindleSnapshot/runtime']
KNOWN_DECIDERS = ['~/.agents/skills/mac-decider-orchestration/scripts/decide.py']
PROJECT_ID = re.compile(r'^[a-z0-9][a-z0-9-]{0,63}$')
PROJECT_KEYS = {'label', 'labels', 'owners', 'recommendation_target'}


def _tilde(value) -> str:
    text, home = str(value), str(Path.home())
    return '~' + text[len(home):] if text == home or text.startswith(home + os.sep) else text


def default_config() -> dict:
    runtime = next((item for item in KNOWN_RUNTIMES if (Path(item).expanduser() / 'findings.json').is_file()),
                   DEFAULT_DATA)
    decider = next((item for item in KNOWN_DECIDERS if Path(item).expanduser().is_file()), None)
    return {'runtime': runtime, 'decider_script': decider, 'discover_projects': True, 'projects': {}}


def parse_project(spec: str) -> tuple[str, dict]:
    """`id` or `id,label=Text,labels=a|b,owners=owner:x|owner:y,recommendation_target=t`."""
    key, *pairs = spec.split(',')
    if not PROJECT_ID.match(key):
        raise ValueError(f'invalid project id: {key!r}')
    entry: dict = {'label': key, 'labels': [], 'owners': []}
    for pair in pairs:
        name, sep, value = pair.partition('=')
        if not sep or name not in PROJECT_KEYS:
            raise ValueError(f'unknown project field: {name!r}')
        entry[name] = [item for item in value.split('|') if item] if name in ('labels', 'owners') else value
    return key, entry


def write_config(path: Path, config: dict) -> bool:
    """Create the config once (0600, parent 0700). Never overwrites; returns False when it exists."""
    path = Path(path)
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, 'w') as output:
        json.dump(config, output, indent=2)
        output.write('\n')
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path(os.environ.get('OBSERVABILITY_CONFIG') or DEFAULT_CONFIG))
    parser.add_argument('--runtime', help='directory that holds findings.json')
    parser.add_argument('--decider', help='optional local Decider script')
    parser.add_argument('--no-discover', action='store_true', help='only list configured projects')
    parser.add_argument('--project', action='append', default=[], help='id[,label=..][,labels=a|b][,owners=x|y]')
    parser.add_argument('--print', action='store_true', help='print the config that would be written')
    args = parser.parse_args(argv)
    config = default_config()
    if args.runtime:
        config['runtime'] = _tilde(Path(args.runtime).expanduser().resolve())
    if args.decider:
        config['decider_script'] = _tilde(Path(args.decider).expanduser().resolve())
    if args.no_discover:
        config['discover_projects'] = False
    try:
        config['projects'] = dict(parse_project(spec) for spec in args.project)
    except ValueError as error:
        parser.error(str(error))
    if args.print:
        print(json.dumps(config, indent=2))
        return 0
    if not write_config(args.config, config):
        print('config exists; edit it explicitly, it is never overwritten', file=sys.stderr)
        return 1
    print(f'Wrote {_tilde(args.config)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
