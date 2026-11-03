"""One install step writes a portable default config once and never overwrites it."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime'))
import configure  # noqa: E402


class InstallTests(unittest.TestCase):
    def run_py(self, script, *args, home):
        env = {**os.environ, 'HOME': str(home)}
        env.pop('OBSERVABILITY_CONFIG', None)
        return subprocess.run([sys.executable, str(ROOT / 'runtime' / script), *args], env=env,
                              capture_output=True, text=True, check=False)

    def test_default_config_written_once_without_absolute_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cfg = home / '.config/developer-observability/config.json'
            first = self.run_py('install.py', '--skip-deps', '--config', str(cfg), home=home)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn('Wrote default config', first.stdout)
            self.assertEqual(stat.S_IMODE(cfg.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(cfg.parent.stat().st_mode), 0o700)
            text = cfg.read_text()
            self.assertNotIn(str(home), text)
            data = json.loads(text)
            self.assertEqual((data['runtime'], data['decider_script'], data['discover_projects'], data['projects']),
                             (configure.DEFAULT_DATA, None, True, {}))
            self.assertTrue((home / '.local/share/developer-observability/data').is_dir())
            second = self.run_py('install.py', '--skip-deps', '--config', str(cfg), home=home)
            self.assertIn('Kept existing config', second.stdout)
            self.assertEqual(cfg.read_text(), text)
            refused = self.run_py('configure.py', '--config', str(cfg), home=home)
            self.assertEqual(refused.returncode, 1)
            self.assertEqual(cfg.read_text(), text)

    def test_print_writes_nothing_and_projects_parse(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / 'c.json'
            shown = self.run_py('configure.py', '--print', '--config', str(cfg), '--project', 'web,labels=web|site',
                                home=Path(tmp))
            self.assertEqual(shown.returncode, 0, shown.stderr)
            self.assertEqual(json.loads(shown.stdout)['projects']['web']['labels'], ['web', 'site'])
            self.assertFalse(cfg.exists())
        self.assertEqual(configure.parse_project('web,label=Web,labels=web|site'),
                         ('web', {'label': 'Web', 'labels': ['web', 'site'], 'owners': []}))
        for bad in ('Bad Id', 'ok,color=red'):
            with self.assertRaises(ValueError):
                configure.parse_project(bad)


if __name__ == '__main__':
    unittest.main()
