import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import preview_attempt as a
import project

class AttemptTests(unittest.TestCase):
    def test_failure_record_excludes_raw_output_and_secrets(self):
        with tempfile.TemporaryDirectory() as d, patch.object(a.shutil, 'which', return_value='/fake/preview'), patch.object(a.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, 'secret-token', 'timed out waiting for relay readiness secret-token')):
            result, path, url = a.record_attempt(Path(d), 'demo', 'ISSUE-01', 'col-123', Path(d)/'records')
            self.assertEqual(result['error_code'], 'relay_readiness_timeout')
            self.assertEqual(result['phase'], 'relay_readiness')
            self.assertNotIn('secret-token', path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIsNone(url)
            tracker = Path(d)/'tracker.json'; tracker.write_text(json.dumps({'findings':[{'id':'ISSUE-01','status':'blocked'}]}))
            with patch.object(project, 'bridge_call', side_effect=[{'snapshot_id':'abc','freshness':'fresh','coverage':'git only'}, {'issues':[{'issue':{'issue_id':'col-123'}}]}]):
                report = a.diagnose_attempt(path, tracker)
            self.assertEqual(report['tracker_reference']['state'], 'found')
            self.assertEqual(report['observability_reference']['state'], 'found')
            self.assertIn('unverified', report['interpretation'])

    def test_unknown_failure_and_missing_executable(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(a.shutil, 'which', return_value='/fake'), patch.object(a.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'arbitrary secret')):
                record, path, _ = a.record_attempt(Path(d), 'demo', record_dir=Path(d))
            self.assertEqual(record['phase'], 'unknown'); self.assertEqual(record['error_code'], 'preview_start_failed')
            self.assertNotIn('arbitrary secret', path.read_text())
            with patch.object(a.shutil, 'which', return_value=None):
                record, _, _ = a.record_attempt(Path(d), 'demo', record_dir=Path(d))
            self.assertEqual(record['error_code'], 'preview_command_missing')

    def test_success_url_is_not_persisted(self):
        with tempfile.TemporaryDirectory() as d, patch.object(a.shutil, 'which', return_value='/fake'), patch.object(a.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'http://127.0.0.1:1234/\n', '')):
            record, path, url = a.record_attempt(Path(d), 'demo', record_dir=Path(d))
            self.assertEqual(record['outcome'], 'succeeded'); self.assertEqual(url, 'http://127.0.0.1:1234/')
            self.assertNotIn(url, path.read_text())

    def test_invalid_identifiers_and_unavailable_references(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError): a.record_attempt(Path(d), 'bad/project', record_dir=Path(d))
            with patch.object(a.shutil, 'which', return_value=None):
                _, path, _ = a.record_attempt(Path(d), 'demo', 'ISSUE-01', 'col-123', Path(d))
            with patch.object(project, 'bridge_call', side_effect=RuntimeError('unavailable')):
                report = a.diagnose_attempt(path, Path(d)/'missing')
            self.assertEqual(report['tracker_reference']['state'], 'unavailable')
            self.assertEqual(report['observability_reference']['state'], 'unavailable')

    def test_unknown_project_after_collection_is_not_health_claim(self):
        with patch.object(project, 'bridge_call', side_effect=[RuntimeError('unknown_project'), {'findings': 0}, RuntimeError('unknown_project')]), contextlib.redirect_stdout(io.StringIO()) as output:
            project.diagnose('demo', Path('/tmp'))
        self.assertIn('no_project_findings', output.getvalue())
        self.assertIn('health is not established', output.getvalue())

if __name__ == '__main__': unittest.main()
