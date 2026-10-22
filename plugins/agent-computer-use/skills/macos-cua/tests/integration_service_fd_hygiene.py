#!/usr/bin/env python3
"""A client disconnect must release the service's socket fd.

Before: CUAService wedged after ~248 connect/call/close cycles. The Swift
server ran with allowRemoteHalfClosure and nothing consumed
ChannelEvent.inputClosed, so every peer close left a child channel open,
leaking one fd. At the 256 fd soft limit the accept path kept accepting and
then dropping connections: every RPC failed while the process looked alive,
with no crash report and only 7 threads.

These cycles are deliberately well under that limit, so a regression fails
here with a growing fd count instead of wedging the machine's service.
"""
from __future__ import annotations

import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))
import cua_client  # noqa: E402

INTEGRATION_REQUIRES = "macOS with the updated CUAService installed on the default socket and lsof"
CYCLES = 80
FD_SLACK = 4


def _fd_count(pid: int) -> int:
    """Open fds held by pid; lsof is the only portable reader on macOS."""
    found = subprocess.run(
        ["lsof", "-p", str(pid)],
        capture_output=True, text=True, timeout=20,
        stdin=subprocess.DEVNULL,
    )
    if found.returncode != 0:
        raise AssertionError(
            f"lsof -p {pid} failed ({found.returncode}): {found.stderr.strip()}")
    return len(found.stdout.splitlines()) - 1  # header


class LiveServiceFdHygieneTests(unittest.TestCase):
    def setUp(self):
        pids = cua_client.owner_pids()
        if not pids:
            self.skipTest(
                "no CUAService on the default socket; install with "
                "python3 service/install_service.py && open "
                "~/.cache/macos-cua/CUAService.app")
        self.pid = pids[0]

    def test_client_disconnects_do_not_leak_service_fds(self):
        before = _fd_count(self.pid)
        for _ in range(CYCLES):
            client = cua_client.CUAClient()
            try:
                client.connect(timeout=5)
                apps = client.call("list_apps", retry=False)
                self.assertIsInstance(apps, list)
            finally:
                client.close()
        time.sleep(0.5)  # let NIO reap closed child channels
        after = _fd_count(self.pid)
        self.assertLessEqual(
            after, before + FD_SLACK,
            f"{CYCLES} disconnects grew the service from {before} to {after} "
            f"fds: peer close is not releasing the child channel "
            f"(allowRemoteHalfClosure without an inputClosed handler)")

    def test_a_fresh_connection_still_answers_after_the_cycles(self):
        """The wedge looked like 'connects, then Socket closed'."""
        client = cua_client.CUAClient()
        try:
            client.connect(timeout=5)
            for _ in range(3):
                self.assertIsInstance(client.call("list_apps", retry=False), list)
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
