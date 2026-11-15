import json
import hashlib
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import live_state
import render


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
