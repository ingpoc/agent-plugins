"""Refresh an exact open batched-change-list PR and probe its installed CLI and viewer."""
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen

import release_agent_computer_use as shared

PACKAGE = 'plugins/batched-change-list'


def bump_policy(root, *, base=None):
    """Version and changelog belong to integration: shipped package changes since base need a version increase."""
    version = json.loads((Path(root) / PACKAGE / 'plugin.json').read_text())['version']
    # The integrated head is judged against main, so a multi-commit version is not re-bumped per commit.
    explicit = base or os.environ.get('AGENT_PLUGINS_RELEASE_BASE')
    base = shared.output(['git', 'rev-parse', '--verify', (explicit or 'refs/remotes/origin/main') + '^{commit}'], root=root)
    if not explicit and base == shared.output(['git', 'rev-parse', 'HEAD'], root=root):
        # On main itself (a push to main), judge the newest commit against its first parent.
        parent = subprocess.run(['git', 'rev-parse', '--verify', 'HEAD^1^{commit}'], cwd=root, text=True, capture_output=True)
        base = parent.stdout.strip() if parent.returncode == 0 else base
    changed = shared.output(['git', 'diff', '--name-only', base, '--', PACKAGE], root=root).splitlines()
    changed += shared.output(['git', 'ls-files', '--others', '--exclude-standard', '--', PACKAGE], root=root).splitlines()
    shipped = [p for p in changed if not Path(p).name.startswith('test_') and not {'tests', 'Tests'} & set(Path(p).parts)]
    previous = subprocess.run(['git', 'show', f'{base}:{PACKAGE}/plugin.json'], cwd=root, text=True, capture_output=True)
    if shipped and previous.returncode == 0:
        semver = lambda value: tuple(map(int, value.split('.')))
        if semver(version) <= semver(json.loads(previous.stdout)['version']):
            raise RuntimeError('shipped batched-change-list changes require a version increase')
    return {'version': version, 'base': base}


def viewer_address(server, timeout=15):
    deadline = time.monotonic() + timeout
    output = b''
    while b'\n' not in output and len(output) < 4096:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([server.stdout], [], [], remaining)[0]:
            raise RuntimeError('installed viewer did not start within the time limit')
        chunk = os.read(server.stdout.fileno(), 4096 - len(output))
        if not chunk:
            ready = select.select([server.stderr], [], [], 0)[0]
            detail = os.read(server.stderr.fileno(), 4096).decode('utf-8', errors='replace').strip() if ready else ''
            raise RuntimeError(f'installed viewer exited before reporting an address: {detail}')
        output += chunk
    url = output.split(b'\n', 1)[0].decode('utf-8').strip()
    if not url.startswith('http://127.0.0.1:'):
        raise RuntimeError(f'installed viewer did not report a loopback address: {url}')
    return url


