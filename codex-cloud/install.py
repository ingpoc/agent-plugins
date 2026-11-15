#!/usr/bin/env python3
"""Install project plugins from this checkout into the current Codex profile."""
import argparse
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MARKET = 'ingpoc-agent-plugins'
PLUGINS = ('batched-change-list', 'developer-observability')


def run(*args, capture=False):
    return subprocess.run(args, check=True, text=True, capture_output=capture)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context-ledger', action='store_true', help='Install Context Ledger too; does not create or bind a ledger')
    args = parser.parse_args()
    output = run('codex', 'plugin', 'marketplace', 'list', capture=True).stdout
    matches = [line.split(None, 1) for line in output.splitlines() if line.split() and line.split()[0] == MARKET]
    if matches:
        if len(matches) != 1 or len(matches[0]) != 2 or Path(matches[0][1]).resolve() != ROOT:
            raise RuntimeError(f'{MARKET} is already registered at a different location; inspect it before changing the registration')
        print(f'Using marketplace at {ROOT}', flush=True)
    else:
        run('codex', 'plugin', 'marketplace', 'add', str(ROOT))
    for name in PLUGINS + (('context-ledger',) if args.context_ledger else ()):
        run('codex', 'plugin', 'add', f'{name}@{MARKET}')
    run(sys.executable, str(ROOT / 'plugins/developer-observability/runtime/install.py'))
    run('codex', 'plugin', 'list')
    print('\nInstallation finished. A new Codex session may be needed for tool discovery.')
    print('Context Ledger was not initialized or bound; follow its own skill and owner authorization.')

if __name__ == '__main__':
    try:
        main()
    except (OSError, subprocess.CalledProcessError, RuntimeError) as error:
        print(f'codex-cloud install: {error}', file=sys.stderr)
        sys.exit(1)
