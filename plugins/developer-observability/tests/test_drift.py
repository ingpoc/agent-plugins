"""The backend source record identifies this plugin copy as canonical."""
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CanonicalSourceTests(unittest.TestCase):
    def test_backend_is_the_canonical_source(self):
        self.assertEqual(json.loads((ROOT / 'runtime/backend/VENDORED.json').read_text()), {
            'canonical': 'plugins/developer-observability/runtime/backend',
            'historical_origin': {
                'repo': 'kindle-project (retired 2026-11-04)',
                'package': 'telemetry',
                'commit': '5e9c4592d92886c4c7c66738ef39818ced65010a',
            },
            'note': 'No upstream; edit in place.',
        })


if __name__ == '__main__':
    unittest.main()
