import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest


_SIBLING = (
    Path(__file__).resolve().parents[4]
    / "agent-computer-use"
    / "skills"
    / "macos-cua"
    / "scripts"
    / "visual_focus_lock.py"
)
MODULE_PATH = (
    _SIBLING
    if _SIBLING.is_file()
    else Path.home() / ".agents" / "skills" / "macos-cua" / "scripts" / "visual_focus_lock.py"
)
SPEC = importlib.util.spec_from_file_location("visual_focus_lock", MODULE_PATH)
focus = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(focus)


class VisualFocusLockTests(unittest.TestCase):
    def test_threads_in_one_process_are_serialized(self):
        with tempfile.TemporaryDirectory() as directory:
            focus.LOCK_PATH = Path(directory) / "focus.lock"
            first = focus.acquire("first-thread", timeout=0)
            outcome = []

            def contend():
                try:
                    focus.acquire("second-thread", timeout=0.1)
                except focus.VisualFocusBusy:
                    outcome.append("busy")

            thread = threading.Thread(target=contend)
            thread.start()
            thread.join(timeout=1)
            self.assertEqual(outcome, ["busy"])
            first.release()

            recovered = focus.acquire("second-thread", timeout=0.2)
            recovered.release()

    def test_contention_fails_bounded_then_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            focus.LOCK_PATH = Path(directory) / "focus.lock"
            first = focus.acquire("first", timeout=0)
            code = (
                "import importlib.util,pathlib,sys;"
                "s=importlib.util.spec_from_file_location('f',sys.argv[1]);"
                "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
                "m.LOCK_PATH=pathlib.Path(sys.argv[2]);"
                "\ntry:m.acquire('second',timeout=.1)\n"
                "except m.VisualFocusBusy:sys.exit(0)\n"
                "sys.exit(1)"
            )
            blocked = subprocess.run(
                [sys.executable, "-c", code, str(MODULE_PATH), str(focus.LOCK_PATH)],
                timeout=3,
            )
            self.assertEqual(blocked.returncode, 0)
            first.release()
            second = focus.acquire("second", timeout=0.2)
            second.release()

    def test_process_exit_releases_kernel_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            focus.LOCK_PATH = Path(directory) / "focus.lock"
            code = (
                "import importlib.util,pathlib,sys,time;"
                "s=importlib.util.spec_from_file_location('f',sys.argv[1]);"
                "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
                "m.LOCK_PATH=pathlib.Path(sys.argv[2]);"
                "lease=m.acquire('child',timeout=1);print('ready',flush=True);time.sleep(30)"
            )
            child = subprocess.Popen(
                [sys.executable, "-c", code, str(MODULE_PATH), str(focus.LOCK_PATH)],
                stdout=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(child.stdout.readline().strip(), "ready")
            child.terminate()
            child.wait(timeout=3)
            child.stdout.close()
            recovered = focus.acquire("parent", timeout=1)
            recovered.release()


if __name__ == "__main__":
    unittest.main()
