"""Cursor CLI run capture: official usage from the CLI's own result, hook metadata, refusals, read-back."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime'))
from collector import cursor_runs  # noqa: E402

FIX = Path(__file__).resolve().parent / 'fixtures' / 'cursor'
LAUNCHER = ROOT / 'bin' / 'developer-observability'


def lines(name):
    return (FIX / name).read_text(encoding='utf-8').splitlines(keepends=True)


class SummarizeTests(unittest.TestCase):
    def test_stream_json_success_keeps_usage_and_drops_text(self):
        summary = cursor_runs.summarize(lines('success.stream.jsonl'), [], 0)
        self.assertEqual(summary['usage'], {'uncached_input': 3, 'output': 6, 'cache_read': 12635, 'cache_write': 3136})
        self.assertEqual((summary['session_id'], summary['model_display'], summary['duration_ms']),
                         ('c6b62c6f-7ead-4fd6-9922-e952131177ff', 'Gemini 4 Neon High', 5234))
        self.assertEqual(summary['tool_calls'], 1)
        self.assertIsNone(summary['error_kind'])
        self.assertNotIn('FIXTURE', json.dumps(summary))

    def test_refusals_are_classified_without_an_envelope(self):
        cases = [(lines('usage_limit.stdout'), [], 'usage_limit'),
                 (lines('plan_refusal.stdout'), [], 'plan_refusal'),
                 ([], lines('model_blocked.stderr'), 'model_blocked'),
                 ([], ['Error: Cannot use this model: nope\n'], 'unknown_model')]
        for stdout, stderr, kind in cases:
            summary = cursor_runs.summarize(stdout, stderr, 1)
            self.assertFalse(summary['envelope'])
            self.assertIsNone(summary['usage'])
            self.assertEqual(summary['error_kind'], kind, (stdout, stderr))
            self.assertTrue(summary['error_message'])
            self.assertNotIn('\x1b', summary['error_message'])

    def test_plain_text_after_an_envelope_is_not_trusted_as_an_error(self):
        summary = cursor_runs.summarize(lines('success.json') + ['ActionRequiredError: out of usage\n'], [], 0)
        self.assertIsNone(summary['error_kind'])


class StoreAndReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / 'usage.sqlite3')

    def tearDown(self):
        self.tmp.cleanup()

    def test_hook_rows_and_capture_merge_into_one_run(self):
        session = 'c6b62c6f-7ead-4fd6-9922-e952131177ff'
        base = {'conversation_id': session, 'generation_id': 'g1', 'model': 'gemini-4-neon-high',
                'model_id': 'gemini-4-neon-high', 'model_params': [{'id': 'effort', 'value': 'high'}],
                'cursor_version': '2026.11.19-f005e9d', 'workspace_roots': ['/work/project'],
                'user_email': 'someone@example.com', 'transcript_path': '/secret/transcript.jsonl'}
        cursor_runs.record_hook(self.db, {**base, 'hook_event_name': 'sessionStart', 'session_id': session,
                                          'composer_mode': 'agent'}, environ={}, at='2026-11-25T03:00:00Z')
        cursor_runs.record_hook(self.db, {**base, 'hook_event_name': 'stop', 'status': 'completed', 'loop_count': 0},
                                environ={}, at='2026-11-25T03:00:06Z')
        cursor_runs.record_hook(self.db, {**base, 'hook_event_name': 'beforeSubmitPrompt', 'prompt': 'FIXTURE'}, environ={})
        summary = cursor_runs.summarize(lines('success.stream.jsonl'), [], 0)
        summary.update(requested_model='gemini-4-neon-high', elapsed_seconds=6.1, output_format='stream-json')
        run_id = cursor_runs.record_run(self.db, '11111111-1111-4111-8111-111111111111', 'capture', summary,
                                        started_at='2026-11-25T03:00:00Z', finished_at='2026-11-25T03:00:06Z')
        result = cursor_runs.read_runs(self.db, run_id=run_id)
        row = result['runs'][0]
        self.assertEqual(row['source'], 'capture+hook')
        self.assertEqual((row['model'], row['client'], row['status'], row['exit_code']),
                         ('gemini-4-neon-high', 'cli', 'success', 0))
        self.assertEqual((row['tokens_in'], row['tokens_out'], row['tokens_total']), (3 + 12635 + 3136, 6, 15780))
        self.assertEqual((row['tokens_source'], row['tokens_method']), ('official', 'harness_usage'))
        self.assertEqual((row['cost'], row['cost_source'], row['model_version'], row['model_version_source']),
                         (None, 'not_reported', None, 'not_reported'))
        self.assertEqual((row['wall_ms'], row['wall_source']), (5234, 'cli_result'))
        self.assertEqual(row['hook_events'], ['sessionStart', 'stop'])
        stored = Path(self.db).read_bytes()
        for leaked in (b'someone@example.com', b'transcript.jsonl', b'FIXTURE'):
            self.assertNotIn(leaked, stored)
        self.assertEqual(result['totals']['official_tokens_total'], 15780)

    def test_hook_only_refusal_reads_back_with_error_and_no_tokens(self):
        session = 'aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa'
        cursor_runs.record_hook(self.db, {'hook_event_name': 'sessionEnd', 'session_id': session, 'reason': 'error',
                                          'duration_ms': 1200, 'model': 'gemini-4-neon-high',
                                          'error_message': "ActionRequiredError: You're out of usage. Switch to Auto.",
                                          'cursor_version': '2026.11.19-f005e9d'}, environ={}, at='2026-11-25T03:10:00Z')
        row = cursor_runs.read_runs(self.db, since='2026-11-25T03:00:00Z', until='2026-11-25T04:00:00Z')['runs'][0]
        self.assertEqual((row['status'], row['error_kind'], row['tokens_source'], row['tokens_total']),
                         ('error', 'usage_limit', 'none', None))
        self.assertEqual((row['wall_ms'], row['wall_source']), (1200, 'hook'))
        self.assertEqual(cursor_runs.read_runs(self.db, since='2026-11-26T00:00:00Z')['runs'], [])

    def test_hook_entry_never_fails_and_prints_empty_json(self):
        out = io.StringIO()
        old = sys.stdout
        sys.stdout = out
        try:
            code = cursor_runs.hook_main(io.StringIO('not json'), self.db)
        finally:
            sys.stdout = old
        self.assertEqual((code, out.getvalue().strip()), (0, '{}'))


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = str(self.dir / 'usage.sqlite3')
        self.env = {**os.environ, 'OBSERVABILITY_PYTHON': sys.executable}

    def tearDown(self):
        self.tmp.cleanup()

    def fake_agent(self, stdout_file, stderr_file=None, code=0):
        script = self.dir / 'agent'
        body = f'#!/bin/sh\necho "$@" > "{self.dir}/argv"\ncat "{FIX / stdout_file}"\n'
        if stderr_file:
            body += f'cat "{FIX / stderr_file}" >&2\n'
        script.write_text(body + f'exit {code}\n')
        script.chmod(0o755)
        return str(script)

    def run_launcher(self, *args, agent=None):
        env = {**self.env, **({'CURSOR_AGENT_BIN': agent} if agent else {})}
        return subprocess.run([str(LAUNCHER), *args], env=env, capture_output=True, text=True, timeout=30)

    def test_capture_success_then_read_back(self):
        agent = self.fake_agent('success.stream.jsonl')
        run = self.run_launcher('cursor-capture', '--database', self.db, '--quiet-events', '--',
                                '-m', 'gemini-4-neon-high', 'hello', agent=agent)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout, '')
        self.assertEqual((self.dir / 'argv').read_text().split()[:3], ['-p', '--output-format', 'stream-json'])
        receipt = json.loads(run.stderr.strip().splitlines()[-1])
        back = json.loads(self.run_launcher('cursor-usage', '--database', self.db, '--run', receipt['run_id']).stdout)
        row = back['runs'][0]
        self.assertEqual((row['requested_model'], row['tokens_source'], row['tokens_total']), ('gemini-4-neon-high', 'official', 15780))

    def test_capture_usage_limit_refusal_keeps_exit_code(self):
        agent = self.fake_agent('usage_limit.stdout', code=1)
        run = self.run_launcher('cursor-capture', '--database', self.db, '--', '-m', 'gemini-4-neon-high', 'hi', agent=agent)
        self.assertEqual(run.returncode, 1)
        receipt = json.loads(run.stderr.strip().splitlines()[-1])
        self.assertEqual((receipt['error_kind'], receipt['tokens_source']), ('usage_limit', 'none'))
        row = json.loads(self.run_launcher('cursor-usage', '--database', self.db).stdout)['runs'][0]
        self.assertEqual((row['status'], row['exit_code'], row['error_kind'], row['cost']), ('error', 1, 'usage_limit', None))

    def test_ingest_saved_json_and_plan_refusal(self):
        back = json.loads(self.run_launcher('cursor-usage', '--database', self.db, '--ingest', str(FIX / 'success.json'),
                                            '--exit-code', '0').stdout)
        self.assertEqual(back['runs'][0]['tokens_total'], 5749 + 9728 + 25)
        back = json.loads(self.run_launcher('cursor-usage', '--database', self.db, '--ingest', str(FIX / 'plan_refusal.stdout'),
                                            '--exit-code', '1').stdout)
        self.assertEqual(back['runs'][0]['error_kind'], 'plan_refusal')

    def test_hook_entry_through_the_launcher(self):
        payload = json.dumps({'hook_event_name': 'stop', 'conversation_id': 'bbbbbbbb-1111-4111-8111-bbbbbbbbbbbb',
                              'status': 'completed', 'model': 'composer-2.5', 'cursor_version': '2026.11.19-f005e9d'})
        env = {**self.env, 'HOME': str(self.dir)}
        run = subprocess.run([str(LAUNCHER), 'cursor-hook'], input=payload, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual((run.returncode, run.stdout.strip()), (0, '{}'))
        db = self.dir / '.local/share/developer-observability/task-usage.sqlite3'
        row = cursor_runs.read_runs(db)['runs'][0]
        self.assertEqual((row['model'], row['status'], row['client']), ('composer-2.5', 'success', 'cli'))

    def test_cursor_hooks_manifest(self):
        hooks = json.loads((ROOT / 'hooks/cursor-hooks.json').read_text())
        command = '"$CURSOR_PLUGIN_ROOT/bin/developer-observability" cursor-hook'
        self.assertEqual(hooks, {'version': 1, 'hooks': {event: [{'command': command, 'timeout': 5}]
                                                         for event in ('sessionStart', 'stop', 'sessionEnd')}})
        self.assertEqual(json.loads((ROOT / '.cursor-plugin/plugin.json').read_text())['hooks'], './hooks/cursor-hooks.json')


if __name__ == '__main__':
    unittest.main()
