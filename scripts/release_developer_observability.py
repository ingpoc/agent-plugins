"""Refresh one verified Developer Observability PR head and probe its installed MCP."""
import json
from pathlib import Path
import subprocess
import sys

import release_agent_computer_use as shared


def release_observability(root: Path, pr: int) -> int:
    head = shared.output(['git', 'rev-parse', 'HEAD'], root=root)
    shared.frozen(root, head)
    change = json.loads(shared.output(['origin', 'pr', 'view', str(pr), '-R', 'gurusharan/agent-plugins',
                                       '--json', 'status,headSha,headRef'], root=root))
    branch = shared.output(['git', 'branch', '--show-current'], root=root)
    if change.get('status') not in ('open', 'draft') or change.get('headSha') != head or change.get('headRef') != branch:
        raise RuntimeError('release requires the exact open Origin PR head and branch')
    shared.hygiene_gate(False)
    package = root / 'plugins/developer-observability'
    identity = shared.contract_module(root).package_identity(package)
    destination = Path.home() / '.agents/plugins/developer-observability'
    if destination.is_symlink() or not destination.is_dir() or destination.resolve() == package.resolve():
        raise RuntimeError('personal package must be a separate existing directory')
    sync = ['rsync', '-a', '--delete', '--exclude=.DS_Store', '--exclude=__pycache__',
            '--exclude=run/', str(package) + '/', str(destination) + '/']
    preview = subprocess.check_output(sync[:1] + ['-ni'] + sync[1:], text=True)
    if any(line.startswith('*deleting') for line in preview.splitlines()):
        raise RuntimeError('personal package contains extra files; inspect before replacing')
    subprocess.run(sync, check=True)
    shared.frozen(root, head)
    result = json.loads(shared.output(['codex', 'plugin', 'add', 'developer-observability@personal', '--json'], root=root))
    installed = Path(result['installedPath'])
    if result.get('version') != identity['version'] or shared.contract_module(root).package_identity(installed) != identity:
        raise RuntimeError('installed package does not match candidate')
    subprocess.run([str(Path.home()/'.local/share/developer-observability/runtime/bin/python3'),
                    str(root/'scripts/probe_developer_observability.py'), str(installed)], check=True, cwd=root)
    shared.frozen(root, head)
    print(json.dumps({'commit': head, 'pr': pr, 'version': identity['version'], 'installed': str(installed), 'ok': True}))
    return 0
