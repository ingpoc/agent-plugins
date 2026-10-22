"""Stale packages and stale running services cannot pass the same build contract."""
import importlib.util
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'service'))
import runtime_contract as contract
import cua_client


class RuntimeContractTests(unittest.TestCase):
    def fixture(self, directory):
        root = Path(directory) / 'plugin'
        root.mkdir()
        (root / 'plugin.json').write_text('{"version":"1.2.3"}')
        (root / 'bin').mkdir()
        (root / 'bin/launcher').write_text('original')
        app = Path(directory) / 'Service.app'
        (app / 'Contents/Resources').mkdir(parents=True)
        (app / contract.RECORD).write_text(json.dumps(contract.package_identity(root)))
        (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleShortVersionString':'1.2.3'}))
        return root, app

    def test_matching_disk_and_live_identity_pass_and_close_socket(self):
        with tempfile.TemporaryDirectory() as td:
            root, app = self.fixture(td)
            client = Mock()
            client.call.return_value = contract.package_identity(root)
            with patch.object(cua_client, 'CUAClient', return_value=client):
                self.assertTrue(contract.check(root, app)['ok'])
            client.call.assert_called_once_with('runtime_contract', retry=False)
            client.close.assert_called_once()
            client.connect.assert_not_called()

    def test_same_version_changed_content_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root, app = self.fixture(td)
            (root / 'bin/launcher').write_text('changed')
            with self.assertRaisesRegex(RuntimeError, 'package/app build mismatch'):
                contract.check(root, app, live=False)

    def test_stale_running_service_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root, app = self.fixture(td)
            client = Mock()
            client.call.return_value = {'version':'0.0.1'}
            with patch.object(cua_client, 'CUAClient', return_value=client):
                with self.assertRaisesRegex(RuntimeError, 'running service build mismatch'):
                    contract.check(root, app)
            client.close.assert_called_once()

    def test_disconnected_probe_never_recovers_or_relaunches(self):
        with tempfile.TemporaryDirectory() as td:
            root, app = self.fixture(td)
            with patch.object(cua_client.CUAClient, '_probe', return_value=Mock()), \
                 patch.object(cua_client.CUAClient, '_call_once', side_effect=ConnectionResetError), \
                 patch.object(cua_client.CUAClient, 'connect') as recover:
                with self.assertRaises(ConnectionResetError):
                    contract.check(root, app)
                recover.assert_not_called()

    def test_missing_manifest_blocks_launcher_without_mcp_stdout(self):
        with tempfile.TemporaryDirectory() as td:
            import os
            result = subprocess.run([str(ROOT.parents[1] / 'bin/agent-computer-use-mcp')],
                input='', text=True, capture_output=True,
                env=os.environ | {'AGENT_COMPUTER_USE_APP':td})
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, '')
            self.assertIn('RUNTIME_CONTRACT_FAILED', result.stderr)


if __name__ == '__main__':
    unittest.main()
