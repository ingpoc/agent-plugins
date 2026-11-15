import errno
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import tracker_lock


class TrackerLockTests(unittest.TestCase):
    def test_windows_retries_contention_then_unlocks(self):
        api = Mock(LK_NBLCK=1, LK_UNLCK=2)
        api.locking.side_effect = [OSError(errno.EACCES, 'busy'), None, None]
        with tempfile.TemporaryDirectory() as root, patch.object(tracker_lock, 'fcntl', None), \
             patch.object(tracker_lock, 'msvcrt', api), patch.object(tracker_lock.time, 'sleep') as sleep:
            with tracker_lock.exclusive_lock(Path(root) / 'lock'):
                self.assertEqual(api.locking.call_count, 2)
            self.assertEqual([call.args[1:] for call in api.locking.call_args_list], [(1, 1), (1, 1), (2, 1)])
            sleep.assert_called_once()

    def test_windows_unexpected_error_fails_closed(self):
        api = Mock(LK_NBLCK=1, LK_UNLCK=2)
        api.locking.side_effect = OSError(errno.EIO, 'io error')
        with tempfile.TemporaryDirectory() as root, patch.object(tracker_lock, 'fcntl', None), \
             patch.object(tracker_lock, 'msvcrt', api):
            with self.assertRaises(OSError):
                with tracker_lock.exclusive_lock(Path(root) / 'lock'):
                    self.fail('unlocked work ran')

    def test_plan_lock_is_released_when_holder_process_crashes(self):
        with tempfile.TemporaryDirectory() as root:
            lock_path = Path(root) / 'plan.lock'
            helper_dir = str(Path(__file__).resolve().parent)
            script = "\n".join((
                "import os, sys",
                "sys.path.insert(0, sys.argv[1])",
                "from pathlib import Path",
                "import tracker_lock",
                "with tracker_lock.exclusive_lock(Path(sys.argv[2])):",
                "    print('held', flush=True)",
                "    os._exit(23)",
            ))
            process = subprocess.Popen([sys.executable, '-c', script, helper_dir, str(lock_path)],
                                       stdout=subprocess.PIPE, text=True)
            self.assertEqual(process.stdout.readline().strip(), 'held')
            self.assertEqual(process.wait(timeout=5), 23)
            process.stdout.close()
            with tracker_lock.exclusive_lock(lock_path):
                pass


if __name__ == '__main__':
    unittest.main()
