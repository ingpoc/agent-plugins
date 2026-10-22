"""Release policy and fail-closed orchestration, without touching installed apps."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import release_agent_computer_use as release


class PolicyTests(unittest.TestCase):
    def setUp(self):
        local_names = subprocess.check_output(['git','rev-parse','--local-env-vars'],text=True).splitlines()
        environment = {k:v for k,v in os.environ.items() if k not in local_names}
        environment.update({'CI':'','AGENT_PLUGINS_RELEASE_BASE':'HEAD'})
        context = patch.dict(os.environ,environment,clear=True)
        context.start();self.addCleanup(context.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.package = self.root/release.PACKAGE
        (self.package/'.cursor-plugin').mkdir(parents=True)
        (self.package/'skills/macos-cua/service/Resources').mkdir(parents=True)
        self.version('1.2.3')
        subprocess.run(['git','init','-q',str(self.root)],check=True)
        subprocess.run(['git','add','.'],cwd=self.root,check=True)
        subprocess.run(['git','-c','user.name=Fixture','-c','user.email=fixture@example.invalid',
                        'commit','-qm','baseline'],cwd=self.root,check=True)

    def version(self, value):
        for relative in ('plugin.json','.cursor-plugin/plugin.json'):
            (self.package/relative).write_text(json.dumps({'version':value}))
        (self.package/'skills/macos-cua/service/Resources/Info.plist').write_bytes(
            plistlib.dumps({'CFBundleShortVersionString':value}))

    def tracked(self, relative):
        p = self.package/relative;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('new content')
        subprocess.run(['git','add','.'],cwd=self.root,check=True)

    def test_version_drift_is_rejected(self):
        (self.package/'.cursor-plugin/plugin.json').write_text('{"version":"1.2.2"}')
        with self.assertRaisesRegex(RuntimeError,'versions disagree'):
            release.policy(self.root)

    def test_shipped_changes_without_bump_are_rejected(self):
        self.tracked('bin/runtime')
        with self.assertRaisesRegex(RuntimeError,'version increase'):
            release.policy(self.root)
        self.version('1.2.4')
        self.assertEqual(release.policy(self.root)['version'],'1.2.4')

    def test_test_only_changes_need_no_bump(self):
        self.tracked('tests/test_regression.py')
        self.assertEqual(release.policy(self.root)['version'],'1.2.3')

    def test_dirty_or_changed_head_cannot_be_released(self):
        head = release.output(['git','rev-parse','HEAD'],root=self.root)
        release.frozen(self.root,head)
        self.tracked('tests/test_new.py')
        with self.assertRaisesRegex(RuntimeError,'clean committed'):
            release.frozen(self.root,head)
        with self.assertRaisesRegex(RuntimeError,'HEAD changed'):
            release.frozen(self.root,'0'*40)

    def test_missing_ci_base_is_an_error(self):
        with patch.dict(os.environ,{'AGENT_PLUGINS_RELEASE_BASE':'missing-base'}):
            with self.assertRaises(subprocess.CalledProcessError):
                release.policy(self.root)


class VoiceProofTests(unittest.TestCase):
    def rows(self):
        return [dict(session_id='session',kind='user_transcript'),
                dict(session_id='session',kind='tool_result',tool='cua_act',app='Calculator',ok=True),
                dict(session_id='session',kind='startup',phase='response_done',status='ok')]

    def test_requires_ordered_same_session_chain(self):
        self.assertEqual(release.voice_events(self.rows()),'session')
        self.assertIsNone(release.voice_events(list(reversed(self.rows()))))
        rows = self.rows();rows[1]['session_id'] = 'other'
        self.assertIsNone(release.voice_events(rows))
        rows = self.rows();rows[1]['ok'] = False
        self.assertIsNone(release.voice_events(rows))

    def test_error_or_disconnect_invalidates_completion(self):
        for kind in ('realtime_error','ws_closed','tool_error','reconnecting'):
            self.assertIsNone(release.voice_events(self.rows()+[dict(session_id='session',kind=kind)]))


class ReleaseTests(unittest.TestCase):
    def test_concurrent_releases_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)/'release.lock'
            with release.exclusive_release(path):
                with self.assertRaisesRegex(RuntimeError,'another ACU release'):
                    with release.exclusive_release(path):
                        self.fail('second release acquired the lock')
            with release.exclusive_release(path):
                pass

    def run_release(self, directory, *, source_exit=0, sign_only=False):
        args = argparse.Namespace(report=Path(directory)/'receipt.json',
                                  verify_installed=True,sign_only=sign_only)
        identity = {'version':'0.2.32','sha256':'fingerprint'}
        contract = Mock();contract.package_identity.return_value = identity
        def output(command, **kwargs):
            if command[:3] == ['git','rev-parse','HEAD']: return 'a'*40
            if command[:3] == ['codex','plugin','add']:
                return json.dumps({'installedPath':str(Path(directory)/'installed')})
            return 'fixture'
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            return Mock(returncode=source_exit if command[-1:] == ['scripts/check.py'] else 0)
        with patch.object(release,'output',side_effect=output), \
             patch.object(release,'frozen'), \
             patch.object(release,'policy',return_value={'version':'0.2.32'}), \
             patch.object(release,'contract_module',return_value=contract), \
             patch.object(release.subprocess,'run',side_effect=run), \
             patch.object(release,'mcp',return_value=('0.2.32','')), \
             patch.object(release,'voice_smoke',return_value={'session_id':'fresh','audio':True}):
            code = release.release(args)
        return code,json.loads(args.report.read_text()),calls

    def test_failed_source_checks_stop_before_any_live_or_install_step(self):
        with tempfile.TemporaryDirectory() as td:
            code,receipt,calls = self.run_release(td,source_exit=1)
        self.assertEqual(code,1)
        self.assertFalse(receipt['release_ready'])
        self.assertEqual(len(calls),1)
        self.assertEqual(receipt['steps'][0]['exit_code'],1)

    def test_signed_only_never_claims_distribution_readiness(self):
        with tempfile.TemporaryDirectory() as td:
            code,receipt,_ = self.run_release(td,sign_only=True)
        self.assertEqual(code,0)
        self.assertFalse(receipt['release_ready'])
        self.assertTrue(receipt['local_ready'])
        self.assertEqual(receipt['notarization'],'not_verified_signed_only')

    def test_full_success_binds_evidence_to_commit_and_identity(self):
        with tempfile.TemporaryDirectory() as td:
            code,receipt,calls = self.run_release(td)
        self.assertEqual(code,0)
        self.assertTrue(receipt['release_ready'])
        self.assertEqual(receipt['commit'],'a'*40)
        self.assertEqual(receipt['identity']['sha256'],'fingerprint')
        self.assertTrue(all(s['ok'] for s in receipt['steps']))
        self.assertTrue(any(c[:3] == ['xcrun','stapler','validate'] for c in calls))


if __name__ == '__main__':
    unittest.main()
