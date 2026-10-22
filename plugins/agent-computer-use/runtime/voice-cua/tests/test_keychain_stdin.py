#!/usr/bin/env python3
"""keychain_put never puts a secret in argv: it feeds `security -i` over stdin."""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from voice_cua import catalog  # noqa: E402

SECRET = "sk-test-NOT-REAL-0123456789"


def _ok(*_a, **_k):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


class KeychainStdinTests(unittest.TestCase):
    def test_secret_goes_over_stdin_not_argv(self):
        with patch.object(catalog.subprocess, "run", MagicMock(side_effect=_ok)) as run:
            catalog.keychain_put(service="svc", account="acct", label="lbl", value=SECRET)
        self.assertEqual(run.call_count, 1)
        argv = run.call_args.args[0]
        self.assertEqual(argv, [catalog.SECURITY, "-i"])
        self.assertFalse(any(SECRET in a for a in argv), "secret leaked into argv")
        self.assertEqual(run.call_args.kwargs["input"],
                         f'add-generic-password -U -s "svc" -a "acct" -l "lbl" -w "{SECRET}"\n')

    def test_unsafe_values_rejected_before_any_call(self):
        for bad in ('a"b', "a\\b", "a\nb", "a\rb"):
            with patch.object(catalog.subprocess, "run") as run:
                with self.assertRaises(ValueError):
                    catalog.keychain_put(service="svc", account="acct", label="lbl", value=bad)
                run.assert_not_called()
        with patch.object(catalog.subprocess, "run") as run, self.assertRaises(ValueError):
            catalog.keychain_put(service='s"vc', account="acct", label="lbl", value=SECRET)
        run.assert_not_called()

    def test_security_failure_raises(self):
        fail = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="SecKeychainItemCreate: error")
        with patch.object(catalog.subprocess, "run", return_value=fail), self.assertRaises(subprocess.CalledProcessError) as ctx:
            catalog.keychain_put(service="svc", account="acct", label="lbl", value=SECRET)
        self.assertNotIn(SECRET, str(ctx.exception.cmd))

    def test_interactive_path_kept_for_none(self):
        with patch.object(catalog.subprocess, "run", MagicMock(side_effect=_ok)) as run:
            catalog.keychain_put(service="svc", account="acct", label="lbl", value=None)
        argv = run.call_args.args[0]
        self.assertEqual(argv[-1], "-w", "security prompts for the value itself")
        self.assertNotIn("input", run.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
