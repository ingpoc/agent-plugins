import importlib.util
import json
from pathlib import Path
import unittest
import tempfile
import os
import sqlite3
import subprocess

path = Path(__file__).resolve().parents[1] / 'runtime/collector/codex_usage.py'
spec = importlib.util.spec_from_file_location('codex_usage', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class CodexUsageTests(unittest.TestCase):
    def test_task_detail_and_external_verification_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / 'usage.sqlite3'
            task = '12345678-1234-4234-8234-123456789012'
            summary = module.summarize([json.dumps({'type': 'thread.started', 'thread_id': task}),
                                        json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 3, 'cached_input_tokens': 1, 'output_tokens': 2}})])
            summary['process_exit_code'] = 0
            module.record(db, task, 'coding', summary)
            scenario, skill, artifact, verifier = [root / name for name in ('scenario', 'skill', 'artifact', 'verifier')]
            for file in (scenario, skill, artifact):
                file.write_text('fixture')
            verifier.write_text('#!/bin/sh\ntest "$(cat "$1")" = fixture\n')
            verifier.chmod(0o755)
            launcher = path.parents[2] / 'bin/developer-observability'
            command = [str(launcher), 'codex-verify', '--database', str(db), '--task-id', task,
                       '--scenario', str(scenario), '--skill', str(skill), '--artifact', str(artifact), '--verifier', str(verifier)]
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            detail = module.task_detail(db, task)
            self.assertEqual(detail['thread_id'], task)
            self.assertEqual(detail['verification']['result'], 'pass')
            self.assertEqual(detail['acceptance'], 'unknown')
            self.assertIn('child_coverage_unknown', detail['reasons'])
            artifact.write_text('changed')
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertEqual(module.task_detail(db, task)['verification']['result'], 'pass')

    def test_counts_only_usage_without_content(self):
        lines = [json.dumps({'type': 'thread.started', 'thread_id': '12345678-1234-4234-8234-123456789012'}),
                 json.dumps({'type': 'item.completed', 'text': 'private task text'}),
                 json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 100,
                            'cached_input_tokens': 40, 'output_tokens': 5}})]
        result = module.summarize(lines)
        self.assertEqual(result['usage']['input_tokens'], 100)
        self.assertEqual(result['usage']['cached_input_tokens'], 40)
        self.assertIsNone(result['usage']['reasoning_output_tokens'])
        self.assertNotIn('private task text', json.dumps(result))
        self.assertEqual(result['acceptance'], 'unknown')
        self.assertEqual(result['thread_id'], '12345678-1234-4234-8234-123456789012')

    def test_incomplete_failure_and_invalid_usage_remain_visible(self):
        result = module.summarize(['not json', '{"type":"turn.failed"}',
                                   '{"type":"turn.completed","usage":{"output_tokens":true}}'])
        self.assertEqual((result['failed_turns'], result['completed_turns'], result['invalid_events']), (1, 1, 1))
        self.assertIsNone(result['usage']['output_tokens'])
        self.assertIsNone(result['cost'])

    def test_partial_multiturn_counts_are_unknown(self):
        result = module.summarize(['{"type":"turn.completed","usage":{"input_tokens":10}}',
                                   '{"type":"turn.completed","usage":{}}'])
        self.assertEqual(result['completed_turns'], 2)
        self.assertIsNone(result['usage']['input_tokens'])

    def test_impossible_cached_count_is_not_reported(self):
        result = module.summarize(['{"type":"turn.completed","usage":{"input_tokens":5,"cached_input_tokens":6,"output_tokens":3}}'])
        self.assertEqual(result['invalid_events'], 1)
        self.assertTrue(all(value is None for value in result['usage'].values()))

    def test_duplicate_and_failed_turns_do_not_produce_totals(self):
        event = json.dumps({'type': 'turn.completed', 'turn_id': 'turn-1', 'usage': {'input_tokens': 10}})
        self.assertIsNone(module.summarize([event, event])['usage']['input_tokens'])
        self.assertIsNone(module.summarize([event, '{"type":"turn.failed"}'])['usage']['input_tokens'])

    def test_record_rejects_unrecognized_or_private_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'usage.sqlite3'
            summary = module.summarize(['{"type":"turn.completed","usage":{"input_tokens":10}}'])
            summary['prompt'] = 'private'
            with self.assertRaises(ValueError):
                module.record(db, '12345678-1234-4234-8234-123456789012', 'coding', summary)
            self.assertFalse(db.exists())

    def test_record_is_idempotent_and_rejects_conflicting_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'usage.sqlite3'
            task = '12345678-1234-4234-8234-123456789012'
            summary = module.summarize(['{"type":"turn.completed","usage":{"input_tokens":10}}'])
            module.record(db, task, 'coding', summary)
            module.record(db, task, 'coding', summary)
            self.assertEqual(module.task_overview(db)['tasks'], 1)
            self.assertEqual(module.task_overview(db)['categories'][0]['input_tokens'], 10)
            self.assertEqual(module.task_overview(db)['categories'][0]['known_output_tasks'], 0)
            with self.assertRaises(ValueError):
                module.record(db, task, 'research', summary)

    def test_absent_database_does_not_create_a_store(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'missing.sqlite3'
            self.assertEqual(module.task_overview(db)['status'], 'not_captured')
            self.assertFalse(db.exists())

    def test_effective_configuration_groups_output_tokens_and_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'usage.sqlite3'
            base = {'model': 'gpt-6-luna', 'effort': 'low', 'check_status': 'passed',
                    'usage': {'input_tokens': 100, 'output_tokens': 50}}
            first = {**base, 'elapsed_seconds': 2.5}
            second = {**base, 'usage': {'input_tokens': 120, 'output_tokens': None}, 'elapsed_seconds': None}
            module.record(db, '12345678-1234-4234-8234-123456789012', 'coding', first)
            module.record(db, '22345678-1234-4234-8234-123456789012', 'coding', second)
            group = module.task_overview(db)['effective_configurations'][0]
            self.assertEqual((group['tasks'], group['output_tokens'], group['known_output_tasks']), (2, 50, 1))
            self.assertEqual((group['elapsed_seconds'], group['known_duration_tasks']), (2.5, 1))

    def test_launcher_preserves_exit_and_stores_only_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / 'codex'
            fake.write_text('#!/bin/sh\nprintf \'%s\\n\' \'{"type":"item.completed","text":"private-content"}\' \'{"type":"turn.completed","usage":{"input_tokens":17}}\'\nexit 7\n')
            fake.chmod(0o755)
            db = root / 'usage.sqlite3'
            launcher = path.parents[2] / 'bin/developer-observability'
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'])
            result = subprocess.run([str(launcher), 'codex-capture', '--database', str(db),
                                     '--category', 'coding', '--', '--model', 'gpt-6-luna', '-c',
                                     'model_reasoning_effort="low"', 'example'], capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 7)
            self.assertIn('private-content', result.stdout)
            with sqlite3.connect(db) as connection:
                row = connection.execute('SELECT summary FROM task_usage').fetchone()[0]
            self.assertNotIn('private-content', row)
            self.assertEqual(json.loads(row)['process_exit_code'], 7)
            self.assertEqual(json.loads(row)['requested_model'], 'gpt-6-luna')
            self.assertEqual(json.loads(row)['requested_effort'], 'low')
            self.assertIsNone(json.loads(row)['model'])
            self.assertEqual(module.task_overview(db)['categories'][0]['input_tokens'], 17)

    def test_quiet_capture_does_not_emit_task_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / 'codex'
            fake.write_text('#!/bin/sh\nprintf \'%s\\n\' \'{"type":"item.completed","text":"private-content"}\' \'{"type":"turn.completed","usage":{"input_tokens":17}}\'\n')
            fake.chmod(0o755)
            launcher = path.parents[2] / 'bin/developer-observability'
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'])
            result = subprocess.run([str(launcher), 'codex-capture', '--quiet-events', '--database', str(root/'usage.sqlite3'), '--', 'example'],
                                    capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 0)
            self.assertNotIn('private-content', result.stdout + result.stderr)

    def test_capture_persistence_failure_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / 'codex'
            fake.write_text('#!/bin/sh\nprintf \'%s\\n\' \'{"type":"turn.completed","usage":{"input_tokens":17}}\'\n')
            fake.chmod(0o755)
            launcher = path.parents[2] / 'bin/developer-observability'
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'])
            result = subprocess.run([str(launcher), 'codex-capture', '--database', str(root), '--', 'example'],
                                    capture_output=True, text=True, env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('usage capture failed', result.stderr)

    def test_caller_check_failure_is_recorded_and_fails_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / 'codex'
            fake.write_text('#!/bin/sh\nprintf \'%s\\n\' \'{"type":"turn.completed","usage":{"input_tokens":17}}\'\n')
            fake.chmod(0o755)
            check = root / 'verify'
            check.write_text('#!/bin/sh\nexit 1\n')
            check.chmod(0o755)
            db = root / 'usage.sqlite3'
            launcher = path.parents[2] / 'bin/developer-observability'
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'])
            result = subprocess.run([str(launcher), 'codex-capture', '--database', str(db), '--verify', str(check),
                                     '--category', 'coding', '--', 'example'], capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 1)
            with sqlite3.connect(db) as connection:
                summary = json.loads(connection.execute('SELECT summary FROM task_usage').fetchone()[0])
            self.assertEqual(summary['check_status'], 'failed')
            self.assertEqual(summary['acceptance'], 'unknown')
            self.assertEqual(module.task_overview(db)['categories'][0]['checks_failed'], 1)

    def test_missing_completion_never_counts_as_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / 'codex'
            fake.write_text('#!/bin/sh\nprintf \'%s\\n\' \'{"type":"turn.started"}\'\n')
            fake.chmod(0o755)
            check = root / 'verify'
            check.write_text('#!/bin/sh\nexit 0\n')
            check.chmod(0o755)
            db = root / 'usage.sqlite3'
            launcher = path.parents[2] / 'bin/developer-observability'
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'])
            result = subprocess.run([str(launcher), 'codex-capture', '--database', str(db), '--verify', str(check),
                                     '--', 'example'], capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 1)
            with sqlite3.connect(db) as connection:
                summary = json.loads(connection.execute('SELECT summary FROM task_usage').fetchone()[0])
            self.assertEqual(summary['check_status'], 'not_run')


if __name__ == '__main__':
    unittest.main()
