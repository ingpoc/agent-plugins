"""Vendored backend drift: recorded upstream hashes still match the producer checkout, when one is present."""
import hashlib
import json
import os
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
VENDORED = ROOT / 'runtime/backend'


def upstream_root():
    if os.environ.get('OBSERVABILITY_UPSTREAM_ROOT'):
        return Path(os.environ['OBSERVABILITY_UPSTREAM_ROOT']).expanduser()
    config = Path(os.environ.get('OBSERVABILITY_CONFIG') or Path.home() / '.config/developer-observability/config.json')
    try:
        root = json.loads(config.read_text()).get('backend_root')
    except (OSError, ValueError, AttributeError):
        return None
    return Path(root).expanduser() if root else None


class DriftTests(unittest.TestCase):
    def test_vendored_backend_matches_upstream(self):
        upstream = upstream_root()
        if upstream is None or not (upstream / 'telemetry/observability_api.py').is_file():
            self.skipTest('upstream findings producer checkout not present')
        manifest = json.loads((VENDORED / 'VENDORED.json').read_text())
        for name, digest in manifest['files'].items():
            source = (upstream / 'telemetry' / name).read_bytes()
            self.assertEqual(hashlib.sha256(source).hexdigest(), digest,
                             f'upstream telemetry/{name} changed since vendoring; re-vendor runtime/backend and update VENDORED.json')
            if name in manifest['verbatim']:
                self.assertEqual((VENDORED / name).read_bytes(), source, f'vendored {name} must stay verbatim')


if __name__ == '__main__':
    unittest.main()
