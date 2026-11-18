import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import live_state
import viewer_test_guard


class ViewerGuardTests(unittest.TestCase):
    def test_leaking_module_fails_when_run_alone(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tmp = root / "tmp"
            scripts = tmp
            tests = root / "tests"
            scripts.mkdir(parents=True)
            tests.mkdir()
            (tests / "test_leak.py").write_text(
                "import subprocess, sys, unittest\n"
                "import tempfile\n"
                "from pathlib import Path\n"
                "import viewer_test_guard\n"
                "tracked = tempfile.TemporaryDirectory()\n"
                "class LeakingTest(unittest.TestCase):\n"
                " def test_leaves_viewer(self):\n"
                "  root = Path(tracked.name)\n"
                "  (root / 'serve.py').write_text('import time\\ntime.sleep(60)\\n')\n"
                "  proc = subprocess.Popen([sys.executable, str(root / 'serve.py'), str(root / 'page.html'), '--data', str(root / 'data.json')], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                "  (Path(__file__).resolve().parents[1] / 'pid').write_text(str(proc.pid))\n"
                "def tearDownModule():\n"
                " viewer_test_guard.assert_no_viewers(viewer_test_guard.TEMP_ROOTS)\n"
            )
            env = {**os.environ, "TMPDIR": str(tmp),
                   "PYTHONPATH": os.pathsep.join((str(Path(__file__).parent), os.environ.get("PYTHONPATH", "")))}
            decoy = subprocess.Popen([shutil.which("yes"), f"serve.py {root}"], stdout=subprocess.DEVNULL)
            try:
                time.sleep(.05)
                self.assertNotIn(decoy.pid, viewer_test_guard.viewer_pids_under([root]))
            finally:
                decoy.terminate()
                decoy.wait(timeout=3)
            pid_file = root / "pid"
            try:
                result = subprocess.run([sys.executable, "-m", "unittest", "test_leak"], cwd=tests,
                                        env=env, capture_output=True, text=True,
                                        timeout=20)
                pid = int(pid_file.read_text())
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("viewer processes leaked from tests", result.stderr)
                self.assertFalse(live_state._pid_is_running(pid))
            finally:
                if pid_file.exists():
                    viewer_test_guard.stop_viewers(root, (int(pid_file.read_text()),))
