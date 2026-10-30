#!/usr/bin/env python3
"""Hardening-gate cleanup: doctor snapshot and evidence survival.

run_gate.py runs the contract `cleanup` after every gate, green or failed, so
this records a post-run doctor (the after-failure health check), then fails if
any evidence that existed before cleanup is gone afterwards.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "macos-cua.py"


def evidence_paths(contract: dict) -> list[Path]:
    return [Path(os.path.expanduser(p)) for p in contract.get("evidence_paths", [])]


def surviving(paths: list[Path]) -> set[str]:
    return {str(p) for p in paths if p.exists()}


def run_cli(*args: str, timeout: int = 30) -> dict:
    done = subprocess.run(
        [sys.executable, str(CLI), *args], capture_output=True, text=True,
        timeout=timeout, stdin=subprocess.DEVNULL,
    )
    try:
        payload = json.loads(done.stdout or "{}")
    except json.JSONDecodeError:
        payload = {"ok": False, "error": (done.stdout + done.stderr)[-400:]}
    payload.setdefault("ok", done.returncode == 0)
    return payload


def cleanup(contract: dict, run=run_cli) -> dict:
    paths = evidence_paths(contract)
    before = surviving(paths)
    doctor = run("doctor")
    lost = sorted(before - surviving(paths))
    return {
        "ok": not lost,
        "doctor": {"ok": doctor.get("ok"), "failed": doctor.get("failed")},
        "evidence_kept": sorted(before - set(lost)),
        "evidence_lost": lost,
    }


def main() -> int:
    contract = json.loads((ROOT / "hardening-contract.json").read_text())
    report = cleanup(contract)
    print(json.dumps(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
