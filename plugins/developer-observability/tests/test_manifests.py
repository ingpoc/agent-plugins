"""Manifests: no cwd, one launcher, aligned versions; the root resolver works for every root and fails loud."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
VERSION = '0.1.11'
PLACEHOLDERS = ('${PLUGIN_ROOT}', '${CURSOR_PLUGIN_ROOT}', '${CLAUDE_PLUGIN_ROOT}')


def server(rel):
    return json.loads((ROOT / rel).read_text())['mcpServers']['developer-observability']


class ManifestTests(unittest.TestCase):
    def test_shape_and_versions(self):
        for rel in ('mcp.json', '.cursor-plugin/mcp.json', '.mcp.json'):
            entry = server(rel)
            self.assertNotIn('cwd', entry, rel)
            self.assertEqual(entry['command'], 'sh', rel)
            self.assertNotIn('${', entry['args'][1], rel)
            self.assertNotIn('|| true', entry['args'][1], rel)
        self.assertEqual(server('.claude-plugin/mcp.json')['command'], '${CLAUDE_PLUGIN_ROOT}/bin/developer-observability')
        for rel in ('plugin.json', '.cursor-plugin/plugin.json', '.claude-plugin/plugin.json',
                    '.grok-plugin/plugin.json', 'package.json'):
            self.assertEqual(json.loads((ROOT / rel).read_text())['version'], VERSION, rel)
        self.assertIn(f"version='{VERSION}'", (ROOT / 'runtime/server.py').read_text())

    def run_resolver(self, rel, roots, home):
        entry = server(rel)
        fake = Path(home) / 'fake-python'
        fake.write_text('#!/bin/sh\necho FAKE-OK\n')
        fake.chmod(0o755)
        env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': home, 'OBSERVABILITY_PYTHON': str(fake)}
        return subprocess.run(['sh', '-c', entry['args'][1], 'developer-observability', *roots],
                              env=env, capture_output=True, text=True, timeout=20)

    def test_resolver_each_root_and_unresolved(self):
        with tempfile.TemporaryDirectory() as home:
            for position in range(3):
                roots = list(PLACEHOLDERS)
                roots[position] = str(ROOT)
                run = self.run_resolver('mcp.json', roots, home)
                self.assertIn('FAKE-OK', run.stdout, (position, run.stderr))
            run = self.run_resolver('.cursor-plugin/mcp.json', [str(ROOT)], home)
            self.assertIn('FAKE-OK', run.stdout, run.stderr)
            for rel, roots in (('mcp.json', list(PLACEHOLDERS)), ('.cursor-plugin/mcp.json', ['${CURSOR_PLUGIN_ROOT}'])):
                run = self.run_resolver(rel, roots, home)
                self.assertEqual(run.returncode, 127, rel)
                self.assertIn('PLUGIN_ROOT_UNRESOLVED', run.stderr, rel)

    def test_no_personal_paths_or_secrets_in_package(self):
        pattern = re.compile(r'/Users/[A-Za-z]|gurusharan|sk-[A-Za-z0-9]{20}|gh[pousr]_[A-Za-z0-9]{20}')
        for path in ROOT.rglob('*'):
            skip = {'node_modules', '__pycache__', '.ruff_cache', '.pytest_cache'}
            if path.is_file() and not skip & set(path.parts) and path.suffix in {'.py', '.json', '.md', '.sh', ''}:
                if 'tests' in path.relative_to(ROOT).parts:
                    continue  # fixtures carry fake secrets and paths on purpose
                self.assertIsNone(pattern.search(path.read_text(errors='ignore')), str(path))


if __name__ == '__main__':
    unittest.main()
