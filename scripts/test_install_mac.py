"""Run fail-closed installer paths with local files; never install a runtime."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
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


ROOT = SCRIPT.parent.parent
CATALOGS = {'codex': '.agents/plugins/marketplace.json', 'cursor': '.cursor-plugin/marketplace.json',
            'grok': '.grok-plugin/marketplace.json', 'claude': '.claude-plugin/marketplace.json'}


def stub(path, body):
    path.write_text('#!/bin/bash\n' + body)
    path.chmod(0o755)


class InstallerCatalogTableTests(unittest.TestCase):
    def test_catalog_table_matches_each_harness_marketplace(self):
        text = SCRIPT.read_text()
        body = re.search(r'^catalog\(\) \{\n(.*?)^\}', text, re.S | re.M).group(1)
        table = {}
        for names, plugins in re.findall(r"^\s+([a-z|]+)\) printf '%s\\n' '([^']*)'", body, re.M):
            for name in names.split('|'):
                table[name] = set(plugins.split())
        for harness, rel in CATALOGS.items():
            listed = {p['name'] for p in json.loads((ROOT / rel).read_text())['plugins']}
            self.assertEqual(table[harness], listed, harness)


@unittest.skipUnless(platform.system() == 'Darwin' and platform.machine() == 'arm64',
                     'installer acceptance requires Apple Silicon macOS')
class InstallerCometAllHarnessesTests(unittest.TestCase):
    """Comet present, every harness present, runtime current; never opens anything."""
    VERSION, BUILD = '9.9.9', '999'

    def run_phase(self, phase, *extra, grok_has_comet=False):
        with tempfile.TemporaryDirectory(prefix='acu-installer-comet-') as temp:
            root = Path(temp)
            calls = root / 'calls.log'
            app = root / 'apps/Agent Computer Use.app/Contents'
            (app / 'MacOS').mkdir(parents=True)
            subprocess.run(['plutil', '-create', 'xml1', str(app / 'Info.plist')], check=True)
            for key, value in [('CFBundleIdentifier', 'com.ingpoc.cua-service'),
                               ('CFBundleShortVersionString', self.VERSION),
                               ('CFBundleVersion', self.BUILD), ('LSMinimumSystemVersion', '13.0')]:
                subprocess.run(['plutil', '-insert', key, '-string', value, str(app / 'Info.plist')], check=True)
            stub(app / 'MacOS/CUAService', 'echo \'{"permissions":{"accessibility":true,"screen_recording":true,"microphone":"granted"}}\'\n')
            (root / 'Comet.app').mkdir()
            bins = root / 'bin'; bins.mkdir()
            log = f'echo "$(basename "$0") $*" >> {calls}\n'
            acu = {'name': 'agent-computer-use', 'version': self.VERSION, 'enabled': True}
            comet = {'name': 'comet-control', 'version': '0.1.0', 'enabled': True}
            stub(bins / 'codex', log + f"echo '{json.dumps([acu, comet])}'\n")
            stub(bins / 'claude', log + f"""case "$*" in
  'plugin list --json') echo '{json.dumps([dict(acu, id='agent-computer-use@ingpoc-agent-plugins')])}' ;;
  'plugin marketplace list --json') echo '[{{"name":"ingpoc-agent-plugins"}}]' ;;
  *install*comet-control*) echo 'Plugin "comet-control" not found' >&2; exit 1 ;;
esac\n""")
            grok_rows = [dict(acu, status='installed')] + ([dict(comet, status='installed')] if grok_has_comet else [])
            stub(bins / 'grok', log + f"""case "$*" in
  'plugin list --json') echo '{json.dumps(grok_rows)}' ;;
  plugin\\ install*) read -r -p 'Trust this plugin? [y/N] ' answer; sleep 600 ;;
esac\n""")
            stub(bins / 'agent', log + 'echo "state"; echo "act"\n')
            stub(bins / 'open', log)
            receipt = root / 'cursor-installed.json'
            receipt.write_text(json.dumps([acu, comet]))
            manifest = root / 'manifest.json'
            manifest.write_bytes(json.dumps({'schema': 1, 'repo': 'ingpoc/agent-plugins', 'plugins': {'agent-computer-use': {
                'version': self.VERSION, 'build': self.BUILD, 'bundleId': 'com.ingpoc.cua-service', 'minMacOS': '13.0',
                'url': 'file:///nonexistent/AgentComputerUse-9.9.9.dmg', 'sha256': '0' * 64}}}).encode())
            (root / 'SHA256SUMS').write_text(f'{hashlib.sha256(manifest.read_bytes()).hexdigest()}  manifest.json\n')
            env = os.environ | {
                'PATH': f"{bins}:{os.environ['PATH']}",
                'AGENT_PLUGINS_TEST': '1', 'AGENT_PLUGINS_MANIFEST_URL': manifest.as_uri(),
                'AGENT_PLUGINS_CACHE_DIR': str(root / 'cache'), 'AGENT_PLUGINS_APP_DIR': str(root / 'apps'),
                'AGENT_PLUGINS_LOG': str(root / 'install.log'), 'AGENT_PLUGINS_COMET_APP': str(root / 'Comet.app'),
                'AGENT_PLUGINS_CURSOR_RECEIPT': str(receipt),
            }
            run = subprocess.run(['bash', str(SCRIPT), phase, *extra], env=env, text=True,
                                 capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
            summary = json.loads(run.stdout.splitlines()[-1])
            made = calls.read_text() if calls.exists() else ''
            return run.returncode, summary, made

    @staticmethod
    def actions(summary):
        return {(h['name'], p['name']): p['action'] for h in summary['harnesses'] for p in h['plugins']}

    def test_install_skips_comet_for_claude_and_exits_zero(self):
        code, summary, calls = self.run_phase('install')
        self.assertEqual(code, 0, summary)
        actions = self.actions(summary)
        self.assertEqual(actions[('claude', 'comet-control')], 'skipped: not in catalog')
        self.assertEqual(actions[('claude', 'agent-computer-use')], 'skip')
        self.assertEqual(actions[('codex', 'comet-control')], 'skip')
        self.assertEqual(actions[('grok', 'agent-computer-use')], 'skip')
        self.assertEqual(summary['runtime']['action'], 'skip')
        self.assertNotIn('comet-control', ''.join(l for l in calls.splitlines() if l.startswith('claude')))
        self.assertNotIn('open ', calls)

    def test_grok_missing_plugin_is_manual_and_never_answers_trust_prompt(self):
        code, summary, calls = self.run_phase('install', grok_has_comet=False)
        self.assertEqual(code, 0)
        self.assertEqual(self.actions(summary)[('grok', 'comet-control')], 'manual')
        self.assertNotIn('grok plugin install', calls)

    def test_finish_dry_run_proves_every_harness_without_opening(self):
        code, summary, calls = self.run_phase('finish', '--dry-run', grok_has_comet=True)
        self.assertEqual(code, 0, summary)
        proofs = {p['harness']: p['ok'] for p in summary['proof']}
        self.assertEqual(proofs, {'codex': True, 'cursor': True, 'claude': True, 'grok': True})
        self.assertEqual(self.actions(summary)[('claude', 'comet-control')], 'skipped: not in catalog')
        self.assertFalse(summary['unlock_opened'])
        self.assertNotIn('open ', calls)


if __name__ == '__main__':
    unittest.main()
