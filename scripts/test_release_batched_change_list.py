"""Release preflight and installed CLI/viewer proof for batched-change-list."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import release_batched_change_list as release


class ReleaseTests(unittest.TestCase):
    def output(self, head='candidate', status='draft'):
        def output(command, **_):
            if command[:2] == ['git', 'rev-parse']:
                return 'candidate'
            if command[:2] == ['git', 'branch']:
                return 'topic'
            return json.dumps({'status': status, 'headSha': head, 'headRef': 'topic'})
        return output

    def test_wrong_pr_head_stops_before_install(self):
        with patch.object(release.shared, 'output', side_effect=self.output('other')), \
             patch.object(release.shared, 'frozen'), patch.object(release.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'exact open Origin PR'):
                release.release_batched_change_list(Path('/candidate'), 1)
            run.assert_not_called()

    def test_hygiene_failure_stops_before_install(self):
        with patch.object(release.shared, 'output', side_effect=self.output()), \
             patch.object(release.shared, 'frozen'), \
             patch.object(release.shared, 'hygiene_gate', side_effect=RuntimeError('hygiene failed')), \
             patch.object(release.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'hygiene failed'):
                release.release_batched_change_list(Path('/candidate'), 1)
            run.assert_not_called()

    def test_extra_personal_files_stop_before_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / '.agents/plugins/batched-change-list').mkdir(parents=True)
            contract = Mock(); contract.package_identity.return_value = {'version': '1.0.0'}
            with patch.object(Path, 'home', return_value=home), \
                 patch.object(release.shared, 'output', side_effect=self.output()), \
                 patch.object(release.shared, 'frozen'), patch.object(release.shared, 'hygiene_gate', return_value={'checked': True, 'failures': []}), \
                 patch.object(release.shared, 'contract_module', return_value=contract), \
                 patch.object(release.subprocess, 'check_output', return_value='*deleting unknown\n'), \
                 patch.object(release.subprocess, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, 'extra files'):
                    release.release_batched_change_list(Path('/candidate'), 1)
                run.assert_not_called()

    def test_probe_runs_real_cli_and_live_viewer(self):
        package = Path(__file__).resolve().parent.parent / 'plugins/batched-change-list'
        version = json.loads((package / 'plugin.json').read_text())['version']
        release.probe(package, version)

    def test_mismatched_installed_package_does_not_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / '.agents/plugins/batched-change-list').mkdir(parents=True)
            contract = Mock(); contract.package_identity.return_value = {'version': '1.0.0', 'sha256': 'candidate'}
            def output(command, **kwargs):
                if command[:3] == ['codex', 'plugin', 'add']:
                    return json.dumps({'version': '0.9.0', 'installedPath': str(home / 'installed')})
                return self.output()(command, **kwargs)
            with patch.object(Path, 'home', return_value=home), \
                 patch.object(release.shared, 'output', side_effect=output), \
                 patch.object(release.shared, 'frozen'), patch.object(release.shared, 'hygiene_gate', return_value={'checked': True, 'failures': []}), \
                 patch.object(release.shared, 'contract_module', return_value=contract), \
                 patch.object(release.subprocess, 'check_output', return_value=''), \
                 patch.object(release.subprocess, 'run'), patch.object(release, 'probe') as probe:
                with self.assertRaisesRegex(RuntimeError, 'does not match candidate'):
                    release.release_batched_change_list(Path('/candidate'), 1)
                probe.assert_not_called()

    def test_missing_hygiene_stops_before_sync(self):
        with patch.object(release.shared, 'output', side_effect=self.output()), \
             patch.object(release.shared, 'frozen'), \
             patch.object(release.shared, 'hygiene_gate', return_value={'checked': False}), \
             patch.object(release.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'completed, passing hygiene'):
                release.release_batched_change_list(Path('/candidate'), 1)
            run.assert_not_called()

    def test_pr_drift_prevents_mutation_and_success(self):
        for drift_on in (2, 4):
            with self.subTest(drift_on=drift_on), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                (home / '.agents/plugins/batched-change-list').mkdir(parents=True)
                contract = Mock(); contract.package_identity.return_value = {'version': '1.0.0', 'sha256': 'candidate'}
                calls = []
                def output(command, **kwargs):
                    if command[:2] == ['origin', 'pr']:
                        calls.append(1)
                        return json.dumps({'status': 'closed' if len(calls) == drift_on else 'draft',
                                           'headSha': 'candidate', 'headRef': 'topic'})
                    if command[:3] == ['codex', 'plugin', 'add']:
                        return json.dumps({'version': '1.0.0', 'installedPath': str(home / 'installed')})
                    return self.output()(command, **kwargs)
                with patch.object(Path, 'home', return_value=home), \
                     patch.object(release.shared, 'output', side_effect=output), \
                     patch.object(release.shared, 'frozen'), \
                     patch.object(release.shared, 'hygiene_gate', return_value={'checked': True, 'failures': []}), \
                     patch.object(release.shared, 'contract_module', return_value=contract), \
                     patch.object(release.subprocess, 'check_output', return_value=''), \
                     patch.object(release.subprocess, 'run') as run, patch.object(release, 'probe'):
                    with self.assertRaisesRegex(RuntimeError, 'exact open Origin PR'):
                        release.release_batched_change_list(Path('/candidate'), 1)
                    if drift_on == 2:
                        run.assert_not_called()

    def test_incomplete_viewer_startup_is_bounded(self):
        with subprocess.Popen([sys.executable, '-u', '-c',
                               'import sys,time;sys.stdout.write("http://127.0.0.1:");sys.stdout.flush();time.sleep(5)'],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as server:
            try:
                with self.assertRaisesRegex(RuntimeError, 'time limit'):
                    release.viewer_address(server, timeout=0.1)
            finally:
                server.terminate()
                server.wait(timeout=2)

    def test_stdout_eof_does_not_wait_for_open_stderr(self):
        stdout_read, stdout_write = os.pipe()
        stderr_read, stderr_write = os.pipe()
        os.close(stdout_write)
        try:
            with os.fdopen(stdout_read, 'rb') as stdout, os.fdopen(stderr_read, 'rb') as stderr:
                server = SimpleNamespace(stdout=stdout, stderr=stderr)
                with self.assertRaisesRegex(RuntimeError, 'exited before reporting'):
                    release.viewer_address(server, timeout=0.1)
        finally:
            os.close(stderr_write)


if __name__ == '__main__':
    unittest.main()
