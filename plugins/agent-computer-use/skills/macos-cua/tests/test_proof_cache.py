import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "proof_cache", ROOT / "scripts" / "proof_cache.py"
)
proof_cache = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proof_cache)


class ProofCacheTests(unittest.TestCase):
    def test_prune_removes_expired_files_but_protects_active_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            screenshots = root / "screenshots"
            screenshots.mkdir()
            protected = screenshots / "active.png"
            expired = screenshots / "expired.png"
            protected.write_bytes(b"active")
            expired.write_bytes(b"expired")
            os.utime(protected, (10, 10))
            os.utime(expired, (10, 10))
            (root / "operator-state.json").write_text(
                json.dumps({"raw_screenshot_path": str(protected)})
            )

            result = proof_cache.prune(
                root, max_bytes=1024, max_age_seconds=10, now=100
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["removed"], 1)
            self.assertTrue(protected.exists())
            self.assertFalse(expired.exists())

    def test_prune_removes_oldest_files_until_size_budget_is_met(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            screenshots = root / "screenshots"
            screenshots.mkdir()
            older = screenshots / "older.png"
            newer = screenshots / "newer.png"
            older.write_bytes(b"a" * 10)
            newer.write_bytes(b"b" * 10)
            os.utime(older, (10, 10))
            os.utime(newer, (20, 20))

            result = proof_cache.prune(
                root, max_bytes=10, max_age_seconds=1000, now=100
            )

            self.assertEqual(result["removed"], 1)
            self.assertFalse(older.exists())
            self.assertTrue(newer.exists())
            self.assertEqual(result["remaining_bytes"], 10)


if __name__ == "__main__":
    unittest.main()