def probe(installed: Path, version: str) -> None:
    """Exercise the installed CLI and live renderer without touching user trackers."""
    scripts = installed / 'skills/batched-change-list/scripts'
    with tempfile.TemporaryDirectory(prefix='bcl-installed-probe-') as tmp:
        temp = Path(tmp)
        subprocess.run(['git', 'init', '-q', str(temp)], check=True, capture_output=True, timeout=15)
        data = temp / 'findings.json'
        data.write_text(json.dumps({'title': 'Installed probe', 'findings': [{
            'id': 'PROBE-01', 'title': 'Before update', 'source': 'Agent reported',
            'status': 'untested', 'observed': 'probe', 'recommendation': 'probe', 'evidence': []}]}))
        env = dict(os.environ, BCL_STATE_DIR=str(temp / 'state'))
        status = subprocess.run([str(installed / 'bin/bcl'), '--data', str(data), 'status'],
                                capture_output=True, text=True, env=env, timeout=15)
        if status.returncode:
            raise RuntimeError(f'installed CLI probe failed: {(status.stderr or status.stdout).strip()}')
        if json.loads(status.stdout).get('adopted') is not False:
            raise RuntimeError('installed CLI did not report the isolated tracker status')
        metadata = temp / 'viewer.json'
        metadata.write_text(json.dumps({'path': str(data), 'version': version, 'started': 'probe', 'instance': 'probe'}))
        inventory = temp / 'inventory.json'
        inventory.write_text(json.dumps({'versions': {}, 'sources': {}}))
        (temp / 'report.html').write_text('probe')
        command = [sys.executable, str(scripts / 'serve.py'), str(temp / 'report.html'), '--data', str(data),
                   '--inventory', str(inventory), '--viewer-metadata', str(metadata), '--port', '0']
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env) as server:
            try:
                url = viewer_address(server)
                with urlopen(url, timeout=10) as response:
                    page = response.read().decode('utf-8')
                    first_etag = response.headers.get('ETag')
                if 'Before update' not in page or f'viewer {version}' not in page or not first_etag:
                    raise RuntimeError('installed viewer did not render probe data and version')
                doc = json.loads(data.read_text())
                doc['findings'][0]['title'] = 'After update'
                data.write_text(json.dumps(doc))
                with urlopen(url, timeout=10) as response:
                    page = response.read().decode('utf-8')
                    second_etag = response.headers.get('ETag')
                if 'After update' not in page or not second_etag or first_etag == second_etag:
                    raise RuntimeError('installed viewer did not reflect changed tracker data')
            finally:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()


def release_batched_change_list(root: Path, pr: int) -> int:
    head = shared.output(['git', 'rev-parse', 'HEAD'], root=root)
    shared.frozen(root, head)
    branch = shared.output(['git', 'branch', '--show-current'], root=root)
    def check_pr():
        shared.frozen(root, head)
        change = json.loads(shared.output(['origin', 'pr', 'view', str(pr), '-R', 'gurusharan/agent-plugins',
                                           '--json', 'status,headSha,headRef'], root=root))
        current_branch = shared.output(['git', 'branch', '--show-current'], root=root)
        if (change.get('status') not in ('open', 'draft') or change.get('headSha') != head
                or change.get('headRef') != branch or current_branch != branch):
            raise RuntimeError('release requires the exact open Origin PR head and branch')
    check_pr()
    hygiene = shared.hygiene_gate(False)
    if not hygiene.get('checked') or hygiene.get('failures'):
        raise RuntimeError('release requires a completed, passing hygiene check')
    package = root / 'plugins/batched-change-list'
    identity = shared.contract_module(root).package_identity(package)
    selector = 'batched-change-list@ingpoc-agent-plugins'
    override = ['-c', 'marketplaces.ingpoc-agent-plugins.source_type="local"',
                '-c', 'marketplaces.ingpoc-agent-plugins.source=' + json.dumps(str(root.resolve()))]
    available = json.loads(shared.output(
        ['codex', 'plugin', 'list', *override, '--marketplace', 'ingpoc-agent-plugins', '--available', '--json'], root=root))
    matches = [plugin for group in ('installed', 'available') if isinstance(available, dict) and isinstance(available.get(group, []), list)
               for plugin in available[group] if isinstance(plugin, dict) and plugin.get('pluginId') == selector]
    if len(matches) != 1 or matches[0].get('source', {}).get('source') != 'local' or Path(matches[0].get('source', {}).get('path', '')).resolve() != package.resolve():
        raise RuntimeError('Codex marketplace does not resolve the existing selector to the candidate package; no install attempted')
    check_pr()
    result = json.loads(shared.output(['codex', 'plugin', 'add', *override, selector, '--json'], root=root))
    installed = Path(result['installedPath'])
    if result.get('version') != identity['version'] or shared.contract_module(root).package_identity(installed) != identity:
        raise RuntimeError('installed package does not match candidate')
    probe(installed, identity['version'])
    check_pr()
    print(json.dumps({'commit': head, 'pr': pr, 'version': identity['version'], 'installed': str(installed),
                      'ok': True, 'probe': 'isolated CLI and HTTP viewer; managed viewer and client discovery not checked'}))
    return 0
