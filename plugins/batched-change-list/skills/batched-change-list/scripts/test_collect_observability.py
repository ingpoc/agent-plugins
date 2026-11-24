import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import collect_observability as helper
from collect_observability import collect


def snapshot(generated_at, findings, coverage=None, collector_stats=None):
    document = {'generated_at': generated_at, 'snapshot_id': 'snap-1', 'findings': findings,
                'coverage': coverage or {}}
    if collector_stats is not None:
        document['collector_stats'] = collector_stats
    return document


def stamp(delta):
    return (datetime.now(timezone.utc) + delta).isoformat()


def coverage_for(directory, rel='events.json', status='collected'):
    directory = Path(directory).expanduser().resolve()
    source = f'{directory.name}:{rel}'
    return {source: status}, {source: {'project_dir': str(directory)}}


class FakeAPI:
    def __init__(self, runtime, collected):
        self.runtime = Path(runtime)
        self.collected = Path(collected)
        self.diagnoses = 0

    def diagnose(self, project_id):
        self.diagnoses += 1
        return {'freshness': 'fresh', 'snapshot_id': 'snap-1'}

    def issues(self, project_id, status, limit, cursor):
        return {'issues': [
            {'issue_id': 'primary:item', 'headline': 'Primary finding'},
            {'issue_id': 'collector:item', 'headline': 'Collector finding'},
        ], 'next_cursor': None}


