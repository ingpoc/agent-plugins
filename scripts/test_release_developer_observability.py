"""Release preflight must reject the wrong PR and unexpected installed files."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import release_developer_observability as release


class ReleaseTests(unittest.TestCase):
    def test_wrong_pr_head_never_changes_installed_files(self):
        def output(command, **_):
            if command[:2] == ['git', 'rev-parse']:
                return 'candidate'
            if command[:2] == ['git', 'branch']:
                return 'topic'
            return json.dumps({'status': 'open', 'headSha': 'other', 'headRef': 'topic'})
        with patch.object(release.shared, 'output', side_effect=output), \
             patch.object(release.shared, 'frozen'), patch.object(release.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'exact open Origin PR'):
                release.release_observability(Path('/candidate'), 1)
            run.assert_not_called()

    def test_draft_pr_reaches_hygiene_preflight(self):
        def output(command, **_):
            if command[:2] == ['git', 'rev-parse']:
                return 'candidate'
            if command[:2] == ['git', 'branch']:
                return 'topic'
            return json.dumps({'status': 'draft', 'headSha': 'candidate', 'headRef': 'topic'})
        with patch.object(release.shared, 'output', side_effect=output), \
             patch.object(release.shared, 'frozen'), \
             patch.object(release.shared, 'hygiene_gate', side_effect=RuntimeError('hygiene reached')):
            with self.assertRaisesRegex(RuntimeError, 'hygiene reached'):
                release.release_observability(Path('/candidate'), 1)

    def test_extra_installed_files_stop_before_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home/'.agents/plugins/developer-observability').mkdir(parents=True)
            def output(command, **_):
                if command[:2] == ['git', 'rev-parse']:
                    return 'candidate'
                if command[:2] == ['git', 'branch']:
                    return 'topic'
                return json.dumps({'status': 'open', 'headSha': 'candidate', 'headRef': 'topic'})
            contract = Mock(); contract.package_identity.return_value = {'version': '1.0.0'}
            with patch.object(Path, 'home', return_value=home), patch.object(release.shared, 'output', side_effect=output), \
                 patch.object(release.shared, 'frozen'), patch.object(release.shared, 'hygiene_gate'), \
                 patch.object(release.shared, 'contract_module', return_value=contract), \
                 patch.object(release.subprocess, 'check_output', return_value='*deleting unknown\n'), \
                 patch.object(release.subprocess, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, 'extra files'):
                    release.release_observability(Path('/candidate'), 1)
                run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
