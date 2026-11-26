import json
import hashlib
import base64
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import Request, urlopen

import live_state
import render
import viewer_test_guard


def tearDownModule():
    viewer_test_guard.assert_no_viewers(viewer_test_guard.TEMP_ROOTS)


class ViewerLivenessTests(unittest.TestCase):
    def _viewer_fixture(self, root):
        state, live, home = root / 'state', root / 'live.json', root / 'home'
        scripts = root / 'install' / 'scripts'
        shutil.copytree(Path(__file__).resolve().parent, scripts)
        shutil.copytree(Path(__file__).resolve().parents[1] / 'templates', scripts.parent / 'templates')
        live.write_text((Path(__file__).resolve().parents[1] / 'examples/findings.json').read_text())
        (home / 'report').mkdir(parents=True)
        (home / 'report/page.html').write_text('<html>viewer test</html>')
        record = {'home': str(home), 'snapshot': 'report/data.json', 'html': 'report/page.html'}
        pids = set()
        def cleanup():
            for path in (state / 'viewers').glob('*.json'):
                try:
                    info = json.loads(path.read_text())
                    if not info.get('instance') or Path(info['data']).resolve() != live.resolve(): continue
                    request = Request(f"http://127.0.0.1:{info['port']}/__bcl_shutdown", method='POST', headers={'X-BCL-Instance': info['instance']})
                    with urlopen(request, timeout=2): pass
                    live_state._wait_for_port(info['port'])
                except (OSError, ValueError, KeyError): pass
            viewer_test_guard.stop_viewers(root, pids)
        self.addCleanup(cleanup)
        return state, live, record, scripts, pids

    def test_same_version_reinstall_restarts_viewer(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        state, live, record, scripts, pids = self._viewer_fixture(Path(temp.name))
        with patch.object(live_state, 'STATE', state), patch.object(live_state, 'SCRIPTS', scripts):
            first = live_state.ensure_viewer('repo/1', record, live, 'same-version'); pids.add(first['pid'])
            with (scripts / 'render.py').open('a') as installed_script:
                installed_script.write('\n# reinstall changed this script\n')
            second = live_state.ensure_viewer('repo/1', record, live, 'same-version'); pids.add(second['pid'])
        self.assertNotEqual(first['pid'], second['pid'])
        self.assertEqual(first['port'], second['port'])
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try: os.waitpid(first['pid'], os.WNOHANG)
            except ChildProcessError: pass
            if not live_state._pid_is_running(first['pid']): break
            time.sleep(.05)
        self.assertFalse(live_state._pid_is_running(first['pid']))

    def test_unchanged_script_tree_reuses_viewer(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        state, live, record, scripts, pids = self._viewer_fixture(Path(temp.name))
        with patch.object(live_state, 'STATE', state), patch.object(live_state, 'SCRIPTS', scripts):
            first = live_state.ensure_viewer('repo/1', record, live, 'same-version'); pids.add(first['pid'])
            second = live_state.ensure_viewer('repo/1', record, live, 'same-version')
        self.assertEqual(first['pid'], second['pid'])

    def test_real_viewer_restarts_and_serves_new_worktree(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        root = Path(temp.name); state = root / 'state'; live = root / 'live.json'
        doc = json.loads((Path(__file__).resolve().parents[1] / 'examples/findings.json').read_text())
        doc['findings'][0]['evidence'] = [{'src': 'capture.png', 'alt': 'capture', 'caption': 'capture'}]
        live.write_text(json.dumps(doc))
        records = []
        for name in ('first', 'second'):
            home = root / name; (home / 'report').mkdir(parents=True)
            (home / 'report/capture.png').write_bytes(name.encode())
            records.append({'home': str(home), 'snapshot': 'report/data.json', 'html': 'report/page.html'})
        def cleanup():
            for path in (state / 'viewers').glob('*.json'):
                try:
                    info = json.loads(path.read_text())
                    if not info.get('instance') or Path(info['data']).resolve() != live.resolve(): continue
                    request = Request(f"http://127.0.0.1:{info['port']}/__bcl_shutdown", method='POST', headers={'X-BCL-Instance': info['instance']})
                    with urlopen(request, timeout=2): pass
                    self.assertTrue(live_state._wait_for_port(info['port']))
                except (OSError, ValueError, KeyError): pass
            viewer_test_guard.stop_viewers(root, pids)
        pids = set()
        self.addCleanup(cleanup)
        with patch.object(live_state, 'STATE', state):
            first = live_state.ensure_viewer('repo/1', records[0], live, 'v1')
            pids.add(first['pid'])
            with urlopen(f"http://127.0.0.1:{first['port']}/") as response:
                self.assertIn(base64.b64encode(b'first'), response.read())
            second = live_state.ensure_viewer('repo/1', records[1], live, 'v1')
            pids.add(second['pid'])
            with urlopen(f"http://127.0.0.1:{second['port']}/") as response:
                self.assertIn(base64.b64encode(b'second'), response.read())
            self.assertNotEqual(first['pid'], second['pid'])

    def test_viewer_restarts_when_worktree_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); state = root / 'state'
            live = root / 'live.json'; live.write_text((Path(__file__).resolve().parents[1] / 'examples/findings.json').read_text())
            first_home, second_home = root / 'first', root / 'second'
            first_home.mkdir(); second_home.mkdir()
            first = {'home': str(first_home), 'snapshot': 'report/data.json', 'html': 'report/page.html'}
            second = {'home': str(second_home), 'snapshot': 'report/data.json', 'html': 'report/page.html'}
            class Response:
                status = 204
                def __enter__(self): return self
                def __exit__(self, *args): return False
            pids = iter((12301, 12302))
            with patch.object(live_state, 'STATE', state), \
                 patch.object(live_state, '_pid_is_running', return_value=True), \
                 patch.object(live_state, '_viewer_responds', return_value=True), \
                 patch.object(live_state, '_viewer_ready', return_value=True), \
                 patch.object(live_state, '_port_is_free', return_value=True), \
                 patch.object(live_state, '_wait_for_port', return_value=True), \
                 patch.object(live_state.subprocess, 'Popen', side_effect=lambda command, **kwargs: SimpleNamespace(pid=next(pids))) as popen, \
                 patch.object(live_state.urllib.request, 'urlopen', return_value=Response()):
                live_state.ensure_viewer('repo/1', first, live, 'v1')
                value = live_state.ensure_viewer('repo/1', second, live, 'v1')
            self.assertEqual(value['report'], str((second_home / 'report/page.html').resolve()))
            with patch.object(live_state, 'STATE', state), patch.object(live_state, '_port_is_free', return_value=True):
                self.assertEqual(value['port'], live_state._project_port('repo/1', {}))
            self.assertEqual(popen.call_count, 2)

    def test_viewer_record_without_instance_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / 'viewers' / 'repo--1.json'
            state.parent.mkdir()
            live = root / 'correct-list.json'
            live.write_text('{}')
            page = root / 'report.html'
            page.write_text('<html></html>')
            old = {'pid': 123, 'data': str(live), 'port': 5000, 'version': 'old'}
            state.write_text(json.dumps(old))
            proc = unittest.mock.Mock(pid=456, poll=lambda: None)
            with patch.object(live_state, 'STATE', root), \
                 patch.object(live_state, '_pid_is_running', return_value=True), \
                 patch.object(live_state.subprocess, 'Popen') as popen:
                with self.assertRaisesRegex(RuntimeError, 'cannot verify viewer ownership'):
                    live_state.ensure_viewer('repo/1', {}, live, 'new')
            popen.assert_not_called()

    def test_rollover_viewers_share_project_state_and_fixed_port(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); state = root / 'state'; live = root / 'live.json'
            live.write_text('{}')
            homes = [root / 'first', root / 'second']
            for home in homes:
                home.mkdir()
                (home / 'report').mkdir()
                (home / 'report/page.html').write_text('<html></html>')
            records = [dict(home=str(home), snapshot='report/data.json', html='report/page.html',
                            repo='owner/project', viewer_port=54123) for home in homes]
            responses = iter((204, 204))
            class Response:
                def __init__(self): self.status = next(responses)
                def __enter__(self): return self
                def __exit__(self, *args): return False
            pids = iter((45601, 45602))
            with patch.object(live_state, 'STATE', state), \
                 patch.object(live_state, '_pid_is_running', return_value=True), \
                 patch.object(live_state, '_viewer_responds', return_value=True), \
                 patch.object(live_state, '_viewer_ready', return_value=True), \
                 patch.object(live_state, '_port_is_free', return_value=True), \
                 patch.object(live_state, '_wait_for_port', return_value=True), \
                 patch.object(live_state.subprocess, 'Popen', side_effect=lambda *a, **k: SimpleNamespace(pid=next(pids))) as popen, \
                 patch.object(live_state.urllib.request, 'urlopen', side_effect=lambda *a, **k: Response()):
                first = live_state.ensure_viewer('owner/project/1', records[0], live, 'v1')
                second = live_state.ensure_viewer('owner/project/2', records[1], live, 'v1')
            self.assertEqual(first['port'], 54123)
            self.assertEqual(second['port'], 54123)
            self.assertEqual(popen.call_count, 2)
            self.assertTrue((state / 'viewers/owner--project.json').exists())

    def test_project_port_is_reused_and_distinct_across_projects(self):
        records = {
            'owner/project/1': {'repo': 'owner/project', 'viewer_port': 54123},
            'other/project/1': {'repo': 'other/project', 'viewer_port': 54124},
        }
        with patch.object(live_state, '_port_is_free', return_value=True):
            self.assertEqual(live_state._project_port('owner/project', records), 54123)
            fresh = live_state._project_port('new/project', records)
            self.assertEqual(fresh, live_state._project_port('new/project', records))
            self.assertTrue(live_state.PORT_RANGE[0] <= fresh < live_state.PORT_RANGE[1])
            self.assertNotIn(fresh, {54123, 54124})

    def test_adoption_refuses_existing_destination_and_report_alias(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root / 'tracker.json'; source.write_text('{}')
            destination = root / 'state' / 'repo' / '1.json'
            destination.parent.mkdir(parents=True); destination.write_text('{}')
            with patch.object(live_state, 'STATE', root / 'state'), \
                 patch.object(live_state, 'repo_id', return_value='repo'), \
                 patch.object(live_state, 'list_id', return_value='1'):
                with self.assertRaisesRegex(ValueError, 'destination already exists'):
                    live_state.locate(source, adopt=True)
            destination.unlink()
            with patch.object(live_state, 'STATE', root / 'state'), \
                 patch.object(live_state, 'repo_id', return_value='repo'), \
                 patch.object(live_state, 'list_id', return_value='1'), \
                 patch.object(live_state, 'git', return_value=str(root)):
                with self.assertRaisesRegex(ValueError, 'aliases the tracker'):
                    live_state.locate(source, adopt=True, render_path=source)

    def test_unidentified_viewer_is_left_running_without_replacement(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / 'viewers' / 'repo--1.json'
            state.parent.mkdir()
            live = root / 'correct-list.json'
            live.write_text('{}')
            old = {'pid': 123, 'data': str(live), 'port': 5000, 'version': 'old'}
            state.write_text(json.dumps(old))
            with patch.object(live_state, 'STATE', root), \
                 patch.object(live_state, '_pid_is_running', return_value=True), \
                 patch.object(live_state.subprocess, 'Popen') as popen, \
                 patch.object(live_state.urllib.request, 'urlopen') as urlopen:
                with self.assertRaisesRegex(RuntimeError, 'cannot verify viewer ownership'):
                    live_state.ensure_viewer('repo/1', {}, live, 'new')
            popen.assert_not_called()
            urlopen.assert_not_called()

    def test_unresponsive_instance_viewer_is_left_running_without_replacement(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / 'viewers' / 'repo--1.json'
            state.parent.mkdir()
            live = root / 'correct-list.json'
            live.write_text('{}')
            old = {'pid': 123, 'data': str(live), 'port': 5000, 'version': 'old', 'instance': 'old-instance'}
            state.write_text(json.dumps(old))
            previous = state.read_bytes()
            with patch.object(live_state, 'STATE', root), \
                 patch.object(live_state, '_pid_is_running', return_value=True), \
                 patch.object(live_state, '_viewer_responds', return_value=False) as verify, \
                 patch.object(live_state.subprocess, 'Popen') as popen, \
                 patch.object(live_state.urllib.request, 'urlopen') as urlopen:
                with self.assertRaisesRegex(RuntimeError, 'cannot verify viewer ownership'):
                    live_state.ensure_viewer('repo/1', {}, live, 'new')
            verify.assert_called_once_with(5000, old)
            popen.assert_not_called()
            urlopen.assert_not_called()
            self.assertEqual(state.read_bytes(), previous)

    def test_unix_probe_reports_missing_and_permission_denied(self):
        with patch.object(live_state.os, 'name', 'posix'), patch.object(live_state.os, 'kill', side_effect=ProcessLookupError):
            self.assertFalse(live_state._pid_is_running(123))
        with patch.object(live_state.os, 'name', 'posix'), patch.object(live_state.os, 'kill', side_effect=PermissionError):
            self.assertTrue(live_state._pid_is_running(123))
            with self.assertRaises(live_state.ViewerPIDProbeError):
                live_state._pid_is_running(123, reject_unverifiable_pid=True)

    @unittest.skipIf(sys.platform == 'win32', 'Unix os.kill probe')
    def test_unix_probe_uses_os_kill_zero(self):
        with patch.object(live_state.os, 'name', 'posix'), patch.object(live_state.os, 'kill') as kill:
            self.assertTrue(live_state._pid_is_running(123))
        kill.assert_called_once_with(123, 0)

    def test_windows_probe_queries_and_closes_without_terminating(self):
        import ctypes
        from ctypes import wintypes
        kernel = type('Kernel', (), {})()
        kernel.OpenProcess = unittest.mock.Mock(return_value=42)
        def get_exit_code(handle, output):
            ctypes.cast(output, ctypes.POINTER(wintypes.DWORD))[0] = 259
            return 1
        kernel.GetExitCodeProcess = unittest.mock.Mock(side_effect=get_exit_code)
        kernel.CloseHandle = unittest.mock.Mock(return_value=1)
        with patch.object(ctypes, 'WinDLL', return_value=kernel, create=True):
            self.assertTrue(live_state._windows_pid_status(123))
        kernel.OpenProcess.assert_called_once_with(0x1000, False, 123)
        kernel.CloseHandle.assert_called_once_with(42)
        self.assertFalse(hasattr(kernel, 'TerminateProcess'))

        def get_exit_code(handle, output):
            ctypes.cast(output, ctypes.POINTER(wintypes.DWORD))[0] = 0
            return 1
        kernel.GetExitCodeProcess.side_effect = get_exit_code
        with patch.object(ctypes, 'WinDLL', return_value=kernel, create=True):
            self.assertFalse(live_state._windows_pid_status(123))
        kernel.CloseHandle.assert_called_with(42)

        kernel.OpenProcess.return_value = 0
        with patch.object(ctypes, 'WinDLL', return_value=kernel, create=True), \
             patch.object(ctypes, 'get_last_error', return_value=87, create=True):
            self.assertFalse(live_state._windows_pid_status(123))

    def test_subprocess_liveness_before_and_after_exit(self):
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(0.3)'])
        try:
            self.assertTrue(live_state._pid_is_running(process.pid))
            self.assertEqual(process.wait(timeout=5), 0)
            self.assertFalse(live_state._pid_is_running(process.pid))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / 'repo'
        self.home.mkdir()
        for command in (('init', '-q'), ('config', 'user.email', 'test@example.com'),
                        ('config', 'user.name', 'Test'), ('commit', '--allow-empty', '-qm', 'initial')):
            subprocess.run(['git', '-C', str(self.home), *command], check=True)
        self.live = self.root / 'live.json'
        self.live.write_text(json.dumps({'title': 'first'}))
        self.record = {'home': str(self.home), 'snapshot': 'report/data.json', 'html': 'report/page.html'}

    def tearDown(self):
        self.temp.cleanup()

    def test_changed_tracker_keeps_one_revision_and_reports_stale_snapshot(self):
        def render_then_change(document, base):
            self.live.write_text(json.dumps({'title': 'second'}))
            return document['title']
        with patch.object(render, 'render', side_effect=render_then_change):
            with self.assertRaisesRegex(RuntimeError, 'changed during snapshot'):
                live_state.snapshot(self.record, self.live)
        self.assertEqual(json.loads((self.home / 'report/data.json').read_text())['title'], 'first')
        self.assertEqual((self.home / 'report/page.html').read_text(), 'first')
        self.assertTrue(self.live.with_suffix('.unsnapshotted').exists())

    def test_snapshot_failure_leaves_recovery_marker(self):
        real_run = subprocess.run
        def fail_add(command, *args, **kwargs):
            if 'add' in command:
                raise subprocess.CalledProcessError(1, command)
            return real_run(command, *args, **kwargs)
        with patch.object(render, 'render', return_value='first'), patch.object(
                live_state.subprocess, 'run', side_effect=fail_add):
            with self.assertRaises(subprocess.CalledProcessError):
                live_state.snapshot(self.record, self.live)
        self.assertTrue(self.live.with_suffix('.unsnapshotted').exists())

    def test_snapshot_commits_once_per_stage_change(self):
        self.record['snapshot'] = 'report/data.json'
        self.record['html'] = 'report/page.html'
        def write(stage, value):
            self.live.write_text(json.dumps({'title': str(value), 'tracking': {'workflow': {'stage': stage}, 'cycle': 1}}))
            with patch.object(render, 'render', return_value=str(value)):
                live_state.snapshot(self.record, self.live)
        write('collect', 1)
        write('collect', 2)
        (self.home / 'unrelated').write_text('other')
        subprocess.run(['git', '-C', str(self.home), 'add', 'unrelated'], check=True)
        subprocess.run(['git', '-C', str(self.home), 'commit', '-qm', 'unrelated'], check=True)
        self.assertEqual(subprocess.check_output(['git', '-C', str(self.home), 'show', '--format=', '--name-only', 'HEAD'], text=True).strip(), 'unrelated')
        write('collect', 3)
        self.assertEqual(subprocess.check_output(['git', '-C', str(self.home), 'rev-list', '--count', 'HEAD'], text=True).strip(), '3')
        write('plan_fix', 4)
        self.assertEqual(subprocess.check_output(['git', '-C', str(self.home), 'rev-list', '--count', 'HEAD'], text=True).strip(), '4')

    def test_snapshot_fails_closed_when_locking_is_unavailable(self):
        with patch.object(live_state.tracker_lock, 'fcntl', None), patch.object(live_state.tracker_lock, 'msvcrt', None):
            with self.assertRaisesRegex(RuntimeError, 'tracker locking unavailable'):
                live_state.snapshot(self.record, self.live)

    def test_snapshots_in_same_worktree_are_serialized(self):
        entered = threading.Event()
        release = threading.Event()
        second_entered = threading.Event()
        errors = []

        def render_wait(document, base):
            if document['title'] == 'first':
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('test timed out waiting for release')
            else:
                second_entered.set()
            return document['title']

        def run():
            try:
                live_state.snapshot(self.record, self.live)
            except Exception as error:
                errors.append(str(error))

        with patch.object(render, 'render', side_effect=render_wait):
            first = threading.Thread(target=run)
            first.start()
            self.assertTrue(entered.wait(5))
            self.live.write_text(json.dumps({'title': 'second'}))
            second = threading.Thread(target=run)
            second.start()
            try:
                self.assertFalse(second_entered.wait(.3))
            finally:
                release.set()
                first.join(10)
                second.join(10)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertTrue(second_entered.is_set())
        self.assertEqual(len(errors), 1)
        self.assertIn('changed during snapshot', errors[0])
        self.assertEqual(json.loads((self.home / 'report/data.json').read_text())['title'], 'second')
        self.assertEqual((self.home / 'report/page.html').read_text(), 'second')
        self.assertTrue(self.live.with_suffix('.unsnapshotted').exists())
        committed = subprocess.check_output(['git', '-C', str(self.home), 'show', 'HEAD:report/data.json'], text=True)
        self.assertIn('first', committed)
        with patch.object(render, 'render', side_effect=lambda document, base: document['title']):
            live_state.snapshot(self.record, self.live, pending_only=True)
        committed = subprocess.check_output(['git', '-C', str(self.home), 'show', 'HEAD:report/data.json'], text=True)
        self.assertIn('second', committed)
        self.assertFalse(self.live.with_suffix('.unsnapshotted').exists())

    def test_final_comparison_and_marker_removal_hold_writer_lock(self):
        removing = threading.Event()
        release = threading.Event()
        writer_acquired = threading.Event()
        errors = []
        original_unlink = Path.unlink
        marker = self.live.with_suffix('.unsnapshotted')

        def delayed_unlink(path, *args, **kwargs):
            if path == marker:
                removing.set()
                if not release.wait(5):
                    raise RuntimeError('test timed out waiting for release')
            return original_unlink(path, *args, **kwargs)

        def snapshot():
            try:
                live_state.snapshot(self.record, self.live)
            except Exception as error:
                errors.append(str(error))

        def writer():
            with live_state.tracker_lock.exclusive_lock(live_state.tracker_lock.tracker_lock_path(self.live)):
                writer_acquired.set()

        with patch.object(render, 'render', return_value='first'), patch.object(Path, 'unlink', delayed_unlink):
            first = threading.Thread(target=snapshot)
            first.start()
            self.assertTrue(removing.wait(5))
            second = threading.Thread(target=writer)
            second.start()
            try:
                self.assertFalse(writer_acquired.wait(.3))
            finally:
                release.set()
                first.join(10)
                second.join(10)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertTrue(writer_acquired.is_set())
        self.assertEqual(errors, [])




class LiveStateGuardTests(unittest.TestCase):
    def test_save_registry_refuses_the_real_state_dir_from_a_test(self):
        live_root = live_state.default_live_root()
        registry = live_root / "registry.json"
        before = registry.read_bytes() if registry.exists() else None
        with patch.object(live_state, "STATE", live_root), patch.object(live_state, "_LIVE_WRITES_ALLOWED", True):
            with self.assertRaisesRegex(RuntimeError, "live BCL state from a test"):
                live_state.save_registry({"example/project/123": {"live": "/tmp/fixture.json"}})
        after = registry.read_bytes() if registry.exists() else None
        self.assertEqual(after, before)

    def test_save_registry_allows_a_temporary_state_dir(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        state = Path(temp.name) / "state"
        with patch.object(live_state, "STATE", state):
            live_state.save_registry({"example/project/123": {"live": str(state / "123.json")}})
        saved = json.loads((state / "registry.json").read_text())
        self.assertEqual(saved["example/project/123"]["live"], str(state / "123.json"))

    def test_job_child_cannot_rely_on_an_inherited_allow_flag(self):
        # The process-local flag is what bcl sets; job children must still be refused
        # if they import live_state and point STATE at the real tree.
        self.assertFalse(live_state._LIVE_WRITES_ALLOWED)
        self.assertIn("unittest", __import__("sys").modules)
        self.assertTrue(live_state.is_live_state_path(live_state.default_live_root() / "registry.json"))


class RegistryConcurrencyTests(unittest.TestCase):
    """BCL-186: concurrent registry updates must never lose a change."""

    def _add_key_process(self, state, key, sleep=0.3):
        helper = "\n".join((
            "import sys, time",
            "sys.path.insert(0, sys.argv[1])",
            "import live_state",
            "def add(records):",
            "    time.sleep(float(sys.argv[3]))",
            "    records[sys.argv[2]] = {'live': sys.argv[2]}",
            "live_state.update_registry(add)",
        ))
        env = {**os.environ, "BCL_STATE_DIR": str(state)}
        return subprocess.Popen([sys.executable, "-c", helper, str(Path(__file__).resolve().parent),
                                 key, str(sleep)], env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)

    def test_two_processes_keep_both_added_keys(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state"
            processes = [self._add_key_process(state, "owner/project/one"),
                         self._add_key_process(state, "owner/project/two")]
            for process in processes:
                out, err = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, err or out)
            saved = json.loads((state / "registry.json").read_text())
            self.assertEqual(set(saved), {"owner/project/one", "owner/project/two"})

    def test_rollover_clear_keeps_a_key_added_concurrently(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state"
            state.mkdir(parents=True)
            record = {"live": str(state / "a.json"), "rollover_pending": True}
            (state / "registry.json").write_text(json.dumps({"owner/project/A": record}))
            process = self._add_key_process(state, "owner/project/B")

            def clear(records):
                time.sleep(0.3)
                records["owner/project/A"].pop("rollover_pending", None)

            with patch.object(live_state, "STATE", state):
                live_state.update_registry(clear)
            out, err = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, err or out)
            saved = json.loads((state / "registry.json").read_text())
            self.assertIn("owner/project/B", saved)
            self.assertNotIn("rollover_pending", saved["owner/project/A"])

    def test_only_update_registry_calls_save_registry(self):
        import ast

        callers = set()
        for node in ast.walk(ast.parse(Path(live_state.__file__).read_text())):
            if isinstance(node, ast.FunctionDef):
                for inner in ast.walk(node):
                    if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
                            and inner.func.id == "save_registry"):
                        callers.add(node.name)
        self.assertEqual(callers, {"update_registry"})
        plugin = Path(__file__).resolve().parents[3]
        sources = (plugin / "bin/bcl", plugin / "bin/bcl-hook",
                   *sorted((Path(__file__).resolve().parent).glob("*.py")))
        for path in sources:
            if path.name == "live_state.py" or path.name.startswith("test_"):
                continue
            self.assertNotIn("save_registry", path.read_text(), f"{path} still writes the registry directly")


class RepoViewerPortTests(unittest.TestCase):
    """BCL-185: one bookmarkable viewer port per repo, chosen once and never moved."""

    def test_new_repo_slot_is_deterministic_and_skips_other_repos_saved_ports(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(live_state, 'STATE', Path(temp)), \
                patch.object(live_state, '_port_is_free', return_value=True):
            slot = live_state._project_port('new/project', {})
            self.assertEqual(slot, live_state._project_port('new/project', {}))
            taken = {'other/project/1': {'repo': 'other/project', 'viewer_port': slot}}
            moved = live_state._project_port('new/project', taken)
            self.assertNotEqual(moved, slot)
            self.assertEqual(moved, live_state._project_port('new/project', taken))
            viewers = Path(temp) / 'viewers'; viewers.mkdir()
            (viewers / 'other--project.json').write_text(json.dumps({'port': slot, 'data': '/other/list.json'}))
            known = {'other/project/1': {'repo': 'other/project'}}
            self.assertNotEqual(live_state._project_port('new/project', known), slot)

    def test_registry_repair_recovers_the_repo_port_from_its_viewer_state(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(live_state, 'STATE', Path(temp)), \
                patch.object(live_state, '_port_is_free', return_value=False):
            viewers = Path(temp) / 'viewers'; viewers.mkdir()
            (viewers / 'owner--project.json').write_text(json.dumps({'port': 58771, 'data': '/x/123.json'}))
            self.assertEqual(live_state._project_port('owner/project', {}), 58771)

    def test_registry_repair_on_the_real_viewer_layout_keeps_58771_and_53366(self):
        # Old per-list files sort before the per-repo file and one of them (dead) still
        # records 53366; neither may win or reserve a port after the registry loses viewer_port.
        with tempfile.TemporaryDirectory() as temp, patch.object(live_state, 'STATE', Path(temp)), \
                patch.object(live_state, '_port_is_free', return_value=False), \
                patch.object(live_state, '_pid_is_running', return_value=False):
            viewers = Path(temp) / 'viewers'; viewers.mkdir()
            layout = {'ingpoc--agent-plugins--107': 61388, 'ingpoc--agent-plugins--111': 53366,
                      'ingpoc--agent-plugins--112': 57666, 'ingpoc--agent-plugins': 58771,
                      'ingpoc--like-minded-app': 53366}
            for stem, port in layout.items():
                (viewers / f'{stem}.json').write_text(json.dumps({'port': port, 'pid': 999999, 'data': f'/x/{stem}.json'}))
            records = {'ingpoc/agent-plugins/123': {'repo': 'ingpoc/agent-plugins', 'live': '/x/123.json'},
                       'ingpoc/agent-plugins/111': {'repo': 'ingpoc/agent-plugins', 'live': '/x/ingpoc--agent-plugins--111.json'},
                       'ingpoc/like-minded-app/bcl-r1': {'repo': 'ingpoc/like-minded-app', 'live': '/x/bcl-r1.json'}}
            self.assertEqual(live_state._project_port('ingpoc/agent-plugins', records), 58771)
            self.assertEqual(live_state._project_port('ingpoc/like-minded-app', records), 53366)

    def test_live_old_viewer_still_reserves_its_port_and_two_saved_ports_fail(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(live_state, 'STATE', Path(temp)), \
                patch.object(live_state, '_port_is_free', return_value=True), \
                patch.object(live_state, '_pid_is_running', return_value=True):
            slot = live_state._project_port('new/project', {})
            viewers = Path(temp) / 'viewers'; viewers.mkdir()
            (viewers / 'some--old--list.json').write_text(json.dumps({'port': slot, 'pid': 4242}))
            self.assertNotEqual(live_state._project_port('new/project', {}), slot)
            records = {'a/b/1': {'repo': 'a/b', 'viewer_port': 51000}, 'a/b/2': {'repo': 'a/b', 'viewer_port': 51001}}
            with self.assertRaisesRegex(RuntimeError, 'more than one saved viewer port'):
                live_state._project_port('a/b', records)

    def test_viewer_start_after_repair_ignores_old_per_list_files(self):
        # Repaired registry, no per-repo file, only an old per-list file for this repo that
        # records another repo's saved port: the viewer must use the repo slot, never 53366.
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / 'state'; home = Path(temp) / 'home'; (home / 'report').mkdir(parents=True)
            (home / 'report/page.html').write_text('<html></html>')
            live = state / 'ingpoc--agent-plugins' / '123.json'; live.parent.mkdir(parents=True); live.write_text('{}')
            old_live = state / 'ingpoc--agent-plugins' / '111.json'; old_live.write_text('{}')
            record = {'repo': 'ingpoc/agent-plugins', 'id': '123', 'live': str(live), 'home': str(home),
                      'snapshot': 'report/data.json', 'html': 'report/page.html'}
            records = {'ingpoc/agent-plugins/123': record,
                       'ingpoc/agent-plugins/111': {**record, 'id': '111', 'live': str(old_live)},
                       'ingpoc/like-minded-app/bcl-r1': {'repo': 'ingpoc/like-minded-app', 'viewer_port': 53366}}
            with patch.object(live_state, 'STATE', state), \
                    patch.object(live_state, '_port_is_free', return_value=True), \
                    patch.object(live_state, '_pid_is_running', return_value=False), \
                    patch.object(live_state, '_viewer_ready', return_value=True), \
                    patch.object(live_state.subprocess, 'Popen', return_value=SimpleNamespace(pid=4321)):
                live_state.save_registry(records)
                (state / 'viewers').mkdir()
                (state / 'viewers/ingpoc--agent-plugins--111.json').write_text(json.dumps(
                    {'port': 53366, 'pid': 999999, 'instance': 'old', 'data': str(old_live)}))
                expected = live_state._project_port('ingpoc/agent-plugins')
                value = live_state.ensure_viewer('ingpoc/agent-plugins/123', record, live, 'v1')
                self.assertNotIn(expected, {53366, 61388})
                self.assertEqual(value['port'], expected)
                self.assertEqual(live_state.registry()['ingpoc/agent-plugins/123']['viewer_port'], expected)

    def test_occupied_first_port_is_not_saved(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / 'state'; home = Path(temp) / 'home'; (home / 'report').mkdir(parents=True)
            (home / 'report/page.html').write_text('<html></html>')
            live = Path(temp) / 'live.json'; live.write_text('{}')
            record = {'repo': 'owner/project', 'id': '1', 'live': str(live), 'home': str(home),
                      'snapshot': 'report/data.json', 'html': 'report/page.html'}
            with patch.object(live_state, 'STATE', state), \
                    patch.object(live_state, '_project_port', return_value=54999), \
                    patch.object(live_state, '_port_is_free', return_value=False):
                live_state.save_registry({'owner/project/1': dict(record)})
                with self.assertRaisesRegex(RuntimeError, 'port 54999 is occupied'):
                    live_state.ensure_viewer('owner/project/1', record, live, 'v1')
                self.assertNotIn('viewer_port', live_state.registry()['owner/project/1'])

    def test_running_old_per_list_viewer_cannot_veto_the_repo_port(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(live_state, 'STATE', Path(temp)), \
                patch.object(live_state, '_port_is_free', return_value=True), \
                patch.object(live_state, '_pid_is_running', return_value=True):
            viewers = Path(temp) / 'viewers'; viewers.mkdir()
            for stem, port in {'ingpoc--agent-plugins--121': 58771, 'ingpoc--agent-plugins': 58771,
                               'ingpoc--agent-plugins--111': 53366, 'ingpoc--like-minded-app': 53366}.items():
                (viewers / f'{stem}.json').write_text(json.dumps({'port': port, 'pid': 4242}))
            records = {'ingpoc/agent-plugins/123': {'repo': 'ingpoc/agent-plugins'},
                       'ingpoc/like-minded-app/bcl-r1': {'repo': 'ingpoc/like-minded-app'}}
            self.assertEqual(live_state._project_port('ingpoc/agent-plugins', records), 58771)
            self.assertEqual(live_state._project_port('ingpoc/like-minded-app', records), 53366)

    def test_recovered_port_saved_by_another_repo_fails_loudly(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(live_state, 'STATE', Path(temp)), \
                patch.object(live_state, '_port_is_free', return_value=True):
            viewers = Path(temp) / 'viewers'; viewers.mkdir()
            (viewers / 'owner--project.json').write_text(json.dumps({'port': 53366}))
            records = {'other/repo/1': {'repo': 'other/repo', 'viewer_port': 53366}}
            with self.assertRaisesRegex(RuntimeError, 'saved by another repo'):
                live_state._project_port('owner/project', records)

    def test_saved_ports_never_change(self):
        records = {'ingpoc/agent-plugins/123': {'repo': 'ingpoc/agent-plugins', 'viewer_port': 58771},
                   'ingpoc/like-minded-app/bcl-r1': {'repo': 'ingpoc/like-minded-app', 'viewer_port': 53366}}
        with patch.object(live_state, '_port_is_free', return_value=False):
            self.assertEqual(live_state._project_port('ingpoc/agent-plugins', records), 58771)
            self.assertEqual(live_state._project_port('ingpoc/like-minded-app', records), 53366)

    def test_occupied_saved_port_fails_loudly_and_is_not_moved(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / 'state'; live = Path(temp) / 'live.json'; live.write_text('{}')
            home = Path(temp) / 'home'; (home / 'report').mkdir(parents=True)
            (home / 'report/page.html').write_text('<html></html>')
            record = {'home': str(home), 'snapshot': 'report/data.json', 'html': 'report/page.html',
                      'repo': 'owner/project', 'id': '1', 'live': str(live), 'viewer_port': 54321}
            with patch.object(live_state, 'STATE', state), \
                    patch.object(live_state, '_port_is_free', return_value=False), \
                    patch.object(live_state.subprocess, 'Popen') as popen:
                live_state.save_registry({'owner/project/1': record})
                with self.assertRaisesRegex(RuntimeError, 'port 54321 is occupied'):
                    live_state.ensure_viewer('owner/project/1', record, live, 'v1')
                self.assertEqual(live_state.registry()['owner/project/1']['viewer_port'], 54321)
            popen.assert_not_called()

    def test_real_rollover_restart_upgrade_and_repair_keep_the_repo_port(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        root = Path(temp.name); state = root / 'state'; home = root / 'home'
        (home / 'report').mkdir(parents=True)
        example = json.loads((Path(__file__).resolve().parents[1] / 'examples/findings.json').read_text())
        live1 = state / 'owner--project' / '1.json'; live1.parent.mkdir(parents=True)
        live1.write_text(json.dumps({**example, 'title': 'List one marker'}))
        record1 = {'repo': 'owner/project', 'id': '1', 'live': str(live1), 'home': str(home),
                   'snapshot': 'report/pr-1-findings.json', 'html': 'report/pr-1.html'}
        pids = set(); lives = []
        def cleanup():
            for path in (state / 'viewers').glob('*.json'):
                try:
                    info = json.loads(path.read_text())
                    if not info.get('instance'): continue
                    request = Request(f"http://127.0.0.1:{info['port']}/__bcl_shutdown", method='POST', headers={'X-BCL-Instance': info['instance']})
                    with urlopen(request, timeout=2): pass
                    live_state._wait_for_port(info['port'])
                except (OSError, ValueError, KeyError): pass
            viewer_test_guard.stop_viewers(root, pids)
        self.addCleanup(cleanup)
        def health(port):
            with urlopen(f"http://127.0.0.1:{port}/__bcl_health", timeout=2) as response:
                return json.load(response)['path']
        def page(port):
            with urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
                return response.read().decode()
        with patch.object(live_state, 'STATE', state):
            expected = live_state._project_port('owner/project', {})
            live_state.save_registry({'owner/project/1': record1})
            first = live_state.ensure_viewer('owner/project/1', record1, live1, 'v1'); pids.add(first['pid'])
            self.assertEqual(first['port'], expected)
            self.assertEqual(live_state.registry()['owner/project/1']['viewer_port'], expected)
            self.assertIn('List one marker', page(expected))
            doc2 = {**example, 'title': 'List two marker',
                    'tracking': {'pull_request': {'label': 'PR #2', 'url': 'https://github.com/owner/project/pull/2'}}}
            key2, record2, live2 = live_state.register_rollover(live_state.registry()['owner/project/1'], live1, doc2, 'home')
            self.assertEqual(record2['viewer_port'], expected)
            second = live_state.ensure_viewer(key2, record2, live2, 'v1'); pids.add(second['pid'])
            self.assertEqual(second['port'], expected)
            self.assertEqual(health(expected), str(live2.resolve()))
            self.assertIn('List two marker', page(expected))
            upgraded = live_state.ensure_viewer(key2, record2, live2, 'v2'); pids.add(upgraded['pid'])
            self.assertNotEqual(upgraded['pid'], second['pid'])
            self.assertEqual(upgraded['port'], expected)
            repaired = {key: {k: v for k, v in value.items() if k != 'viewer_port'}
                        for key, value in live_state.registry().items()}
            live_state.save_registry(repaired)
            self.assertEqual(live_state._project_port('owner/project'), expected)
            restarted = live_state.ensure_viewer(key2, repaired[key2], live2, 'v3'); pids.add(restarted['pid'])
            self.assertEqual(restarted['port'], expected)
            self.assertEqual(live_state.registry()[key2]['viewer_port'], expected)
            self.assertEqual(health(expected), str(live2.resolve()))
            other = live_state._project_port('other/project')
            self.assertNotEqual(other, expected)


if __name__ == '__main__':
    unittest.main()
