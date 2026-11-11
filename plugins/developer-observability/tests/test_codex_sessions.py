from datetime import date
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from collector.codex_sessions import effective_config, sample


class SessionTests(unittest.TestCase):
    def test_effective_config_requires_matching_session_and_unambiguous_context(self):
        with tempfile.TemporaryDirectory() as directory:
            day = Path(directory) / '2026/11/05'
            day.mkdir(parents=True)
            thread = '12345678-1234-4234-8234-123456789012'
            path = day / f'rollout-{thread}.jsonl'
            rows = [{'type': 'session_meta', 'payload': {'id': thread}},
                    {'type': 'turn_context', 'payload': {'model': 'gpt-6-luna', 'effort': 'low'}}]
            path.write_text('\n'.join(map(json.dumps, rows)))
            self.assertEqual(effective_config(thread, directory, date(2026, 11, 5)), ('gpt-6-luna', 'low'))
            rows.append({'type': 'turn_context', 'payload': {'model': 'gpt-6-luna', 'effort': 'high'}})
            path.write_text('\n'.join(map(json.dumps, rows)))
            self.assertEqual(effective_config(thread, directory, date(2026, 11, 5)), (None, None))
            rows[0]['payload']['id'] = 'other'
            path.write_text('\n'.join(map(json.dumps, rows[:2])))
            self.assertEqual(effective_config(thread, directory, date(2026, 11, 5)), (None, None))
    def test_latest_turn_snapshot_and_no_prompt_exposure(self):
        with tempfile.TemporaryDirectory() as directory:
            day = Path(directory) / '2026/11/05'
            day.mkdir(parents=True)
            turn = '12345678-1234-4234-8234-123456789012'
            rows = [
                {'type': 'turn_context', 'payload': {'turn_id': turn, 'model': 'gpt-6-luna', 'effort': 'low'}},
                {'type': 'response_item', 'payload': {'content': 'private material'}},
                {'type': 'token_usage_record', 'payload': {'turn_id': turn, 'turn_token_usage': {'input_tokens': 10, 'cached_input_tokens': 4, 'output_tokens': 1}}},
                {'type': 'token_usage_record', 'payload': {'turn_id': turn, 'turn_token_usage': {'input_tokens': 20, 'cached_input_tokens': 5, 'output_tokens': 2}}},
                {'type': 'event_msg', 'payload': {'turn_id': turn, 'type': 'task_complete'}},
            ]
            (day / 'a.jsonl').write_text('\n'.join(map(json.dumps, rows)))
            result = sample(directory, date(2026, 11, 5))
            group = result['groups'][0]
            self.assertEqual((group['turns'], group['completed'], group['input_tokens']), (1, 1, 20))
            self.assertEqual((group['model'], group['effort']), ('gpt-6-luna', 'low'))
            self.assertNotIn('private material', json.dumps(result))
            self.assertEqual(result['acceptance'], 'unknown')

    def test_conflicting_settings_and_invalid_usage_are_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            day = Path(directory) / '2026/11/05'
            day.mkdir(parents=True)
            turn = '12345678-1234-4234-8234-123456789012'
            rows = [{'type': 'turn_context', 'payload': {'turn_id': turn, 'model': 'gpt-6-luna', 'effort': 'low'}},
                    {'type': 'turn_context', 'payload': {'turn_id': turn, 'model': 'gpt-6-luna', 'effort': 'high'}},
                    {'type': 'token_usage_record', 'payload': {'turn_id': turn, 'turn_token_usage': {'input_tokens': 5, 'cached_input_tokens': 6, 'output_tokens': 1}}}]
            (day / 'a.jsonl').write_text('\n'.join(map(json.dumps, rows)))
            group = sample(directory, date(2026, 11, 5))['groups'][0]
            self.assertIsNone(group['model'])
            self.assertEqual(group['usage_known'], 0)

    def test_file_limit_and_large_files_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            day = Path(directory) / '2026/11/05'
            day.mkdir(parents=True)
            for i in range(31):
                (day / f'{i}.jsonl').write_text('')
            result = sample(directory, date(2026, 11, 5))
            self.assertEqual(result['candidate_files'], 31)
            self.assertEqual(result['files_examined'], 30)
            huge = day / 'huge.jsonl'
            with huge.open('wb') as handle:
                handle.truncate(8 * 1024 * 1024 + 1)
            result = sample(directory, date(2026, 11, 5))
            self.assertEqual(result['files_skipped_large'], 1)
            self.assertEqual(result['files_examined'], 29)


if __name__ == '__main__':
    unittest.main()