class CollectObservabilityTests(unittest.TestCase):
    def setup_roots(self, collector_stamp=None, project_dir=None):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        runtime = root / 'runtime'
        collected = root / 'collected'
        runtime.mkdir()
        collected.mkdir()
        (runtime / 'findings.json').write_text(json.dumps(snapshot(
            stamp(timedelta(seconds=-10)), [{'finding_key': 'primary:item'}])))
        if collector_stamp:
            coverage_dir = Path(project_dir) if project_dir is not None else root
            coverage, stats = coverage_for(coverage_dir)
            (collected / 'findings.json').write_text(json.dumps(snapshot(
                collector_stamp, [{'finding_key': 'collector:item'}], coverage, stats)))
        self.addCleanup(temp.cleanup)
        return root, runtime, collected

    def test_stale_collector_snapshot_runs_even_when_primary_is_fresh(self):
        root, _, collected = self.setup_roots(stamp(timedelta(seconds=-600)))
        api = FakeAPI(root / 'runtime', collected)
        calls = []
        collect('demo', str(root), backend_loader=lambda _: ({}, api),
                collector_runner=lambda *args: calls.append(args))
        self.assertEqual(len(calls), 1)

    def test_missing_collector_snapshot_runs(self):
        root, _, collected = self.setup_roots()
        api = FakeAPI(root / 'runtime', collected)
        calls = []
        collect('demo', str(root), backend_loader=lambda _: ({}, api),
                collector_runner=lambda *args: calls.append(args))
        self.assertEqual(len(calls), 1)

    def test_fresh_collector_does_not_run_even_if_primary_is_old(self):
        root, runtime, collected = self.setup_roots(stamp(timedelta(seconds=-10)))
        (runtime / 'findings.json').write_text(json.dumps(snapshot(
            stamp(timedelta(seconds=-600)), [{'finding_key': 'primary:item'}])))
        api = FakeAPI(runtime, collected)
        calls = []
        collect('demo', str(root), backend_loader=lambda _: ({}, api),
                collector_runner=lambda *args: calls.append(args))
        self.assertEqual(calls, [])

    def test_fresh_snapshot_for_another_project_reports_insufficient_coverage(self):
        root, _, collected = self.setup_roots(stamp(timedelta(seconds=-10)))
        other = root / 'another-project'
        other.mkdir()
        coverage, stats = coverage_for(other)
        (collected / 'findings.json').write_text(json.dumps(snapshot(
            stamp(timedelta(seconds=-10)), [{'finding_key': 'collector:item'}], coverage, stats)))
        api = FakeAPI(root / 'runtime', collected)
        calls = []
        with self.assertRaisesRegex(ValueError, 'fresh collector snapshot does not cover requested --project-dir'):
            collect('demo', str(root), str(root), backend_loader=lambda _: ({}, api),
                    collector_runner=lambda *args: calls.append(args))
        self.assertEqual(calls, [])

    def test_fresh_snapshot_for_same_basename_different_path_reports_insufficient_coverage(self):
        root, _, collected = self.setup_roots(stamp(timedelta(seconds=-10)))
        first = Path(tempfile.mkdtemp()) / 'same'
        other = Path(tempfile.mkdtemp()) / 'same'
        first.mkdir(); other.mkdir()
        self.addCleanup(lambda: __import__('shutil').rmtree(first.parent, ignore_errors=True))
        self.addCleanup(lambda: __import__('shutil').rmtree(other.parent, ignore_errors=True))
        coverage, stats = coverage_for(first)
        (collected / 'findings.json').write_text(json.dumps(snapshot(
            stamp(timedelta(seconds=-10)), [{'finding_key': 'collector:item'}], coverage, stats)))
        api = FakeAPI(root / 'runtime', collected)
        calls = []
        with self.assertRaisesRegex(ValueError, 'fresh collector snapshot does not cover requested --project-dir'):
            collect('demo', str(root), str(other), backend_loader=lambda _: ({}, api),
                    collector_runner=lambda *args: calls.append(args))
        self.assertEqual(calls, [])

    def test_fresh_snapshot_with_missing_status_is_not_coverage(self):
        root, _, collected = self.setup_roots(stamp(timedelta(seconds=-10)))
        coverage, stats = coverage_for(root, status='missing')
        (collected / 'findings.json').write_text(json.dumps(snapshot(
            stamp(timedelta(seconds=-10)), [{'finding_key': 'collector:item'}], coverage, stats)))
        api = FakeAPI(root / 'runtime', collected)
        calls = []
        with self.assertRaisesRegex(ValueError, 'fresh collector snapshot does not cover requested --project-dir'):
            collect('demo', str(root), str(root), backend_loader=lambda _: ({}, api),
                    collector_runner=lambda *args: calls.append(args))
        self.assertEqual(calls, [])

    def test_fresh_snapshot_covering_project_dir_is_reused(self):
        root, _, collected = self.setup_roots(stamp(timedelta(seconds=-10)))
        api = FakeAPI(root / 'runtime', collected)
        calls = []
        rows = collect('demo', str(root), str(root), backend_loader=lambda _: ({}, api),
                       collector_runner=lambda *args: calls.append(args))
        self.assertEqual(calls, [])
        self.assertEqual(rows[1]['finding_key'], 'collector:item')

    def test_each_issue_uses_its_own_source_evidence_path(self):
        root, runtime, collected = self.setup_roots(stamp(timedelta(seconds=-10)))
        api = FakeAPI(runtime, collected)
        rows = collect('demo', str(root), backend_loader=lambda _: ({}, api),
                       collector_runner=lambda *args: self.fail('fresh collector ran'))
        self.assertEqual(rows[0]['evidence_path'], str(runtime / 'findings.json'))
        self.assertEqual(rows[1]['evidence_path'], str(collected / 'findings.json'))

    def test_collector_stdout_cannot_break_result_json(self):
        root, runtime, collected = self.setup_roots(stamp(timedelta(seconds=-600)))
        api = FakeAPI(runtime, collected)
        stdout = io.StringIO()
        stderr = io.StringIO()

        def noisy_run(argv, check, stdout):
            print('collector progress', file=stdout)

        with patch.object(helper, '_observability_root', return_value=root), \
                patch.object(helper, '_load_backend', return_value=({}, api)), \
                patch.object(helper.shutil, 'which', return_value='/fake/developer-observability'), \
                patch.object(helper.subprocess, 'run', side_effect=noisy_run), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(helper.main(['--project-id', 'demo']), 0)
        self.assertEqual(json.loads(stdout.getvalue())[0]['finding_key'], 'primary:item')
        self.assertIn('collector progress', stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
