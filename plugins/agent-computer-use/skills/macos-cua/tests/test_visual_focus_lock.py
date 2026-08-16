from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "visual_focus_lock.py"
SPEC = importlib.util.spec_from_file_location("macos_cua_visual_focus_lock", MODULE_PATH)
assert SPEC and SPEC.loader
focus = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(focus)


class VisualFocusLockTests(unittest.TestCase):
    def test_setup_failure_releases_process_local_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            blocked_parent = Path(directory) / "not-a-directory"
            blocked_parent.write_text("file")
            focus.LOCK_PATH = blocked_parent / "focus.lock"
            with self.assertRaises(OSError):
                focus.acquire("broken", timeout=0)

            focus.LOCK_PATH = Path(directory) / "recovered.lock"
            recovered = focus.acquire("recovered", timeout=0)
            recovered.release()

    def test_threads_in_one_process_are_serialized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            focus.LOCK_PATH = Path(directory) / "focus.lock"
            first = focus.acquire("first-thread", timeout=0)
            outcome: list[str] = []

            def contend() -> None:
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

    def test_process_exit_releases_kernel_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            focus.LOCK_PATH = Path(directory) / "focus.lock"
            code = (
                "import importlib.util,pathlib,sys,time;"
                "s=importlib.util.spec_from_file_location('f',sys.argv[1]);"
                "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
                "m.LOCK_PATH=pathlib.Path(sys.argv[2]);"
                "m.acquire('child',timeout=1);print('ready',flush=True);time.sleep(30)"
            )
            child = subprocess.Popen(
                [sys.executable, "-c", code, str(MODULE_PATH), str(focus.LOCK_PATH)],
                stdout=subprocess.PIPE,
                text=True,
            )
            assert child.stdout is not None
            self.assertEqual(child.stdout.readline().strip(), "ready")
            child.terminate()
            child.wait(timeout=3)
            child.stdout.close()

            recovered = focus.acquire("parent", timeout=1)
            recovered.release()


if __name__ == "__main__":
    unittest.main()
