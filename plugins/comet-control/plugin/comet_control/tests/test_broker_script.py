from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "ensure-wip-broker.sh"
BROKER = REPO_ROOT / "plugin" / "comet_control" / "native" / "broker.py"


class BrokerScriptTests(unittest.TestCase):
    def test_start_owns_only_repo_runtime_and_probe_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ccb-", dir="/tmp") as raw:
            root = Path(raw)
            native = root / "plugin/comet_control/native"
            native.mkdir(parents=True)
            shutil.copy2(BROKER, native / "broker.py")
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as port_probe:
                port_probe.bind(("127.0.0.1", 0))
                port = port_probe.getsockname()[1]
            environment = {
                **os.environ,
                "COMET_CONTROL_WIP_ROOT": str(root),
                "COMET_CONTROL_USER_HOME": str(root / "home"),
                "COMET_CONTROL_BROKER_PORT": str(port),
                "COMET_CONTROL_BROKER_DIRECT": "1",
            }
            started = subprocess.run(
                [str(SCRIPT), "start"], env=environment, text=True, capture_output=True, check=False
            )
            self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
            pid = int((root / "run/comet-control-broker.pid").read_text())
            try:
                before = (root / "run/comet-control.sock").stat().st_ino
                probed = subprocess.run(
                    [str(SCRIPT), "probe", "--json"],
                    env=environment,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                payload = json.loads(probed.stdout)
                self.assertEqual(probed.returncode, 2)
                self.assertEqual(
                    payload["error_code"],
                    "COMET_RUNTIME_NOT_FOUND",
                    f"{payload}\n{(root / 'run/comet-control-broker.log').read_text()}",
                )
                self.assertEqual(
                    Path(payload["broker"]["python_executable"]).resolve(),
                    Path(sys.executable).resolve(),
                )
                self.assertEqual((root / "run/comet-control.sock").stat().st_ino, before)
            finally:
                os.kill(pid, signal.SIGTERM)
                for _ in range(20):
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.05)

    def test_script_has_no_browser_manifest_registration(self) -> None:
        source = SCRIPT.read_text()
        self.assertNotIn("NativeMessagingHosts", source)
        self.assertNotIn("com.perplexity", source)
        self.assertNotIn("Google/Chrome", source)
        self.assertIn("session_count()", source)
        self.assertIn("refusing restart", source)
        self.assertIn("websockets 16.0 required", source)
        self.assertIn("for _ in {1..300}", source)


if __name__ == "__main__":
    unittest.main()
