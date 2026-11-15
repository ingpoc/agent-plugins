import subprocess
import unittest
from unittest.mock import patch
import cloud_health as h

class HealthTests(unittest.TestCase):
    @patch('cloud_health.shutil.which', return_value=None)
    def test_missing_command(self, _):
        self.assertEqual(h.preview_status()['state'], 'unavailable')

    @patch('cloud_health.shutil.which', return_value='/fake/sites-preview')
    @patch('cloud_health.subprocess.run')
    def test_stopped_and_unknown_output(self, run, _):
        run.return_value = subprocess.CompletedProcess([], 0, 'Sites preview stopped\n', '')
        self.assertEqual(h.preview_status()['state'], 'stopped')
        run.return_value = subprocess.CompletedProcess([], 0, 'unexpected private text', '')
        result = h.preview_status()
        self.assertEqual(result['state'], 'unknown')
        self.assertNotIn('private', str(result))

    @patch('cloud_health.shutil.which', return_value='/fake/sites-preview')
    @patch('cloud_health.subprocess.run', side_effect=subprocess.TimeoutExpired('status', 5))
    def test_timeout(self, *_):
        self.assertEqual(h.preview_status()['state'], 'unknown')

    @patch('cloud_health.glob.iglob', return_value=iter(()))
    def test_missing_helper(self, _):
        self.assertEqual(h.helper_status()['state'], 'not_found')

if __name__ == '__main__': unittest.main()
