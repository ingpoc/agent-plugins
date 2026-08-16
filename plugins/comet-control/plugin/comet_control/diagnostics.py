"""Compact deterministic diagnostics for the repository-owned Comet Control runtime.

Ownership, socket, and lease discovery are delegated to the canonical
read-only probe. This module checks the single packaged extension and broker.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any


PLUGIN_DIR = Path(__file__).resolve().parent
WIP_ROOT = PLUGIN_DIR.parents[1]
PROBE = WIP_ROOT / "scripts" / "ensure-wip-broker.sh"
SOURCE_EXTENSION = PLUGIN_DIR / "extension"
SOURCE_BROKER = PLUGIN_DIR / "native" / "broker.py"
DRIFT_FILES = (
    "manifest.json",
    "service_worker.js",
    "parity_capabilities.js",
    "content-scripts/cursor-agent.js",
)


def _check(
    name: str,
    ok: bool | None,
    *,
    severity: str,
    detail: str,
    surface: str,
    fix: str,
) -> dict[str, Any]:
    return {
        "name": name,
        "ok": ok,
        "severity": severity,
        "detail": detail,
        "surface": surface,
        "fix": fix,
    }


def _sha(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _runtime_probe() -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [str(PROBE), "probe", "--json"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        payload = json.loads(completed.stdout)
        if not isinstance(payload, dict):
            raise ValueError("probe did not return an object")
        return payload
    except Exception as error:
        return {
            "success": False,
            "ready": False,
            "error_code": "PROBE_FAILED",
            "error": f"{type(error).__name__}: {error}",
        }


def _runtime_check(payload: dict[str, Any]) -> dict[str, Any]:
    ready = payload.get("success") is True
    broker = payload.get("broker") or {}
    detail = (
        f"logged-in Comet ready; browser_pid={broker.get('browser_pid')}"
        if ready
        else f"runtime not ready: {payload.get('error_code') or 'unknown'}"
    )
    return _check(
        "comet_runtime_ready",
        ready,
        severity="blocking",
        detail=detail,
        surface="scripts/ensure-wip-broker.sh",
        fix=(
            "-"
            if ready
            else f"Run {PROBE} start, launch Comet, then probe again."
        ),
    )


def _loaded_build_check(payload: dict[str, Any]) -> dict[str, Any]:
    broker = payload.get("broker") or {}
    expected_broker = _sha(SOURCE_BROKER)
    expected_extension = _sha(SOURCE_EXTENSION / "service_worker.js")
    loaded_broker = broker.get("broker_build_sha256")
    loaded_extension = broker.get("extension_build_sha256")
    ok = bool(
        payload.get("success") is True
        and expected_broker
        and expected_extension
        and loaded_broker == expected_broker
        and loaded_extension == expected_extension
        and broker.get("protocol_version") == 1
    )
    return _check(
        "loaded_build_current",
        ok,
        severity="blocking",
        detail=(
            f"protocol=1 broker={str(loaded_broker)[:12]} extension={str(loaded_extension)[:12]}"
            if ok
            else "loaded broker or extension differs from the packaged protocol-1 build"
        ),
        surface="running Comet Control broker and extension",
        fix="-" if ok else "With no active leases, restart the broker and reload the Comet extension.",
    )


def _broker_check() -> dict[str, Any]:
    ok = SOURCE_BROKER.is_file()
    return _check(
        "broker_present",
        ok,
        severity="blocking",
        detail="broker present" if ok else f"missing: {SOURCE_BROKER}",
        surface="plugin/comet_control/native/broker.py",
        fix="-" if ok else "Restore plugin/comet_control/native/broker.py.",
    )


def _extension_check() -> dict[str, Any]:
    missing = [name for name in DRIFT_FILES if not (SOURCE_EXTENSION / name).is_file()]
    ok = not missing
    return _check(
        "extension_present",
        ok,
        severity="blocking",
        detail="extension present" if ok else f"missing: {', '.join(missing)}",
        surface="plugin/comet_control/extension",
        fix="-" if ok else "Restore plugin/comet_control/extension.",
    )


def _cursor_assets_check() -> dict[str, Any]:
    manifest_path = SOURCE_EXTENSION / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text())
        resources = [
            resource
            for entry in manifest.get("web_accessible_resources", [])
            for resource in entry.get("resources", [])
            if str(resource).startswith("images/")
        ]
    except Exception as error:
        return _check(
            "cursor_assets_present",
            False,
            severity="blocking",
            detail=f"extension manifest unreadable: {error}",
            surface="plugin/comet_control/extension/manifest.json",
            fix="Restore plugin/comet_control/extension/manifest.json.",
        )
    missing = [resource for resource in resources if not (SOURCE_EXTENSION / resource).is_file()]
    return _check(
        "cursor_assets_present",
        not missing,
        severity="blocking",
        detail=(f"{len(resources)} cursor assets present" if not missing else f"missing: {', '.join(missing)}"),
        surface="plugin/comet_control/extension/images",
        fix="-" if not missing else "Restore plugin/comet_control/extension/images.",
    )


def run_diagnostics() -> dict[str, Any]:
    runtime = _runtime_probe()
    checks = [
        _runtime_check(runtime),
        _loaded_build_check(runtime),
        _broker_check(),
        _extension_check(),
        _cursor_assets_check(),
    ]
    blocking = [
        check["name"]
        for check in checks
        if check["ok"] is False and check["severity"] == "blocking"
    ]
    return {
        "success": True,
        "platform": "macos-comet",
        "preflight_ok": not blocking,
        "blocking_checks": blocking,
        "warnings": [check["name"] for check in checks if check["ok"] is False and check["severity"] == "warning"],
        "unknown_checks": [check["name"] for check in checks if check["ok"] is None],
        "checks": checks,
        "runtime": {
            key: (runtime.get("broker") or {}).get(key)
            for key in (
                "browser_pid",
                "user_data_dir",
                "socket_path",
                "runtime_verified",
                "protocol_version",
                "broker_build_sha256",
                "extension_build_sha256",
                "python_executable",
                "websockets_version",
                "connection_generation",
                "pending_count",
            )
            if (runtime.get("broker") or {}).get(key) is not None
        },
    }
