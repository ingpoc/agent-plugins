"""diagnose loaded_build_current hashes exactly what the extension hashes."""

from __future__ import annotations

import hashlib
import importlib.util
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SW = (ROOT / "extension" / "service_worker.js").read_text()


def _load():
    spec = importlib.util.spec_from_file_location("cc_diagnostics_build", ROOT / "diagnostics.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class LoadedBuildHash(unittest.TestCase):
    def setUp(self):
        self.d = _load()

    def test_file_list_matches_extension(self):
        block = SW.split("const extensionBuildSha256 = Promise.all([", 1)[1].split("].map(", 1)[0]
        self.assertEqual(tuple(re.findall(r'"([^"]+)"', block)), self.d.EXTENSION_BUILD_FILES)

    def test_hash_is_concatenation_of_the_three_files(self):
        ext = ROOT / "extension"
        want = hashlib.sha256(b"".join((ext / f).read_bytes() for f in self.d.EXTENSION_BUILD_FILES)).hexdigest()
        self.assertEqual(self.d.extension_build_sha256(), want)
        self.assertNotEqual(want, hashlib.sha256((ext / "service_worker.js").read_bytes()).hexdigest())

    def _payload(self, ext_sha):
        return {"success": True, "broker": {"protocol_version": 1, "extension_build_sha256": ext_sha,
                                            "broker_build_sha256": self.d._sha(self.d.SOURCE_BROKER)}}

    def test_correct_install_passes(self):
        c = self.d._loaded_build_check(self._payload(self.d.extension_build_sha256()))
        self.assertIs(c["ok"], True, c)

    def test_sw_only_hash_fails_loud_with_both_shas(self):
        sw_only = hashlib.sha256((ROOT / "extension" / "service_worker.js").read_bytes()).hexdigest()
        c = self.d._loaded_build_check(self._payload(sw_only))
        self.assertIs(c["ok"], False)
        self.assertIn(sw_only[:12], c["detail"])
        self.assertIn(self.d.extension_build_sha256()[:12], c["detail"])

    def test_missing_file_is_none_not_a_partial_hash(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "service_worker.js").write_text("x")
            self.assertIsNone(self.d.extension_build_sha256(Path(tmp)))


if __name__ == "__main__":
    unittest.main()
