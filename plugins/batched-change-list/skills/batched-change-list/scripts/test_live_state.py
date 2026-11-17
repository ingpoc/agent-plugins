import json
import hashlib
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import live_state
import render


class ViewerLivenessTests(unittest.TestCase):
    def test_legacy_health_requires_pid_and_data_path(self):
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return json.dumps({'pid': 123, 'path': '/tmp/list.json'}).encode()

        expected = {'pid': 123, 'data': '/tmp/list.json'}
        with patch.object(live_state.urllib.request, 'urlopen', return_value=Response()):
            self.assertTrue(live_state._legacy_viewer_matches(5000, expected))
        with patch.object(live_state.urllib.request, 'urlopen', return_value=Response()):
            self.assertFalse(live_state._legacy_viewer_matches(5000, {**expected, 'pid': 124}))
            self.assertFalse(live_state._legacy_viewer_matches(5000, {**expected, 'data': '/tmp/other.json'}))

    def test_legacy_record_migrates_without_shutdown_and_starts_for_list(self):
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
                 patch.object(live_state, 'report_paths', return_value=(page, root)), \
                 patch.object(live_state, '_pid_is_running', return_value=True), \
                 patch.object(live_state, '_legacy_viewer_matches', return_value=True) as verify, \
                 patch.object(live_state, '_reserved_ports', return_value=set()), \
                 patch.object(live_state, '_port_is_free', side_effect=lambda port: port == 53366), \
                 patch.object(live_state.subprocess, 'Popen', return_value=proc) as popen, \
                 patch.object(live_state, '_viewer_ready', return_value=True), \
                 patch.object(live_state.urllib.request, 'urlopen') as urlopen:
                result = live_state.ensure_viewer('repo/1', {}, live, 'new')
            verify.assert_called_once_with(5000, old)
            urlopen.assert_not_called()
            command = popen.call_args.args[0]
            self.assertEqual(Path(command[command.index('--data') + 1]).resolve(), live.resolve())
            self.assertEqual(result['data'], str(live.resolve()))
            self.assertEqual(result['pid'], 456)
            self.assertEqual(json.loads(state.read_text())['instance'], result['instance'])

    def test_unverified_legacy_record_is_left_running_without_replacement(self):
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
                 patch.object(live_state, '_legacy_viewer_matches', return_value=False) as verify, \
                 patch.object(live_state.subprocess, 'Popen') as popen, \
                 patch.object(live_state.urllib.request, 'urlopen') as urlopen:
                with self.assertRaisesRegex(RuntimeError, 'cannot verify viewer ownership'):
                    live_state.ensure_viewer('repo/1', {}, live, 'new')
            verify.assert_called_once_with(5000, old)
            popen.assert_not_called()
            urlopen.assert_not_called()

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


if __name__ == '__main__':
    unittest.main()
