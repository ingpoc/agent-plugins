"""Release policy and fail-closed orchestration, without touching installed apps."""
import argparse
import contextlib
import io
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
            plistlib.dumps({'CFBundleShortVersionString':value, 'CFBundleVersion':value}))

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

    def test_committing_without_a_bump_cannot_evade_release_base(self):
        base = release.output(['git','rev-parse','HEAD'],root=self.root)
        self.tracked('bin/runtime')
        subprocess.run(['git','-c','user.name=Fixture','-c','user.email=fixture@example.invalid',
                        'commit','-qm','unversioned runtime'],cwd=self.root,check=True)
        with self.assertRaisesRegex(RuntimeError,'version increase'):
            release.policy(self.root,base=base)

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

    def test_current_session_phase_completion_is_accepted(self):
        rows = self.rows()
        rows[2]['kind'] = 'session_phase'
        self.assertEqual(release.voice_events(rows), 'session')

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

    def run_release(self, directory, *, source_exit=0, sign_only=False, microphone_exit=0, skip_voice_smoke=False):
        args = argparse.Namespace(report=Path(directory)/'receipt.json',
                                  verify_installed=True,sign_only=sign_only,hygiene_override=False,skip_voice_smoke=skip_voice_smoke)
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
            code = source_exit if command[-1:] == ['scripts/check.py'] else microphone_exit if command[-1:] == ['--preflight-mic'] else 0
            return Mock(returncode=code)
        def smoke(*_args):
            self.assertEqual(json.loads(args.report.read_text())['stage'], 'voice-smoke')
            return {'session_id':'fresh','audio':True}
        with patch.object(release,'output',side_effect=output), \
             patch.object(release,'APP',Path(directory)/'app'), \
             patch.object(release,'hygiene_gate',return_value={'checked':False}), \
             patch.object(release,'frozen'), \
             patch.object(release,'policy',return_value={'version':'0.2.32'}), \
             patch.object(release,'contract_module',return_value=contract), \
             patch.object(release.subprocess,'run',side_effect=run), \
             patch.object(release,'mcp',return_value=('0.2.32','')), \
             patch.object(release,'voice_smoke',side_effect=smoke):
            code = release.release(args)
        return code,json.loads(args.report.read_text()),calls

    def test_failed_source_checks_stop_before_any_live_or_install_step(self):
        with tempfile.TemporaryDirectory() as td:
            code,receipt,calls = self.run_release(td,source_exit=1)
        self.assertEqual(code,1)
        self.assertFalse(receipt['release_ready'])
        self.assertEqual(len(calls),1)
        self.assertEqual(receipt['steps'][0]['exit_code'],1)

    def test_failed_source_checks_do_not_request_voice(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as td, contextlib.redirect_stdout(output):
            code, _, _ = self.run_release(td, source_exit=1)
        self.assertEqual(code, 1)
        self.assertNotIn('VOICE SMOKE', output.getvalue())
        self.assertNotIn('Prepare to say', output.getvalue())

    def test_failed_installed_microphone_does_not_request_voice(self):
        with tempfile.TemporaryDirectory() as td:
            code, receipt, _ = self.run_release(td, microphone_exit=1)
        self.assertEqual(code, 1)
        self.assertFalse(receipt['release_ready'])
        self.assertEqual(receipt['steps'][-1]['name'], 'microphone')
        self.assertNotIn('voice', receipt)

    def test_skipped_voice_smoke_keeps_release_not_ready(self):
        with tempfile.TemporaryDirectory() as td:
            code, receipt, _ = self.run_release(td, skip_voice_smoke=True)
        self.assertEqual(code, 1)
        self.assertFalse(receipt['release_ready'])
        self.assertFalse(receipt['voice']['ok'])
        self.assertEqual(receipt['stage'], 'voice-smoke')

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
        self.assertEqual(receipt['native_smoke']['version'],'0.2.32')
        self.assertEqual(receipt['existing_client'],'not_checked')
        self.assertIsNone(receipt['microphone_preflight']['ok'])
        self.assertTrue(all(s['ok'] for s in receipt['steps']))
        self.assertTrue(any(c[:3] == ['xcrun','stapler','validate'] for c in calls))


class HygieneGateTests(unittest.TestCase):
    TARGET = str(release.owning_clone_root(release.ROOT))
    OTHER = str(release.ROOT.parent / 'agent-plugins-other')
    ROWS = [
        {'check':'worktree-placement','status':'FAIL','items':[
            '/Users/u/Library/Caches/agent-plugins-origin-ci/wt',
            '/Users/u/code/wt/agent-plugins-old']},
        {'check':'local-only-work','status':'FAIL','items':[
            f'{TARGET}:codex/x: 2 local-only commit(s)',
            f'{OTHER}:main: 4 local-only commit(s)',
            f'{OTHER}: 1 dirty file(s) remain in place']},
        {'check':'duplicate-clones','status':'PASS','items':['/Users/u/code/agent-plugins']},
        {'check':'worktree-age','status':'FAIL','items':['/Users/u/code/agent-plugins']},
    ]

    def test_only_gated_fails_naming_the_release_checkout(self):
        found = release.hygiene_failures(self.ROWS)
        self.assertEqual(len(found),1)
        self.assertIn(f'local-only-work: {self.TARGET}:codex/x',found[0])
        self.assertNotIn(self.OTHER, found[0])

    def tool(self, directory, rows):
        tool = Path(directory)/'hygiene.py'
        tool.write_text('import json,sys\nprint(json.dumps(%r))\n' % (rows,))
        return tool

    def test_gate_blocks_without_override_and_records_override(self):
        with tempfile.TemporaryDirectory() as td, \
             patch.dict(os.environ,{'AGENT_PLUGINS_HYGIENE':str(self.tool(td,self.ROWS))}):
            report = {}
            with self.assertRaisesRegex(RuntimeError,'hygiene check fails for agent-plugins'):
                release.hygiene_gate(False,report)
            self.assertEqual(len(report['hygiene']['failures']),1)
            result = release.hygiene_gate(True)
        self.assertTrue(result['override'])
        self.assertEqual(len(result['failures']),1)

    def test_clean_machine_passes(self):
        with tempfile.TemporaryDirectory() as td, \
             patch.dict(os.environ,{'AGENT_PLUGINS_HYGIENE':str(self.tool(td,self.ROWS[2:3]))}):
            self.assertEqual(release.hygiene_gate(False)['failures'],[])

    def test_failed_hygiene_stops_before_source_checks(self):
        args = argparse.Namespace(verify_installed=True,sign_only=False,hygiene_override=False)
        with tempfile.TemporaryDirectory() as td:
            args.report = Path(td)/'receipt.json'
            with patch.object(release,'output',return_value='a'*40), \
                 patch.object(release,'frozen'), \
                 patch.object(release,'policy',return_value={}), \
                 patch.object(release,'hygiene_gate',side_effect=RuntimeError('hygiene check fails')), \
                 patch.object(release.subprocess,'run') as run:
                code = release.release(args)
            receipt = json.loads(args.report.read_text())
        self.assertEqual(code,1)
        self.assertEqual(receipt['steps'],[])
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
