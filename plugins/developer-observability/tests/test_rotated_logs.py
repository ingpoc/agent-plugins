"""0.1.9: the collector reads Samantha's rotated logs (agent-computer-use 0.2.86: <name>.N.<ext>.gz, N = 1 … 5)."""
import gzip
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime'))
from collector.sources import iter_spec, known_sources, rotated_logs  # noqa: E402


def row(i, **extra):
    return json.dumps({'ts': 1793800000.0 + i, 'kind': 'tool_call', 'session_id': 's1', 'tool': 'state', 'i': i, **extra}) + '\n'


class RotatedLogTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix='devobs-rot-'))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.cache = self.home / '.cache/macos-cua'
        self.cache.mkdir(parents=True)
        with gzip.open(self.cache / 'samantha-activity.2.jsonl.gz', 'wt') as fh:
            fh.write(row(0) + row(1, kind='tool_error', status='error', error='target_missing'))
        with gzip.open(self.cache / 'samantha-activity.1.jsonl.gz', 'wt') as fh:
            fh.write(row(2))
        (self.cache / 'samantha-activity.jsonl').write_text(row(3))
        with gzip.open(self.cache / 'voice.1.log.gz', 'wt') as fh:
            fh.write('2026-11-04T06:00:00Z [startup] phase=mic_preflight status=fail error=mic open failed\n')
        (self.cache / 'voice.log').write_text('2026-11-04T06:01:00Z [startup] phase=audio_ready status=ok\n')
        for other in ('cua-service.sock.lock', 'island-events.jsonl', 'samantha-activity.jsonl.lock'):
            (self.cache / other).write_text('x')

    def test_rotated_copies_are_known_sources_oldest_first_under_one_source_id(self):
        with patch.dict(os.environ, {'HOME': str(self.home)}):
            specs = [s for s in known_sources() if s['project'] == 'samantha']
        names = [Path(s['path']).name for s in specs]
        self.assertEqual(names, ['samantha-activity.2.jsonl.gz', 'samantha-activity.1.jsonl.gz', 'samantha-activity.jsonl',
                                 'voice.1.log.gz', 'voice.log'])
        self.assertEqual({s.get('source') or f"samantha:{Path(s['path']).name}" for s in specs},
                         {'samantha:samantha-activity.jsonl', 'samantha:voice.log'})
        self.assertEqual(rotated_logs(self.cache / 'island-events.jsonl'), [], 'only the rotated names, nothing else')

    def test_rows_inside_gz_files_are_collected(self):
        with patch.dict(os.environ, {'HOME': str(self.home)}):
            specs = [s for s in known_sources() if s['project'] == 'samantha']
        stats = {}
        events = [e for s in specs for e in iter_spec(s, stats)]
        text = json.dumps(events)
        self.assertEqual(len([e for e in events if 'samantha-activity' in json.dumps(e)]), 4, 'all four rows: .2.gz, .1.gz, live')
        self.assertIn('target_missing', text, 'an error that only exists in .2.jsonl.gz')
        self.assertIn('mic_preflight', text, 'an error that only exists in voice.1.log.gz')
        self.assertFalse(any(stats.get(k, {}).get('io_errors') for k in stats))


if __name__ == '__main__':
    unittest.main()
