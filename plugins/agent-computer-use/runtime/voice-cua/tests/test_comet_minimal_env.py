"""Comet probe under launchd's minimal env (Finder-launched app).

Regression: the installed app runs with PATH=/usr/bin:/bin:/usr/sbin:/sbin, so
ensure-broker.sh resolved the system python3 (wrong websockets) and every
comet_begin failed in ~100 ms with a generic error.
"""

from __future__ import annotations

import os
import plistlib
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from voice_cua import comet_bridge  # noqa: E402
from voice_cua.comet_bridge import CometLease  # noqa: E402

MINIMAL = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}

# Mirrors ensure-broker.sh: python from COMET_CONTROL_PYTHON or PATH's python3,
# then a hard requirement only the right interpreter satisfies.
FAKE_PROBE = """#!/bin/bash
set -euo pipefail
PYTHON="${COMET_CONTROL_PYTHON:-$(command -v python3 || echo /nonexistent)}"
"$PYTHON" -c 'import os,sys; assert os.path.realpath(sys.executable) == os.path.realpath(os.environ["WANT_PY"]), "wrong python: " + sys.executable'
echo '{"success": true, "broker": {"runtime_verified": true, "extension_connected": false}}'
"""


class MinimalEnvProbe(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="comet-minenv-"))
        root = self.tmp / "comet-control"
        (root / "scripts").mkdir(parents=True)
        (root / "skills/comet-control/scripts").mkdir(parents=True)
        (root / "plugin.json").write_text("{}")
        (root / "skills/comet-control/scripts/durable_lease_controller.py").write_text("")
        probe = root / "scripts/ensure-broker.sh"
        probe.write_text(FAKE_PROBE)
        probe.chmod(probe.stat().st_mode | stat.S_IXUSR)
        self.root = root
        self.plist = self.tmp / "broker.plist"
        self.env = {
            **MINIMAL,
            "HOME": str(self.tmp),
            "COMET_CONTROL_ROOT": str(root),
            "WANT_PY": sys.executable,
            "VOICE_CUA_COMET_STATE": str(self.tmp / "state.json"),
            "VOICE_CUA_ACTIVITY_LOG": str(self.tmp / "activity.jsonl"),
        }

    def _begin(self) -> dict:
        lease = CometLease()
        with patch.dict(os.environ, self.env, clear=True), patch.object(comet_bridge, "BROKER_PLIST", self.plist):
            with lease._lock:
                return lease._start_locked("https://example.com", "self_test")

    def test_python_resolved_from_broker_launch_agent(self) -> None:
        with self.plist.open("wb") as handle:
            plistlib.dump({"ProgramArguments": [sys.executable, "broker.py"]}, handle)
        with patch.dict(os.environ, self.env, clear=True), patch.object(comet_bridge, "BROKER_PLIST", self.plist):
            self.assertEqual(comet_bridge._comet_python(), sys.executable)
            env = comet_bridge._subprocess_env()
        self.assertEqual(env["COMET_CONTROL_PYTHON"], sys.executable)
        self.assertEqual(env["PATH"].split(os.pathsep)[0], str(Path(sys.executable).parent))
        result = self._begin()
        # Probe now runs with the right python; the remaining failure is the real one.
        self.assertFalse(result["ok"])
        self.assertIn("extension not connected", result["error"])

    def test_probe_stderr_is_surfaced_when_python_unknown(self) -> None:
        env_py = "/usr/bin/python3"
        if os.path.realpath(env_py) == os.path.realpath(sys.executable):
            self.skipTest("system python is the test python")
        result = self._begin()  # no plist → falls back to PATH python3
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"].startswith("Comet Control unavailable: "), result["error"])
        self.assertIn("wrong python", result["error"])

    def test_pyinstaller_vars_do_not_leak(self) -> None:
        with patch.dict(os.environ, {**self.env, "_MEIPASS2": "/x", "PYTHONHOME": "/x"}, clear=True):
            env = comet_bridge._subprocess_env()
        self.assertNotIn("_MEIPASS2", env)
        self.assertNotIn("PYTHONHOME", env)


REAL_ROOT = Path.home() / ".agents/plugins/comet-control"


@unittest.skipUnless(
    os.environ.get("VOICE_CUA_LIVE_COMET") == "1" and (REAL_ROOT / "scripts/ensure-broker.sh").is_file(),
    "set VOICE_CUA_LIVE_COMET=1 to probe the installed Comet Control under a minimal env",
)
class LiveMinimalEnvProbe(unittest.TestCase):
    def test_real_probe_minimal_env(self) -> None:
        with patch.dict(os.environ, {**MINIMAL, "HOME": str(Path.home())}, clear=True):
            probe = comet_bridge._run_json([str(REAL_ROOT / "scripts/ensure-broker.sh"), "probe", "--json"], timeout=30)
        broker = probe.get("broker") or {}
        self.assertTrue(probe.get("success"), probe.get("error"))
        self.assertTrue(broker.get("runtime_verified"))


if __name__ == "__main__":
    unittest.main()
