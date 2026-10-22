#!/usr/bin/env python3
"""Re-exec into the locked plugin runtime when the ambient interpreter lacks mcp 2.2.

`context_ledger.server` imports `mcp.server.mcpserver`, which a plain `python3`
does not have; check.py also runs tests with `PYTHONNOUSERSITE=1`, which hides a
user-site copy. Every test that imports the server or spawns it calls this first,
so unittest discovery still runs under any interpreter.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path


def reexec_locked_runtime(root: Path) -> None:
    if importlib.util.find_spec("mcp.server.mcpserver") is not None:
        return
    sha = hashlib.sha256((root / "requirements.lock").read_bytes()).hexdigest()
    py = Path.home() / ".context-ledger" / "runtime" / sha / "bin" / "python3"
    if not py.is_file():
        raise SystemExit(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "UNAVAILABLE",
                        "retryable": False,
                        "message": f"mcp.server.mcpserver is not importable and {py} is missing",
                    },
                }
            )
        )
    sys.stderr.write(f"re-exec {py} because this interpreter has no mcp.server.mcpserver\n")
    if "unittest" in sys.argv[0]:
        os.execv(str(py), [str(py), "-m", "unittest", *sys.argv[1:]])
    os.execv(str(py), [str(py), *sys.argv])
