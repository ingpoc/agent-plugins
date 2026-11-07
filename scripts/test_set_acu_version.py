import json
import plistlib
import tempfile
import unittest
from pathlib import Path

from set_acu_version import set_version


class VersionTests(unittest.TestCase):
    def test_align_and_reject_decrease_without_partial_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('plugin.json', '.cursor-plugin/plugin.json', '.claude-plugin/plugin.json', '.grok-plugin/plugin.json'):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({'version': '1.2.3'}, indent=2))
            info = root / 'skills/macos-cua/service/Resources/Info.plist'
            info.parent.mkdir(parents=True)
            info.write_bytes(plistlib.dumps({'CFBundleShortVersionString':'1.2.3', 'CFBundleVersion':'1.2.3'}))
            set_version(root, '1.2.4')
            self.assertEqual(json.loads((root / '.grok-plugin/plugin.json').read_text())['version'], '1.2.4')
            self.assertEqual(plistlib.loads(info.read_bytes())['CFBundleVersion'], '1.2.4')
            before = (root / 'plugin.json').read_bytes()
            with self.assertRaises(ValueError):
                set_version(root, '1.2.2')
            self.assertEqual((root / 'plugin.json').read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
