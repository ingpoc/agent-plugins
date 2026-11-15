import contextlib
import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('cloud_install', Path(__file__).with_name('install.py'))
INSTALL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALL)

class InstallerTests(unittest.TestCase):
    def exercise(self, marketplace, extra=()):
        calls = []
        def run(*args, capture=False):
            calls.append(args)
            return SimpleNamespace(stdout=marketplace)
        with patch.object(INSTALL, 'run', side_effect=run), patch('sys.argv', ['install.py', *extra]), contextlib.redirect_stdout(io.StringIO()):
            INSTALL.main()
        return calls

    def test_new_registration_and_default_plugins(self):
        calls = self.exercise('MARKETPLACE ROOT\n')
        self.assertIn(('codex', 'plugin', 'marketplace', 'add', str(INSTALL.ROOT)), calls)
        self.assertIn(('codex', 'plugin', 'add', 'batched-change-list@ingpoc-agent-plugins'), calls)
        self.assertIn(('codex', 'plugin', 'add', 'developer-observability@ingpoc-agent-plugins'), calls)
        self.assertFalse(any('context-ledger@ingpoc-agent-plugins' in c for c in calls))
        self.assertTrue(any('runtime/install.py' in ' '.join(c) for c in calls))

    def test_existing_registration_and_opt_in(self):
        calls = self.exercise(f'MARKETPLACE ROOT\ningpoc-agent-plugins {INSTALL.ROOT}\n', ('--context-ledger',))
        self.assertFalse(any(c[:4] == ('codex', 'plugin', 'marketplace', 'add') for c in calls))
        self.assertIn(('codex', 'plugin', 'add', 'context-ledger@ingpoc-agent-plugins'), calls)

    def test_other_registration_is_never_overwritten(self):
        with self.assertRaisesRegex(RuntimeError, 'different location'):
            self.exercise('MARKETPLACE ROOT\ningpoc-agent-plugins /another/checkout\n')

if __name__ == '__main__':
    unittest.main()
