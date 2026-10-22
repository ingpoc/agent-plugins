#!/usr/bin/env python3
"""Release ACU from one frozen commit; --check is the source-only CI gate."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import re
import signal
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = 'plugins/agent-computer-use'
APP = Path('/Applications/Agent Computer Use.app')


def output(command, *, root=ROOT):
    return subprocess.check_output(command, cwd=root, text=True).strip()


def policy(root=ROOT, *, base=None):
    """Shared by CI, pre-commit, and release; no GUI or installed-state access."""
    package = root / PACKAGE
    version = json.loads((package / 'plugin.json').read_text())['version']
    cursor = json.loads((package / '.cursor-plugin/plugin.json').read_text())['version']
    app = plistlib.loads((package / 'skills/macos-cua/service/Resources/Info.plist').read_bytes())
    if cursor != version or app['CFBundleShortVersionString'] != version:
        raise RuntimeError('release metadata versions disagree with plugin.json')
    baseline = base or os.environ.get('AGENT_PLUGINS_RELEASE_BASE') or (
        'refs/remotes/origin/main' if os.environ.get('CI') else 'HEAD')
    base = output(['git','rev-parse','--verify',baseline+'^{commit}'],root=root)
    changed = output(['git','diff','--name-only',base,'--',PACKAGE],root=root).splitlines()
    shipped = [p for p in changed if not {'tests','Tests'} & set(Path(p).parts)]
    if shipped:
        previous = subprocess.run(['git','show',f'{base}:{PACKAGE}/plugin.json'],
                                  cwd=root,text=True,capture_output=True)
        if previous.returncode == 0:
            old = json.loads(previous.stdout)['version']
            def semver(value):
                if not re.fullmatch(r'\d+\.\d+\.\d+',value):
                    raise RuntimeError('release versions must use stable major.minor.patch')
                return tuple(map(int,value.split('.')))
            if semver(version) <= semver(old):
                raise RuntimeError('shipped package changes require a version increase')
    return {'version': version, 'base':base}


def frozen(root, head):
    if output(['git', 'rev-parse', 'HEAD'], root=root) != head:
        raise RuntimeError('HEAD changed during release')
    if output(['git', 'status', '--porcelain', '--untracked-files=all'], root=root):
        raise RuntimeError('release requires a clean committed worktree')


def contract_module(root):
    path = root / PACKAGE / 'skills/macos-cua/scripts/runtime_contract.py'
    spec = importlib.util.spec_from_file_location('acu_release_contract', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def mcp(plugin, *, compute=False):
    messages = [
        {'jsonrpc':'2.0', 'id':1, 'method':'initialize', 'params':{
            'protocolVersion':'2025-11-25', 'capabilities':{},
            'clientInfo':{'name':'release-gate','version':'1'}}},
        {'jsonrpc':'2.0', 'method':'notifications/initialized'},
        {'jsonrpc':'2.0', 'id':2, 'method':'tools/list', 'params':{}},
    ]
    if compute:
        for identifier, digit in [(3,'7'), (4,'9')]:
            messages.append({'jsonrpc':'2.0', 'id':identifier, 'method':'tools/call', 'params':{
                'name':'act', 'arguments':{'app':'Calculator', 'steps':[
                    {'op':'click','label':label} for label in ['All Clear',digit,'Multiply','8','Equals']],
                    'expect':{'value':{'label':'Edit field','equals':str(int(digit)*8)}}}}})
    messages.append({'jsonrpc':'2.0','id':5,'method':'tools/call','params':{
        'name':'state','arguments':{'app':'Calculator','max':35}}})
    result = subprocess.run([str(plugin/'bin/agent-computer-use-mcp')],
        input=''.join(json.dumps(row)+'\n' for row in messages), text=True,
        capture_output=True, timeout=60)
    if result.returncode:
        raise RuntimeError('installed MCP startup failed: ' + result.stderr.strip())
    rows = {r['id']:r for line in result.stdout.splitlines()
            if (r := json.loads(line)).get('id') is not None}
    version = rows[1]['result']['serverInfo']['version']
    if {t['name'] for t in rows[2]['result']['tools']} != {'state','act'}:
        raise RuntimeError('installed MCP tool contract differs')
    def data(identifier):
        result = rows[identifier]['result']
        value = result.get('structuredContent') or json.loads(result['content'][0]['text'])
        if not value.get('ok') or result.get('isError'):
            raise RuntimeError('installed MCP returned an unsuccessful result')
        return value
    if compute and not all(data(i).get('verified') is True for i in (3,4)):
        raise RuntimeError('Calculator actions were not verified')
    state = data(5)['text']
    if compute and 'value="72"' not in state:
        raise RuntimeError('independent Calculator readback is not 72')
    return version, state


def restart_app(app):
    candidates = subprocess.run(['pgrep','-x','CUAService'], text=True, capture_output=True).stdout.split()
    for candidate in candidates:
        command = output(['ps','-o','comm=','-p',candidate])
        if command != str(app/'Contents/MacOS/CUAService'):
            continue
        pid = int(candidate)
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(.1)
        else:
            raise RuntimeError('previous service failed to stop')
    subprocess.run(['open',str(app)], check=True)
    time.sleep(1)


def voice_events(rows):
    """Require an ordered same-session input/action/response chain; keep no transcript."""
    sessions = {}
    for row in rows:
        sid = row.get('session_id')
        if not sid:
            continue
        state = sessions.setdefault(sid, {'input':False,'action':False,'done':False,'failed':False})
        kind = row.get('kind')
        if kind in {'realtime_error','ws_closed','tool_error','reconnecting'}:
            state['failed'] = True
        if kind == 'user_transcript':
            state['input'] = True
        if kind == 'tool_result' and row.get('tool') == 'cua_act' and row.get('app') == 'Calculator' and row.get('ok') is True:
            state['action'] = state['input']
        if kind == 'startup' and row.get('phase') == 'response_done' and row.get('status') == 'ok':
            state['done'] = state['action']
    return next((sid for sid,s in sessions.items()
                 if s['input'] and s['action'] and s['done'] and not s['failed']), None)


def voice_smoke(plugin, version):
    activity = Path.home()/'.cache/macos-cua/samantha-activity.jsonl'
    offset = activity.stat().st_size if activity.exists() else 0
    print('VOICE SMOKE: turn Samantha ON and say: Open Calculator and calculate thirteen times seventeen.', flush=True)
    deadline = time.monotonic()+90
    while time.monotonic() < deadline:
        rows = []
        if activity.exists():
            if activity.stat().st_size < offset:
                raise RuntimeError('voice activity log rotated during proof')
            with activity.open('rb') as stream:
                stream.seek(offset)
                for line in stream:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        pass
        sid = voice_events(rows)
        if sid:
            _, state = mcp(plugin)
            if 'value="221"' not in state or 'value="13 × 17"' not in state:
                raise RuntimeError('voice tool completed but independent display is not 13 × 17 = 221')
            listeners = output(['lsof','-nP','-a','-c','voice-cua','-iTCP','-sTCP:LISTEN','-Fn'])
            matches = []
            for line in listeners.splitlines():
                if not line.startswith('n127.0.0.1:'):
                    continue
                port = int(line.rsplit(':',1)[1])
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/health',timeout=3) as response:
                    health = json.load(response)
                if health.get('service') == 'voice-cua' and health.get('version') == version:
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/session/status',timeout=3) as response:
                        status = json.load(response)
                    if all(status.get(k) is True for k in ('active','ready','audio')) and status.get('text_only') is False:
                        matches.append(port)
            if len(matches) != 1:
                raise RuntimeError('could not establish one matching live audio gateway')
            return {'session_id':sid,'port':matches[0],'calculator':'13 × 17 = 221','audio':True}
        time.sleep(.5)
    raise RuntimeError('fresh spoken input, successful action and completed response were not proven')


@contextmanager
def exclusive_release(path):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a') as stream:
        try:
            fcntl.flock(stream,fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('another ACU release is active') from exc
        try:
            yield
        finally:
            fcntl.flock(stream,fcntl.LOCK_UN)


def release(args, root=ROOT):
    report_path = args.report.expanduser().resolve()
    if report_path.is_relative_to(root.resolve()):
        raise RuntimeError('release evidence must be outside the source worktree')
    report_path.parent.mkdir(parents=True,exist_ok=True)
    report = {'ok':False,'release_ready':False,'steps':[], 'voice_left_on':'unknown'}
    def save():
        temporary = report_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(report,indent=2)+'\n')
        temporary.replace(report_path)
    try:
        head = output(['git','rev-parse','HEAD'],root=root)
        report['commit'] = head
        report['dirty_paths'] = output(['git','status','--porcelain','--untracked-files=all'],root=root).splitlines()
        frozen(root,head)
        report.update({'commit':head,'branch':output(['git','branch','--show-current'],root=root),
                       'worktrees':output(['git','worktree','list','--porcelain'],root=root), **policy(root,base=os.environ.get('AGENT_PLUGINS_RELEASE_BASE') or 'refs/remotes/origin/main')})
        module = contract_module(root)
        identity = module.package_identity(root/PACKAGE)
        report['identity'] = identity
        def run(name, command, timeout=1800):
            frozen(root,head)
            log = report_path.parent/(report_path.stem+'-'+name+'.log')
            step = {'name':name,'ok':False,'log':str(log)}
            report['steps'].append(step);save()
            started = time.monotonic()
            with log.open('w') as stream:
                result = subprocess.run(command,cwd=root,stdout=stream,stderr=subprocess.STDOUT,timeout=timeout)
            step.update({'exit_code':result.returncode,'seconds':round(time.monotonic()-started,3)})
            if result.returncode:
                raise RuntimeError(f'{name} failed; see {log}')
            frozen(root,head)
            step['ok'] = True;save()
        run('source-checks',[sys.executable,'scripts/check.py'])
        if not args.verify_installed:
            command = [sys.executable,str(root/PACKAGE/'skills/macos-cua/service/install_service.py'),'--release']
            if args.sign_only:
                command.append('--sign-only')
            run('build-install',command)
            destination = Path.home()/'.agents/plugins/agent-computer-use'
            if destination.resolve() == (root/PACKAGE).resolve() or destination.is_symlink():
                raise RuntimeError('installed source must be a separate ordinary directory')
            destination.mkdir(parents=True,exist_ok=True)
            run('sync-installed',['rsync','-a','--delete','--exclude=.DS_Store','--exclude=.build',
                '--exclude=__pycache__','--exclude=run/',str(root/PACKAGE)+'/',str(destination)+'/'])
            restart_app(APP)
        refreshed = json.loads(output(['codex','plugin','add','agent-computer-use@personal','--json'],root=root))
        plugin = Path(refreshed['installedPath'])
        if module.package_identity(plugin) != identity:
            raise RuntimeError('installed Codex package does not match source')
        report['installed_plugin'] = str(plugin)
        run('signature',['codesign','--verify','--deep','--strict',str(APP)],60)
        if not args.sign_only:
            run('notarization',['xcrun','stapler','validate',str(APP)],60)
        report['notarization'] = 'not_verified_signed_only' if args.sign_only else 'staple_verified'
        module.check(root/PACKAGE, APP)
        for name, relative in [('client','service/Tests/CUAServiceTests/integration_cua_client.py'),
                ('cursor','tests/integration_macos_cua_cursor.py'),('fd','tests/integration_service_fd_hygiene.py')]:
            run('live-'+name,[sys.executable,'scripts/run_strict.py','--allowlist','scripts/check_test_allowlist.json',
                '-q','module',str(root/PACKAGE/'skills/macos-cua'/relative)],120)
        version, _ = mcp(plugin,compute=True)
        if version != identity['version']:
            raise RuntimeError('fresh MCP process version does not match candidate')
        report['native_smoke'] = {'ok':True,'fresh_mcp':True,'independent_readback':True}
        run('microphone',[str(APP/'Contents/Helpers/voice-cua.app/Contents/MacOS/voice-cua'),'--preflight-mic'],30)
        report['voice'] = voice_smoke(plugin,version)
        report['voice_left_on'] = True
        frozen(root,head)
        if module.package_identity(root/PACKAGE) != identity:
            raise RuntimeError('package changed during release')
        report.update({'ok':True,'release_ready':not args.sign_only,'local_ready':True})
        return 0
    except Exception as exc:
        report['error'] = str(exc)
        print(str(exc),file=sys.stderr)
        return 1
    finally:
        save()
        print(f'Evidence: {report_path}',flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check',action='store_true',help='Source policy only; safe in CI.')
    parser.add_argument('--verify-installed',action='store_true',help='Reuse the existing app; refresh and verify the Codex package without rebuilding or restarting.')
    parser.add_argument('--sign-only',action='store_true',help='Local signed validation; never produces distribution-ready evidence.')
    parser.add_argument('--report',type=Path,default=Path.home()/'.agents/artifacts/acu-release.json')
    args = parser.parse_args(argv)
    if args.check:
        try:
            print(json.dumps(policy()));return 0
        except Exception as exc:
            print(str(exc),file=sys.stderr);return 1
    try:
        with exclusive_release(Path.home()/'.cache/macos-cua/release.lock'):
            return release(args)
    except (RuntimeError, OSError) as exc:
        print(str(exc),file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
