"""Run fail-closed installer paths with local files; never install a runtime."""
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).with_name('install-mac.sh')


@unittest.skipUnless(platform.system() == 'Darwin' and platform.machine() == 'arm64',
                     'installer acceptance requires Apple Silicon macOS')
class InstallerBoundaryTests(unittest.TestCase):
    def invoke(self, content, valid_checksum=True):
        with tempfile.TemporaryDirectory(prefix='acu-installer-test-') as temp:
            root = Path(temp)
            manifest = root / 'manifest.json'
            manifest.write_bytes(content)
            digest = hashlib.sha256(content).hexdigest() if valid_checksum else '0' * 64
            (root / 'SHA256SUMS').write_text(f'{digest}  manifest.json\n')
            env = os.environ | {
                'AGENT_PLUGINS_TEST': '1',
                'AGENT_PLUGINS_MANIFEST_URL': manifest.as_uri(),
                'AGENT_PLUGINS_CACHE_DIR': str(root / 'cache'),
                'AGENT_PLUGINS_APP_DIR': str(root / 'apps'),
                'AGENT_PLUGINS_LOG': str(root / 'install.log'),
            }
            run = subprocess.run(['bash', str(SCRIPT), 'install', '--dry-run'],
                                 env=env, text=True, capture_output=True, timeout=30)
            summary = json.loads(run.stdout.splitlines()[-1])
            self.assertFalse((root / 'apps').exists())
            self.assertFalse(summary['unlock_opened'])
            return run.returncode, summary

    def test_tampered_manifest_fails_verification_with_json(self):
        code, result = self.invoke(b'{}', valid_checksum=False)
        self.assertEqual(code, 10)
        self.assertEqual(result['runtime']['action'], 'failed')

    def test_invalid_json_is_manifest_error_not_success(self):
        code, _ = self.invoke(b'not json')
        self.assertEqual(code, 13)

    def test_wrong_repository_is_manifest_error(self):
        code, _ = self.invoke(b'{"schema":1,"repo":"someone/else","plugins":{}}')
        self.assertEqual(code, 13)


if __name__ == '__main__':
    unittest.main()
