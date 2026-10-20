#!/usr/bin/env python3
"""Re-exec into the locked plugin runtime when the ambient interpreter lacks mcp 2.2.

`context_ledger.server` imports `mcp.server.mcpserver`, which a plain `python3`
does not have; check.py also runs tests with `PYTHONNOUSERSITE=1`, which hides a
user-site copy. Every test that imports the server or spawns it calls this first,
so unittest discovery still runs under any interpreter.

On some Python 3.12 builds, `importlib.util.find_spec("mcp.server.mcpserver")`
raises `ModuleNotFoundError` when the top-level `mcp` package is absent instead of
returning None. Treat that as missing and fall through to the locked runtime.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path


def _mcp_server_available() -> bool:
    try:
        return importlib.util.find_spec("mcp.server.mcpserver") is not None
    except ModuleNotFoundError:
        return False


def _runtime_python(root: Path) -> Path:
    sha = hashlib.sha256((root / "requirements.lock").read_bytes()).hexdigest()
    runtime = Path.home() / ".context-ledger" / "runtime" / sha
    if os.name == "nt":
        return runtime / "Scripts" / "python.exe"
    # venv creates `python`; some hosts also expose `python3`.
    py = runtime / "bin" / "python"
    if py.is_file():
        return py
    return runtime / "bin" / "python3"


def _ensure_runtime(root: Path, py: Path) -> None:
    if py.is_file():
        return
    setup = root / "bin" / "context_ledger.py"
    proc = subprocess.run(
        [sys.executable, str(setup), "setup"],
        cwd=str(root),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not py.is_file():
        detail = (proc.stderr or proc.stdout or "").strip()[-500:]
        raise SystemExit(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "UNAVAILABLE",
                        "retryable": False,
                        "message": (
                            f"mcp.server.mcpserver is not importable and setup failed for {py}"
                            + (f": {detail}" if detail else "")
                        ),
                    },
                }
            )
        )


def reexec_locked_runtime(root: Path) -> None:
    if _mcp_server_available():
        return
    py = _runtime_python(root)
    _ensure_runtime(root, py)
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
