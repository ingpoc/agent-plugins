"""One build identity for the portable package, signed app and running service."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import sys

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
APP = Path('/Applications/Agent Computer Use.app')
RECORD = 'Contents/Resources/runtime-contract.json'


def package_identity(root: Path) -> dict:
    digest = hashlib.sha256()
    paths = []
    for directory, children, files in os.walk(root):
        children[:] = [d for d in children if d not in
                       {'.git', '.build', '__pycache__', 'Tests', 'tests', '.swiftpm',
                        'run', '.ruff_cache', '.pytest_cache'}]
        paths.extend(Path(directory) / f for f in files
                     if f != '.DS_Store' and not f.endswith('.pyc'))
    for path in sorted(set(paths)):
        digest.update(path.relative_to(root).as_posix().encode() + b'\0')
        digest.update(path.read_bytes() + b'\0')
    return {'version': json.loads((root / 'plugin.json').read_text())['version'],
            'sha256': digest.hexdigest()}


def check(root: Path, app: Path, *, live: bool = True) -> dict:
    expected = package_identity(root)
    recorded = json.loads((app / RECORD).read_text())
    info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    if recorded != expected or info.get('CFBundleShortVersionString') != expected['version']:
        raise RuntimeError('package/app build mismatch; rebuild and sync from the same source')
    if live:
        sys.path.insert(0, str(root / 'skills/macos-cua/service'))
        from cua_client import CUAClient
        client = CUAClient()
        # Probe only: verification must not relaunch or replace a service.
        client._sock = client._probe()
        try:
            active = client.call('runtime_contract')
        finally:
            client.close()
        if active != expected:
            raise RuntimeError('running service build mismatch; restart the installed app')
    return {'ok': True, 'identity': expected, 'app': str(app), 'live': live}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, default=Path(os.environ.get('AGENT_COMPUTER_USE_APP', str(APP))))
    args = parser.parse_args()
    try:
        print(json.dumps(check(PLUGIN_ROOT, args.app)))
        return 0
    except Exception as exc:
        print(f'RUNTIME_CONTRACT_FAILED: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
