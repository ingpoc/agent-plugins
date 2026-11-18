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
    def test_leaked_viewer_fails_the_suite(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tmp = root / "tmp"
            suite = tmp / "suite"
            scripts = suite / "install" / "scripts"
            tests = suite / "tests"
            scripts.mkdir(parents=True)
            tests.mkdir()
            viewer = scripts / "serve.py"
            viewer.write_text("import time\ntime.sleep(60)\n")
            (tests / "test_leak.py").write_text(
                "import subprocess, sys, unittest\n"
                "from pathlib import Path\n"
                "class LeakingTest(unittest.TestCase):\n"
                " def test_leaves_viewer(self):\n"
                "  root = Path(__file__).resolve().parents[1]\n"
                "  proc = subprocess.Popen([sys.executable, str(root / 'install/scripts/serve.py'), str(root / 'page.html'), '--data', str(root / 'data.json')], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                "  (root / 'pid').write_text(str(proc.pid))\n"
            )
            (tests / "test_zz_guard.py").write_text(
                "import tempfile, unittest\n"
                "from pathlib import Path\n"
                "import viewer_test_guard\n"
                "class GuardTest(unittest.TestCase):\n"
                " def test_guard_is_installed(self): pass\n"
                "def tearDownModule():\n"
                " viewer_test_guard.assert_no_viewers([Path(tempfile.gettempdir())])\n"
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
            pid_file = suite / "pid"
            try:
                result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(tests),
                                         "-p", "test_*.py"], cwd=tests, env=env, capture_output=True, text=True,
                                        timeout=20)
                pid = int(pid_file.read_text())
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("viewer processes leaked from tests", result.stderr)
                self.assertFalse(live_state._pid_is_running(pid))
            finally:
                if pid_file.exists():
                    viewer_test_guard.stop_viewers(root, (int(pid_file.read_text()),))


def tearDownModule():
    viewer_test_guard.assert_no_viewers(viewer_test_guard.TEMP_ROOTS)
